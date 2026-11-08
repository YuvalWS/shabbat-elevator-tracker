import numpy as np
import pytest

from shabbat_elevator_tracker.detector import Candidate
from shabbat_elevator_tracker.sync import SyncEngine, SyncParams

C = 404.4
T0 = 1_800_000_000.0
SPEC = np.linspace(1, 30, 80).astype(np.float32)


def cand(t, dur=7.0, spectrum=SPEC, rejected=None):
    c = Candidate(onset=t, duration=dur, peak_db=35, mean_db=33, spectrum=spectrum)
    c.rejected = rejected
    return c


def run(engine, events, until, stream=lambda t: True, window=lambda t: None, step=1.0):
    """events: sorted list of (onset, Candidate). Each candidate is reported 10 s after onset."""
    q = sorted(events, key=lambda e: e[0])
    t = q[0][0] if q else T0
    i = 0
    while t <= until:
        while i < len(q) and q[i][0] + 10 <= t:
            engine.candidate(q[i][1], t)
            i += 1
        engine.tick(t, stream(t), window(t))
        t += step


def new_engine(**kw):
    evs = []
    return SyncEngine(SyncParams(cycle_seconds=C, **kw), on_event=evs.append), evs


def test_lock_after_confirmed_pair():
    e, evs = new_engine()
    run(e, [(T0, cand(T0)), (T0 + C, cand(T0 + C))], T0 + C + 20)
    assert e.state == "locked"
    assert e.last_anchor == pytest.approx(T0 + C)
    assert [x["type"] for x in evs if x["type"] in ("lock", "sync")] == ["lock"]


def test_single_candidate_is_provisional_then_expires():
    e, _ = new_engine()
    run(e, [(T0, cand(T0))], T0 + 60)
    assert e.state == "provisional"
    run(e, [], T0 + 3 * C + 100)
    assert e.state == "searching"


def test_off_phase_events_ignored_while_locked():
    e, evs = new_engine()
    ev = [(T0 + k * C, cand(T0 + k * C)) for k in range(6)]
    ev += [(T0 + 2 * C + 100, cand(T0 + 2 * C + 100)), (T0 + 3 * C + 250, cand(T0 + 3 * C + 250))]
    run(e, ev, T0 + 5 * C + 30)
    syncs = [x["anchor_time"] for x in evs if x["type"] in ("lock", "sync")]
    assert all(abs((s - T0) / C - round((s - T0) / C)) * C < 1 for s in syncs)
    assert e.state == "locked" and e.missed == 0


def test_distractor_just_before_anchor_is_replaced():
    e, evs = new_engine()
    ev = [(T0 + k * C, cand(T0 + k * C)) for k in range(4)]
    ev.append((T0 + 2 * C - 7, cand(T0 + 2 * C - 7, dur=2.5)))
    run(e, ev, T0 + 3 * C + 30)
    replaced = {x["replaces"] for x in evs if x.get("replaces")}
    eff = [x["anchor_time"] for x in evs if x["type"] in ("lock", "sync") and x["anchor_time"] not in replaced]
    assert T0 + 2 * C - 7 not in eff
    assert any(abs(s - (T0 + 2 * C)) < 0.01 for s in eff)


def test_pause_after_three_misses_then_relock_after_phase_jump():
    e, evs = new_engine()
    ev = [(T0 + k * C, cand(T0 + k * C)) for k in range(4)]
    resume = T0 + 3 * C + 3 * 3600 + 94
    ev += [(resume + k * C, cand(resume + k * C)) for k in range(3)]
    run(e, ev, T0 + 3 * C + 3 * C + 60, step=2)
    assert e.state == "paused"
    pause = next(x for x in evs if x["type"] == "pause")
    assert pause["at"] <= T0 + 3 * C + 3 * C + 8 + 32 + 2
    run(e, [x for x in ev if x[0] >= resume], resume + C + 20, step=2)
    assert e.state == "locked"
    assert e.last_anchor == pytest.approx(resume + C)


def test_stream_loss_does_not_count_misses():
    e, _ = new_engine()
    ev = [(T0, cand(T0)), (T0 + C, cand(T0 + C))]
    lost = (T0 + C + 50, T0 + C + 5 * C)
    run(e, ev, T0 + 5 * C, stream=lambda t: not (lost[0] <= t <= lost[1]), step=2)
    assert e.state == "locked" and e.missed == 0


def test_idle_outside_window_and_calendar_never_overrides_audio():
    e, _ = new_engine()
    run(e, [], T0 + 10, window=lambda t: False)
    assert e.state == "idle"
    run(e, [(T0 + 20, cand(T0 + 20)), (T0 + 20 + C, cand(T0 + 20 + C))], T0 + C + 60, window=lambda t: False)
    assert e.state == "locked"
    # elevator stops, window closed: back to idle after missed anchors / 2 cycles
    run(e, [], T0 + 5 * C, window=lambda t: False, step=5)
    assert e.state == "idle"


def test_rejected_candidates_do_nothing():
    e, _ = new_engine()
    run(e, [(T0, cand(T0, rejected="short")), (T0 + C, cand(T0 + C, rejected="short"))], T0 + C + 30)
    assert e.state == "searching"


def test_dissimilar_pair_does_not_lock():
    e, _ = new_engine()
    other = np.linspace(30, 1, 80).astype(np.float32)
    run(e, [(T0, cand(T0)), (T0 + C, cand(T0 + C, spectrum=other))], T0 + C + 30)
    assert e.state == "provisional"


def test_duration_ratio_blocks_lock():
    e, _ = new_engine()
    run(e, [(T0, cand(T0, dur=2.2)), (T0 + C, cand(T0 + C, dur=7.0))], T0 + C + 30)
    assert e.state == "provisional"


def test_adaptive_cycle_clamped():
    e, _ = new_engine(max_deviation_percent=1.0)
    c2 = C * 1.05
    run(e, [(T0 + k * c2, cand(T0 + k * c2)) for k in range(2)], T0 + c2 + 30)
    assert e.cycle <= C * 1.01 + 1e-9


def test_adaptive_cycle_follows_real_cycle():
    e, _ = new_engine()
    real = 404.6
    run(e, [(T0 + k * real, cand(T0 + k * real)) for k in range(15)], T0 + 14 * real + 30)
    assert e.cycle == pytest.approx(real, abs=0.05)


def test_prediction_status_and_stale():
    e, _ = new_engine(stale_after_seconds=1000)
    run(e, [(T0, cand(T0)), (T0 + C, cand(T0 + C))], T0 + C + 30)
    assert e.prediction_status(T0 + C + 30) == (True, None)
    assert e.prediction_status(T0 + C + 2000) == (False, "stale")


def test_snapshot_restore_roundtrip():
    e, _ = new_engine()
    run(e, [(T0, cand(T0)), (T0 + C, cand(T0 + C))], T0 + C + 30)
    snap = e.snapshot()
    e2, _ = new_engine()
    e2.restore(snap, T0 + 5 * C)
    assert e2.state == "locked" and e2.missed == 0
    assert e2.ref_spectrum is not None
    e2.tick(T0 + 5 * C + 1, True, None)
    assert e2.missed == 0  # expectations that passed while down are not misses


def test_restore_ignores_snapshot_from_the_future():
    e, _ = new_engine()
    run(e, [(T0, cand(T0)), (T0 + C, cand(T0 + C))], T0 + C + 30)
    snap = e.snapshot()
    e2, _ = new_engine()
    e2.restore(snap, T0 - 3600)
    assert e2.state != "locked" and e2.last_sync_at is None and e2.last_anchor is None
    assert e2.ref_spectrum is not None


def test_manual_sync():
    e, evs = new_engine()
    e.manual_sync(T0, T0 + 5)
    assert e.state == "locked" and e.source == "manual"


IN_WINDOW = lambda t: True  # noqa: E731
NOISE = np.linspace(30, 1, 80).astype(np.float32)


def learned_engine(**kw):
    """An engine that already knows the anchor sound (as after a restart), not locked."""
    e, evs = new_engine(**kw)
    e._learn(SPEC)
    return e, evs


def test_quick_lock_from_one_stop_in_the_window():
    e, evs = learned_engine()
    run(e, [(T0, cand(T0))], T0 + 30, window=IN_WINDOW)
    assert e.state == "locked" and e.confirmed is False
    assert e.last_anchor == pytest.approx(T0)
    assert e.prediction_status(T0 + 30) == (True, None)
    run(e, [(T0 + C, cand(T0 + C))], T0 + C + 30, window=IN_WINDOW)
    assert e.confirmed is True and e.syncs_total == 2


def test_quick_lock_dropped_when_next_stop_missing():
    e, evs = learned_engine()
    run(e, [(T0, cand(T0))], T0 + C + 60, window=IN_WINDOW)
    assert e.state == "provisional" and not e.locked  # kept as a pending candidate
    assert e.last_sync_at is None and e.syncs_total == 0
    assert "unlock" in [x["type"] for x in evs]


def test_quick_lock_needs_learned_sound_length_and_window():
    # no learned sound yet (first install): needs a pair
    e, _ = new_engine()
    run(e, [(T0, cand(T0))], T0 + 30, window=IN_WINDOW)
    assert not e.locked
    # a different sound, or a short one
    e, _ = learned_engine()
    run(e, [(T0, cand(T0, spectrum=NOISE)), (T0 + 50, cand(T0 + 50, dur=2.5))], T0 + 80, window=IN_WINDOW)
    assert not e.locked
    # outside the Shabbat window only "always" allows it
    e, _ = learned_engine()
    run(e, [(T0, cand(T0))], T0 + 30, window=lambda t: False)
    assert not e.locked
    e, _ = learned_engine(quick_lock="always")
    run(e, [(T0, cand(T0))], T0 + 30, window=lambda t: False)
    assert e.locked
    e, _ = learned_engine(quick_lock="off")
    run(e, [(T0, cand(T0))], T0 + 30, window=IN_WINDOW)
    assert not e.locked
