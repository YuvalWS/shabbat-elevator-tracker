"""Regenerate tests/fixtures/timeline_reference.json (python tests/make_fixtures.py)."""

import json
from pathlib import Path

from shabbat_elevator_tracker.config import config_from_dict
from shabbat_elevator_tracker.model import next_arrival, position, timeline_from_config


def main() -> None:
    tl = timeline_from_config(config_from_dict({}))
    # anchor rounded to whole ms so JS and Python start from the same number
    anchor_ms = 1794061800123
    a = anchor_ms / 1000
    queries = []
    for k in range(0, 900, 7):
        now_ms = anchor_ms + int(k * 997.3)
        now = now_ms / 1000
        p = position(tl, a, now)
        nxt = {}
        for f in tl.floors:
            n = next_arrival(tl, a, f, now)
            if n:
                nxt[f] = {"arrive_at": round(n["arrive_at"] * 1000, 3), "here": "here_until" in n}
        queries.append({"now_ms": now_ms, "position": {"kind": p["kind"], "floor_pos": p["floor_pos"]}, "next": nxt})
    out = {"floors": tl.floors, "anchor_time_ms": anchor_ms, "timeline": tl.as_dict(), "queries": queries}
    path = Path(__file__).parent / "fixtures" / "timeline_reference.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
