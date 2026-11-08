"""Diagnostics download."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ElevatorConfigEntry

TO_REDACT = {"url", "configuration_url"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ElevatorConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "transport": c.transport,
        "clock_offset_ms": round(c.clock_offset_ms),
        "state": c.data,
    }
