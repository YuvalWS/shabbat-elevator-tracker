"""`calibrate`: derive the cycle, the anchor sound and the route timing from recordings
(section 15.3). Reads recorder WAVs file by file, keeps 0.1 s band features in memory
(float16, ~6 MB per hour), and writes a report plus `calibration.yaml`.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from .config import Config, config_from_dict
from .detector import Detector, similarity
from .features import BAND_HZ, FRAME_SECONDS, FeatureExtractor
from .model import build_timeline
from .replay import GAP_SECONDS, read_wav, recording_files
from .sync import SyncEngine, SyncParams

FPS = int(round(1 / FRAME_SECONDS))


@dataclass
class Features:
    sr: int
    times: np.ndarray  # (n,) epoch s of each frame
    excess: np.ndarray  # (n, bands) float16
    files: list[dict] = field(default_factory=list)

    @property
    def nbands(self) -> int:
        return self.excess.shape[1]


def _file_stats(path: Path, t0: float, sr: int, x: np.ndarray) -> dict:
    xf = x.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(xf * xf))) if len(xf) else 0.0
    clip = float(np.mean(np.abs(x) >= 32767)) if len(x) else 0.0
    # dropouts: runs of exact digital silence longer than 0.5 s
    z = (x == 0).astype(np.int8)
    drop = 0.0
    if z.any():
        d = np.diff(np.concatenate([[0], z, [0]]))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        runs = (ends - starts) / sr
        drop = float(runs[runs > 0.5].sum())
    return {"file": path.name, "start": t0, "seconds": len(x) / sr,
            "level_dbfs": round(20 * math.log10(rms + 1e-12), 1), "clipped_fraction": clip,
            "dropout_seconds": round(drop, 1)}


def extract(directory: str | Path, progress: bool = False) -> Features:
    files = recording_files(directory)
    if not files:
        raise FileNotFoundError(f"no recorder WAVs (<epoch_ms>.wav) in {directory}")
    fe = None
    times, excess, stats = [], [], []
    prev_end = None
    sr0 = None
    for path, t0 in files:
        sr, x = read_wav(path)
        if sr0 is None:
            sr0 = sr
            fe = FeatureExtractor(sr)
        elif sr != sr0:
            raise ValueError(f"{path.name}: sample rate {sr} differs from {sr0}")
        st = _file_stats(path, t0, sr, x)
        st["gap_before_s"] = None if prev_end is None else round(t0 - prev_end, 3)
        stats.append(st)
        if prev_end is None or abs(t0 - prev_end) > GAP_SECONDS:
            fe.reset()
            fe.set_frame_index(0)
            session_t0 = t0
        b = fe.push(x)
        idx = np.arange(b.first_index, b.first_index + len(b))
        times.append(session_t0 + (idx * fe.hop + fe.win / 2) / sr)
        excess.append(b.excess.astype(np.float16))
        prev_end = t0 + len(x) / sr
        if progress:
            print(f"  read {path.name} ({st['seconds'] / 60:.1f} min, {st['level_dbfs']} dBFS)", flush=True)
    return Features(sr0, np.concatenate(times), np.concatenate(excess), stats)


def _band_idx(lo: float, hi: float, nb: int) -> np.ndarray:
    a = int(lo // BAND_HZ)
    b = min(nb, int(math.ceil(hi / BAND_HZ)))
    return np.arange(a, max(a + 1, b))


def _per_second(t: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Max of v per whole second on a regular grid (NaN where there is no audio)."""
    s0 = math.floor(t[0])
    k = np.floor(t - s0).astype(np.int64)
    n = int(k[-1]) + 1
    out = np.full(n, np.nan, np.float32)
    np.fmax.at(out, k, v.astype(np.float32))
    return s0 + np.arange(n, dtype=np.float64), out


def _acf(x: np.ndarray, lags: np.ndarray) -> np.ndarray:
    m = ~np.isnan(x)
    y = np.where(m, x - np.nanmean(x), 0.0)
    w = m.astype(np.float64)
    n = len(y)
    out = np.zeros(len(lags))
    den = float(np.sum(y * y)) or 1.0
    for i, L in enumerate(lags):
        if L >= n:
            continue
        num = float(np.dot(y[:-L], y[L:]))
        cover = float(np.dot(w[:-L], w[L:])) / max(1.0, w.sum())
        out[i] = num / den / max(cover, 1e-3) * (cover > 0.3)
    return out


@dataclass
class Window:
    start: float
    end: float
    peak_lag: float
    strength: float
    active: bool


def find_activity(t_s: np.ndarray, e_s: np.ndarray, win: float = 5400, step: float = 1800,
                  lag_min: int = 300, lag_max: int = 1800, min_strength: float = 0.25) -> list[Window]:
    lags = np.arange(lag_min, lag_max + 1)
    out = []
    a = t_s[0]
    while a + win * 0.5 <= t_s[-1] or not out:
        m = (t_s >= a) & (t_s < a + win)
        seg = e_s[m]
        if np.sum(~np.isnan(seg)) < win * 0.5:
            a += step
            if a > t_s[-1]:
                break
            continue
        r = _acf(seg, lags)
        i = int(np.argmax(r))
        lag, strength = float(lags[i]), float(r[i])
        # prefer the fundamental if half the lag is nearly as strong
        half = lag / 2
        if half >= lag_min:
            j = int(np.argmin(np.abs(lags - half)))
            lo, hi = max(0, j - 2), min(len(r), j + 3)
            if r[lo:hi].max() > 0.8 * strength:
                k = lo + int(np.argmax(r[lo:hi]))
                lag, strength = float(lags[k]), float(r[k])
        out.append(Window(float(a), float(min(a + win, t_s[-1])), lag, strength, strength >= min_strength))
        a += step
        if a > t_s[-1]:
            break
    return out


def refine_period(t: np.ndarray, e: np.ndarray, windows: list[Window], p0: float,
                  span: float = 2.0, step: float = 0.01, multiples: int = 6) -> float:
    """Maximise the summed autocorrelation at 1..K multiples of P on the 0.1 s envelope."""
    segs = []
    for w in windows:
        if not w.active:
            continue
        m = (t >= w.start) & (t < w.end)
        if m.sum() < FPS * 600:
            continue
        tt, ee = t[m], e[m].astype(np.float64)
        grid = np.arange(tt[0], tt[-1], FRAME_SECONDS)
        g = np.interp(grid, tt, ee)
        g = (g - g.mean()) / (g.std() + 1e-9)
        segs.append(g)
    if not segs:
        return p0
    grid_p = np.arange(p0 - span, p0 + span + step / 2, step)
    score = np.zeros(len(grid_p))
    for g in segs:
        n = len(g)
        idx = np.arange(n)
        for i, P in enumerate(grid_p):
            s = 0.0
            for k in range(1, multiples + 1):
                L = k * P / FRAME_SECONDS
                if L >= n - FPS * 60:
                    break
                L0 = int(L)
                fr = L - L0
                a = g[: n - L0 - 1]
                b = (1 - fr) * g[L0:n - 1] + fr * g[L0 + 1:n]
                s += float(np.dot(a, b)) / len(a)
            score[i] += s
        del idx
    return float(grid_p[int(np.argmax(score))])


def run_detector(feat: Features, band: np.ndarray, threshold: float, release: float,
                 min_dur: float = 2.0, max_dur: float = 30.0) -> list:
    det = Detector(threshold, release, min_dur, max_dur)
    score = feat.excess[:, band].astype(np.float32).mean(axis=1)
    cands = []
    # split on gaps so events don't span missing audio
    gaps = np.flatnonzero(np.diff(feat.times) > 1.0) + 1
    bounds = [0, *gaps.tolist(), len(score)]
    for a, b in zip(bounds[:-1], bounds[1:], strict=False):
        step = FPS * 600
        for i in range(a, b, step):
            j = min(b, i + step)
            cands += det.push(feat.times[i:j], score[i:j], feat.excess[i:j].astype(np.float32))
        cands += det.flush()
        det.reset()
    return cands


def _fold_phase(times: np.ndarray, period: float, ref: float, values: np.ndarray, nbins: int) -> np.ndarray:
    ph = ((times - ref) % period) / period
    k = np.minimum((ph * nbins).astype(np.int64), nbins - 1)
    s = np.zeros((nbins,) + values.shape[1:])
    c = np.zeros(nbins)
    np.add.at(s, k, values)
    np.add.at(c, k, 1)
    return s / np.maximum(c, 1).reshape((-1,) + (1,) * (values.ndim - 1))


def simulate_sync(cfg: Config, feat: Features, band_idx: np.ndarray) -> tuple[list[dict], float]:
    """Detector + sync on the stored features (no FFT); returns (events, final cycle)."""
    events: list[dict] = []
    sync = SyncEngine(SyncParams.from_config(cfg), on_event=events.append)
    d = cfg.detector
    det = Detector(float(d["threshold_db"]), float(d["release_db"]), float(d["min_event_seconds"]),
                   float(d["max_event_seconds"]))
    score = feat.excess[:, band_idx].astype(np.float32).mean(axis=1)
    step = FPS * 5
    gaps = set((np.flatnonzero(np.diff(feat.times) > 1.0) + 1).tolist())
    for i in range(0, len(score), step):
        j = min(len(score), i + step)
        if any(i <= g < j for g in gaps):
            for c in det.flush():
                sync.candidate(c, float(feat.times[i]))
            det.reset()
        for c in det.push(feat.times[i:j], score[i:j], feat.excess[i:j].astype(np.float32)):
            sync.candidate(c, float(feat.times[j - 1]))
        sync.tick(float(feat.times[j - 1]), True, None)
    for c in det.flush():
        sync.candidate(c, float(feat.times[-1]))
    return events, sync.cycle


def _effective_syncs(events: list[dict]) -> list[dict]:
    replaced = {e["replaces"] for e in events if e.get("replaces") is not None}
    return [e for e in events if e["type"] in ("sync", "lock", "rephase") and e["anchor_time"] not in replaced]


def fit_route(cfg: Config, feat: Features, anchors: list[float], cycle: float,
              remote_band: tuple[float, float] = (0, 300), search: float = 6.0,
              fit_overrides: bool = False) -> dict | None:
    """Fit travel parameters (and optionally the dwell overrides) from the faint sounds of
    the other floors. Dwell times are taken from the config by default: audio cannot
    separate dwell from travel, and the configured values come from observation."""
    a = np.array(sorted(anchors))
    if len(a) < 4:
        return None
    # use anchors whose next anchor follows one cycle later (inside a locked stretch)
    good = [x for x, y in zip(a[:-1], a[1:], strict=False) if abs((y - x) - cycle) < 3]
    if len(good) < 3:
        return None
    nbins = int(round(cycle / FRAME_SECONDS))
    bidx = _band_idx(remote_band[0], remote_band[1], feat.nbands)
    low = feat.excess[:, bidx].astype(np.float32).mean(axis=1)
    prof = np.zeros(nbins)
    cnt = np.zeros(nbins)
    for x in good:
        i0, i1 = np.searchsorted(feat.times, [x, x + cycle])
        k = ((feat.times[i0:i1] - x) / FRAME_SECONDS).astype(np.int64)
        k = np.clip(k, 0, nbins - 1)
        np.add.at(prof, k, low[i0:i1])
        np.add.at(cnt, k, 1)
    prof = prof / np.maximum(cnt, 1)
    sm = np.convolve(np.concatenate([prof[-5:], prof, prof[:5]]), np.ones(11) / 11, mode="same")[5:-5]
    base = np.median(sm)
    x = sm - base
    peaks = []
    for i in range(nbins):
        if x[i] > 0.8 and x[i] == max(x[max(0, i - 40):i + 41].max(), x[(i + np.arange(-40, 41)) % nbins].max()):
            peaks.append((i * FRAME_SECONDS, float(x[i])))
    if not peaks:
        return None
    pk = np.array([p[0] for p in peaks])

    e = cfg.elevator
    floors = cfg.building["floors"]
    pos = {f: i for i, f in enumerate(floors)}
    route = e["route"]
    ia = cfg.anchor.get("route_index")
    ia = route.index(cfg.anchor["floor"]) if ia is None else int(ia)
    r = route[ia:] + route[:ia]
    n = len(r)
    d0 = float(e["dwell_seconds"])
    over = [f for f in e["dwell_overrides"] if f in r] if fit_overrides else []
    fixed_over = {} if fit_overrides else {f: float(v) for f, v in e["dwell_overrides"].items()}

    def design(i: int) -> tuple[np.ndarray, float]:
        """arrival(i) = row . [O, F, x_over..., -lead] + const"""
        row = np.zeros(3 + len(over))
        const = 0.0
        for j in range(i):
            f = r[j]
            if f in over:
                row[2 + over.index(f)] += 1
            else:
                const += fixed_over.get(f, d0)
            row[0] += 1
            row[1] += abs(pos[r[(j + 1) % n]] - pos[f])
        return row, const

    rows = [design(i) for i in range(n)]
    total_row, total_const = design(n)  # full cycle
    params = np.array([float(e["travel"]["leg_overhead_seconds"]), float(e["travel"]["per_floor_seconds"]),
                       *[float(e["dwell_overrides"][f]) for f in over], 0.0])

    def predict(p: np.ndarray) -> np.ndarray:
        # scale to the cycle like the model does
        arr = np.array([row[:-1] @ p[:-1] + c for row, c in rows])
        raw = total_row[:-1] @ p[:-1] + total_const
        return arr * (cycle / raw)

    # find the lead (remote sound before the arrival) with the configured model
    arr0 = predict(params)
    best = None
    for lead in np.arange(0, 15.01, 0.25):
        pred = (arr0 - lead) % cycle
        d = np.abs(((pred[:, None] - pk[None, :]) + cycle / 2) % cycle - cycle / 2).min(axis=1)
        sc = float(np.sum(np.minimum(d, search) ** 2))
        if best is None or sc < best[0]:
            best = (sc, lead)
    params[-1] = best[1]
    used = None
    for _ in range(8):
        pred = (predict(params) - params[-1]) % cycle
        obs = np.full(n, np.nan)
        for i in range(n):
            dd = ((pk - pred[i]) + cycle / 2) % cycle - cycle / 2
            j = int(np.argmin(np.abs(dd)))
            if abs(dd[j]) <= search:
                obs[i] = pred[i] + dd[j]
        # one peak may serve one floor only
        for j in range(len(pk)):
            idx = [i for i in range(n) if not np.isnan(obs[i]) and abs(((obs[i] - pk[j]) + cycle / 2) % cycle - cycle / 2) < 1e-6]
            if len(idx) > 1:
                keep = min(idx, key=lambda i: abs(obs[i] - pred[i]))
                for i in idx:
                    if i != keep:
                        obs[i] = np.nan
        m = ~np.isnan(obs)
        if m.sum() < len(params) + 1:
            return None
        A, y = [], []
        for i in range(n):
            if not m[i]:
                continue
            row, c = rows[i]
            o = obs[i] if i > 0 or obs[i] < cycle / 2 else obs[i] - cycle  # anchor's own sound wraps
            if i == 0 and o > cycle / 2:
                o -= cycle
            A.append(np.concatenate([row[:-1], [-1.0]]))
            y.append(o - c)
        w = 100.0  # cycle closure constraint
        A.append(np.concatenate([total_row[:-1], [0.0]]) * w)
        y.append((cycle - total_const) * w)
        sol, *_ = np.linalg.lstsq(np.array(A), np.array(y), rcond=None)
        if np.allclose(sol, params, atol=1e-4):
            params = sol
            used = m
            break
        params, used = sol, m
    arr = predict(params)
    lead = params[-1]
    table = []
    for i, f in enumerate(r):
        o = None
        if used is not None and used[i]:
            pred = (arr[i] - lead) % cycle
            dd = ((pk - pred) + cycle / 2) % cycle - cycle / 2
            j = int(np.argmin(np.abs(dd)))
            o = round(float(arr[i] + dd[j]), 2)
            if i == 0 and o > cycle / 2:
                o -= cycle
        table.append({"floor": f, "model_arrive": round(float(arr[i]), 2), "observed_arrive": o,
                      "residual": None if o is None else round(o - float(arr[i]), 2)})
    res = [t["residual"] for t in table if t["residual"] is not None]
    return {
        "leg_overhead_seconds": round(float(params[0]), 2),
        "per_floor_seconds": round(float(params[1]), 2),
        "dwell_overrides": {f: round(float(params[2 + k]), 2) for k, f in enumerate(over)},
        "dwell_fitted": bool(over),
        "remote_lead_seconds": round(float(lead), 2),
        "rms_residual": round(float(np.sqrt(np.mean(np.square(res)))), 2) if res else None,
        "matched_floors": len(res),
        "table": table,
        "peaks": [{"phase": round(p, 1), "db": round(v, 1)} for p, v in peaks],
    }


def calibrate(directory: str | Path, cfg: Config | None = None, progress: bool = False,
              fit: bool = True, fit_overrides: bool = False) -> dict:
    t_start = time.perf_counter()
    cfg = cfg or config_from_dict({})
    tz = ZoneInfo(cfg.calendar["timezone"])
    feat = extract(directory, progress)
    if feat.sr != int(cfg.audio["sample_rate"]):
        raw = copy.deepcopy(cfg.raw)
        raw["audio"]["sample_rate"] = feat.sr
        cfg = config_from_dict(raw)
    nb = feat.nbands
    broad = _band_idx(300, min(4000, feat.sr / 2), nb)
    env = feat.excess[:, broad].astype(np.float32).mean(axis=1)
    t_s, e_s = _per_second(feat.times, env)
    windows = find_activity(t_s, e_s)
    active = [w for w in windows if w.active]
    report: dict = {
        "files": feat.files,
        "audio_hours": round(len(feat.times) * FRAME_SECONDS / 3600, 2),
        "windows": [{"start": w.start, "end": w.end, "peak_lag": w.peak_lag,
                     "strength": round(w.strength, 3), "active": w.active} for w in windows],
    }
    if not active:
        report["error"] = "no periodic activity found"
        return report
    p0 = float(np.median([w.peak_lag for w in active]))
    period = refine_period(feat.times, env, windows, p0)
    report["cycle_coarse"] = p0
    report["cycle_refined"] = round(period, 2)

    # anchor phase and band: fold each active window, align on its broadband peak
    nbins = int(round(period / FRAME_SECONDS))
    acc = np.zeros((nbins, nb))
    for w in active:
        m = (feat.times >= w.start) & (feat.times < w.end)
        if m.sum() < FPS * 600:
            continue
        tt = feat.times[m]
        pe = _fold_phase(tt, period, w.start, env[m], nbins)
        pe_s = np.convolve(np.concatenate([pe[-10:], pe, pe[:10]]), np.ones(21) / 21, mode="same")[10:-10]
        ph = int(np.argmax(pe_s))
        fb = _fold_phase(tt, period, w.start, feat.excess[m].astype(np.float32), nbins)
        acc += np.roll(fb, -ph, axis=0)
    acc /= max(1, len(active))
    med = np.median(acc, axis=0)
    peak = acc[np.r_[nbins - 100:nbins, 0:101] % nbins].max(axis=0)  # +-10 s around the peak
    contrast = peak - med
    b0 = int(np.argmax(contrast[: min(nb, int(4000 // BAND_HZ))]))
    lo = hi = b0
    while lo > 0 and contrast[lo - 1] >= 0.5 * contrast[b0]:
        lo -= 1
    while hi + 1 < nb and contrast[hi + 1] >= 0.5 * contrast[b0]:
        hi += 1
    band_hz = [int(lo * BAND_HZ), int(min((hi + 1) * BAND_HZ, feat.sr / 2))]
    band = _band_idx(*band_hz, nb)
    report["anchor_band_hz"] = band_hz
    report["band_contrast_db"] = [round(float(c), 1) for c in contrast]

    # candidates at several thresholds; anchors are those on the cycle grid
    thr_table = []
    cands25 = None
    for thr in (15.0, 20.0, 25.0, 30.0):
        cands = [c for c in run_detector(feat, band, thr, thr - 10) if not c.rejected]
        on = np.array([c.onset for c in cands])
        is_anchor = np.zeros(len(cands), bool)
        for i, x in enumerate(on):
            d = on - x
            for k in (-2, -1, 1, 2):
                if np.any(np.abs(d - k * period) < 5):
                    is_anchor[i] = True
                    break
        thr_table.append({"threshold_db": thr, "candidates": len(cands), "on_cycle": int(is_anchor.sum()),
                          "off_cycle": int((~is_anchor).sum())})
        if thr == 25.0:
            cands25 = (cands, is_anchor)
    report["thresholds"] = thr_table
    cands, is_anchor = cands25
    anchors = [c for c, a in zip(cands, is_anchor, strict=False) if a]
    if len(anchors) < 3:
        report["error"] = "anchor sound not found at 25 dB"
        return report
    peaks = np.array([c.peak_db for c in anchors])
    durs = np.array([c.duration for c in anchors])
    thr = float(np.clip(round(0.65 * float(np.median(peaks))), 15, 30))
    report["anchor"] = {"count": len(anchors), "median_peak_db": round(float(np.median(peaks)), 1),
                        "median_duration_s": round(float(np.median(durs)), 1),
                        "recommended_threshold_db": thr}
    ons = np.array([c.onset for c in anchors])
    iv = np.diff(ons)
    one = iv[np.abs(iv - period) < 5]
    cycle = float(np.median(one)) if len(one) >= 3 else period
    report["cycle_seconds"] = round(cycle, 2)
    report["cycle_std"] = round(float(np.std(one)), 3) if len(one) >= 3 else None
    spec = np.mean([c.spectrum for c in anchors if c.spectrum is not None], axis=0)
    sims = [similarity(c.spectrum, spec) for c in anchors]
    report["anchor"]["self_similarity_min"] = round(float(np.min(sims)), 3)
    non = [c for c, a in zip(cands, is_anchor, strict=False) if not a]
    if non:
        report["anchor"]["others_similarity_max"] = round(float(max(similarity(c.spectrum, spec) for c in non)), 3)

    # hourly detection rate while the elevator runs (between the first and last anchor of
    # each running stretch; a stretch ends at a gap of more than 3 cycles)
    hours: dict[int, list[float]] = {}
    srt = np.sort(ons)
    stretches, s0 = [], srt[0]
    for x, y in zip(srt[:-1], srt[1:], strict=False):
        if y - x > 3 * period:
            stretches.append((s0, x))
            s0 = y
    stretches.append((s0, srt[-1]))
    for a0, a1 in stretches:
        for h in range(int(a0 // 3600) * 3600, int(a1) + 1, 3600):
            lo_t, hi_t = max(h, a0 - period / 2), min(h + 3600, a1 + period / 2)
            if hi_t <= lo_t:
                continue
            expected_n = max(1, round((hi_t - lo_t) / cycle))
            got = int(np.sum((ons >= lo_t) & (ons < hi_t)))
            e0 = hours.setdefault(h, [0, 0])
            e0[0] += got
            e0[1] += expected_n
    hours = {h: min(1.0, g / n) for h, (g, n) in hours.items()}
    report["hourly_detection"] = [{"hour": dt.datetime.fromtimestamp(h, tz).strftime("%a %H:00"),
                                   "rate": round(r, 2)} for h, r in sorted(hours.items())]

    # suggested config, then sync simulation for pauses and phase jumps
    raw = copy.deepcopy(cfg.raw)
    raw["elevator"]["cycle_seconds"] = round(cycle, 2)
    raw["detector"]["band_hz"] = band_hz
    raw["detector"]["threshold_db"] = thr
    raw["detector"]["release_db"] = thr - 10
    cfg2 = config_from_dict(raw)
    events, final_cycle = simulate_sync(cfg2, feat, band)
    syncs = _effective_syncs(events)
    report["sync_simulation"] = {
        "locks": [e["anchor_time"] for e in events if e["type"] == "lock"],
        "rephases": [e["anchor_time"] for e in events if e["type"] == "rephase"],
        "pauses": [e["at"] for e in events if e["type"] == "pause"],
        "syncs": len(syncs),
        "final_cycle": round(final_cycle, 3),
    }
    st = sorted(e["anchor_time"] for e in syncs)
    gaps = [(a, b) for a, b in zip(st[:-1], st[1:], strict=False) if b - a > 1.5 * cycle]
    jumps = []
    for a, b in gaps:
        k = (b - a) / cycle
        jumps.append({"from": a, "to": b, "phase_shift_s": round(((b - a) - round(k) * cycle), 1)})
    report["pauses"] = jumps

    if fit:
        report["route_fit"] = fit_route(cfg2, feat, st, cycle, fit_overrides=fit_overrides)

    suggestion = {
        "elevator": {"cycle_seconds": round(cycle, 2)},
        "detector": {"band_hz": band_hz, "threshold_db": thr, "release_db": thr - 10,
                     "signature": [round(float(x), 2) for x in spec], "min_similarity": 0.9},
    }
    rf = report.get("route_fit")
    if rf:
        suggestion["elevator"]["travel"] = {"leg_overhead_seconds": rf["leg_overhead_seconds"],
                                            "per_floor_seconds": rf["per_floor_seconds"]}
        if rf["dwell_overrides"]:
            suggestion["elevator"]["dwell_overrides"] = {k: round(v, 1) for k, v in rf["dwell_overrides"].items()}
    report["suggestion"] = suggestion
    report["runtime_s"] = round(time.perf_counter() - t_start, 1)
    return report


def format_report(rep: dict, tz: str) -> str:
    z = ZoneInfo(tz)

    def loc(t: float) -> str:
        return dt.datetime.fromtimestamp(t, z).strftime("%a %Y-%m-%d %H:%M:%S")

    L = ["Shabbat Elevator Tracker - calibration report", ""]
    L.append(f"Audio: {rep['audio_hours']} h in {len(rep['files'])} files")
    for f in rep["files"]:
        gap = "" if not f.get("gap_before_s") or abs(f["gap_before_s"]) < 1 else f"  gap {f['gap_before_s']:.1f} s"
        flags = []
        if f["clipped_fraction"] > 1e-4:
            flags.append(f"clipped {f['clipped_fraction']:.2%}")
        if f["dropout_seconds"]:
            flags.append(f"dropouts {f['dropout_seconds']} s")
        L.append(f"  {f['file']}  {loc(f['start'])}  {f['seconds'] / 60:5.1f} min  {f['level_dbfs']:6.1f} dBFS"
                 f"{gap}{'  ' + ', '.join(flags) if flags else ''}")
    L.append("")
    L.append("Periodic activity (90 min windows):")
    for w in rep["windows"]:
        L.append(f"  {loc(w['start'])}  lag {w['peak_lag']:6.0f} s  strength {w['strength']:.2f}"
                 f"{'  ACTIVE' if w['active'] else ''}")
    if "error" in rep:
        L += ["", f"ERROR: {rep['error']}"]
        return "\n".join(L)
    L += ["", f"Cycle: {rep['cycle_seconds']} s (std {rep['cycle_std']} s; coarse {rep['cycle_coarse']:.0f}, "
              f"refined {rep['cycle_refined']})"]
    a = rep["anchor"]
    L.append(f"Anchor sound: band {rep['anchor_band_hz'][0]}-{rep['anchor_band_hz'][1]} Hz, "
             f"{a['count']} events, median peak +{a['median_peak_db']} dB, median duration {a['median_duration_s']} s")
    L.append(f"  similarity: anchors >= {a['self_similarity_min']}"
             + (f", other sounds <= {a['others_similarity_max']}" if "others_similarity_max" in a else ""))
    L.append(f"  recommended threshold: {a['recommended_threshold_db']} dB")
    L.append("Thresholds:")
    for t in rep["thresholds"]:
        L.append(f"  {t['threshold_db']:4.0f} dB: {t['candidates']:4d} candidates, {t['on_cycle']:4d} on the cycle, "
                 f"{t['off_cycle']:4d} off the cycle")
    L.append("Hourly detection rate:")
    for h in rep["hourly_detection"]:
        L.append(f"  {h['hour']}  {h['rate']:.0%}")
    s = rep["sync_simulation"]
    L += ["", f"Sync simulation: {s['syncs']} syncs, final cycle {s['final_cycle']} s"]
    for t in s["locks"]:
        L.append(f"  lock   {loc(t)}")
    for t in s["rephases"]:
        L.append(f"  rephase {loc(t)}")
    for t in s["pauses"]:
        L.append(f"  pause  {loc(t)}")
    for p in rep["pauses"]:
        L.append(f"  gap {loc(p['from'])} -> {loc(p['to'])}, phase shift {p['phase_shift_s']:+.1f} s")
    rf = rep.get("route_fit")
    if rf:
        L += ["", f"Route fit: leg overhead {rf['leg_overhead_seconds']} s + {rf['per_floor_seconds']} s/floor, "
                  f"dwell overrides {rf['dwell_overrides']}, remote sound {rf['remote_lead_seconds']} s before "
                  f"arrival, RMS {rf['rms_residual']} s over {rf['matched_floors']} floors"]
        L.append("  floor   model  observed  residual")
        for t in rf["table"]:
            o = "    -   " if t["observed_arrive"] is None else f"{t['observed_arrive']:8.1f}"
            r = "   -" if t["residual"] is None else f"{t['residual']:+5.1f}"
            L.append(f"  {t['floor']:>5}  {t['model_arrive']:6.1f}  {o}  {r}")
    elif "route_fit" in rep:
        L += ["", "Route fit: not enough faint floor sounds found."]
    L += ["", f"Runtime {rep.get('runtime_s')} s"]
    return "\n".join(L)


def write_suggestion(rep: dict, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Suggested values from `shabbat-elevator-tracker calibrate`. Merge into config.yaml.\n")
        yaml.safe_dump(rep["suggestion"], f, sort_keys=False, default_flow_style=None)


def check_against_model(rf: dict, cfg: Config) -> list[tuple[str, float, float]]:
    """(floor, configured-model arrival, fitted arrival) for comparisons."""
    e = cfg.elevator
    tl = build_timeline(cfg.building["floors"], e["route"], cfg.anchor["floor"], float(e["cycle_seconds"]),
                        float(e["dwell_seconds"]), e["dwell_overrides"], float(e["door_transition_seconds"]),
                        float(e["travel"]["leg_overhead_seconds"]), float(e["travel"]["per_floor_seconds"]),
                        cfg.anchor.get("route_index"))
    fitted = {t["floor"]: t["model_arrive"] for t in rf["table"]}
    return [(s.floor, s.arrive, fitted[s.floor]) for s in tl.stops if s.floor in fitted]
