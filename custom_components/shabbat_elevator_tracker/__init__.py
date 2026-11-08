"""Shabbat Elevator Tracker integration: next Shabbat-elevator arrivals as sensors."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_URL
from .coordinator import ElevatorCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type ElevatorConfigEntry = ConfigEntry[ElevatorCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ElevatorConfigEntry) -> bool:
    coordinator = ElevatorCoordinator(hass, entry, entry.data[CONF_URL])
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_push()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElevatorConfigEntry) -> bool:
    await entry.runtime_data.stop_push()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: ElevatorConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
