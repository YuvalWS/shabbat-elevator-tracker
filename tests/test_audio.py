"""Tier 0: synthetic audio through features -> detector -> sync."""

import numpy as np
import pytest

from shabbat_elevator_tracker import synth
from shabbat_elevator_tracker.config import config_from_dict
from shabbat_elevator_tracker.detector import Detector, similarity
from shabbat_elevator_tracker.features import FeatureExtractor
from shabbat_elevator_tracker.pipeline import AudioPipeline
from shabbat_elevator_tracker.sync import SyncEngine, SyncParams

T0 = 1_800_000_000.0


def tone_burst(sr, seconds, start, dur, band=(600, 4000), db=35, seed=0):
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    x = rng.standard_normal(n) * 10 ** (-60 / 20)
    k = int(dur * sr)
    burst = rng.standard_normal(k)
    f = np.fft.rfftfreq(k, 1 / sr)
    spec = np.fft.rfft(burst)
    spec[(f < band[0]) | (f > band[1])] = 0
    burst = np.fft.irfft(spec, k)
    burst *= 10 ** ((-60 + db) / 20) / (np.std(burst) + 1e-12)
    s = int(start * sr)
    x[s:s + k] += burst
    return np.clip(x * 32768, -32768, 32767).astype(np.int16)


def test_features_shapes_and_score():
    fe = FeatureExtractor(16000)
    assert fe.hop == 1600 and fe.win == 2048 and fe.nbands == 80
    x = tone_burst(16000, 60, 40, 8)
    b = fe.push(x)
    assert b.excess.shape == (len(b), 80)
    t = np.arange(len(b)) * 0.1
    assert b.score[(t > 41) & (t < 47)].mean() > 25
    assert b.score[(t > 31) & (t < 39)].mean() < 5


def test_features_chunking_is_consistent():
    x = tone_burst(8000, 50, 35, 6)
    a = FeatureExtractor(8000).push(x).score
    fe = FeatureExtractor(8000)
    parts = [fe.push(x[i:i + 777]) for i in range(0, len(x), 777)]
    b = np.concatenate([p.score for p in parts])
    assert len(a) == len(b)
    # identical once the first second has set the background (cold start differs by chunking)
    assert np.allclose(a[10:], b[10:], atol=1e-3)


def test_detector_finds_burst_and_rejects_short():
    sr = 16000
    x = tone_burst(sr, 80, 40, 8) + tone_burst(sr, 80, 60, 0.6, seed=2)
    fe = FeatureExtractor(sr)
    b = fe.push(x)
    times = T0 + (np.arange(len(b)) * fe.hop + fe.win / 2) / sr
    det = Detector()
    cands = det.push(times, b.score, b.excess) + det.flush()
    ok = [c for c in cands if not c.rejected]
    assert len(ok) == 1
    assert ok[0].onset - T0 == pytest.approx(40, abs=0.3)
    assert 7 < ok[0].duration < 9.5
    assert any(c.rejected == "short" for c in cands)
    assert ok[0].spectrum is not None and len(ok[0].spectrum) == 80


def test_signature_rejection():
    sr = 16000
    x = tone_burst(sr, 60, 35, 8, band=(300, 1200))
    fe = FeatureExtractor(sr, band_hz=(300, 1200))
    b = fe.push(x)
    times = T0 + np.arange(len(b)) * 0.1
    sig = np.zeros(80)
    sig[25:40] = 30  # a different sound
    det = Detector(signature=list(sig), min_similarity=0.9)
    cands = [c for c in det.push(times, b.score, b.excess) + det.flush() if c.duration > 2]
    assert cands and cands[0].rejected == "signature"


def test_similarity():
    a = np.linspace(0, 10, 80)
    assert similarity(a, a * 2 + 1) == pytest.approx(1.0)
    assert similarity(a, a[::-1]) == pytest.approx(-1.0)
    assert similarity(None, a) is None


@pytest.mark.parametrize("seed", [3, 4])
def test_synthetic_hours_lock_pause_relock(seed):
    cfg = config_from_dict({"audio": {"sample_rate": 8000}, "calendar": {"enabled": False}})
    pl = synth.plan(cfg, 2.2 * 3600, seed=seed, pauses=[(3000, 2000, 94)], distractors_per_hour=30)
    events = []
    sync = SyncEngine(SyncParams.from_config(cfg), on_event=events.append)
    pipe = AudioPipeline(cfg, sync)
    pipe.start_session(T0)
    for _t, x in synth.render(pl, seed=seed):
        pipe.push(x)
        sync.tick(pipe.session_time, True, None)
    replaced = {e["replaces"] for e in events if e.get("replaces")}
    syncs = [e for e in events if e["type"] in ("sync", "lock", "rephase") and e["anchor_time"] not in replaced]
    truth = np.array(pl.anchors) + T0
    errs = [np.min(np.abs(truth - s["anchor_time"])) for s in syncs]
    assert max(errs) < 1.0, "no off-phase syncs"
    unsynced = [a for a in truth if min(abs(a - s["anchor_time"]) for s in syncs) > 1.5]
    assert len(unsynced) <= 2  # the confirmations after start and after the pause
    assert any(e["type"] == "pause" for e in events)
    assert sync.state == "locked"
    assert sync.cycle == pytest.approx(pl.cycle, abs=0.3)


def test_stream_gap_starts_new_session():
    cfg = config_from_dict({"audio": {"sample_rate": 8000}, "calendar": {"enabled": False}})
    pl = synth.plan(cfg, 1500, seed=9, distractors_per_hour=0)
    events = []
    sync = SyncEngine(SyncParams.from_config(cfg), on_event=events.append)
    pipe = AudioPipeline(cfg, sync)
    pipe.start_session(T0)
    gap = (600, 660)
    for t, x in synth.render(pl, seed=9):
        if gap[0] <= t < gap[1]:
            continue
        if t == gap[1]:
            pipe.start_session(T0 + t)
        pipe.push(x)
        sync.tick(pipe.session_time, True, None)
    syncs = [e["anchor_time"] for e in events if e["type"] in ("sync", "lock")]
    truth = np.array(pl.anchors) + T0
    assert syncs and all(np.min(np.abs(truth - s)) < 1.0 for s in syncs)
