"""Persistence in the data dir: instance id, sync state, event log (JSON lines)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, data_dir: str | os.PathLike, event_log_days: int = 14, memory_events: int = 500) -> None:
        self.dir = Path(data_dir)
        self.event_log_days = event_log_days
        self.events: deque[dict] = deque(maxlen=memory_events)
        self.writable = True
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            probe = self.dir / ".write_test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            log.warning("data dir %s is not writable (%s); running without persistence", self.dir, e)
            self.writable = False
        self.instance_id = self._instance_id()
        self._load_recent_events()

    def _instance_id(self) -> str:
        p = self.dir / "instance_id"
        if self.writable:
            try:
                if p.exists():
                    v = p.read_text().strip()
                    if v:
                        return v
                v = uuid.uuid4().hex
                p.write_text(v)
                return v
            except OSError:
                pass
        return uuid.uuid4().hex

    # ------------------------------------------------------------ sync state
    def load_state(self) -> dict | None:
        p = self.dir / "state.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_state(self, snap: dict) -> None:
        if not self.writable:
            return
        p = self.dir / "state.json"
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(snap), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            log.warning("cannot save state: %s", e)

    # ------------------------------------------------------------ events
    def _log_path(self, t: float) -> Path:
        return self.dir / f"events-{time.strftime('%Y%m%d', time.gmtime(t))}.jsonl"

    def add_event(self, ev: dict) -> None:
        self.events.append(ev)
        if not self.writable:
            return
        try:
            with self._log_path(ev.get("at") or time.time()).open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        except OSError as e:
            log.debug("cannot write event: %s", e)

    def _load_recent_events(self) -> None:
        if not self.writable:
            return
        files = sorted(self.dir.glob("events-*.jsonl"))
        for p in files[-2:]:
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        self.events.append(json.loads(line))
                    except ValueError:
                        pass
            except OSError:
                pass

    def prune(self, now: float) -> None:
        if not self.writable:
            return
        cutoff = time.strftime("%Y%m%d", time.gmtime(now - self.event_log_days * 86400))
        for p in self.dir.glob("events-*.jsonl"):
            if p.stem.split("-", 1)[1] < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass

    def recent_events(self, limit: int = 100) -> list[dict]:
        return list(self.events)[-limit:][::-1]
