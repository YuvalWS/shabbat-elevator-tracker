"""Configuration: YAML file + environment overrides + validation.

Secrets (audio-source credentials) are never read from the file; see `auth_from_env`.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "building": {
        "name": "Example building",
        "floors": ["G", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
        "default_floor": None,
    },
    "elevator": {
        "route": ["G", "2", "4", "6", "8", "10", "12", "11", "9", "7", "5", "3", "1"],
        "cycle_seconds": 404.4,
        "adaptive_cycle": {"enabled": True, "max_deviation_percent": 2.0},
        "dwell_seconds": 10.0,
        "dwell_overrides": {"G": 60.0},
        "door_transition_seconds": 2.5,
        "travel": {"leg_overhead_seconds": 12.1, "per_floor_seconds": 2.8},
    },
    "anchor": {"floor": "3", "route_index": None, "offset_seconds": 0.0},
    "audio": {
        "source": "",
        "sample_rate": 16000,
        "stall_seconds": 20.0,
        "reconnect_max_backoff_seconds": 60.0,
        "loop_start": None,  # "HH:MM" local time to start a loop: source at
        "auth": {"type": "none", "login_url": None, "tls_verify": True},
    },
    "detector": {
        "band_hz": [600, 2200],
        "threshold_db": 25.0,
        "release_db": 15.0,
        "min_event_seconds": 2.0,
        "max_event_seconds": 30.0,
        "signature": None,
        "min_similarity": 0.80,
        "background_window_seconds": 30,
        "background_percentile": 20,
    },
    "sync": {
        "gate_seconds": 8.0,
        "confirm_tolerance_seconds": 3.0,
        "confirm_min_similarity": 0.90,
        "confirm_max_duration_ratio": 1.6,
        "sync_min_similarity": 0.80,
        "confirm_window_cycles": 3,
        "pause_after_missed": 3,
        "stale_after_seconds": 7200.0,
    },
    "calendar": {
        "enabled": True,
        "latitude": 32.0781,
        "longitude": 34.8503,
        "elevation_m": 50.0,
        "timezone": "Asia/Jerusalem",
        "israel": True,
        "candle_lighting_minutes": 18,
        "havdalah_minutes": 0,
        "elevator_window": {"before_start_minutes": 10, "after_end_minutes": 30},
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "cors_origins": ["*"],
        "expose_debug": True,
        "allow_manual_sync": False,
        "web_root": None,
        "dev_clock_speed": 1.0,
    },
    "storage": {"data_dir": "/data", "event_log_days": 14, "save_clips": False},
    "logging": {"level": "INFO"},
}

AUTH_TYPES = ("none", "basic", "bearer", "header", "frigate")


class ConfigError(ValueError):
    pass


REPLACE_KEYS = {"dwell_overrides"}  # mappings that replace the default instead of merging


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in REPLACE_KEYS:
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Config:
    raw: dict
    path: str | None = None
    warnings: list[str] = field(default_factory=list)

    # convenience accessors
    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def building(self) -> dict:
        return self.raw["building"]

    @property
    def elevator(self) -> dict:
        return self.raw["elevator"]

    @property
    def anchor(self) -> dict:
        return self.raw["anchor"]

    @property
    def audio(self) -> dict:
        return self.raw["audio"]

    @property
    def detector(self) -> dict:
        return self.raw["detector"]

    @property
    def sync(self) -> dict:
        return self.raw["sync"]

    @property
    def calendar(self) -> dict:
        return self.raw["calendar"]

    @property
    def server(self) -> dict:
        return self.raw["server"]

    @property
    def storage(self) -> dict:
        return self.raw["storage"]

    def public(self) -> dict:
        """Everything that may be shown to clients (never the audio source or auth)."""
        r = self.raw
        return {
            "building": r["building"],
            "elevator": r["elevator"],
            "anchor": {"floor": r["anchor"]["floor"], "route_index": r["anchor"]["route_index"]},
            "calendar": {
                k: v for k, v in r["calendar"].items() if k not in ("latitude", "longitude", "elevation_m")
            }
            | {"location_set": True},
            "sync": r["sync"],
        }


def _coerce_env_value(v: str) -> Any:
    try:
        out = yaml.safe_load(v)
    except yaml.YAMLError:
        return v
    # YAML 1.1 reads "00:56" as the base-60 integer 56; keep clock times as strings.
    if isinstance(out, int) and ":" in v:
        return v
    return out


def apply_env(raw: dict, env: dict[str, str]) -> dict:
    raw = copy.deepcopy(raw)
    simple = {
        "SET_AUDIO_SOURCE": ("audio", "source", str),
        "SET_DATA_DIR": ("storage", "data_dir", str),
        "SET_HOST": ("server", "host", str),
        "SET_PORT": ("server", "port", int),
        "SET_LOG_LEVEL": ("logging", "level", str),
        "SET_DEV_CLOCK_SPEED": ("server", "dev_clock_speed", float),
        "SET_AUTH_TYPE": ("audio", "auth", None),
    }
    for name, (sec, key, typ) in simple.items():
        if env.get(name):
            if name == "SET_AUTH_TYPE":
                raw["audio"]["auth"]["type"] = env[name]
            else:
                raw[sec][key] = typ(env[name])
    # Generic: SET_CFG__section__key[__sub]=yaml-value
    for name, value in env.items():
        if not name.startswith("SET_CFG__"):
            continue
        parts = [p.lower() for p in name[len("SET_CFG__"):].split("__") if p]
        node = raw
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        if parts:
            node[parts[-1]] = _coerce_env_value(value)
    return raw


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_refs(text: str, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return _ENV_REF.sub(lambda m: env.get(m.group(1), ""), text or "")


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    if path is None:
        path = env.get("SET_CONFIG") or ("/config/config.yaml" if Path("/config/config.yaml").exists() else None)
    user: dict = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        with p.open(encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        if not isinstance(user, dict):
            raise ConfigError("config root must be a mapping")
    raw = _deep_merge(DEFAULTS, user)
    raw = apply_env(raw, env)
    cfg = Config(raw=raw, path=str(path) if path else None)
    validate(cfg)
    return cfg


def config_from_dict(d: dict | None = None, env: dict[str, str] | None = None) -> Config:
    raw = _deep_merge(DEFAULTS, d or {})
    if env:
        raw = apply_env(raw, env)
    cfg = Config(raw=raw)
    validate(cfg)
    return cfg


def _norm_floor(x: Any) -> str:
    return str(x)


def is_file_source(source: str) -> bool:
    if not source:
        return False
    if source.startswith("loop:"):
        return True
    if "://" in source or source.startswith("alsa:"):
        return source.startswith("file://")
    return True


def validate(cfg: Config) -> None:
    r = cfg.raw
    errs: list[str] = []
    b, e, a, d = r["building"], r["elevator"], r["anchor"], r["detector"]

    name = b.get("name")
    if isinstance(name, dict):
        if not name or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in name.items()):
            errs.append("building.name must be a string or a {language: name} mapping")
    elif not isinstance(name, str) or not name:
        errs.append("building.name must be a string or a {language: name} mapping")

    floors = [_norm_floor(f) for f in (b.get("floors") or [])]
    b["floors"] = floors
    if not floors:
        errs.append("building.floors must not be empty")
    if len(set(floors)) != len(floors):
        errs.append("building.floors must be unique")
    if b.get("default_floor") is not None:
        b["default_floor"] = _norm_floor(b["default_floor"])
        if b["default_floor"] not in floors:
            errs.append("building.default_floor must be one of building.floors")

    route = [_norm_floor(f) for f in (e.get("route") or [])]
    e["route"] = route
    unknown = [f for f in route if f not in floors]
    if unknown:
        errs.append(f"elevator.route has unknown floors: {unknown}")
    if len(set(route)) < 2:
        errs.append("elevator.route needs at least 2 distinct stops")
    for i in range(len(route)):
        if route and route[i] == route[(i + 1) % len(route)]:
            errs.append("elevator.route must not contain two consecutive identical stops")
            break
    e["dwell_overrides"] = {_norm_floor(k): float(v) for k, v in (e.get("dwell_overrides") or {}).items()}
    for k in e["dwell_overrides"]:
        if k not in floors:
            errs.append(f"elevator.dwell_overrides: unknown floor {k}")

    c = float(e.get("cycle_seconds", 0))
    if not 60 < c < 3600:
        errs.append("elevator.cycle_seconds must be between 60 and 3600")
    durations = [e["dwell_seconds"], e["door_transition_seconds"], e["travel"]["leg_overhead_seconds"]]
    if any(float(x) <= 0 for x in durations) or float(e["travel"]["per_floor_seconds"]) < 0:
        errs.append("elevator durations must be > 0")
    dwells = [float(e["dwell_seconds"])] + list(e["dwell_overrides"].values())
    if any(x <= 0 for x in dwells):
        errs.append("dwell values must be > 0")
    elif 2 * float(e["door_transition_seconds"]) >= min(dwells):
        errs.append("2 x elevator.door_transition_seconds must be smaller than the shortest dwell")

    a["floor"] = _norm_floor(a.get("floor"))
    if a["floor"] not in route:
        errs.append("anchor.floor must be on elevator.route")
    elif route.count(a["floor"]) > 1:
        ri = a.get("route_index")
        if ri is None:
            errs.append("anchor.route_index is required: anchor.floor appears more than once in the route")
        elif not (0 <= int(ri) < len(route)) or route[int(ri)] != a["floor"]:
            errs.append("anchor.route_index must point to anchor.floor in elevator.route")

    sr = int(r["audio"]["sample_rate"])
    if sr < 4000:
        errs.append("audio.sample_rate must be >= 4000")
    lo, hi = (float(x) for x in d["band_hz"])
    if not (0 <= lo < hi <= sr / 2):
        errs.append("detector.band_hz must be within [0, sample_rate/2] and low < high")
    if float(d["release_db"]) >= float(d["threshold_db"]):
        errs.append("detector.release_db must be lower than detector.threshold_db")
    if d.get("signature") is not None and len(d["signature"]) != int(sr / 2 // 100):
        errs.append(f"detector.signature must have {int(sr / 2 // 100)} values for this sample rate")

    auth = r["audio"].get("auth") or {}
    r["audio"]["auth"] = _deep_merge(DEFAULTS["audio"]["auth"], auth)
    if r["audio"]["auth"]["type"] not in AUTH_TYPES:
        errs.append(f"audio.auth.type must be one of {AUTH_TYPES}")
    if r["audio"]["auth"]["type"] == "frigate" and not r["audio"]["auth"].get("login_url"):
        errs.append("audio.auth.login_url is required for auth type 'frigate'")

    cal = r["calendar"]
    if not -90 <= float(cal["latitude"]) <= 90 or not -180 <= float(cal["longitude"]) <= 180:
        errs.append("calendar latitude/longitude out of range")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(cal["timezone"])
    except Exception:
        errs.append(f"calendar.timezone is not a known time zone: {cal['timezone']!r}")
    if float(cal["havdalah_minutes"]) < 0:
        errs.append("calendar.havdalah_minutes must be >= 0")

    speed = float(r["server"]["dev_clock_speed"])
    if speed <= 0:
        errs.append("server.dev_clock_speed must be > 0")
    elif speed > 1 and not is_file_source(r["audio"]["source"]):
        errs.append("server.dev_clock_speed > 1 is only allowed with a file or loop: audio source")

    if r["audio"].get("loop_start") and not re.fullmatch(r"\d{1,2}:\d{2}", str(r["audio"]["loop_start"])):
        errs.append("audio.loop_start must look like HH:MM")

    if errs:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errs))
