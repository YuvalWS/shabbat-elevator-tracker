"""Binary sensors: audio stream, running, Shabbat window, issur melacha."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElevatorConfigEntry
from .entity import ElevatorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ElevatorConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([AudioStreamSensor(c), RunningSensor(c), ShabbatWindowSensor(c), IssurMelachaSensor(c)])


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


class IssurMelachaSensor(_CalendarSensor):
    _attr_translation_key = "issur_melacha"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "issur_melacha")

    @property
    def is_on(self) -> bool:
        return bool(self.state_data["calendar"]["now"]["issur_melacha"])
