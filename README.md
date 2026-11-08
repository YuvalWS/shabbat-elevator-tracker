# Shabbat Elevator Tracker

Know when the Shabbat elevator will reach your floor.

A Shabbat elevator runs a fixed, repeating route. This service listens to a microphone
near one floor (for example a doorbell camera's audio through Frigate/go2rtc), detects
the sound of the elevator stopping there, syncs a timing model of the route to it, and
predicts the next arrival at every floor. It also knows when Shabbat and Yom Tov start
and end (computed offline). You get the prediction through:

- a thin web client: phone to wall screen, light/dark, English/Hebrew/French, keeps the
  screen on, countdown and arrival time, elevator shaft illustration on large screens;
- an HTTP API with a push stream (Server-Sent Events);
- a Home Assistant integration (in this repository, HACS-compatible).

It is one small Python process (≈ 50 MB RAM, ~1–2 % of a core) and runs in Docker
(amd64/arm64) or an LXC container (Proxmox helper script included).

## How it works

1. **Listen.** ffmpeg reads the audio stream (RTSP, HTTP, ALSA or a file).
2. **Detect.** Every 0.1 s the sound is split into 100 Hz bands and compared with the
   rolling background. A loud event in the configured band is a *candidate* stop.
3. **Sync.** A candidate is trusted only when it fits the rhythm: either it lands where
   the current lock expects the next stop (± 8 s), or two candidates one to three cycles
   apart confirm each other and sound alike. Random loud sounds (doors, people) don't
   repeat every 6 min 44 s, so they don't sync. After three missed stops the elevator
   is considered paused (the night pause), and a new confirmed pair re-locks it.
4. **Predict.** The route model (stop times, travel times, scaled to the measured cycle)
   gives every floor's arrival relative to the anchor stop. Clients compute countdowns
   locally; the server only pushes when something changes.
5. **Calendar.** Candle lighting and havdalah come from [hdate](https://github.com/py-libhdate/py-libhdate)
   (the library Home Assistant's Jewish Calendar uses), offline. Outside the Shabbat
   window the tracker is *idle*, but it still locks if it hears the elevator.

## Quick start (Docker)

```bash
mkdir -p config && cp config.example.yaml config/config.yaml   # then edit it
docker compose up -d
```

Open `http://<host>:8080/`. Set at least `audio.source` (or `SET_AUDIO_SOURCE`) and the
building's floors, route and anchor floor. See [docs/proxmox.md](docs/proxmox.md) for
an LXC container on Proxmox.

### Stream credentials

The web UI and API have no password, but the audio source can. Credentials are read only
from environment variables, never from the config file, and never logged:

| `audio.auth.type` | Environment | Use |
|---|---|---|
| `basic` | `SET_STREAM_USERNAME`, `SET_STREAM_PASSWORD` | go2rtc / Frigate RTSP restream with `rtsp: username/password` (recommended), or HTTP Basic |
| `bearer` | `SET_STREAM_TOKEN` | HTTP(S) streams with a bearer token |
| `header` | `SET_STREAM_HEADERS` | any extra HTTP headers |
| `frigate` | `SET_STREAM_USERNAME`, `SET_STREAM_PASSWORD` + `audio.auth.login_url` | logs in to Frigate's authenticated port (8971) and sends the session cookie |

`${VAR}` inside `audio.source` is also expanded from the environment.

## Setting up a new building

1. **Record** a day of audio (night-only recordings avoid capturing conversations):
   ```bash
   shabbat-elevator-tracker record --output recordings --hours 22:30-06:30
   ```
2. **Calibrate**: finds the cycle, the anchor sound's band and level, pauses, and fits
   the travel times from the faint sounds of the other floors:
   ```bash
   shabbat-elevator-tracker -c config.yaml calibrate recordings
   ```
   It prints a report and writes `calibration.yaml` with suggested values. Dwell times
   come from your config (audio can't separate dwell from travel); observe them once.
3. **Check** the config against the recordings:
   ```bash
   shabbat-elevator-tracker -c config.yaml replay recordings
   ```

Recordings are private: keep them out of version control (this repository refuses to
push audio files; enable the hook with `git config core.hooksPath tools/git-hooks`).

## Keeping the screen on

The web client keeps the screen awake while it is open:

- over **HTTPS** (e.g. a Cloudflare Tunnel) or on `localhost` it uses the Screen Wake
  Lock API, supported by all current browsers;
- on plain **http://** in the local network browsers don't allow Wake Lock, so it plays a
  tiny invisible looping video after the first tap, which works on most phones but is
  less reliable (battery savers may still dim the screen).

Settings shows which mode is active. For a wall tablet, HTTPS is the reliable option.

## Public exposure

For access from outside (e.g. Cloudflare Tunnel), set `server.expose_debug: false` and
keep `server.allow_manual_sync: false`; everything left is read-only. The web client can
also be hosted elsewhere and pointed at the API (`?api=https://…` or Settings).

## Home Assistant

Install `custom_components/shabbat_elevator_tracker` (HACS custom repository, or copy
the folder), then *Settings → Devices & services → Add integration → Shabbat Elevator
Tracker* and enter the server URL. It uses push (SSE) and falls back to polling.

Entities: next arrival at your floor (timestamp), per-floor next arrival (disabled by
default), last sync, sync state, cycle length, audio stream, elevator running, Shabbat
window, issur melacha, car position (disabled by default). Event
`shabbat_elevator_tracker_sync` fires on each sync.

Example: notify one minute before the elevator reaches your floor:

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

## API

`GET /api/v1/state` returns everything a client needs; `GET /api/v1/stream` pushes it
(SSE). See [docs/api.md](docs/api.md).

## Development

```bash
pip install -e ".[dev]"
pytest                                   # unit, synthetic audio, server, ingest
node --test tests/web/model.test.mjs     # web model vs Python fixture
sh tools/run-ha-tests.sh                 # Home Assistant tests (Docker)
```

Tests on real recordings run when `test-data/` exists locally (it is git-ignored and
never pushed). For a live-looking demo from recordings:
`SET_DEV_CLOCK_SPEED=10` with `audio.source: "loop:<dir>"` plays them 10× faster.

Design and decisions: [docs/DESIGN.md](docs/DESIGN.md).

## Acknowledgements

The idea of detecting a Shabbat elevator by sound at one floor and extrapolating its
fixed route comes from **[udid/shabat_elevator](https://github.com/udid/shabat_elevator)**.
This project is an independent implementation aimed at being generic and easy to deploy.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). Copyright (c) 2026 Yuval Weiss.
