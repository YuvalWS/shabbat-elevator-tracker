import json

import pytest

from shabbat_elevator_tracker.config import config_from_dict
from shabbat_elevator_tracker.model import (
    build_timeline,
    next_arrival,
    next_arrivals,
    phase,
    position,
    scaled,
    timeline_from_config,
)

# Design doc section 4.3: arrivals after the floor-3 arrival (fit with G = 59.75 s)
TABLE_4_3 = {"3": 0, "1": 27.7, "G": 52.6, "2": 130.1, "4": 157.8, "6": 185.5, "8": 213.2,
             "10": 240.9, "12": 268.6, "11": 293.5, "9": 321.3, "7": 349.0, "5": 376.7}


@pytest.fixture
def tl():
    return timeline_from_config(config_from_dict({}))


def test_reference_table(tl):
    arrive = {s.floor: s.arrive for s in tl.stops}
    for f, v in TABLE_4_3.items():
        assert arrive[f] == pytest.approx(v, abs=0.5), f


def test_closure_and_order(tl):
    assert tl.stops[0].floor == "3" and tl.stops[0].arrive == 0
    assert tl.moves[-1].end == pytest.approx(tl.cycle)
    t = 0.0
    for s, m in zip(tl.stops, tl.moves, strict=True):
        assert s.arrive == pytest.approx(t)
        assert s.arrive < s.door_open < s.door_close < s.depart == pytest.approx(m.start)
        t = m.end
    assert {m.direction for m in tl.moves} == {"up", "down"}


def test_dwell_override(tl):
    g = next(s for s in tl.stops if s.floor == "G")
    assert g.depart - g.arrive == pytest.approx(60 * tl.scale)


def test_phase_and_position(tl):
    a = 1_000_000.0
    assert phase(a + tl.cycle + 5, a, tl.cycle) == pytest.approx(5)
    assert phase(a - 5, a, tl.cycle) == pytest.approx(tl.cycle - 5)
    p = position(tl, a, a + 1)
    assert p["kind"] == "stop" and p["floor"] == "3" and p["doors"] == "opening"
    p = position(tl, a, a + 5)
    assert p["doors"] == "open"
    m = tl.moves[0]
    p = position(tl, a, a + (m.start + m.end) / 2)
    assert p["kind"] == "move" and p["from"] == "3" and p["to"] == "1" and p["direction"] == "down"
    assert p["floor_pos"] == pytest.approx(2.0, abs=0.01)  # halfway between floor 3 (pos 3) and 1 (pos 1)


def test_next_arrival_wraps(tl):
    a = 1_000_000.0
    n = next_arrival(tl, a, "3", a + 20)
    assert n["arrive_at"] == pytest.approx(a + tl.cycle)
    n = next_arrival(tl, a, "3", a + 3)
    assert "here_until" in n and n["here_until"] == pytest.approx(a + tl.stops[0].depart)
    n = next_arrival(tl, a, "1", a + 1)
    assert n["arrive_at"] == pytest.approx(a + tl.stops[1].arrive)


def test_floor_not_on_route():
    tl = build_timeline(["G", "1", "2", "3"], ["G", "2", "3"], "3", 120, 10, door_transition_seconds=2)
    assert next_arrival(tl, 0, "1", 5) is None
    assert next_arrivals(tl, 0, 5)["1"] is None


def test_duplicate_floor_in_route():
    tl = build_timeline(["G", "1", "2"], ["G", "1", "2", "1"], "1", 100, 8, door_transition_seconds=2,
                        anchor_route_index=3)
    visits = [s for s in tl.stops if s.floor == "1"]
    assert [v.visit for v in visits] == [0, 1]
    n = next_arrival(tl, 0, "1", 1)  # at the first visit right now: next is the second visit
    assert n["arrive_at"] == pytest.approx(visits[1].arrive)


def test_scale_warning():
    tl = build_timeline(["G", "1"], ["G", "1"], "1", 600, 10, door_transition_seconds=2)
    assert tl.warnings


def test_scaled(tl):
    t2 = scaled(tl, tl.cycle + 1)
    assert t2.cycle == tl.cycle + 1
    assert t2.moves[-1].end == pytest.approx(t2.cycle)


def test_reference_fixture_matches(root, tl):
    """tests/fixtures/timeline_reference.json is what web/model.js is tested against."""
    fx = json.loads((root / "tests/fixtures/timeline_reference.json").read_text())
    assert fx["timeline"] == json.loads(json.dumps(tl.as_dict()))
    a = fx["anchor_time_ms"] / 1000
    for q in fx["queries"]:
        now = q["now_ms"] / 1000
        pos = position(tl, a, now)
        assert pos["kind"] == q["position"]["kind"]
        assert pos["floor_pos"] == pytest.approx(q["position"]["floor_pos"], abs=1e-3)
        for f, v in q["next"].items():
            n = next_arrival(tl, a, f, now)
            assert n["arrive_at"] * 1000 == pytest.approx(v["arrive_at"], abs=1)
