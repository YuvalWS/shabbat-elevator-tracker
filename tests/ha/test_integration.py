"""Home Assistant integration tests.

Needs pytest-homeassistant-custom-component (Python 3.13); run with tools/run-ha-tests.sh.
Not collected by the main suite (see tests/ha/conftest.py).
"""

import json
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shabbat_elevator_tracker.const import CONF_HOME_FLOOR, CONF_URL, DOMAIN
from custom_components.shabbat_elevator_tracker.model import building_name, next_arrival

URL = "http://elevator.test:8080"
FX = json.loads((Path(__file__).parent / "fixtures.json").read_text())
LOCKED, WAITING = FX["locked"], FX["waiting"]
IID = LOCKED["instance_id"]
HEALTH = {"ok": True, "version": "0.1.0", "api_version": 1, "instance_id": IID,
          "stream": {"state": "connected"}, "sync": {"state": "locked"}}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def mock_server(aioclient_mock, state=LOCKED, sse_body=""):
    aioclient_mock.get(f"{URL}/api/v1/health", json=HEALTH)
    aioclient_mock.get(f"{URL}/api/v1/state", json=state)
    aioclient_mock.get(f"{URL}/api/v1/stream", text=sse_body, headers={"Content-Type": "text/event-stream"})


async def setup_entry(hass, aioclient_mock, state=LOCKED, sse_body="", home="3"):
    mock_server(aioclient_mock, state, sse_body)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL}, options={CONF_HOME_FLOOR: home},
                            unique_id=IID, title="Example building")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_success(hass: HomeAssistant, aioclient_mock):
    mock_server(aioclient_mock)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL + "/"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "floor"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_HOME_FLOOR: "5"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"] == {CONF_URL: URL}
    assert r["options"][CONF_HOME_FLOOR] == "5"
    assert r["result"].unique_id == IID


async def test_config_flow_cannot_connect(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/v1/health", status=500)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "cannot_connect"}


async def test_config_flow_already_configured(hass: HomeAssistant, aioclient_mock):
    MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL}, unique_id=IID).add_to_hass(hass)
    mock_server(aioclient_mock)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"


async def test_removed_entity_is_cleaned_up(hass: HomeAssistant, aioclient_mock):
    """An issur melacha sensor left by 0.1.0 is removed from the registry on setup."""
    ent = er.async_get(hass)
    old = ent.async_get_or_create("binary_sensor", DOMAIN, f"{IID}_issur_melacha")
    await setup_entry(hass, aioclient_mock)
    assert ent.async_get(old.entity_id) is None
    assert ent.async_get_entity_id("binary_sensor", DOMAIN, f"{IID}_shabbat_window") is not None


async def test_entities_when_locked(hass: HomeAssistant, aioclient_mock):
    entry = await setup_entry(hass, aioclient_mock)
    ent = er.async_get(hass)
    nid = ent.async_get_entity_id("sensor", DOMAIN, f"{IID}_next_arrival")
    st = hass.states.get(nid)
    assert st is not None and st.state != STATE_UNAVAILABLE
    assert st.attributes["floor"] == "3"
    assert hass.states.get(ent.async_get_entity_id("sensor", DOMAIN, f"{IID}_sync_state")).state == "locked"
    assert hass.states.get(ent.async_get_entity_id("binary_sensor", DOMAIN, f"{IID}_running")).state == "on"
    assert hass.states.get(ent.async_get_entity_id("binary_sensor", DOMAIN, f"{IID}_audio_stream")).state == "on"
    assert ent.async_get_entity_id("binary_sensor", DOMAIN, f"{IID}_issur_melacha") is None
    cyc = hass.states.get(ent.async_get_entity_id("sensor", DOMAIN, f"{IID}_cycle_length"))
    assert float(cyc.state) == pytest.approx(LOCKED["cycle"]["seconds"])
    # one next-arrival sensor, for your floor
    assert not [e for e in er.async_entries_for_config_entry(ent, entry.entry_id) if "_floor_" in e.unique_id]
    car = ent.async_get(ent.async_get_entity_id("sensor", DOMAIN, f"{IID}_car_position"))
    assert car.disabled_by is not None


async def test_next_arrival_unavailable_when_waiting(hass: HomeAssistant, aioclient_mock):
    await setup_entry(hass, aioclient_mock, state=WAITING)
    ent = er.async_get(hass)
    st = hass.states.get(ent.async_get_entity_id("sensor", DOMAIN, f"{IID}_next_arrival"))
    assert st.state == STATE_UNAVAILABLE
    assert hass.states.get(ent.async_get_entity_id("binary_sensor", DOMAIN, f"{IID}_running")).state == "off"


async def test_sse_push_updates(hass: HomeAssistant, aioclient_mock):
    body = "retry: 5000\n\nevent: state\ndata: " + json.dumps(LOCKED) + "\n\n"
    entry = await setup_entry(hass, aioclient_mock, state=WAITING, sse_body=body)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    assert coord.data["sync"]["state"] == "locked"  # came from the SSE event, /state said searching
    ent = er.async_get(hass)
    assert hass.states.get(ent.async_get_entity_id("binary_sensor", DOMAIN, f"{IID}_running")).state == "on"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow(hass: HomeAssistant, aioclient_mock):
    entry = await setup_entry(hass, aioclient_mock)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.options.async_configure(r["flow_id"], {CONF_HOME_FLOOR: "G"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_HOME_FLOOR] == "G"
    nid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{IID}_next_arrival")
    assert hass.states.get(nid).attributes["floor"] == "G"


async def test_diagnostics_redacts_url(hass: HomeAssistant, aioclient_mock):
    from custom_components.shabbat_elevator_tracker.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await setup_entry(hass, aioclient_mock)
    d = await async_get_config_entry_diagnostics(hass, entry)
    assert d["entry"]["url"] == "**REDACTED**"
    assert d["state"]["instance_id"] == IID


def test_model_matches_server():
    tl = LOCKED["timeline"]
    now = LOCKED["server_time_ms"]
    for f, v in LOCKED["prediction"]["next_arrivals"].items():
        n = next_arrival(tl, f, now)
        if v is None:
            assert n is None
        else:
            assert abs(n["arrive_at"] - v["arrive_at_ms"]) <= 1


def test_building_name_per_language():
    b = {"name": {"en": "Oak 5", "fr": "Chene 5"}}
    assert building_name(b, "fr") == "Chene 5"
    assert building_name(b, "pt-BR") == "Oak 5"
    assert building_name({"name": "Plain"}, "he") == "Plain"
