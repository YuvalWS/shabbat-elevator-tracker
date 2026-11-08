"""Sensors: next arrival per floor, last sync, sync state, cycle length, car position."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval

from . import ElevatorConfigEntry
from .const import CONF_EXTRA_FLOORS, CONF_HOME_FLOOR, SYNC_STATES
from .entity import ElevatorEntity
from .model import next_arrival, position


def _dt(ms: float | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


async def async_setup_entry(hass: HomeAssistant, entry: ElevatorConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    home = entry.options.get(CONF_HOME_FLOOR)
    tracked = {home, *entry.options.get(CONF_EXTRA_FLOORS, [])}
    entities: list[SensorEntity] = []
    if home:
        entities.append(NextArrivalSensor(coordinator, home, home=True, enabled=True))
    for f in coordinator.data["building"]["floors"]:
        entities.append(NextArrivalSensor(coordinator, f, home=False, enabled=f in tracked))
    entities += [LastSyncSensor(coordinator), SyncStateSensor(coordinator), CycleSensor(coordinator),
                 CarPositionSensor(coordinator)]
    async_add_entities(entities)


class NextArrivalSensor(ElevatorEntity, SensorEntity):
    """Next arrival at a floor. Recomputed locally when the elevator leaves the floor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, floor: str, home: bool, enabled: bool) -> None:
        super().__init__(coordinator, "next_arrival" if home else f"next_arrival_floor_{floor}")
        self.floor = floor
        self.home = home
        if home:
            self._attr_translation_key = "next_arrival"
        else:
            self._attr_translation_key = "next_arrival_floor"
            self._attr_translation_placeholders = {"floor": floor}
        self._attr_entity_registry_enabled_default = enabled
        self._unsub: CALLBACK_TYPE | None = None
        self._value: dict | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._recompute()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._recompute()
        super()._handle_coordinator_update()

    def _recompute(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        d = self.coordinator.data
        self._value = None
        if d and d["prediction"]["valid"]:
            self._value = next_arrival(d["timeline"], self.floor, self.coordinator.server_now_ms())
        if self._value:
            # when the car leaves this floor, the next arrival changes: recompute then
            when = self._value.get("here_until") or self._value["depart_at"]
            local = when - self.coordinator.clock_offset_ms
            self._unsub = async_track_point_in_utc_time(
                self.hass, self._scheduled, datetime.fromtimestamp(local / 1000 + 0.2, UTC))

    @callback
    def _scheduled(self, _now: datetime) -> None:
        self._unsub = None
        self._recompute()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self._value is not None

    @property
    def native_value(self) -> datetime | None:
        return _dt(self._value["arrive_at"]) if self._value else None

    @property
    def extra_state_attributes(self) -> dict:
        v = self._value or {}
        return {"floor": self.floor, "depart_at": _dt(v.get("depart_at")),
                "here_now": "here_until" in v, "here_until": _dt(v.get("here_until"))}


class LastSyncSensor(ElevatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_sync"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "last_sync")

    @property
    def native_value(self) -> datetime | None:
        return _dt(self.state_data["sync"].get("last_sync_at_ms"))


class SyncStateSensor(ElevatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SYNC_STATES
    _attr_translation_key = "sync_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "sync_state")

    @property
    def native_value(self) -> str:
        return self.state_data["sync"]["state"]

    @property
    def extra_state_attributes(self) -> dict:
        s = self.state_data
        return {"reason": s["prediction"]["reason"], "missed": s["sync"].get("missed"),
                "transport": self.coordinator.transport}


class CycleSensor(ElevatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_translation_key = "cycle_length"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cycle_length")

    @property
    def native_value(self) -> float:
        return self.state_data["cycle"]["seconds"]


class CarPositionSensor(ElevatorEntity, SensorEntity):
    """Estimated car position; disabled by default (changes every few seconds)."""

    _attr_translation_key = "car_position"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "car_position")
        self._unsub: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(seconds=5))

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
        await super().async_will_remove_from_hass()

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        d = self.state_data
        if not d["prediction"]["valid"]:
            return None
        p = position(d["timeline"], d["building"]["floors"], self.coordinator.server_now_ms())
        if p is None:
            return None
        return p["floor"] if p["kind"] == "stop" else f"{p['from']}→{p['to']}"
