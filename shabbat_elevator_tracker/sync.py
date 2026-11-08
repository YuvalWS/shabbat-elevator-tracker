"""Phase-gated sync state machine (section 9.4).

States: idle, searching, provisional, locked, paused.

A candidate (loud event at the anchor floor) is trusted only if it falls on the expected
phase of an existing lock, or if two candidates confirm each other one to three cycles
apart. A quick lock trusts a single candidate that sounds like the learned anchor sound
and lasts as long as one; it is *unconfirmed* until the next stop is heard on time, and
is dropped if that stop is missed. All times are epoch seconds from the service clock.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .detector import Candidate, similarity

STATES = ("idle", "searching", "provisional", "locked", "paused")


@dataclass
class _Pending:
    t: float
    duration: float
    spectrum: object  # np.ndarray | None


@dataclass
class SyncParams:
    cycle_seconds: float = 404.4
    gate_seconds: float = 8.0
    confirm_tolerance_seconds: float = 3.0
    confirm_min_similarity: float = 0.90  # spectral correlation of the two candidates
    confirm_max_duration_ratio: float = 1.6
    sync_min_similarity: float = 0.80  # a locked sync must resemble the learned anchor sound
    confirm_window_cycles: int = 3
    pause_after_missed: int = 3
    stale_after_seconds: float = 7200.0
    adaptive: bool = True
    max_deviation_percent: float = 2.0
    offset_seconds: float = 0.0
    report_delay_seconds: float = 32.0  # latest a candidate with onset in the gate can arrive
    quick_lock: str = "shabbat"  # trust one stop: "shabbat" (in the window only), "always", "off"
    quick_lock_min_similarity: float = 0.95
    quick_lock_min_seconds: float = 5.0

    @classmethod
    def from_config(cls, cfg) -> SyncParams:
        s, e = cfg.sync, cfg.elevator
        return cls(
            cycle_seconds=float(e["cycle_seconds"]),
            gate_seconds=float(s["gate_seconds"]),
            confirm_tolerance_seconds=float(s.get("confirm_tolerance_seconds", 3.0)),
            confirm_min_similarity=float(s.get("confirm_min_similarity", 0.90)),
            confirm_max_duration_ratio=float(s.get("confirm_max_duration_ratio", 1.6)),
            sync_min_similarity=float(s.get("sync_min_similarity", 0.80)),
            confirm_window_cycles=int(s["confirm_window_cycles"]),
            pause_after_missed=int(s["pause_after_missed"]),
            stale_after_seconds=float(s["stale_after_seconds"]),
            adaptive=bool(e["adaptive_cycle"]["enabled"]),
            max_deviation_percent=float(e["adaptive_cycle"]["max_deviation_percent"]),
            offset_seconds=float(cfg.anchor.get("offset_seconds", 0.0)),
            report_delay_seconds=float(cfg.detector["max_event_seconds"]) + 2.0,
            quick_lock=str(s.get("quick_lock", "shabbat")),
            quick_lock_min_similarity=float(s.get("quick_lock_min_similarity", 0.95)),
            quick_lock_min_seconds=float(s.get("quick_lock_min_seconds", 5.0)),
        )


class SyncEngine:
    def __init__(self, params: SyncParams, on_event: Callable[[dict], None] | None = None) -> None:
        self.p = params
        self.on_event = on_event
        self.cycle = params.cycle_seconds
        self.locked = False
        self.confirmed = True  # False after a quick lock, until the next stop is heard
        self._before_quick: tuple | None = None  # (last_anchor, last_sync_at, source) to undo it
        self.paused = False
        self.paused_since: float | None = None
        self.last_anchor: float | None = None
        self.last_sync_at: float | None = None
        self.locked_since: float | None = None
        self.source: str | None = None
        self.missed = 0
        self.n_next = 1
        self.pending: deque[_Pending] = deque(maxlen=20)
        self.in_window: bool | None = None
        self.state = "searching"
        self.cycle_history: deque[tuple[float, float]] = deque(maxlen=50)
        self.recent: deque[dict] = deque(maxlen=20)
        self.syncs_total = 0
        self._stream_gap = False
        self._now = 0.0
        self._slot: tuple | None = None  # (expected, error, cycle before, previous anchor, k)
        # what this elevator's anchor sounds like (mean excess spectrum), learned from syncs
        self.ref_spectrum = None
        self._ref_vetoes = 0  # pairs that matched each other but not the reference sound

    # ---------------------------------------------------------------- helpers
    def _emit(self, kind: str, at: float, **data) -> None:
        ev = {"type": kind, "at": at} | data
        if self.on_event:
            self.on_event(ev)

    def _set_state(self, now: float) -> None:
        if self.locked:
            st = "locked"
        elif self.in_window is False and not self.paused:
            st = "idle"  # candidates are still collected and may lock
        elif self.pending:
            st = "provisional"
        elif self.paused:
            st = "paused"
        else:
            st = "searching"
        if st != self.state:
            old = self.state
            self.state = st
            self._emit("transition", now, **{"from": old, "to": st})

    def _clamp(self, c: float) -> float:
        base = self.p.cycle_seconds
        d = base * self.p.max_deviation_percent / 100.0
        return min(max(c, base - d), base + d)

    def _adapt(self, interval: float, k: int) -> None:
        if not self.p.adaptive or k < 1 or k > 3:
            return
        self.cycle = self._clamp(0.8 * self.cycle + 0.2 * (interval / k))
        self.cycle_history.append((self._now, self.cycle))

    def _lock(self, anchor: float, now: float, source: str, kind: str, **extra) -> None:
        was_locked = self.locked
        self.locked = True
        self.confirmed = True
        self._before_quick = None
        self.paused = False
        self.paused_since = None
        self.last_anchor = anchor
        self.last_sync_at = anchor
        self.locked_since = now if not was_locked or kind != "sync" else self.locked_since
        self.missed = 0
        self.n_next = 1
        self.source = source
        self.syncs_total += 1
        if kind != "sync":
            self.pending.clear()
        self._stream_gap = False
        if kind != "sync":
            self._slot = None
        self._emit(kind, now, anchor_time=anchor, cycle_seconds=round(self.cycle, 3), source=source, **extra)

    def _confirm(self, t: float, cand: Candidate) -> tuple[float, int] | None:
        """Return (older pending onset, k) if the candidate confirms a pending one: about
        k cycles later (k <= confirm_window_cycles), similar duration and similar sound."""
        best = None
        for pend in self.pending:
            p = pend.t
            d = t - p
            if d <= 0:
                continue
            k = round(d / self.cycle)
            if not 1 <= k <= self.p.confirm_window_cycles:
                continue
            tol = self.p.confirm_tolerance_seconds + 1.0 * (k - 1)
            err = abs(d - k * self.cycle)
            if err > tol:
                continue
            ratio = max(cand.duration, pend.duration) / max(1e-3, min(cand.duration, pend.duration))
            if ratio > self.p.confirm_max_duration_ratio:
                continue
            sim = similarity(cand.spectrum, pend.spectrum)
            if sim is not None and sim < self.p.confirm_min_similarity:
                continue
            if self.ref_spectrum is not None and self._ref_vetoes < 3:
                s1 = similarity(cand.spectrum, self.ref_spectrum)
                s2 = similarity(pend.spectrum, self.ref_spectrum)
                if (s1 is not None and s1 < self.p.confirm_min_similarity) or (
                        s2 is not None and s2 < self.p.confirm_min_similarity):
                    # after three such pairs the reference is probably outdated: allow it
                    self._ref_vetoes += 1
                    continue
            if best is None or err < best[2]:
                best = (p, k, err)
        return (best[0], best[1]) if best else None

    # ---------------------------------------------------------------- inputs
    def candidate(self, cand: Candidate, now: float) -> str:
        """Process a detector candidate. Returns the verdict."""
        self._now = now
        if cand.rejected:
            verdict = f"rejected_{cand.rejected}"
            cand.verdict = verdict
            self.recent.append(cand.as_dict())
            return verdict
        t = cand.onset - self.p.offset_seconds
        verdict = "unmatched"
        if self.locked and self.last_anchor is not None:
            rel = t - self.last_anchor
            k = round(rel / self.cycle)
            slot = self._slot
            if (slot and self.source == "audio" and abs(t - slot[0]) < slot[1]
                    and abs(t - slot[0]) <= self.p.gate_seconds and not self._dissimilar(cand)):
                # a later sound in the same slot fits the expected phase better (e.g. a
                # door slam just before the elevator): replace that sync
                expected, _err, cycle_before, prev_anchor, k0 = slot
                replaced = self.last_anchor
                self.cycle = cycle_before
                self._adapt(t - prev_anchor, k0)
                self.syncs_total -= 1
                self._lock(t, now, "audio", "sync", replaces=replaced)
                self._slot = (expected, abs(t - expected), cycle_before, prev_anchor, k0)
                verdict = "sync"
            elif (k >= 1 and abs(rel - k * self.cycle) <= self.p.gate_seconds
                  and not self._dissimilar(cand)):
                expected = self.last_anchor + k * self.cycle
                slot = (expected, abs(t - expected), self.cycle, self.last_anchor, k)
                self._adapt(rel, k)
                self._lock(t, now, "audio", "sync")
                self._slot = slot
                verdict = "sync"
            elif k == 0 and abs(rel) <= self.p.gate_seconds:
                verdict = "duplicate"
            else:
                # off-phase while locked: remember it; if the expected anchors are being
                # missed and two off-phase candidates confirm each other, re-phase.
                conf = self._confirm(t, cand)
                if conf and self.missed >= 1:
                    older, k2 = conf
                    self._adapt(t - older, k2)
                    self._lock(t, now, "audio", "rephase")
                    verdict = "rephase"
                else:
                    self.pending.append(_Pending(t, cand.duration, cand.spectrum))
                    verdict = "unmatched"
        else:
            conf = self._confirm(t, cand)
            if conf:
                older, k2 = conf
                self._adapt(t - older, k2)
                self._lock(t, now, "audio", "lock")
                verdict = "lock"
            elif self._quick_ok(cand):
                before = (self.last_anchor, self.last_sync_at, self.source)
                self._lock(t, now, "audio", "lock", confirmed=False)
                self.confirmed = False
                self._before_quick = before
                # keep it pending too, so a later pair can still lock if this one is dropped
                self.pending.append(_Pending(t, cand.duration, cand.spectrum))
                verdict = "quick_lock"
            else:
                self.pending.append(_Pending(t, cand.duration, cand.spectrum))
                verdict = "pending"
        if verdict in ("sync", "lock", "rephase") and cand.spectrum is not None:
            self._learn(cand.spectrum)
        cand.verdict = verdict
        self.recent.append(cand.as_dict())
        self._emit("candidate", now, onset=cand.onset, duration=round(cand.duration, 2),
                   peak_db=round(cand.peak_db, 1), verdict=verdict)
        self._prune(now)
        self._set_state(now)
        return verdict

    def _quick_ok(self, cand: Candidate) -> bool:
        """A single candidate may lock: it sounds like the learned anchor and is as long as one."""
        mode = self.p.quick_lock
        if mode == "off" or (mode != "always" and self.in_window is not True):
            return False
        if cand.duration < self.p.quick_lock_min_seconds or self.ref_spectrum is None:
            return False
        sim = similarity(cand.spectrum, self.ref_spectrum)
        return sim is not None and sim >= self.p.quick_lock_min_similarity

    def manual_sync(self, anchor_time: float, now: float) -> None:
        self._now = now
        self._lock(anchor_time, now, "manual", "lock")
        self._set_state(now)

    def tick(self, now: float, stream_ok: bool = True, in_window: bool | None = None) -> None:
        self._now = now
        self.in_window = in_window
        if not stream_ok:
            self._stream_gap = True
        if self.locked and self.last_anchor is not None:
            while now > self.expected_next() + self.p.gate_seconds + self.p.report_delay_seconds:
                exp = self.expected_next()
                self.n_next += 1
                if not self._stream_gap:
                    self.missed += 1
                    self._emit("miss", now, expected=exp, missed=self.missed)
                if not self.confirmed and self.missed >= 1:
                    # an unconfirmed quick lock whose next stop didn't come: it was not the elevator
                    self.locked = False
                    self.confirmed = True
                    if self._before_quick:
                        self.last_anchor, self.last_sync_at, self.source = self._before_quick
                    self._before_quick = None
                    self.syncs_total -= 1
                    self.missed = 0
                    self._emit("unlock", now, reason="unconfirmed", expected=exp)
                    break
                self._stream_gap = not stream_ok
                if self.missed >= self.p.pause_after_missed:
                    self.locked = False
                    self.paused = True
                    self.paused_since = now
                    self.pending.clear()
                    self._emit("pause", now, last_sync_at=self.last_sync_at, missed=self.missed)
                    break
        idle_due = (
            in_window is False
            and (self.last_sync_at is None or now - self.last_sync_at > 2 * self.cycle)
        )
        if idle_due and (self.locked or self.paused):
            self.locked = False
            self.paused = False
            self.paused_since = None
        self._prune(now)
        self._set_state(now)

    def _dissimilar(self, cand: Candidate) -> bool:
        if self.ref_spectrum is None or cand.spectrum is None:
            return False
        sim = similarity(cand.spectrum, self.ref_spectrum)
        return sim is not None and sim < self.p.sync_min_similarity

    def _learn(self, spectrum) -> None:
        import numpy as np

        spec = np.asarray(spectrum, dtype=np.float32)
        self._ref_vetoes = 0
        if self.ref_spectrum is None or len(self.ref_spectrum) != len(spec) or (
                similarity(spec, self.ref_spectrum) or 0) < self.p.confirm_min_similarity:
            self.ref_spectrum = spec.copy()
        else:
            self.ref_spectrum = 0.8 * self.ref_spectrum + 0.2 * spec

    def _prune(self, now: float) -> None:
        horizon = self.p.confirm_window_cycles * self.cycle + self.p.gate_seconds + self.p.report_delay_seconds
        while self.pending and now - self.pending[0].t > horizon:
            self.pending.popleft()

    # ---------------------------------------------------------------- outputs
    def expected_next(self) -> float:
        assert self.last_anchor is not None
        return self.last_anchor + self.n_next * self.cycle

    def is_stale(self, now: float) -> bool:
        return self.last_sync_at is None or now - self.last_sync_at > self.p.stale_after_seconds

    def prediction_status(self, now: float) -> tuple[bool, str | None]:
        if self.state == "locked":
            if self.is_stale(now):
                return False, "stale"
            return True, None
        return False, {
            "paused": "elevator_paused",
            "idle": "outside_shabbat",
        }.get(self.state, "no_sync")

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "locked": self.locked,
            "confirmed": self.confirmed,
            "paused": self.paused,
            "paused_since": self.paused_since,
            "last_anchor": self.last_anchor,
            "last_sync_at": self.last_sync_at,
            "locked_since": self.locked_since,
            "source": self.source,
            "missed": self.missed,
            "n_next": self.n_next,
            "cycle": self.cycle,
            "syncs_total": self.syncs_total,
            "ref_spectrum": [round(float(x), 2) for x in self.ref_spectrum]
            if self.ref_spectrum is not None else None,
        }

    def restore(self, snap: dict, now: float) -> None:
        """Restore after a restart. Expected anchors that passed while down are skipped
        without counting as misses. A snapshot from the future (the clock moved back, or a
        sped-up dev clock) is ignored apart from the cycle and the reference sound."""
        try:
            times = [snap.get(k) for k in ("last_anchor", "last_sync_at", "locked_since", "paused_since")]
            if any(t is not None and float(t) > now + 60 for t in times):
                snap = {"cycle": snap.get("cycle"), "ref_spectrum": snap.get("ref_spectrum")}
            self.cycle = self._clamp(float(snap.get("cycle") or self.cycle))
            self.last_anchor = snap.get("last_anchor")
            self.last_sync_at = snap.get("last_sync_at")
            self.source = snap.get("source")
            self.syncs_total = int(snap.get("syncs_total") or 0)
            if snap.get("ref_spectrum"):
                self._learn(snap["ref_spectrum"])
            if snap.get("locked") and self.last_anchor is not None and not self.is_stale(now):
                self.locked = True
                self.confirmed = bool(snap.get("confirmed", True))
                self.locked_since = snap.get("locked_since")
                self.missed = int(snap.get("missed") or 0)
                late = now - self.last_anchor - self.p.gate_seconds - self.p.report_delay_seconds
                n = max(1, int(late // self.cycle) + 1)
                self.n_next = max(int(snap.get("n_next") or 1), n)
            elif snap.get("paused"):
                self.paused = True
                self.paused_since = snap.get("paused_since")
        except (TypeError, ValueError):
            return
        self._set_state(now)
