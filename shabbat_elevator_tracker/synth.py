"""Deterministic synthetic elevator audio (section 17.1).

Pink-ish background noise, a loud band-limited burst at every anchor-floor arrival,
faint low-frequency "approach" sounds before the other floors' arrivals, random loud
distractors, and optional pauses with a phase jump. Generated chunk by chunk with
overlap-add, so hours of audio need little memory.
"""

from __future__ import annotations

import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .model import timeline_from_config


@dataclass
class SynthEvent:
    t: float  # start, seconds from the synthetic start
    duration: float
    kind: str  # anchor | approach | distractor
    band: tuple[float, float]
    level_db: float  # per-band level over the background at 1 kHz


@dataclass
class SynthPlan:
    duration: float
    sample_rate: int
    cycle: float
    anchors: list[float] = field(default_factory=list)  # true anchor onsets (s from start)
    events: list[SynthEvent] = field(default_factory=list)
    pauses: list[tuple[float, float]] = field(default_factory=list)


def plan(cfg, duration: float, seed: int = 1, first_anchor: float = 30.0,
         pauses: list[tuple[float, float, float]] | None = None,
         distractors_per_hour: float = 20.0, jitter: float = 0.15,
         anchor_db: float = 35.0, approach_db: float = 8.0, approach_lead: float = 7.5,
         sample_rate: int | None = None) -> SynthPlan:
    """pauses: (start_s, length_s, phase_jump_s)."""
    rng = np.random.default_rng(seed)
    sr = int(sample_rate or cfg.audio["sample_rate"])
    tl = timeline_from_config(cfg)
    C = tl.cycle
    hi = min(4000.0, sr / 2 - 50)
    p = SynthPlan(duration, sr, C)
    pauses = sorted(pauses or [])
    t = first_anchor
    pi = 0
    while t < duration:
        if pi < len(pauses) and t >= pauses[pi][0]:
            ps, pl, jump = pauses[pi]
            p.pauses.append((ps, ps + pl))
            t = ps + pl + (jump % C)
            pi += 1
            continue
        ta = t + rng.normal(0, jitter)
        if ta < duration:
            p.anchors.append(ta)
            p.events.append(SynthEvent(ta, float(rng.uniform(8, 13)), "anchor", (600.0, hi), anchor_db))
            for s in tl.stops[1:]:
                tap = ta + s.arrive - approach_lead
                if pi < len(pauses) and tap >= pauses[pi][0]:
                    break
                if 0 <= tap < duration:
                    p.events.append(SynthEvent(tap, 4.0, "approach", (60.0, 300.0), approach_db))
        t += C
    n = rng.poisson(distractors_per_hour * duration / 3600)
    for _ in range(n):
        lo_f = float(rng.uniform(200, 1500))
        hi_f = float(min(hi, lo_f + rng.uniform(500, 2500)))
        p.events.append(SynthEvent(float(rng.uniform(0, duration)), float(rng.uniform(0.3, 6.0)),
                                   "distractor", (lo_f, hi_f), float(rng.uniform(15, 38))))
    p.events.sort(key=lambda e: e.t)
    return p


class _Renderer:
    def __init__(self, plan_: SynthPlan, seed: int, background_dbfs: float = -60.0) -> None:
        self.p = plan_
        self.sr = plan_.sample_rate
        self.seed = seed
        self.L = self.sr  # 1 s hop for overlap-add
        f = np.fft.rfftfreq(2 * self.L, 1 / self.sr)
        self.f = f
        shape = 1.0 / np.sqrt(np.maximum(f, 50.0) / 1000.0)
        shape[f < 30] = 0
        self.bg_shape = shape
        # calibrate background level
        x = self._noise(np.random.default_rng(0), shape, 2 * self.L)
        rms = float(np.sqrt(np.mean(x**2)))
        self.bg_gain = 10 ** (background_dbfs / 20) / rms
        self.win = np.sqrt(np.hanning(2 * self.L + 1)[:-1])
        self._prev_tail = np.zeros(self.L)
        self._cache: dict[int, np.ndarray] = {}

    def _noise(self, rng, amp: np.ndarray, n: int) -> np.ndarray:
        spec = (rng.standard_normal(len(amp)) + 1j * rng.standard_normal(len(amp))) * amp
        return np.fft.irfft(spec, n)

    def _event_wave(self, i: int, ev: SynthEvent) -> np.ndarray:
        if i in self._cache:
            return self._cache[i]
        n = int(ev.duration * self.sr)
        rng = np.random.default_rng((self.seed, i, 7))
        f = np.fft.rfftfreq(n, 1 / self.sr)
        amp = ((f >= ev.band[0]) & (f <= ev.band[1])).astype(float)
        # per-bin amplitude relative to background at 1 kHz (bins differ in width: scale by length)
        bg_ref = self.bg_gain * np.sqrt(n / (2 * self.L))
        x = self._noise(rng, amp, n) * bg_ref * 10 ** (ev.level_db / 20)
        env = np.ones(n)
        a = min(n // 4, int(0.4 * self.sr))
        if a > 0:
            env[:a] = np.linspace(0, 1, a)
            env[-a:] = np.linspace(1, 0, a)
        if ev.kind == "distractor":  # speech-like amplitude modulation
            tt = np.arange(n) / self.sr
            env *= 0.55 + 0.45 * np.sin(2 * np.pi * rng.uniform(3, 6) * tt) ** 2
        w = x * env
        self._cache[i] = w
        return w

    def chunks(self) -> Iterator[tuple[float, np.ndarray]]:
        total = int(self.p.duration * self.sr)
        evs = self.p.events
        ei = 0
        k = 0
        pos = 0
        while pos < total:
            rng = np.random.default_rng((self.seed, k))
            seg = self._noise(rng, self.bg_shape, 2 * self.L) * self.bg_gain * self.win
            out = self._prev_tail + seg[: self.L]
            self._prev_tail = seg[self.L:]
            n = min(self.L, total - pos)
            out = out[:n].copy()
            t0 = pos / self.sr
            t1 = t0 + n / self.sr
            while ei < len(evs) and evs[ei].t + evs[ei].duration < t0:
                self._cache.pop(ei, None)
                ei += 1
            j = ei
            while j < len(evs) and evs[j].t < t1:
                ev = evs[j]
                if ev.t + ev.duration > t0:
                    w = self._event_wave(j, ev)
                    s = int(round(ev.t * self.sr))
                    a = max(s, pos)
                    b = min(s + len(w), pos + n)
                    if b > a:
                        out[a - pos:b - pos] += w[a - s:b - s]
                j += 1
            yield t0, np.clip(out * 32768, -32768, 32767).astype(np.int16)
            pos += n
            k += 1


def render(plan_: SynthPlan, seed: int = 1, background_dbfs: float = -60.0) -> Iterator[tuple[float, np.ndarray]]:
    """Yields (seconds from start, int16 samples) in 1 s chunks."""
    return _Renderer(plan_, seed, background_dbfs).chunks()


def write_recordings(directory: str | Path, plan_: SynthPlan, start_epoch: float, seed: int = 1,
                     segment_seconds: float = 1800.0) -> list[Path]:
    """Write recorder-style WAVs (<epoch_ms>.wav, contiguous segments)."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    w = None
    seg_start = None
    for t, x in render(plan_, seed):
        if w is None or (t - seg_start) >= segment_seconds:
            if w is not None:
                w.close()
            seg_start = t
            path = d / f"{int(round((start_epoch + t) * 1000))}.wav"
            w = wave.open(str(path), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(plan_.sample_rate)
            paths.append(path)
        w.writeframes(x.tobytes())
    if w is not None:
        w.close()
    return paths
