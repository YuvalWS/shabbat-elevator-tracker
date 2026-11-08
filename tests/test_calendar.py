import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from shabbat_elevator_tracker.calendar import ShabbatCalendar

TZ = ZoneInfo("Asia/Jerusalem")


def cal(**kw):
    args = dict(latitude=32.0781, longitude=34.8503, timezone="Asia/Jerusalem", elevation_m=50)
    args.update(kw)
    return ShabbatCalendar(**args)


def at(y, mo, d, h=12, mi=0, tz=TZ):
    return dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def local(t, tz=TZ):
    return dt.datetime.fromtimestamp(t, tz)


def test_reference_shabbat_givat_shmuel():
    c = cal()
    s = c.current_span(at(2026, 11, 7, 12))
    assert local(s.start).strftime("%a %H:%M") == "Fri 16:30"
    assert local(s.end).strftime("%a %H:%M") == "Sat 17:25"
    st = c.state(at(2026, 11, 7, 12))
    assert st["parsha_ids"] == ["chayei_sara"]
    assert st["now"]["issur_melacha"] and st["now"]["in_rest_span"] and st["now"]["in_elevator_window"]
    assert st["hebrew_date"]["month_id"] == "marcheshvan"


def test_window_padding_and_idle():
    c = cal()
    s = c.current_span(at(2026, 11, 7, 12))
    assert s.window_start == pytest.approx(s.start - 600)
    assert s.window_end == pytest.approx(s.end + 1800)
    assert c.in_window(at(2026, 11, 6, 16, 25)) is True  # 5 min before candle lighting
    assert c.in_window(at(2026, 11, 6, 15, 0)) is False
    assert c.in_window(at(2026, 11, 7, 17, 50)) is True
    assert c.in_window(at(2026, 11, 7, 18, 30)) is False
    assert cal(enabled=False).in_window(at(2026, 11, 7, 12)) is None


def test_after_havdalah_shows_next_week():
    st = cal().state(at(2026, 11, 7, 23))
    assert st["current_span"] is None
    assert st["next_span"]["start"].startswith("2026-11-13")
    assert st["parsha_ids"] == ["toldot"]
    assert not st["now"]["issur_melacha"]


def test_rosh_hashana_merges_with_shabbat():
    # Rosh Hashana 5787: Sat-Sun 12-13 Sep 2026, preceded by Shabbat? 12 Sep 2026 is Saturday
    c = cal()
    s = c.current_span(at(2026, 9, 12, 12))
    days = [d["date"] for d in s.days]
    assert days == ["2026-09-12", "2026-09-13"]
    assert "rosh_hashana_i" in s.days[0]["ids"] and "shabbat" in s.days[0]["ids"]
    assert local(s.start).date() == dt.date(2026, 9, 11)
    assert local(s.end).date() == dt.date(2026, 9, 13)


def test_israel_vs_diaspora_pesach_and_shavuot():
    # Pesach 5787 starts Thu 22 Apr 2027 (15 Nisan)
    il, di = cal(), cal(israel=False, latitude=40.71, longitude=-74.0, timezone="America/New_York")
    s_il = il.current_span(at(2027, 4, 22, 12))
    assert [d["date"] for d in s_il.days] == ["2027-04-22"]
    ny = ZoneInfo("America/New_York")
    s_di = di.current_span(at(2027, 4, 22, 12, tz=ny))
    # diaspora: Thu, Fri (2nd day) and Shabbat merge into one span
    assert [d["date"] for d in s_di.days] == ["2027-04-22", "2027-04-23", "2027-04-24"]
    # Shavuot 5787: Israel one day, diaspora two
    sv_il = il.current_span(at(2027, 6, 11, 12))
    assert sv_il is not None and len(sv_il.days) >= 1
    sv_di = di.current_span(at(2027, 6, 11, 12, tz=ny))
    assert len(sv_di.days) >= 2


def test_candle_lighting_offset_and_havdalah_minutes():
    a = cal().current_span(at(2026, 11, 7, 12))
    b = cal(candle_lighting_minutes=40, havdalah_minutes=72).current_span(at(2026, 11, 7, 12))
    assert a.start - b.start == pytest.approx(22 * 60, abs=60)
    assert b.end > a.end


def test_dst_weekend():
    # Israel DST ends Sun 25 Oct 2026; the Shabbat before still in summer time
    c = cal()
    s = c.current_span(at(2026, 10, 24, 12))
    assert local(s.start).hour == 17 and local(s.end).hour == 18
    s2 = c.current_span(at(2026, 10, 31, 12))
    assert local(s2.start).hour == 16


def test_spans_listing():
    spans = cal().spans(at(2026, 11, 8, 12), days=30)
    assert len(spans) >= 4
    assert all(s.start < s.end for s in spans)


def test_hebrew_date_changes_at_sunset():
    c = cal()
    assert c.state(at(2026, 11, 7, 12))["hebrew_date"]["day"] == 27
    assert c.state(at(2026, 11, 7, 18))["hebrew_date"]["day"] == 28
