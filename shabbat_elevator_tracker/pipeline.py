"""Features -> detector -> sync, shared by the live service, replay and calibrate."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from .detector import Candidate, Detector
from .features import FeatureExtractor
from .sync import SyncEngine


class AudioPipeline:
    def __init__(self, cfg, sync: SyncEngine | None,
                 on_candidate: Callable[[Candidate, str | None], None] | None = None) -> None:
        d = cfg.detector
        self.sr = int(cfg.audio["sample_rate"])
        self.features = FeatureExtractor(
            self.sr, tuple(d["band_hz"]), int(d["background_window_seconds"]), float(d["background_percentile"])
        )
        self.detector = Detector.from_config(d)
        self.sync = sync
        self.on_candidate = on_candidate
        self._t0: float | None = None  # epoch time of sample 0 of the session
        self._samples = 0
        self.last_score = 0.0
        self.last_level_db = -120.0
        self.frames_total = 0

    def start_session(self, t0: float) -> None:
        """New contiguous audio session starting at epoch t0 (after a gap or reconnect)."""
        self.features.reset()
        for c in self.detector.flush():
            self._handle(c, t0)
        self.detector.reset()
        self._t0 = t0
        self._samples = 0
        self.features.set_frame_index(0)

    @property
    def session_time(self) -> float | None:
        """Epoch time just after the last sample pushed."""
        if self._t0 is None:
            return None
        return self._t0 + self._samples / self.sr

    def push(self, samples: np.ndarray, now: float | None = None) -> list[Candidate]:
        if self._t0 is None:
            raise RuntimeError("start_session() first")
        self._samples += len(samples)
        batch = self.features.push(samples)
        if len(batch) == 0:
            return []
        idx = np.arange(batch.first_index, batch.first_index + len(batch))
        times = self._t0 + (idx * self.features.hop + self.features.win / 2) / self.sr
        self.last_score = float(batch.score[-1])
        self.last_level_db = float(batch.level_db[-1])
        self.frames_total += len(batch)
        cands = self.detector.push(times, batch.score, batch.excess)
        t_now = now if now is not None else float(times[-1])
        for c in cands:
            self._handle(c, t_now)
        return cands

    def _handle(self, c: Candidate, now: float) -> None:
        verdict = self.sync.candidate(c, now) if self.sync else None
        if self.on_candidate:
            self.on_candidate(c, verdict)
