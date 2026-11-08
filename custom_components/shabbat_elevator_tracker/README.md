# Shabbat Elevator Tracker — Home Assistant integration

Shows when the Shabbat elevator reaches your floor, from a running
[Shabbat Elevator Tracker](../../README.md) server. Local push (server-sent events) with a
60 s polling fallback; next arrivals are computed inside Home Assistant, so they stay
right between updates.

## Install

- **HACS**: add this repository as a custom repository (type *Integration*), install
  *Shabbat Elevator Tracker*, restart Home Assistant.
- **Manual**: copy `custom_components/shabbat_elevator_tracker` into your Home Assistant
  `config/custom_components/`, restart.

Then *Settings → Devices & services → Add integration → Shabbat Elevator Tracker*:

1. **Server URL**, e.g. `http://192.168.1.50:8080` (the tracker's API has no password;
   keep it on your LAN).
2. **Your floor**.

To change your floor later: *Configure* on the integration shows the same floor form.

## Entities

One device per tracker server, named after the building (in Home Assistant's language
when the building has names per language). Entity IDs start with the device name; for a
building called "Example building":

| Entity | Shows |
|---|---|
| `sensor.example_building_next_arrival` | Next arrival at your floor (timestamp). Attributes `floor`, `depart_at`. Unavailable when there is no valid prediction. |
| `sensor.example_building_last_sync` | When the tracker last heard the anchor stop. |
| `sensor.example_building_sync_state` | Idle / Listening / Confirming / Synced / Paused. |
| `sensor.example_building_cycle_length` | Measured cycle, seconds. |
| `sensor.example_building_car_position` | Estimated car position, e.g. `5` or `3→1`. Disabled by default (changes every few seconds). |
| `binary_sensor.example_building_audio_stream` | The tracker receives audio. |
| `binary_sensor.example_building_elevator_running` | On while synced to a running elevator. |
| `binary_sensor.example_building_shabbat_elevator_window` | On inside the expected elevator window (candle lighting − padding … havdalah + padding). |

Event `shabbat_elevator_tracker_sync` fires on each sync, with `anchor_time` and
`cycle_seconds`.

## Dashboard

A ready dashboard: next arrival at your floor (relative and clock time), sync state,
and the Shabbat status. While the elevator isn't running, the arrival cards are
replaced by a short note.

*Settings → Dashboards → Add dashboard → New dashboard from scratch*, open it,
*⋮ → Edit dashboard → ⋮ → Raw configuration editor*, paste, and replace
`example_building` with your device's prefix.

```yaml
title: Shabbat Elevator
views:
  - title: Elevator
    path: elevator
    icon: mdi:elevator-passenger
    type: sections
    max_columns: 2
    sections:
      - type: grid
        cards:
          - type: heading
            heading: Next elevator
            icon: mdi:elevator-passenger
          - type: tile
            entity: sensor.example_building_next_arrival
            name: My floor
            icon: mdi:home-account
            grid_options: {columns: 12}
            visibility: &running
              - condition: state
                entity: binary_sensor.example_building_elevator_running
                state: "on"
          - type: entities
            visibility: *running
            entities:
              - entity: sensor.example_building_next_arrival
                name: Arrives at
                format: time
          - type: markdown
            content: >-
              {% set s = states('sensor.example_building_sync_state') %}
              {% if s == 'searching' %}**Listening for the elevator.**
              The first prediction usually takes a few minutes.
              {% elif s == 'provisional' %}**Heard the elevator once.**
              Confirming on the next round (about 7 minutes).
              {% elif s == 'paused' %}**The elevator seems to have stopped.**
              Listening for it to start again.
              {% else %}**The Shabbat elevator is not running now.**
              Predictions start once it runs and the tracker has synced.{% endif %}
            visibility:
              - condition: state
                entity: binary_sensor.example_building_elevator_running
                state_not: "on"
          - type: tile
            entity: sensor.example_building_sync_state
            name: Sync
            grid_options: {columns: 6}
          - type: tile
            entity: sensor.example_building_last_sync
            name: Last sync
            grid_options: {columns: 6}

      - type: grid
        cards:
          - type: heading
            heading: Shabbat
            icon: mdi:candle
          - type: tile
            entity: binary_sensor.example_building_shabbat_elevator_window
            name: Elevator window
            grid_options: {columns: 6}
          - type: tile
            entity: binary_sensor.example_building_elevator_running
            name: Elevator
            grid_options: {columns: 6}
          - type: tile
            entity: binary_sensor.example_building_audio_stream
            name: Sensor
            grid_options: {columns: 6}
          - type: history-graph
            title: Last 24 hours
            hours_to_show: 24
            entities:
              - entity: binary_sensor.example_building_elevator_running
                name: Elevator
              - entity: binary_sensor.example_building_audio_stream
                name: Sensor
              - entity: sensor.example_building_sync_state
                name: Sync
```

Notify one minute before the elevator reaches your floor:

```yaml
automation:
  - alias: Elevator in one minute
    triggers:
      - trigger: time
        at: sensor.example_building_next_arrival
        offset: "-00:01:00"
    conditions:
      - condition: state
        entity_id: binary_sensor.example_building_elevator_running
        state: "on"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Elevator at your floor in 1 minute"
```

## Try it locally (development)

`docker-compose.dev.yml` runs a tracker that loops recordings from `test-data/`
(local only, never committed) and a Home Assistant with this integration and the
dashboard above:

```bash
echo LOOP_SPEED=1 >> .env    # real time: Home Assistant shows arrivals against its own clock
docker compose -f docker-compose.dev.yml up -d --build loop homeassistant
python tools/ha-dev/setup.py
```

`setup.py` finishes onboarding (user `dev`), adds the integration for the loop tracker
(floor 3) and writes the dashboard with the right entity IDs. Open
<http://localhost:8123/shabbat-elevator/elevator> and pick the `dev` user: Home
Assistant is published on 127.0.0.1 only and lets this machine in without a password
(`tools/ha-dev/configuration.yaml`; the API password defaults to `dev-only-password`,
override with `HA_DEV_PASSWORD`). The tracker needs two elevator stops (about 7–14
minutes) before the first prediction.

Tests: `sh tools/run-ha-tests.sh` (pytest-homeassistant-custom-component in Docker).
