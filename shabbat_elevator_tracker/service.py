"""The running service: ingest -> pipeline -> sync, calendar, state, change notification."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import numpy as np

from . import API_VERSION, __version__
from .calendar import ShabbatCalendar, iso
from .clock import Clock, RealClock, ScaledClock
from .config import Config
from .ingest import AudioIngest, StreamStatus
from .model import next_arrival, next_arrivals, position, scaled, timeline_from_config
from .pipeline import AudioPipeline
from .store import StateStore
from .sync import SyncEngine, SyncParams

log = logging.getLogger(__name__)

NOTIFY_TYPES = {"lock", "sync", "rephase", "pause", "transition", "miss"}


def ms(t: float | None) -> int | None:
    return None if t is None else int(round(t * 1000))


class Service:
    def __init__(self, cfg: Config, clock: Clock | None = None, store: StateStore | None = None) -> None:
        self.cfg = cfg
        rate = float(cfg.server["dev_clock_speed"])
        self.clock = clock or (ScaledClock(rate) if rate != 1.0 else RealClock())
        self.debug = bool(cfg.server["expose_debug"])
        self.base_timeline = timeline_from_config(cfg)
        for w in self.base_timeline.warnings:
            log.warning("%s", w)
        self.calendar = ShabbatCalendar.from_config(cfg)
        self.store = store or StateStore(cfg.storage["data_dir"], int(cfg.storage["event_log_days"]))
        self.sync = SyncEngine(SyncParams.from_config(cfg), on_event=self._on_event)
        self.pipeline = AudioPipeline(cfg, self.sync, on_candidate=self._on_candidate)
        self.ingest = AudioIngest(cfg.audio, self.clock, self._on_audio, self._on_status,
                                  tz=cfg.calendar["timezone"], data_dir=cfg.storage["data_dir"])
        self.started_at = self.clock.now()
        self._subscribers: set[asyncio.Queue] = set()
        self._last_sig = None
        self._tasks: list[asyncio.Task] = []
        self.candidates: deque[dict] = deque(maxlen=20)
        self._clip_buf: deque[tuple[float, np.ndarray]] = deque(maxlen=400)
        self.version = 0
        snap = self.store.load_state()
        if snap:
            if str(cfg.audio.get("source") or "").startswith("loop:"):
                # a loop restarts at loop_start, so the saved phase means nothing: keep only
                # what was learned about the elevator
                snap = {"cycle": snap.get("cycle"), "ref_spectrum": snap.get("ref_spectrum")}
            self.sync.restore(snap, self.clock.now())

    # ------------------------------------------------------------ callbacks
    def _on_event(self, ev: dict) -> None:
        self.store.add_event(ev)
        if ev["type"] in ("lock", "sync", "rephase", "pause"):
            self.store.save_state(self.sync.snapshot())
        if ev["type"] in NOTIFY_TYPES:
            self._maybe_notify(force=ev["type"] in ("sync", "lock", "rephase"))

    def _on_candidate(self, cand, verdict) -> None:
        self.candidates.append(cand.as_dict())
        if self.cfg.storage.get("save_clips"):
            self._save_clip(cand)

    def _on_audio(self, samples: np.ndarray, t_first: float, new_session: bool) -> None:
        if new_session or self.pipeline.session_time is None:
            self.pipeline.start_session(t_first)
        self.pipeline.push(samples, now=self.clock.now())
        if self.cfg.storage.get("save_clips"):
            self._clip_buf.append((t_first, samples))

    def _on_status(self, status: StreamStatus) -> None:
        self.store.add_event({"type": "stream", "at": self.clock.now(), "state": status.state,
                              "error": status.last_error if status.state == "lost" else None})
        self._maybe_notify()

    def _save_clip(self, cand) -> None:
        import wave
        from pathlib import Path

        try:
            d = Path(self.cfg.storage["data_dir"]) / "clips"
            d.mkdir(parents=True, exist_ok=True)
            parts = [(t, x) for t, x in self._clip_buf if cand.onset - 6 <= t <= cand.onset + 20]
            if not parts:
                return
            with wave.open(str(d / f"{int(cand.onset * 1000)}-{cand.verdict}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(int(self.cfg.audio["sample_rate"]))
                for _t, x in parts:
                    w.writeframes(x.tobytes())
        except OSError as e:
            log.debug("clip not saved: %s", e)

    # ------------------------------------------------------------ notify
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _signature(self, now: float) -> tuple:
        valid, reason = self.sync.prediction_status(now)
        cal = (self.calendar.in_window(now), self.calendar.issur_melacha(now)) if self.calendar.enabled else None
        return (self.sync.state, self.sync.last_anchor, round(self.sync.cycle, 3), self.sync.missed,
                self.ingest.status.state, valid, reason, cal)

    def _maybe_notify(self, force: bool = False) -> None:
        now = self.clock.now()
        sig = self._signature(now)
        if not force and sig == self._last_sig:
            return
        self._last_sig = sig
        self.version += 1
        for q in list(self._subscribers):
            try:
                q.put_nowait(self.version)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------ loop
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self.ingest.run(), name="ingest"),
                       asyncio.create_task(self._ticker(), name="ticker")]

    async def stop(self) -> None:
        await self.ingest.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.store.save_state(self.sync.snapshot())

    def tick(self) -> None:
        now = self.clock.now()
        stream_ok = self.ingest.status.state == "connected"
        self.sync.tick(now, stream_ok, self.calendar.in_window(now))
        self._maybe_notify()

    async def _ticker(self) -> None:
        last_prune = 0.0
        while True:
            try:
                self.tick()
                if time.time() - last_prune > 3600:
                    self.store.prune(self.clock.now())
                    self.store.save_state(self.sync.snapshot())
                    last_prune = time.time()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ state
    def timeline(self):
        return scaled(self.base_timeline, self.sync.cycle) if abs(self.sync.cycle - self.base_timeline.cycle) > 1e-9 else self.base_timeline

    def manual_sync(self, floor: str, event: str, at: float) -> float:
        tl = self.timeline()
        stops = [s for s in tl.stops if s.floor == floor]
        if not stops:
            raise KeyError(floor)
        s = stops[0]
        off = s.arrive if event == "arrive" else s.depart
        anchor = at - off
        self.sync.manual_sync(anchor, self.clock.now())
        self.store.save_state(self.sync.snapshot())
        return anchor

    def state(self) -> dict:
        now = self.clock.now()
        tl = self.timeline()
        valid, reason = self.sync.prediction_status(now)
        degraded = None
        if valid and self.ingest.status.state != "connected":
            degraded = "audio_lost"
        elif valid and not self.sync.confirmed:
            degraded = "unconfirmed"
        anchor = self.sync.last_anchor
        pred: dict = {"valid": valid, "reason": reason, "degraded": degraded, "position": None,
                      "next_arrivals": None}
        if anchor is not None and valid:
            pred["position"] = position(tl, anchor, now)
            pred["next_arrivals"] = {
                f: None if v is None else {
                    "arrive_at": iso(v["arrive_at"]), "arrive_at_ms": ms(v["arrive_at"]),
                    "depart_at": iso(v["depart_at"]), "depart_at_ms": ms(v["depart_at"]),
                    "here_now": "here_until" in v,
                }
                for f, v in next_arrivals(tl, anchor, now).items()
            }
        sync = {
            "state": self.sync.state,
            "last_sync_at": iso(self.sync.last_sync_at),
            "last_sync_at_ms": ms(self.sync.last_sync_at),
            "anchor_floor": self.cfg.anchor["floor"],
            "missed": self.sync.missed,
            "confirmed": self.sync.confirmed if self.sync.locked else None,
            "source": self.sync.source,
            "paused_since": iso(self.sync.paused_since),
        }
        if self.debug:
            sync["pending"] = len(self.sync.pending)
            sync["syncs_total"] = self.sync.syncs_total
            sync["next_expected_at"] = iso(self.sync.expected_next()) if self.sync.locked and anchor else None
        b = self.cfg.building
        out = {
            "api_version": API_VERSION,
            "version": __version__,
            "instance_id": self.store.instance_id,
            "server_time": iso(now),
            "server_time_ms": ms(now),
            "clock_rate": self.clock.rate,
            "building": {"name": b["name"], "floors": b["floors"], "default_floor": b["default_floor"]},
            "stream": self.ingest.status.as_dict(self.debug),
            "sync": sync,
            "cycle": {"seconds": round(self.sync.cycle, 3),
                      "configured_seconds": float(self.cfg.elevator["cycle_seconds"]),
                      "adaptive": bool(self.cfg.elevator["adaptive_cycle"]["enabled"])},
            "timeline": {"anchor_time_ms": ms(anchor), **tl.as_dict()},
            "prediction": pred,
            "calendar": self.calendar.state(now),
            "debug_enabled": self.debug,
        }
        return out

    def floor_state(self, floor: str) -> dict:
        if floor not in self.cfg.building["floors"]:
            raise KeyError(floor)
        now = self.clock.now()
        valid, reason = self.sync.prediction_status(now)
        out = {"floor": floor, "valid": valid, "reason": reason, "server_time": iso(now),
               "next_arrive_at": None, "next_depart_at": None, "here_now": False, "seconds_until": None,
               "on_route": any(s.floor == floor for s in self.base_timeline.stops)}
        if valid and self.sync.last_anchor is not None:
            v = next_arrival(self.timeline(), self.sync.last_anchor, floor, now)
            if v:
                out.update(next_arrive_at=iso(v["arrive_at"]), next_depart_at=iso(v["depart_at"]),
                           here_now="here_until" in v, seconds_until=round(v["arrive_at"] - now, 1),
                           here_until=iso(v.get("here_until")))
        return out

    def health(self) -> dict:
        return {"ok": True, "version": __version__, "api_version": API_VERSION,
                "instance_id": self.store.instance_id,
                "stream": {"state": self.ingest.status.state},
                "sync": {"state": self.sync.state}}

    def debug_info(self) -> dict:
        now = self.clock.now()
        return {
            "server_time": iso(now),
            "score_db": round(self.pipeline.last_score, 1),
            "frame_level_db": round(self.pipeline.last_level_db, 1),
            "background_db": None if self.pipeline.features._background is None
            else [round(float(x), 1) for x in self.pipeline.features._background],
            "candidates": list(self.candidates)[::-1],
            "pending": [iso(p.t) for p in self.sync.pending],
            "next_expected_at": iso(self.sync.expected_next()) if self.sync.locked and self.sync.last_anchor else None,
            "cycle_history": [{"at": iso(t), "cycle": round(c, 3)} for t, c in self.sync.cycle_history],
            "ffmpeg_stderr_tail": list(self.ingest.status.stderr_tail),
            "stream": self.ingest.status.as_dict(True),
            "sync": {k: v for k, v in self.sync.snapshot().items() if k != "ref_spectrum"},
            "has_reference_sound": self.sync.ref_spectrum is not None,
            "uptime_s": round(now - self.started_at, 1),
        }
