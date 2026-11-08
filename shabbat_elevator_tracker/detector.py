"""Candidate detection with hysteresis on the band score (section 9.2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .features import FRAME_SECONDS


@dataclass
class Candidate:
    onset: float  # epoch seconds of the first frame >= threshold
    duration: float
    peak_db: float
    mean_db: float
    similarity: float | None = None
    rejected: str | None = None  # None = accepted; "short", "long", "signature"
    verdict: str | None = None  # filled by the sync engine (sync, lock, pending, unmatched, ...)
    spectrum: np.ndarray | None = None  # mean band excess during the event (not serialized)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "onset": self.onset,
            "duration": round(self.duration, 2),
            "peak_db": round(self.peak_db, 1),
            "mean_db": round(self.mean_db, 1),
            "rejected": self.rejected,
            "verdict": self.verdict,
        }
        if self.similarity is not None:
            d["similarity"] = round(self.similarity, 3)
        return d


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    """Pearson correlation of two mean-excess spectra (None if unknown)."""
    if a is None or b is None or len(a) != len(b):
        return None
    a = np.asarray(a, dtype=np.float64) - np.mean(a)
    b = np.asarray(b, dtype=np.float64) - np.mean(b)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class Detector:
    def __init__(
        self,
        threshold_db: float = 25.0,
        release_db: float = 15.0,
        min_event_seconds: float = 2.0,
        max_event_seconds: float = 30.0,
        signature: list[float] | None = None,
        min_similarity: float = 0.8,
        release_hold_seconds: float = 1.0,
    ) -> None:
        self.threshold = threshold_db
        self.release = release_db
        self.min_dur = min_event_seconds
        self.max_dur = max_event_seconds
        self.signature = np.asarray(signature, dtype=np.float32) if signature is not None else None
        self.min_sim = min_similarity
        self.hold = int(round(release_hold_seconds / FRAME_SECONDS))
        self._open = False
        self._onset = 0.0
        self._last_above_release = 0.0
        self._below = 0
        self._scores: list[float] = []
        self._spec_sum: np.ndarray | None = None
        self._spec_n = 0
        self.last_score = 0.0

    @classmethod
    def from_config(cls, d: dict) -> Detector:
        return cls(
            threshold_db=float(d["threshold_db"]),
            release_db=float(d["release_db"]),
            min_event_seconds=float(d["min_event_seconds"]),
            max_event_seconds=float(d["max_event_seconds"]),
            signature=d.get("signature"),
            min_similarity=float(d.get("min_similarity", 0.8)),
        )

    def reset(self) -> None:
        self._open = False
        self._scores = []
        self._spec_sum = None
        self._below = 0

    def push(self, times: np.ndarray, scores: np.ndarray, excess: np.ndarray | None = None) -> list[Candidate]:
        """Feed per-frame times (epoch s) and scores; returns closed candidates."""
        out: list[Candidate] = []
        if len(scores):
            self.last_score = float(scores[-1])
        # fast path: nothing open and nothing reaches the threshold
        if not self._open:
            hits = np.flatnonzero(scores >= self.threshold)
            if len(hits) == 0:
                return out
            start = int(hits[0])
        else:
            start = 0
        for i in range(start, len(scores)):
            s = float(scores[i])
            t = float(times[i])
            if not self._open:
                if s >= self.threshold:
                    self._open = True
                    self._onset = t
                    self._last_above_release = t
                    self._below = 0
                    self._scores = [s]
                    self._spec_sum = excess[i].astype(np.float64).copy() if excess is not None else None
                    self._spec_n = 1
                continue
            self._scores.append(s)
            if excess is not None and self._spec_sum is not None and s >= self.release:
                self._spec_sum += excess[i]
                self._spec_n += 1
            if s >= self.release:
                self._last_above_release = t
                self._below = 0
            else:
                self._below += 1
            too_long = (t - self._onset) > self.max_dur + 1.0
            if self._below >= self.hold or too_long:
                out.append(self._close(too_long))
                if not too_long:
                    continue
        return out

    def flush(self) -> list[Candidate]:
        if self._open:
            return [self._close(False)]
        return []

    def _close(self, forced: bool) -> Candidate:
        dur = self._last_above_release - self._onset + FRAME_SECONDS
        sc = np.asarray(self._scores, dtype=np.float32)
        cand = Candidate(self._onset, dur, float(sc.max()), float(sc[sc >= self.release].mean()) if (sc >= self.release).any() else float(sc.mean()))
        if self._spec_sum is not None:
            cand.spectrum = (self._spec_sum / max(1, self._spec_n)).astype(np.float32)
        if self.signature is not None and cand.spectrum is not None:
            cand.similarity = similarity(cand.spectrum, self.signature)
        if forced or dur > self.max_dur:
            cand.rejected = "long"
        elif dur < self.min_dur:
            cand.rejected = "short"
        elif cand.similarity is not None and cand.similarity < self.min_sim:
            cand.rejected = "signature"
        self._open = False
        self._scores = []
        self._spec_sum = None
        self._below = 0
        return cand
