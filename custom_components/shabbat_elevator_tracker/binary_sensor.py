"""Binary sensors: audio stream, running, Shabbat window."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElevatorConfigEntry
from .const import DOMAIN
from .entity import ElevatorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ElevatorConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    _remove_old_entities(hass, c)
    async_add_entities([AudioStreamSensor(c), RunningSensor(c), ShabbatWindowSensor(c)])


def _remove_old_entities(hass: HomeAssistant, coordinator) -> None:
    """Drop entities earlier versions created, so they don't linger as 'not provided'."""
    reg = er.async_get(hass)
    iid = coordinator.data["instance_id"]
    for key in ("issur_melacha",):  # removed in 0.1.1: the calendar integrations cover it
        entity_id = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{iid}_{key}")
        if entity_id:
            reg.async_remove(entity_id)


class AudioStreamSensor(ElevatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "audio_stream"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "audio_stream")

    @property
    def is_on(self) -> bool:
        return self.state_data["stream"]["state"] == "connected"


class RunningSensor(ElevatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "running"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "running")

    @property
    def is_on(self) -> bool:
        return self.state_data["sync"]["state"] == "locked"


class _CalendarSensor(ElevatorEntity, BinarySensorEntity):
    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.get("calendar", {}).get("enabled"))


class ShabbatWindowSensor(_CalendarSensor):
    _attr_translation_key = "shabbat_window"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "shabbat_window")

    @property
    def is_on(self) -> bool:
        return bool(self.state_data["calendar"]["now"]["in_elevator_window"])

    @property
    def extra_state_attributes(self) -> dict:
        cal = self.state_data["calendar"]
        span = cal.get("current_span") or cal.get("next_span") or {}
        return {"window_start": span.get("window_start"), "window_end": span.get("window_end"),
                "candle_lighting": span.get("start"), "havdalah": span.get("end")}

