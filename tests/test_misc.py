import datetime as dt
import json
import re
import struct

from hdate.holidays import HOLIDAYS
from hdate.parasha import Parasha

from shabbat_elevator_tracker.calendar import DISPLAY_HOLIDAY_TYPES
from shabbat_elevator_tracker.recorder import (
    HEADER_BYTES,
    finalize,
    in_hours,
    parse_hours,
    wav_header,
)

I18N = "shabbat_elevator_tracker/web/i18n"


def load(root, lang):
    return json.loads((root / I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_strings_files_have_identical_keys(root):
    en, he, fr = (load(root, x) for x in ("en", "he", "fr"))
    assert set(en) == set(he) == set(fr)
    assert he["_meta.dir"] == "rtl" and en["_meta.dir"] == "ltr"


def test_strings_cover_calendar_ids(root):
    en = load(root, "en")
    for p in Parasha:
        if p.value:
            assert f"parsha.{p.name.lower()}" in en, p
    for h in HOLIDAYS:
        if h.type.name in DISPLAY_HOLIDAY_TYPES:
            assert f"holiday.{h.name}" in en, h.name
    for m in ("tishrei", "marcheshvan", "kislev", "tevet", "shvat", "adar", "adar_i", "adar_ii",
              "nisan", "iyyar", "sivan", "tammuz", "av", "elul"):
        assert f"month.{m}" in en


def test_placeholders_match_across_languages(root):
    en, he, fr = (load(root, x) for x in ("en", "he", "fr"))
    ph = re.compile(r"\{(\w+)\}")
    for k, v in en.items():
        assert set(ph.findall(v)) == set(ph.findall(he[k])) == set(ph.findall(fr[k])), k


HEBREW = re.compile("[" + chr(0x0590) + "-" + chr(0x05FF) + "]")


def test_no_hebrew_outside_strings_files(root):
    allowed = {f"{I18N}/he.json"}
    bad = []
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or rel.startswith((".git/", "test-data/", "data/")) or "/__pycache__/" in rel:
            continue
        if rel in allowed or rel.startswith("custom_components/shabbat_elevator_tracker/translations/he"):
            continue
        if p.suffix not in (".py", ".js", ".mjs", ".json", ".md", ".yaml", ".yml", ".html", ".css", ".sh", ".toml"):
            continue
        if HEBREW.search(p.read_text(encoding="utf-8", errors="ignore")):
            bad.append(rel)
    assert not bad, bad


def test_parse_and_in_hours():
    w = parse_hours("22:30-06:30")
    assert in_hours(w, dt.datetime(2026, 1, 1, 23, 0))
    assert in_hours(w, dt.datetime(2026, 1, 1, 3, 0))
    assert not in_hours(w, dt.datetime(2026, 1, 1, 12, 0))
    assert in_hours(parse_hours("08:00-09:00"), dt.datetime(2026, 1, 1, 8, 30))
    assert in_hours(None, dt.datetime(2026, 1, 1, 12, 0))


def test_finalize_repairs_part_file(tmp_path):
    part = tmp_path / "1794005584141.wav.part"
    data = b"\x01\x00" * 1000 + b"\x05"  # odd trailing byte from a crash
    part.write_bytes(wav_header(16000, 0) + data)
    final = finalize(part)
    assert final.name == "1794005584141.wav"
    raw = final.read_bytes()
    assert len(raw) == HEADER_BYTES + 2000
    assert struct.unpack("<I", raw[40:44])[0] == 2000
