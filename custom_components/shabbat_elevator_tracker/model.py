"""Timeline queries (same rules as the server's model.py and the web client's model.js).

The server sends the anchor time, the cycle and the route timeline; next arrivals are
computed locally so they stay correct between pushes.
"""

from __future__ import annotations


def phase(now_ms: float, anchor_ms: float, cycle_s: float) -> float:
    c = cycle_s * 1000
    return ((now_ms - anchor_ms) % c) / 1000


def next_arrival(timeline: dict, floor: str, now_ms: float) -> dict | None:
    anchor = timeline.get("anchor_time_ms")
    cycle = timeline.get("cycle_seconds")
    if anchor is None or not cycle:
        return None
    visits = [s for s in timeline.get("stops", []) if s["floor"] == floor]
    if not visits:
        return None
    ph = phase(now_ms, anchor, cycle)
    base = now_ms - ph * 1000
    best = None
    here_until = None
    for s in visits:
        if s["arrive"] <= ph < s["depart"]:
            here_until = base + s["depart"] * 1000
        t = base + s["arrive"] * 1000
        if t < now_ms:
            t += cycle * 1000
        if best is None or t < best["arrive_at"]:
            best = {"arrive_at": t, "depart_at": t + (s["depart"] - s["arrive"]) * 1000}
    if here_until is not None:
        best["here_until"] = here_until
    return best


def position(timeline: dict, floors: list[str], now_ms: float) -> dict | None:
    anchor = timeline.get("anchor_time_ms")
    cycle = timeline.get("cycle_seconds")
    if anchor is None or not cycle:
        return None
    ph = phase(now_ms, anchor, cycle)
    for s in timeline.get("stops", []):
        if s["arrive"] <= ph < s["depart"]:
            return {"kind": "stop", "floor": s["floor"]}
    for m in timeline.get("moves", []):
        if m["start"] <= ph < m["end"]:
            return {"kind": "move", "from": m["from"], "to": m["to"], "direction": m["direction"]}
    return None
