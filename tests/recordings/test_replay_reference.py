"""Tier 1: the local-only reference recordings (test-data/, never pushed).

Skipped automatically when test-data/ is absent (e.g. in CI).
"""

import time

import pytest

from shabbat_elevator_tracker.calibrate import calibrate
from shabbat_elevator_tracker.config import config_from_dict
from shabbat_elevator_tracker.replay import compare_expected, load_expected, run_replay

from ..conftest import EXPECTED, RECORDINGS

pytestmark = pytest.mark.recordings

TABLE_4_3 = {"3": 0, "1": 27.7, "G": 52.6, "2": 130.1, "4": 157.8, "6": 185.5, "8": 213.2,
             "10": 240.9, "12": 268.6, "11": 293.5, "9": 321.3, "7": 349.0, "5": 376.7}


def test_replay_meets_acceptance():
    cfg = config_from_dict({})
    t = time.perf_counter()
    res = run_replay(cfg, RECORDINGS)
    ok, lines = compare_expected(res, load_expected(EXPECTED), cfg)
    print("\n".join(lines))
    assert ok, "\n".join(lines)
    assert time.perf_counter() - t < 60


def test_calibrate_reproduces_reference():
    rep = calibrate(RECORDINGS, config_from_dict({}))
    assert "error" not in rep
    assert rep["cycle_seconds"] == pytest.approx(404.5, abs=0.3)
    lo, hi = rep["anchor_band_hz"]
    assert lo < 2200 and hi > 600
    assert rep["pauses"], "the night pause must be reported"
    rf = rep["route_fit"]
    assert rf is not None
    for row in rf["table"]:
        assert row["model_arrive"] == pytest.approx(TABLE_4_3[row["floor"]], abs=1.5), row
