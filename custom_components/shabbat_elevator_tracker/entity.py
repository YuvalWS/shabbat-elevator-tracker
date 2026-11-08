"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ElevatorCoordinator
from .model import building_name


class ElevatorEntity(CoordinatorEntity[ElevatorCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ElevatorCoordinator, key: str) -> None:
        super().__init__(coordinator)
        data = coordinator.data
        iid = data["instance_id"]
        self._attr_unique_id = f"{iid}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, iid)},
            name=building_name(data["building"], coordinator.hass.config.language),
            manufacturer="Shabbat Elevator Tracker",
            model="Audio-synced Shabbat elevator",
            sw_version=data.get("version"),
            configuration_url=coordinator.base,
        )

    @property
    def state_data(self) -> dict:
        return self.coordinator.data
