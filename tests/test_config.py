import pytest

from shabbat_elevator_tracker.config import (
    ConfigError,
    config_from_dict,
    expand_env_refs,
    load_config,
)


def test_example_config_loads(root):
    cfg = load_config(root / "config.example.yaml", env={})
    assert cfg.anchor["floor"] == "3"
    assert cfg.building["floors"][0] == "G"
    assert cfg.calendar["latitude"] == pytest.approx(32.0781)


def test_defaults_valid():
    cfg = config_from_dict({})
    assert cfg.elevator["cycle_seconds"] == 404.4


def test_floors_are_strings():
    cfg = config_from_dict({"building": {"floors": [-1, 0, 1, 2]}, "elevator": {"route": [0, 1, 2, -1],
                            "dwell_overrides": {}}, "anchor": {"floor": 1}})
    assert cfg.building["floors"] == ["-1", "0", "1", "2"]
    assert cfg.anchor["floor"] == "1"


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"elevator": {"route": ["G", "X"]}}, "unknown floors"),
        ({"elevator": {"route": ["G", "G", "2"]}}, "consecutive"),
        ({"anchor": {"floor": "99"}}, "anchor.floor"),
        ({"elevator": {"cycle_seconds": 30}}, "cycle_seconds"),
        ({"detector": {"release_db": 30}}, "release_db"),
        ({"detector": {"band_hz": [600, 9000]}}, "band_hz"),
        ({"elevator": {"door_transition_seconds": 6}}, "door_transition"),
        ({"audio": {"auth": {"type": "magic"}}}, "auth.type"),
        ({"audio": {"auth": {"type": "frigate"}}}, "login_url"),
        ({"server": {"dev_clock_speed": 10}, "audio": {"source": "rtsp://x/y"}}, "dev_clock_speed"),
        ({"calendar": {"timezone": "Mars/Base"}}, "timezone"),
    ],
)
def test_validation_errors(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        config_from_dict(patch)


def test_duplicate_anchor_floor_needs_route_index():
    route = ["G", "3", "5", "3"]
    with pytest.raises(ConfigError, match="route_index"):
        config_from_dict({"elevator": {"route": route, "dwell_overrides": {}}})
    cfg = config_from_dict({"elevator": {"route": route, "dwell_overrides": {}}, "anchor": {"route_index": 3}})
    assert cfg.anchor["route_index"] == 3


def test_env_overrides():
    cfg = config_from_dict({}, env={"SET_AUDIO_SOURCE": "rtsp://a/b", "SET_PORT": "9000",
                                    "SET_CFG__sync__gate_seconds": "5", "SET_AUTH_TYPE": "basic"})
    assert cfg.audio["source"] == "rtsp://a/b"
    assert cfg.server["port"] == 9000
    assert cfg.sync["gate_seconds"] == 5
    assert cfg.audio["auth"]["type"] == "basic"


def test_building_name_per_language():
    cfg = config_from_dict({"building": {"name": {"en": "Oak 5", "fr": "Chene 5"}}})
    assert cfg.building["name"]["fr"] == "Chene 5"
    with pytest.raises(ConfigError, match="building.name"):
        config_from_dict({"building": {"name": {"en": 5}}})
    with pytest.raises(ConfigError, match="building.name"):
        config_from_dict({"building": {"name": ""}})


def test_env_clock_time_stays_string():
    cfg = config_from_dict({}, env={"SET_CFG__audio__loop_start": "00:56"})
    assert cfg.audio["loop_start"] == "00:56"


def test_dev_speed_allowed_for_loop():
    cfg = config_from_dict({"server": {"dev_clock_speed": 10}, "audio": {"source": "loop:somewhere"}})
    assert cfg.server["dev_clock_speed"] == 10


def test_expand_env_refs():
    assert expand_env_refs("rtsp://${U}:${P}@h/x", {"U": "me", "P": "pw"}) == "rtsp://me:pw@h/x"
    assert expand_env_refs("${MISSING}x", {}) == "x"


def test_public_config_hides_source_and_location():
    cfg = config_from_dict({"audio": {"source": "rtsp://user:pw@cam/x"}})
    pub = cfg.public()
    text = str(pub)
    assert "rtsp" not in text and "pw" not in text
    assert "latitude" not in pub["calendar"]
