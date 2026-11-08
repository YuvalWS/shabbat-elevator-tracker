"""Replay recordings through the live pipeline at full speed (section 15.4)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .calendar import ShabbatCalendar, iso
from .config import Config, config_from_dict
from .pipeline import AudioPipeline
from .sync import SyncEngine, SyncParams

NAME_RE = re.compile(r"^(\d{12,14})\.wav$")
GAP_SECONDS = 3.0


def recording_files(directory: str | Path) -> list[tuple[Path, float]]:
    """Recorder WAVs (named by epoch ms of the first sample), sorted by time."""
    out = []
    for p in Path(directory).iterdir():
        m = NAME_RE.match(p.name)
        if m:
            out.append((p, int(m.group(1)) / 1000.0))
    return sorted(out, key=lambda x: x[1])


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: only 16-bit PCM WAV is supported")
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype="<i2")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return sr, data


@dataclass
class ReplayResult:
    events: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    final_cycle: float = 0.0
    runtime_s: float = 0.0
    audio_seconds: float = 0.0
    start: float = 0.0
    end: float = 0.0

    @property
    def syncs(self) -> list[dict]:
        """Effective syncs (a sync replaced by a better one in the same slot is dropped)."""
        replaced = {e["replaces"] for e in self.events if e.get("replaces") is not None}
        return [e for e in self.events
                if e["type"] in ("sync", "lock", "rephase") and e["anchor_time"] not in replaced]


def run_replay(cfg: Config, directory: str | Path, chunk_seconds: float = 5.0,
               progress: bool = False, use_calendar: bool = True) -> ReplayResult:
    files = recording_files(directory)
    if not files:
        raise FileNotFoundError(f"no recorder WAVs (<epoch_ms>.wav) in {directory}")
    with wave.open(str(files[0][0]), "rb") as w:
        sr0 = w.getframerate()
    if sr0 != int(cfg.audio["sample_rate"]):
        raw = copy.deepcopy(cfg.raw)
        raw["audio"]["sample_rate"] = sr0
        cfg = config_from_dict(raw)
    res = ReplayResult()
    sync = SyncEngine(SyncParams.from_config(cfg), on_event=res.events.append)
    pipe = AudioPipeline(cfg, sync, on_candidate=lambda c, v: res.candidates.append(c.as_dict()))
    cal = ShabbatCalendar.from_config(cfg) if use_calendar else None
    t_start = time.perf_counter()
    prev_end: float | None = None
    res.start = files[0][1]
    for path, t0 in files:
        sr, data = read_wav(path)
        if prev_end is None or abs(t0 - prev_end) > GAP_SECONDS:
            pipe.start_session(t0)
        step = int(chunk_seconds * sr)
        for i in range(0, len(data), step):
            pipe.push(data[i:i + step])
            now = pipe.session_time
            sync.tick(now, True, cal.in_window(now) if cal else None)
        prev_end = t0 + len(data) / sr
        res.audio_seconds += len(data) / sr
        if progress:
            print(f"  {path.name}: {len(data) / sr / 60:.1f} min, state {sync.state}", flush=True)
    for c in pipe.detector.flush():
        sync.candidate(c, prev_end)
        res.candidates.append(c.as_dict())
    res.end = prev_end or res.start
    res.final_cycle = sync.cycle
    res.runtime_s = time.perf_counter() - t_start
    return res


def _parse_iso(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def compare_expected(res: ReplayResult, expected: dict, cfg: Config,
                     tolerance: float = 1.5, max_unsynced: int = 2,
                     relock_by_local: str = "06:20") -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    gate = float(cfg.sync["gate_seconds"])
    exp = [_parse_iso(a["onset"]) for a in expected["anchors"]]
    syncs = [e["anchor_time"] for e in res.syncs]
    unsynced = [e for e in exp if not any(abs(s - e) <= tolerance for s in syncs)]
    lines.append(f"expected anchors: {len(exp)}, synced: {len(exp) - len(unsynced)}, not synced: {len(unsynced)}")
    if len(unsynced) > max_unsynced:
        ok = False
        lines.append("  FAIL: too many expected anchors not synced: " + ", ".join(iso(u) for u in unsynced))
    off = [s for s in syncs if not any(abs(s - e) <= gate for e in exp)]
    if off:
        ok = False
        lines.append("  FAIL: off-phase syncs: " + ", ".join(iso(s) for s in off))
    for na in expected.get("non_anchor_candidates", []):
        t = _parse_iso(na["onset"])
        used = {round(e["anchor_time"], 3) for e in res.syncs}
        bad = [c for c in res.candidates if abs(c["onset"] - t) <= 2 and round(c["onset"], 3) in used]
        if bad:
            ok = False
            lines.append(f"  FAIL: non-anchor sound at {na['local']} was used for sync")
    cyc = res.final_cycle
    lo, hi = 404.2, 404.8
    lines.append(f"final cycle: {cyc:.3f} s")
    if expected.get("cycle_seconds") and not lo <= cyc <= hi:
        ok = False
        lines.append(f"  FAIL: cycle outside {lo}-{hi}")
    for pz in expected.get("pauses", []):
        last, nxt = _parse_iso(pz["last_anchor"]), _parse_iso(pz["next_anchor"])
        pauses = [e for e in res.events if e["type"] == "pause" and last < e["at"] < nxt]
        limit = last + 3 * cyc + gate + float(cfg.detector["max_event_seconds"]) + 2 + 5
        if not pauses:
            ok = False
            lines.append(f"  FAIL: no pause detected for {pz['local']}")
        elif pauses[0]["at"] > limit:
            ok = False
            lines.append(f"  FAIL: pause detected late ({iso(pauses[0]['at'])})")
        else:
            lines.append(f"pause detected at {iso(pauses[0]['at'])}")
        relocks = [e for e in res.events if e["type"] in ("lock", "rephase") and e["at"] > nxt - 1]
        tz = cfg.calendar["timezone"]
        if relocks:
            from zoneinfo import ZoneInfo

            loc = dt.datetime.fromtimestamp(relocks[0]["at"], ZoneInfo(tz))
            hh, mm = (int(x) for x in relock_by_local.split(":"))
            lines.append(f"re-locked at {loc.strftime('%H:%M:%S')} local")
            if (loc.hour, loc.minute) > (hh, mm):
                ok = False
                lines.append(f"  FAIL: re-lock after {relock_by_local}")
        else:
            ok = False
            lines.append("  FAIL: never re-locked after the pause")
    lines.append(f"runtime: {res.runtime_s:.1f} s for {res.audio_seconds / 3600:.2f} h of audio")
    return ok, lines


def summary(res: ReplayResult, tz: str) -> list[str]:
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tz)

    def loc(t: float) -> str:
        return dt.datetime.fromtimestamp(t, z).strftime("%a %H:%M:%S")

    lines = []
    for e in res.events:
        if e["type"] == "transition":
            lines.append(f"{loc(e['at'])}  state {e['from']} -> {e['to']}")
        elif e["type"] in ("lock", "rephase", "pause"):
            extra = f" anchor {loc(e['anchor_time'])}" if "anchor_time" in e else ""
            lines.append(f"{loc(e['at'])}  {e['type']}{extra}")
    n_sync = sum(1 for e in res.events if e["type"] == "sync")
    lines.append(f"syncs: {n_sync}, locks: {sum(1 for e in res.events if e['type'] == 'lock')}, "
                 f"candidates: {len(res.candidates)}, final cycle {res.final_cycle:.3f} s")
    return lines


def load_expected(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
