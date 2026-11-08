// node --test tests/web/
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { nextArrival, phase, position } from "../../shabbat_elevator_tracker/web/model.js";

const fx = JSON.parse(readFileSync(fileURLToPath(new URL("../fixtures/timeline_reference.json", import.meta.url)), "utf8"));

test("phase is non-negative modulo the cycle", () => {
  assert.ok(Math.abs(phase(1000, 6000, 10) - 5) < 1e-9);
  assert.ok(Math.abs(phase(16000, 6000, 10) - 0) < 1e-9);
});

test("JS model matches the Python reference fixture", () => {
  const tl = fx.timeline;
  for (const q of fx.queries) {
    const p = position(tl, fx.floors, fx.anchor_time_ms, q.now_ms);
    assert.equal(p.kind, q.position.kind, `kind at ${q.now_ms}`);
    assert.ok(Math.abs(p.floor_pos - q.position.floor_pos) < 1e-3, `floor_pos at ${q.now_ms}`);
    for (const [f, v] of Object.entries(q.next)) {
      const n = nextArrival(tl, fx.anchor_time_ms, f, q.now_ms);
      assert.ok(Math.abs(n.arrive_at - v.arrive_at) <= 1, `${f} at ${q.now_ms}: ${n.arrive_at} vs ${v.arrive_at}`);
      assert.equal(n.here_until !== undefined, v.here);
    }
  }
});

test("floors not on the route return null", () => {
  assert.equal(nextArrival({ cycle_seconds: 100, stops: [], moves: [] }, 0, "X", 0), null);
});
