import asyncio
import json

import numpy as np
import pytest

from shabbat_elevator_tracker.clock import ManualClock
from shabbat_elevator_tracker.config import config_from_dict
from shabbat_elevator_tracker.detector import Candidate
from shabbat_elevator_tracker.server import create_app
from shabbat_elevator_tracker.service import Service
from shabbat_elevator_tracker.store import StateStore

T0 = 1794061800.0  # Sat 2026-11-07 ~13:10 local, inside the Shabbat window
SPEC = np.linspace(1, 30, 80).astype(np.float32)


def make_service(tmp_path, **server):
    cfg = config_from_dict({"storage": {"data_dir": str(tmp_path)}, "server": server,
                            "audio": {"source": "rtsp://user:secret@cam.local/x"}})
    clock = ManualClock(T0)
    svc = Service(cfg, clock=clock, store=StateStore(tmp_path))
    return svc, clock


def lock(svc, clock):
    C = svc.sync.cycle
    for k in range(2):
        c = Candidate(onset=T0 + k * C, duration=7, peak_db=35, mean_db=33, spectrum=SPEC)
        clock.set(T0 + k * C + 10)
        svc.sync.candidate(c, clock.now())
    svc.ingest.status.state = "connected"
    svc.tick()


@pytest.fixture
async def client(aiohttp_client, tmp_path):
    svc, clock = make_service(tmp_path, allow_manual_sync=True)
    cl = await aiohttp_client(create_app(svc))
    cl.svc, cl.clock = svc, clock
    return cl


async def test_state_waiting(client):
    r = await client.get("/api/v1/state")
    assert r.status == 200
    s = await r.json()
    assert s["api_version"] == 1
    assert s["prediction"] == {"valid": False, "reason": "no_sync", "degraded": None, "position": None,
                               "next_arrivals": None}
    assert s["building"]["floors"][0] == "G"
    assert len(s["timeline"]["stops"]) == 13
    assert s["calendar"]["now"]["issur_melacha"] is True
    assert "secret" not in json.dumps(s)


async def test_state_locked_and_floor(client):
    lock(client.svc, client.clock)
    s = await (await client.get("/api/v1/state")).json()
    assert s["sync"]["state"] == "locked"
    assert s["prediction"]["valid"] is True
    na = s["prediction"]["next_arrivals"]
    assert set(na) == set(s["building"]["floors"])
    assert na["3"]["arrive_at_ms"] > s["server_time_ms"]
    f = await (await client.get("/api/v1/floors/G")).json()
    assert f["valid"] and f["seconds_until"] > 0
    assert (await client.get("/api/v1/floors/99")).status == 404


async def test_degraded_when_audio_lost(client):
    lock(client.svc, client.clock)
    client.svc.ingest.status.state = "lost"
    s = await (await client.get("/api/v1/state")).json()
    assert s["prediction"]["valid"] and s["prediction"]["degraded"] == "audio_lost"


async def test_health_calendar_events_config(client):
    h = await (await client.get("/api/v1/health")).json()
    assert h["ok"] and h["api_version"] == 1
    cal = await (await client.get("/api/v1/calendar?days=14")).json()
    assert cal["enabled"] and len(cal["spans"]) >= 2
    lock(client.svc, client.clock)
    ev = await (await client.get("/api/v1/events?limit=5")).json()
    assert ev["events"] and ev["events"][0]["at_iso"]
    pub = await (await client.get("/api/v1/config/public")).json()
    assert "audio" not in pub and "latitude" not in pub["calendar"]


async def test_cors(client):
    r = await client.get("/api/v1/state", headers={"Origin": "https://example.com"})
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    r = await client.options("/api/v1/state", headers={"Origin": "https://example.com"})
    assert r.status == 204


async def test_manual_sync(client):
    r = await client.post("/api/v1/sync", json={"floor": "3", "event": "arrive", "at": int(T0 * 1000)})
    assert r.status == 200
    assert client.svc.sync.state == "locked" and client.svc.sync.source == "manual"
    r = await client.post("/api/v1/sync", json={"floor": "nope"})
    assert r.status == 404


async def test_debug_and_sync_hidden(aiohttp_client, tmp_path):
    svc, _ = make_service(tmp_path, expose_debug=False)
    cl = await aiohttp_client(create_app(svc))
    assert (await cl.get("/api/v1/debug")).status == 404
    assert (await cl.post("/api/v1/sync", json={})).status in (404, 405)
    s = await (await cl.get("/api/v1/state")).json()
    assert "level_dbfs" not in s["stream"] and "pending" not in s["sync"]


async def test_debug_endpoint(client):
    d = await (await client.get("/api/v1/debug")).json()
    assert "score_db" in d and "candidates" in d


async def test_web_client_served(client):
    r = await client.get("/")
    assert r.status == 200 and "<html" in await r.text()
    r = await client.get("/i18n/he.json")
    assert r.status == 200
    r = await client.get("/model.js")
    assert r.status == 200
    # web files must be revalidated, so a CDN never serves old scripts after an update
    assert r.headers["Cache-Control"] == "no-cache"
    # and Cloudflare must not cache them (it would replace the header with its own max-age)
    assert r.headers["CDN-Cache-Control"] == "no-store"
    r2 = await client.get("/model.js", headers={"If-Modified-Since": r.headers["Last-Modified"]})
    assert r2.status == 304


async def test_sse_pushes_state_on_sync(client):
    r = await client.get("/api/v1/stream")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")

    async def read_event():
        ev, data = None, None
        while True:
            line = (await r.content.readline()).decode().rstrip("\n")
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
            elif line == "" and ev:
                return ev, data

    ev, data = await asyncio.wait_for(read_event(), 5)
    assert ev == "state" and data["sync"]["state"] == "searching"
    lock(client.svc, client.clock)
    for _ in range(5):
        ev, data = await asyncio.wait_for(read_event(), 5)
        if data["sync"]["state"] == "locked":
            break
    assert data["sync"]["state"] == "locked"
    r.close()


async def test_state_persisted(tmp_path):
    svc, clock = make_service(tmp_path)
    lock(svc, clock)
    svc2 = Service(svc.cfg, clock=clock, store=StateStore(tmp_path))
    assert svc2.sync.state == "locked"
    assert svc2.store.instance_id == svc.store.instance_id
