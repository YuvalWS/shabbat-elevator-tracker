"""Config and options flows."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import API_VERSION, CONF_EXTRA_FLOORS, CONF_HOME_FLOOR, CONF_URL, DOMAIN
from .coordinator import ApiError, fetch_json


class ElevatorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url: str | None = None
        self._state: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            session = async_get_clientsession(self.hass)
            try:
                health = await fetch_json(session, f"{url}/api/v1/health")
                state = await fetch_json(session, f"{url}/api/v1/state")
            except ApiError:
                errors["base"] = "cannot_connect"
            else:
                if health.get("api_version") != API_VERSION:
                    errors["base"] = "unsupported_version"
                else:
                    await self.async_set_unique_id(health["instance_id"])
                    self._abort_if_unique_id_configured(updates={CONF_URL: url})
                    self._url, self._state = url, state
                    return await self.async_step_floor()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_URL, default="http://"): str}),
            errors=errors,
        )

    async def async_step_floor(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._state is not None
        floors = self._state["building"]["floors"]
        if user_input is not None:
            return self.async_create_entry(
                title=self._state["building"]["name"],
                data={CONF_URL: self._url},
                options={CONF_HOME_FLOOR: user_input[CONF_HOME_FLOOR], CONF_EXTRA_FLOORS: []},
            )
        default = self._state["building"].get("default_floor") or floors[0]
        return self.async_show_form(
            step_id="floor",
            data_schema=vol.Schema({vol.Required(CONF_HOME_FLOOR, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(options=floors, mode=selector.SelectSelectorMode.DROPDOWN))}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ElevatorOptionsFlow()


class ElevatorOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        coordinator = self.config_entry.runtime_data
        floors = coordinator.data["building"]["floors"]
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_HOME_FLOOR, default=opts.get(CONF_HOME_FLOOR, floors[0])): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=floors, mode=selector.SelectSelectorMode.DROPDOWN)),
                vol.Optional(CONF_EXTRA_FLOORS, default=opts.get(CONF_EXTRA_FLOORS, [])): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=floors, multiple=True,
                                                  mode=selector.SelectSelectorMode.DROPDOWN)),
            }),
        )
