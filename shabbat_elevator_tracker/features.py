"""Streaming spectral features (section 9.1).

Per 0.1 s frame: Hann-windowed power spectrum summed into 100 Hz bands (dB), a rolling
per-band background (percentile of 1 s band medians over the last N seconds), the excess
over the background (clipped 0..40 dB) and a score = mean excess in the detector band.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

FRAME_SECONDS = 0.1
BAND_HZ = 100.0


@dataclass
class FrameBatch:
    first_index: int  # index of the first frame (frame k starts at sample k*hop)
    excess: np.ndarray  # (n, bands) float32, dB over background, clipped 0..40
    score: np.ndarray  # (n,) float32
    level_db: np.ndarray  # (n,) float32 band-summed level (dB), for debug

    def __len__(self) -> int:
        return len(self.score)


class FeatureExtractor:
    def __init__(
        self,
        sample_rate: int = 16000,
        band_hz: tuple[float, float] = (600, 2200),
        background_window_seconds: int = 30,
        background_percentile: float = 20,
    ) -> None:
        self.sr = int(sample_rate)
        self.hop = int(round(self.sr * FRAME_SECONDS))
        self.win = int(2 ** round(np.log2(self.hop * 1.28)))  # 2048 at 16 kHz
        self.nbands = int(self.sr / 2 // BAND_HZ)
        freqs = np.fft.rfftfreq(self.win, 1 / self.sr)
        idx = np.minimum((freqs // BAND_HZ).astype(int), self.nbands - 1)
        self._band_of_bin = idx
        self._window = np.hanning(self.win).astype(np.float32)
        lo, hi = band_hz
        self.score_bands = np.arange(int(lo // BAND_HZ), max(int(lo // BAND_HZ) + 1, int(np.ceil(hi / BAND_HZ))))
        self.score_bands = self.score_bands[self.score_bands < self.nbands]
        self.bg_window = int(background_window_seconds)
        self.bg_pct = float(background_percentile)
        self._buf = np.zeros(0, dtype=np.float32)
        self._frame_index = 0
        self._sec_frames: list[np.ndarray] = []  # band dB of frames in the current second
        self._sec_medians: deque[np.ndarray] = deque(maxlen=self.bg_window)
        self._background: np.ndarray | None = None

    @property
    def frames_per_second(self) -> int:
        return int(round(1 / FRAME_SECONDS))

    def reset(self) -> None:
        """Forget buffered samples (after a gap); the background is kept."""
        self._buf = np.zeros(0, dtype=np.float32)
        self._sec_frames = []

    def set_frame_index(self, index: int) -> None:
        self._frame_index = index

    def band_db(self, frames: np.ndarray) -> np.ndarray:
        spec = np.fft.rfft(frames * self._window, axis=1)
        p = (spec.real**2 + spec.imag**2).astype(np.float32, copy=False)
        # sum FFT bins into 100 Hz bands. Live chunks are tiny: reduceat avoids BLAS, whose
        # thread pool costs far more than it saves on a few frames.
        if len(p) >= 64:  # offline batches: one matrix product is fastest
            bands = p @ self._band_matrix()
        else:
            bands = np.add.reduceat(p, self._band_starts, axis=1)
        return (10 * np.log10(bands / np.float32(self.win**2) + np.float32(1e-12))).astype(np.float32)

    @property
    def _band_starts(self) -> np.ndarray:
        if self._bs is None:
            self._bs = np.searchsorted(self._band_of_bin, np.arange(self.nbands))
        return self._bs

    _bs: np.ndarray | None = None
    _bm: np.ndarray | None = None

    def _band_matrix(self) -> np.ndarray:
        if self._bm is None:
            m = np.zeros((len(self._band_of_bin), self.nbands), dtype=np.float32)
            m[np.arange(len(self._band_of_bin)), self._band_of_bin] = 1.0
            self._bm = m
        return self._bm

    def push(self, samples: np.ndarray) -> FrameBatch:
        """Feed int16 (or float in [-1, 1]) samples; returns the frames completed."""
        if samples.dtype == np.int16:
            x = samples.astype(np.float32) / 32768.0
        else:
            x = samples.astype(np.float32)
        buf = np.concatenate([self._buf, x]) if len(self._buf) else x
        n = 0 if len(buf) < self.win else 1 + (len(buf) - self.win) // self.hop
        first = self._frame_index
        if n == 0:
            self._buf = buf
            return FrameBatch(first, np.zeros((0, self.nbands), np.float32), np.zeros(0, np.float32), np.zeros(0, np.float32))
        frames = np.lib.stride_tricks.sliding_window_view(buf[: (n - 1) * self.hop + self.win], self.win)[:: self.hop]
        db = self.band_db(frames)
        self._buf = buf[n * self.hop:]
        self._frame_index += n
        excess = np.empty_like(db)
        fps = self.frames_per_second
        # process frame by frame groups aligned to whole seconds for background updates
        i = 0
        while i < n:
            pos_in_sec = (first + i) % fps
            take = min(n - i, fps - pos_in_sec)
            chunk = db[i:i + take]
            if self._background is None:
                bg = np.median(chunk, axis=0)  # cold start: first frames' own median
                self._background = bg
            excess[i:i + take] = np.clip(chunk - self._background, 0, 40)
            self._sec_frames.append(chunk)
            if pos_in_sec + take == fps:
                sec = np.concatenate(self._sec_frames)
                self._sec_medians.append(np.median(sec, axis=0))
                self._sec_frames = []
                self._background = np.percentile(np.asarray(self._sec_medians), self.bg_pct, axis=0).astype(np.float32)
            i += take
        score = excess[:, self.score_bands].mean(axis=1) if len(self.score_bands) else np.zeros(n, np.float32)
        level = (10 * np.log10(np.sum(10 ** (db / 10), axis=1) + 1e-12)).astype(np.float32)
        return FrameBatch(first, excess, score.astype(np.float32), level)

    def frame_offset_seconds(self) -> float:
        """Time of a frame relative to its start sample: the window centre."""
        return self.win / 2 / self.sr
