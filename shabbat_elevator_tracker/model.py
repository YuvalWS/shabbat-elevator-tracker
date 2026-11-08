"""Timeline model of one elevator cycle (pure functions).

Mirrored in web/model.js; both are checked against tests/fixtures/timeline_reference.json.
Offsets are seconds after the anchor arrival (offset 0). Absolute times are epoch seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Stop:
    floor: str
    visit: int
    arrive: float
    door_open: float
    door_close: float
    depart: float

    def as_dict(self) -> dict:
        return {
            "floor": self.floor,
            "visit": self.visit,
            "arrive": round(self.arrive, 3),
            "door_open": round(self.door_open, 3),
            "door_close": round(self.door_close, 3),
            "depart": round(self.depart, 3),
        }


@dataclass
class Move:
    from_floor: str
    to_floor: str
    direction: str
    start: float
    end: float

    def as_dict(self) -> dict:
        return {
            "from": self.from_floor,
            "to": self.to_floor,
            "direction": self.direction,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
        }


@dataclass
class Timeline:
    floors: list[str]
    cycle: float
    stops: list[Stop]
    moves: list[Move]
    scale: float
    warnings: list[str] = field(default_factory=list)

    def pos(self, floor: str) -> int:
        return self.floors.index(floor)

    def as_dict(self) -> dict:
        return {
            "cycle_seconds": round(self.cycle, 3),
            "stops": [s.as_dict() for s in self.stops],
            "moves": [m.as_dict() for m in self.moves],
        }


def build_timeline(
    floors: list[str],
    route: list[str],
    anchor_floor: str,
    cycle_seconds: float,
    dwell_seconds: float,
    dwell_overrides: dict[str, float] | None = None,
    door_transition_seconds: float = 2.5,
    leg_overhead_seconds: float = 12.1,
    per_floor_seconds: float = 2.8,
    anchor_route_index: int | None = None,
) -> Timeline:
    dwell_overrides = dwell_overrides or {}
    idx = anchor_route_index if anchor_route_index is not None else route.index(anchor_floor)
    r = route[idx:] + route[:idx]
    n = len(r)
    pos = {f: i for i, f in enumerate(floors)}
    dwell = [float(dwell_overrides.get(f, dwell_seconds)) for f in r]
    travel = [
        leg_overhead_seconds + per_floor_seconds * abs(pos[r[(i + 1) % n]] - pos[r[i]]) for i in range(n)
    ]
    raw = sum(dwell) + sum(travel)
    scale = cycle_seconds / raw
    warnings = []
    if abs(scale - 1) > 0.10:
        warnings.append(
            f"route model sums to {raw:.1f} s but cycle is {cycle_seconds:.1f} s "
            f"(scale {scale:.3f}); the config probably does not describe this elevator"
        )
    stops: list[Stop] = []
    moves: list[Move] = []
    t = 0.0
    visits: dict[str, int] = {}
    for i, f in enumerate(r):
        v = visits.get(f, 0)
        visits[f] = v + 1
        d = dwell[i] * scale
        tr = door_transition_seconds * scale
        stops.append(Stop(f, v, t, t + tr, t + d - tr, t + d))
        t += d
        nxt = r[(i + 1) % n]
        end = t + travel[i] * scale
        if i == n - 1:
            end = cycle_seconds  # exact closure
        direction = "up" if pos[nxt] > pos[f] else "down"
        moves.append(Move(f, nxt, direction, t, end))
        t = end
    return Timeline(list(floors), float(cycle_seconds), stops, moves, scale, warnings)


def timeline_from_config(cfg, cycle_seconds: float | None = None) -> Timeline:
    e = cfg.elevator
    return build_timeline(
        floors=cfg.building["floors"],
        route=e["route"],
        anchor_floor=cfg.anchor["floor"],
        cycle_seconds=float(cycle_seconds or e["cycle_seconds"]),
        dwell_seconds=float(e["dwell_seconds"]),
        dwell_overrides=e["dwell_overrides"],
        door_transition_seconds=float(e["door_transition_seconds"]),
        leg_overhead_seconds=float(e["travel"]["leg_overhead_seconds"]),
        per_floor_seconds=float(e["travel"]["per_floor_seconds"]),
        anchor_route_index=cfg.anchor.get("route_index"),
    )


def scaled(tl: Timeline, cycle: float) -> Timeline:
    """Same timeline stretched to another cycle length (adaptive cycle)."""
    k = cycle / tl.cycle
    return Timeline(
        tl.floors,
        cycle,
        [Stop(s.floor, s.visit, s.arrive * k, s.door_open * k, s.door_close * k, s.depart * k) for s in tl.stops],
        [Move(m.from_floor, m.to_floor, m.direction, m.start * k, m.end * k) for m in tl.moves],
        tl.scale * k,
        tl.warnings,
    )


def phase(now: float, anchor_time: float, cycle: float) -> float:
    return (now - anchor_time) % cycle


def position(tl: Timeline, anchor_time: float, now: float) -> dict:
    ph = phase(now, anchor_time, tl.cycle)
    for s in tl.stops:
        if s.arrive <= ph < s.depart:
            if ph < s.door_open:
                doors = "opening"
            elif ph < s.door_close:
                doors = "open"
            else:
                doors = "closing"
            return {
                "kind": "stop",
                "floor": s.floor,
                "floor_pos": float(tl.pos(s.floor)),
                "doors": doors,
                "phase": round(ph, 3),
            }
    for m in tl.moves:
        if m.start <= ph < m.end:
            prog = (ph - m.start) / (m.end - m.start) if m.end > m.start else 0.0
            a, b = tl.pos(m.from_floor), tl.pos(m.to_floor)
            return {
                "kind": "move",
                "from": m.from_floor,
                "to": m.to_floor,
                "direction": m.direction,
                "floor_pos": round(a + (b - a) * prog, 4),
                "phase": round(ph, 3),
            }
    # numeric edge: phase == cycle
    s = tl.stops[0]
    return {"kind": "stop", "floor": s.floor, "floor_pos": float(tl.pos(s.floor)), "doors": "opening", "phase": 0.0}


def next_arrival(tl: Timeline, anchor_time: float, floor: str, now: float) -> dict | None:
    """Next arrival at `floor` at or after now. If the car is there now, also `here_until`."""
    visits = [s for s in tl.stops if s.floor == floor]
    if not visits:
        return None
    ph = phase(now, anchor_time, tl.cycle)
    base = now - ph  # start of the current cycle
    here_until = None
    best = None
    for s in visits:
        if s.arrive <= ph < s.depart:
            here_until = base + s.depart
        t = base + s.arrive
        if t < now:
            t += tl.cycle
        if best is None or t < best[0]:
            best = (t, t + (s.depart - s.arrive))
    out = {"arrive_at": best[0], "depart_at": best[1]}
    if here_until is not None:
        out["here_until"] = here_until
    return out


def next_arrivals(tl: Timeline, anchor_time: float, now: float) -> dict[str, dict | None]:
    return {f: next_arrival(tl, anchor_time, f, now) for f in tl.floors}


def is_finite(x: float) -> bool:
    return x is not None and math.isfinite(x)
