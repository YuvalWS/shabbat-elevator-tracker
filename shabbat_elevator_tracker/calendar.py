"""Offline Shabbat / Yom Tov calendar (section 10), built on hdate.

Rest days are Saturdays and Yom Tov days (hdate, Israel or diaspora). Consecutive rest
days merge into one span from candle lighting on the eve to havdalah after the last day.
Sunset is only used (inside hdate) as the base of candle lighting and of havdalah in
minutes mode.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from hdate import HDateInfo, Location, Zmanim
from hdate.gematria import hebrew_number
from hdate.translator import context_language

DISPLAY_HOLIDAY_TYPES = {
    "YOM_TOV", "EREV_YOM_TOV", "HOL_HAMOED", "MELACHA_PERMITTED_HOLIDAY", "FAST_DAY",
    "ROSH_CHODESH", "MINOR_HOLIDAY", "MODERN_HOLIDAY",
}


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Span:
    start: float  # candle lighting (epoch s)
    end: float  # havdalah (epoch s)
    window_start: float
    window_end: float
    days: list[dict]

    def as_dict(self) -> dict:
        return {
            "start": iso(self.start),
            "end": iso(self.end),
            "start_ms": int(self.start * 1000),
            "end_ms": int(self.end * 1000),
            "window_start": iso(self.window_start),
            "window_end": iso(self.window_end),
            "days": self.days,
        }


class ShabbatCalendar:
    def __init__(
        self,
        latitude: float,
        longitude: float,
        timezone: str = "Asia/Jerusalem",
        elevation_m: float = 0.0,
        israel: bool = True,
        candle_lighting_minutes: int = 18,
        havdalah_minutes: int = 0,
        before_start_minutes: float = 10,
        after_end_minutes: float = 30,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.tz = ZoneInfo(timezone)
        self.diaspora = not israel
        self.location = Location(
            name="site", latitude=float(latitude), longitude=float(longitude),
            timezone=timezone, altitude=float(elevation_m), diaspora=self.diaspora,
        )
        self.candle = int(candle_lighting_minutes)
        self.havdalah = int(havdalah_minutes)
        self.before = float(before_start_minutes) * 60
        self.after = float(after_end_minutes) * 60
        self._spans: list[Span] = []
        self._starts: list[float] = []
        self._range: tuple[dt.date, dt.date] | None = None

    @classmethod
    def from_config(cls, cfg) -> ShabbatCalendar:
        c = cfg.calendar
        w = c.get("elevator_window") or {}
        return cls(
            latitude=c["latitude"], longitude=c["longitude"], timezone=c["timezone"],
            elevation_m=c.get("elevation_m", 0), israel=c["israel"],
            candle_lighting_minutes=c["candle_lighting_minutes"],
            havdalah_minutes=c.get("havdalah_minutes", 0),
            before_start_minutes=w.get("before_start_minutes", 10),
            after_end_minutes=w.get("after_end_minutes", 30),
            enabled=bool(c.get("enabled", True)),
        )

    # ------------------------------------------------------------ primitives
    def _zmanim(self, d: dt.date) -> Zmanim:
        return Zmanim(date=d, location=self.location, candle_lighting_offset=self.candle,
                      havdalah_offset=self.havdalah)

    def is_rest_day(self, d: dt.date) -> bool:
        return d.weekday() == 5 or HDateInfo(d, self.diaspora).is_yom_tov

    def day_ids(self, d: dt.date) -> list[str]:
        info = HDateInfo(d, self.diaspora)
        ids = ["shabbat"] if d.weekday() == 5 else []
        ids += [h.name for h in info.holidays if h.type.name in DISPLAY_HOLIDAY_TYPES]
        return ids

    def hebrew_date(self, d: dt.date) -> dict:
        h = HDateInfo(d, self.diaspora).hdate
        # Hebrew numerals for the Hebrew UI, from hdate (its language setting is context-local)
        token = context_language.set("he")
        try:
            day_he, year_he = hebrew_number(h.day), hebrew_number(h.year % 1000)
        finally:
            context_language.reset(token)
        return {"day": h.day, "month_id": h.month.name.lower(), "year": h.year,
                "day_he": day_he, "year_he": year_he}

    def _hebrew_day(self, t: float) -> dt.date:
        """The Hebrew day starts at sunset: after sunset, report tomorrow's Hebrew date."""
        now = dt.datetime.fromtimestamp(t, self.tz)
        sunset = self._zmanim(now.date()).shkia.local
        return now.date() + dt.timedelta(days=1) if now >= sunset else now.date()

    def parsha_ids(self, d: dt.date) -> list[str]:
        """Parsha of the Shabbat on or after d (empty if that Shabbat is Yom Tov)."""
        sat = d + dt.timedelta(days=(5 - d.weekday()) % 7)
        p = HDateInfo(sat, self.diaspora).parasha_obj
        return [] if p.value == 0 else [p.name.lower()]

    # ------------------------------------------------------------ spans
    def _build(self, first: dt.date, last: dt.date) -> None:
        spans: list[Span] = []
        d = first
        while d <= last:
            if self.is_rest_day(d):
                start_day = d
                while self.is_rest_day(d + dt.timedelta(days=1)):
                    d += dt.timedelta(days=1)
                end_day = d
                eve = start_day - dt.timedelta(days=1)
                cl = self._zmanim(eve).candle_lighting
                hv = self._zmanim(end_day).havdalah
                if cl is not None and hv is not None:
                    s, e = cl.timestamp(), hv.timestamp()
                    days = []
                    x = start_day
                    while x <= end_day:
                        days.append({"date": x.isoformat(), "ids": self.day_ids(x)})
                        x += dt.timedelta(days=1)
                    spans.append(Span(s, e, s - self.before, e + self.after, days))
            d += dt.timedelta(days=1)
        self._spans = spans
        self._starts = [s.window_start for s in spans]
        self._range = (first, last)

    def _ensure(self, t: float, days_ahead: int = 40) -> None:
        today = dt.datetime.fromtimestamp(t, self.tz).date()
        need_first, need_last = today - dt.timedelta(days=3), today + dt.timedelta(days=days_ahead)
        if self._range is None or need_first < self._range[0] or need_last > self._range[1]:
            self._build(today - dt.timedelta(days=10), today + dt.timedelta(days=days_ahead + 30))

    def spans(self, t: float, days: int = 30) -> list[Span]:
        self._ensure(t, days)
        horizon = t + days * 86400
        return [s for s in self._spans if s.window_end >= t and s.window_start <= horizon]

    def current_span(self, t: float) -> Span | None:
        self._ensure(t)
        for s in self._spans:
            if s.window_start <= t <= s.window_end:
                return s
        return None

    def next_span(self, t: float) -> Span | None:
        self._ensure(t)
        i = bisect.bisect_right(self._starts, t)
        return self._spans[i] if i < len(self._spans) else None

    def in_window(self, t: float) -> bool | None:
        """True inside an expected elevator window; None when the calendar is disabled."""
        if not self.enabled:
            return None
        return self.current_span(t) is not None

    def issur_melacha(self, t: float) -> bool:
        now = dt.datetime.fromtimestamp(t, self.tz)
        return bool(self._zmanim(now.date()).issur_melacha_in_effect(now))

    def in_rest_span(self, t: float) -> bool:
        s = self.current_span(t)
        return s is not None and s.start <= t < s.end

    def state(self, t: float) -> dict:
        if not self.enabled:
            return {"enabled": False}
        cur = self.current_span(t)
        nxt = self.next_span(t)
        today = dt.datetime.fromtimestamp(t, self.tz).date()
        # the parsha of the Shabbat being shown: inside a span, that span's; otherwise the
        # next span's (after havdalah on Saturday this is already next week's)
        shown = cur or nxt
        pday = today
        if shown is not None:
            sats = [dt.date.fromisoformat(d["date"]) for d in shown.days
                    if dt.date.fromisoformat(d["date"]).weekday() == 5]
            pday = sats[0] if sats else dt.date.fromisoformat(shown.days[0]["date"])
        return {
            "enabled": True,
            "now": {
                "in_rest_span": cur is not None and cur.start <= t < cur.end,
                "issur_melacha": self.issur_melacha(t),
                "in_elevator_window": cur is not None,
            },
            "current_span": cur.as_dict() if cur else None,
            "next_span": nxt.as_dict() if nxt else None,
            "parsha_ids": self.parsha_ids(pday) if pday.weekday() == 5 else [],
            "today_ids": self.day_ids(today),
            "hebrew_date": self.hebrew_date(self._hebrew_day(t)),
            "timezone": str(self.tz),
        }
