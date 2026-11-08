"""HTTP API (REST + Server-Sent Events) and the bundled web client (section 11)."""

from __future__ import annotations

import asyncio
import json
import logging
from importlib import resources
from pathlib import Path

from aiohttp import web

from .calendar import iso
from .service import Service

log = logging.getLogger(__name__)

SERVICE_KEY = web.AppKey("service", Service)


def _json(data, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False, default=str), status=status,
                        content_type="application/json", headers={"Cache-Control": "no-store"})


def _cors_middleware(origins: list[str]):
    allow_all = "*" in origins

    @web.middleware
    async def cors(request: web.Request, handler):
        origin = request.headers.get("Origin")
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = e
        if origin and (allow_all or origin in origins):
            resp.headers["Access-Control-Allow-Origin"] = "*" if allow_all else origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            if not allow_all:
                resp.headers["Vary"] = "Origin"
        if isinstance(resp, web.HTTPException) and not isinstance(resp, web.HTTPSuccessful):
            raise resp
        return resp

    return cors


def web_root(cfg) -> Path | None:
    wr = cfg.server.get("web_root")
    if wr == "":
        return None
    if wr:
        return Path(wr)
    return Path(str(resources.files("shabbat_elevator_tracker") / "web"))


def create_app(service: Service) -> web.Application:
    cfg = service.cfg
    app = web.Application(middlewares=[_cors_middleware(list(cfg.server["cors_origins"]))])
    app[SERVICE_KEY] = service
    r = app.router
    r.add_get("/api/v1/state", h_state)
    r.add_get("/api/v1/health", h_health)
    r.add_get("/api/v1/floors/{floor}", h_floor)
    r.add_get("/api/v1/stream", h_stream)
    r.add_get("/api/v1/calendar", h_calendar)
    r.add_get("/api/v1/events", h_events)
    r.add_get("/api/v1/config/public", h_config)
    if cfg.server["expose_debug"]:
        r.add_get("/api/v1/debug", h_debug)
    if cfg.server["allow_manual_sync"]:
        r.add_post("/api/v1/sync", h_sync)
    root = web_root(cfg)
    if root and root.exists():
        async def index(_req):
            return web.FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

        r.add_get("/", index)
        r.add_static("/", root, show_index=False, append_version=False)
    return app


async def h_state(request: web.Request) -> web.Response:
    return _json(request.app[SERVICE_KEY].state())


async def h_health(request: web.Request) -> web.Response:
    return _json(request.app[SERVICE_KEY].health())


async def h_floor(request: web.Request) -> web.Response:
    svc = request.app[SERVICE_KEY]
    try:
        return _json(svc.floor_state(request.match_info["floor"]))
    except KeyError:
        return _json({"error": "unknown_floor"}, 404)


async def h_calendar(request: web.Request) -> web.Response:
    svc = request.app[SERVICE_KEY]
    try:
        days = max(1, min(400, int(request.query.get("days", "30"))))
    except ValueError:
        return _json({"error": "bad_days"}, 400)
    now = svc.clock.now()
    if not svc.calendar.enabled:
        return _json({"enabled": False, "spans": [], "server_time": iso(now)})
    return _json({"enabled": True, "server_time": iso(now),
                  "spans": [s.as_dict() for s in svc.calendar.spans(now, days)]})


async def h_events(request: web.Request) -> web.Response:
    svc = request.app[SERVICE_KEY]
    try:
        limit = max(1, min(1000, int(request.query.get("limit", "100"))))
    except ValueError:
        return _json({"error": "bad_limit"}, 400)
    evs = svc.store.recent_events(limit)
    if not svc.debug:
        evs = [e for e in evs if e.get("type") != "candidate"]
        evs = [{k: v for k, v in e.items() if k != "error"} for e in evs]
    return _json({"events": [e | {"at_iso": iso(e.get("at"))} for e in evs]})


async def h_config(request: web.Request) -> web.Response:
    return _json(request.app[SERVICE_KEY].cfg.public())


async def h_debug(request: web.Request) -> web.Response:
    return _json(request.app[SERVICE_KEY].debug_info())


async def h_sync(request: web.Request) -> web.Response:
    svc = request.app[SERVICE_KEY]
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    floor = str(body.get("floor", svc.cfg.anchor["floor"]))
    event = body.get("event", "arrive")
    if event not in ("arrive", "depart"):
        return _json({"error": "event must be arrive or depart"}, 400)
    at = body.get("at")
    at_s = float(at) / 1000 if at is not None else svc.clock.now()
    try:
        anchor = svc.manual_sync(floor, event, at_s)
    except KeyError:
        return _json({"error": "floor_not_on_route"}, 404)
    return _json({"ok": True, "anchor_time": iso(anchor)})


async def h_stream(request: web.Request) -> web.StreamResponse:
    svc = request.app[SERVICE_KEY]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = svc.subscribe()

    async def send(event: str, data) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        await resp.write(f"event: {event}\ndata: {payload}\n\n".encode())

    try:
        await resp.write(b"retry: 5000\n\n")
        await send("state", svc.state())
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=15)
                while not q.empty():
                    q.get_nowait()
                await send("state", svc.state())
            except TimeoutError:
                await send("ping", {"server_time": iso(svc.clock.now())})
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        svc.unsubscribe(q)
    return resp


async def run_server(service: Service) -> None:
    cfg = service.cfg
    app = create_app(service)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.server["host"], int(cfg.server["port"]))
    await service.start()
    await site.start()
    log.info("listening on http://%s:%s", cfg.server["host"], cfg.server["port"])
    try:
        await asyncio.Event().wait()
    finally:
        await service.stop()
        await runner.cleanup()
