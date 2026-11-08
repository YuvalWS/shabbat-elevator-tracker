// Timeline queries; mirror of shabbat_elevator_tracker/model.py.
// Times: epoch milliseconds. Offsets inside the timeline: seconds after the anchor arrival.

export function phase(nowMs, anchorMs, cycleS) {
  const c = cycleS * 1000;
  return ((((nowMs - anchorMs) % c) + c) % c) / 1000;
}

export function floorIndex(floors, floor) {
  return floors.indexOf(floor);
}

export function position(tl, floors, anchorMs, nowMs) {
  const ph = phase(nowMs, anchorMs, tl.cycle_seconds);
  for (const s of tl.stops) {
    if (s.arrive <= ph && ph < s.depart) {
      const doors = ph < s.door_open ? "opening" : ph < s.door_close ? "open" : "closing";
      // door openness 0..1 for the illustration
      let open = 1;
      if (ph < s.door_open) open = (ph - s.arrive) / Math.max(1e-6, s.door_open - s.arrive);
      else if (ph >= s.door_close) open = 1 - (ph - s.door_close) / Math.max(1e-6, s.depart - s.door_close);
      return { kind: "stop", floor: s.floor, floor_pos: floorIndex(floors, s.floor), doors, open, phase: ph };
    }
  }
  for (const m of tl.moves) {
    if (m.start <= ph && ph < m.end) {
      const prog = m.end > m.start ? (ph - m.start) / (m.end - m.start) : 0;
      const a = floorIndex(floors, m.from), b = floorIndex(floors, m.to);
      return { kind: "move", from: m.from, to: m.to, direction: m.direction,
               floor_pos: a + (b - a) * smooth(prog), progress: prog, open: 0, phase: ph };
    }
  }
  const s = tl.stops[0];
  return { kind: "stop", floor: s.floor, floor_pos: floorIndex(floors, s.floor), doors: "opening", open: 0, phase: 0 };
}

// gentle acceleration for drawing only (the Python model reports linear floor_pos)
function smooth(p) {
  return p;
}

export function nextArrival(tl, anchorMs, floor, nowMs) {
  const visits = tl.stops.filter((s) => s.floor === floor);
  if (!visits.length) return null;
  const ph = phase(nowMs, anchorMs, tl.cycle_seconds);
  const base = nowMs - ph * 1000;
  let best = null;
  let hereUntil = null;
  for (const s of visits) {
    if (s.arrive <= ph && ph < s.depart) hereUntil = base + s.depart * 1000;
    let t = base + s.arrive * 1000;
    if (t < nowMs) t += tl.cycle_seconds * 1000;
    if (best === null || t < best.arrive_at) best = { arrive_at: t, depart_at: t + (s.depart - s.arrive) * 1000 };
  }
  if (hereUntil !== null) best.here_until = hereUntil;
  return best;
}

export function routeOrder(tl) {
  return tl.stops.map((s) => s.floor);
}
