"""Data coordinator: Server-Sent Events push with polling fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import API_VERSION, DOMAIN, EVENT_SYNC, POLL_INTERVAL_SECONDS, SSE_RETRY_MAX_SECONDS

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """The server could not be reached or answered badly."""


async def fetch_json(session: aiohttp.ClientSession, url: str, timeout: float = 10) -> dict:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise ApiError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise ApiError(str(err)) from err


class ElevatorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the latest /api/v1/state; pushed by SSE, polled while SSE is down."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, url: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.base = url.rstrip("/")
        self.session = async_get_clientsession(hass)
        self.transport = "poll"
        self.clock_offset_ms = 0.0
        self._sse_task: asyncio.Task | None = None
        self._last_sync_ms: int | None = None

    def server_now_ms(self) -> float:
        return time.time() * 1000 + self.clock_offset_ms

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await fetch_json(self.session, f"{self.base}/api/v1/state")
        except ApiError as err:
            raise UpdateFailed(f"cannot reach {self.base}: {err}") from err
        return self._process(data)

    def _process(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("api_version") != API_VERSION:
            raise UpdateFailed(f"unsupported server API version {data.get('api_version')}")
        if data.get("server_time_ms"):
            self.clock_offset_ms = data["server_time_ms"] - time.time() * 1000
        last = (data.get("sync") or {}).get("last_sync_at_ms")
        if last and last != self._last_sync_ms:
            if self._last_sync_ms is not None:
                self.hass.bus.async_fire(EVENT_SYNC, {
                    "instance_id": data.get("instance_id"),
                    "anchor_time": (data.get("sync") or {}).get("last_sync_at"),
                    "cycle_seconds": (data.get("cycle") or {}).get("seconds"),
                })
            self._last_sync_ms = last
        return data

    # ------------------------------------------------------------------ SSE
    def start_push(self) -> None:
        if self._sse_task is None:
            self._sse_task = self.config_entry.async_create_background_task(
                self.hass, self._sse_loop(), f"{DOMAIN} SSE {self.base}"
            )

    async def stop_push(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            self._sse_task = None

    async def _sse_loop(self) -> None:
        backoff = 5
        while True:
            try:
                await self._sse_once()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("SSE disconnected: %s", err)
            self.transport = "poll"
            self.update_interval = timedelta(seconds=POLL_INTERVAL_SECONDS)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, SSE_RETRY_MAX_SECONDS)

    async def _sse_once(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=45)
        async with self.session.get(f"{self.base}/api/v1/stream", timeout=timeout,
                                    headers={"Accept": "text/event-stream"}) as resp:
            if resp.status != 200:
                raise ApiError(f"HTTP {resp.status}")
            self.transport = "sse"
            self.update_interval = None  # push only while the stream is up
            event, data_lines = None, []
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "":
                    if event == "state" and data_lines:
                        try:
                            self.async_set_updated_data(self._process(json.loads("\n".join(data_lines))))
                        except (ValueError, UpdateFailed) as err:
                            _LOGGER.warning("bad state event: %s", err)
                    event, data_lines = None, []
        raise ApiError("stream closed")
