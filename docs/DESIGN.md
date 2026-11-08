# Shabbat Elevator Tracker — Design Document

Status: **rev 5** (2026-11-08): implemented as v0.1. Rev 3 added the hdate calendar,
GPL-3.0-or-later, authenticated audio sources and the Givat Shmuel location; rev 4 records
what changed during implementation (section 23); rev 5 adds the one-time floor picker,
the short-landscape layout, Hebrew numerals, per-language building names and a local Home
Assistant for development (23.1).

This document is the implementation spec. It is written so an implementer (human or
model) can build the project milestone by milestone (section 20) without re-deriving
decisions. Where a choice was made, the reason is given once. Open items are in
section 21 and must not be guessed.

Names: repository `shabbat-elevator-tracker`, Python package `shabbat_elevator_tracker`,
CLI `shabbat-elevator-tracker`, Home Assistant domain `shabbat_elevator_tracker`,
environment-variable prefix `SET_` (e.g. `SET_CONFIG`, `SET_AUDIO_SOURCE`).

---

## 1. What it does

A Shabbat elevator runs a fixed, repeating route. If you know **when** it was at one
known floor, you can predict when it reaches every other floor.

This project:

1. listens to a microphone near one floor of the elevator (in practice, a doorbell
   camera's audio stream via Frigate/go2rtc),
2. detects the distinctive sound of the elevator stopping at that floor (the **anchor**),
3. syncs a configurable timing model of the route to those detections,
4. knows when Shabbat and Yom Tov start and end (offline calculation),
5. serves the prediction through an HTTP API, a push stream, a thin web client
   (English, Hebrew, French), and a Home Assistant integration.

### Goals

- Generic: building, floors, route, timings and location come from one config file;
  nothing about a specific building is hard-coded.
- Lightweight: one Python process, runs in Docker (amd64/arm64) **or** an LXC container
  (Proxmox helper script, or any Debian/Ubuntu LXC/VM/host via `install.sh`).
- Honest status: the user always sees when the last sync was, whether the audio stream
  is alive, and whether the elevator currently appears to be running.
- Tooling to set up a new building: record audio, find the cycle and the anchor sound,
  fit the timing model, and check the config against recordings.
- Public repository with no personal data (section 18).

### Non-goals (v1)

- No authentication on this service's own API/web UI (the data is not sensitive).
  Debug-only endpoints can be disabled for public exposure (section 11.6). The audio
  **source** may require credentials; they come from environment variables (section 8.1).
- No weather, analytics, or online calendar services.
- One process = one elevator. Two elevators → two containers.
- No Hebrew in code, identifiers, comments, or the main README. Hebrew (and French)
  text lives only in UI string files and HA translation files (sections 12.5, 13.4).

## 2. Credits

The idea — detecting a Shabbat elevator's stop at one floor by sound and extrapolating
its fixed route — comes from **[udid/shabat_elevator](https://github.com/udid/shabat_elevator)**.
This project is an independent implementation. The credit must appear in:

- `README.md` ("Acknowledgements"),
- the web client footer and its About dialog ("Based on the idea of
  udid/shabat_elevator", linked), in all three languages.

## 3. Decision: new repository, not a fork

| Factor | upstream `udid/shabat_elevator` | Impact |
|---|---|---|
| License | **No LICENSE file** → all rights reserved by default | Its code may not be copied or adapted; only the idea is reused. Decisive on its own. |
| Language | Hebrew UI, README, config values, many comments | Requirement: no Hebrew in code. Near-total rewrite anyway. |
| Generality | Floors validated as `-1..12`, one building id, anchor floor 7 in defaults | Requirement: generic. |
| Audio input | Local microphone on a Raspberry Pi (`sounddevice`) | We need a network stream (RTSP). |
| Deploy | Raspberry Pi + systemd + Wi-Fi recovery + Cloudflare tunnel | We need Docker and LXC. |
| Calibrator on our data | Found cycle **404.36 s** but returned `review: competing_periodic_sound_patterns` and refused to write a config | Our detector needs a different strategy (section 9). |

Conclusion: start fresh, credit the idea. License: **GPL-3.0-or-later** (rev 3: the
calendar uses `hdate`, which is GPL-3.0-or-later; the owner accepted GPL). The `record` tool is based on our own
`record_frigate_audio.py`.

## 4. Field data (reference site)

Recorded from a doorbell camera's audio (via Frigate's go2rtc restream), 16 kHz mono
PCM, Fri 2026-11-06 12:53 → Sat 2026-11-07 17:36 local time (28.7 h, contiguous, no
clipping or dropouts). Background ≈ −55 dBFS by day, ≈ −75 dBFS at night.

### 4.1 Building and route

- Floors: `G, 1 … 12`. Microphone floor (anchor): **3**.
- Route (circular): up on even floors, down on odd floors:
  `G → 2 → 4 → 6 → 8 → 10 → 12 → 11 → 9 → 7 → 5 → 3 → 1 → G`.
- Every stop has the **same** dwell, ≈ 10 s (≈ 5 s with doors fully open), except G,
  where the doors stay open ≈ 60 s. The elevator pauses at night.

### 4.2 Measured

| Quantity | Value |
|---|---|
| Cycle length | **404.4 s** (6 min 44 s). Night subset at 0.1 s resolution: median 404.5 s, σ 0.18 s. Matches the owner's stopwatch measurement. |
| Shabbat mode start | first periodic anchor Fri **16:31**; computed candle lighting (sunset − 18 min) was **16:28**. No periodic pattern on Friday before 16:00 (control). |
| End | still running at the last anchor Sat 17:26; computed nightfall (8.5°) was 17:23. Recording ended 17:36. |
| Night pause | last anchor Sat **02:58:53**, resumed **06:06:00**. Same cycle length afterwards, but the phase shifted by ≈ 94 s → **re-sync after a pause; never extrapolate across one.** |
| Small phase step | ≈ +11 s around Sat 15:00 (probably a held door). Re-sync handles it. |
| Anchor sound | Floor-3 stop: broadband 600–4 000 Hz, ≈ 7–17 s above +25 dB, median **+38 dB** over the rolling background (600–2 200 Hz band). |
| Anchor detection rate | ≈ 100 % of cycles while running. |
| False-trigger pressure | Daytime: a random 21 s window exceeds +25 dB **≈ 20 %** of the time (people, doors). Night (01:00–06:00): 2 loud non-elevator events in 6 h. → Level alone is not enough; **phase gating is required** (section 9). |
| Other floors | Faint (+2 … +9 dB) and only visible when many cycles are averaged: an approach sound ≈ 7.5 s **before** each arrival, mostly below 300 Hz. Seen for G, 2, 4, 6, 8, 10, 12 (up) and 9, 7, 5, 3 (down). Floor 1 is masked by floor 3's own loud stop; floor 11 (a one-floor leg from 12) is too faint. |

### 4.3 Fitted timing (defaults for the reference site)

Least-squares fit of the 11 audible stops, with a common dwell fixed at 10 s:

- dwell 10 s everywhere, **G 59.75 s** (≈ the 60 s the owner observed),
- travel per leg = **12.1 s + 2.8 s × floors crossed** → 14.9 s for one floor, 17.7 s for two,
- RMS residual **0.8 s**. Dwell and travel can't be separated by audio alone (they
  always appear as a sum), so the common dwell comes from the owner's observation.

Arrival times in seconds after the floor-3 arrival (cycle 404.4 s):

| Floor | 3 | 1 | G | 2 | 4 | 6 | 8 | 10 | 12 | 11 | 9 | 7 | 5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Model | 0 | 27.7 | 52.6 | 130.1 | 157.8 | 185.5 | 213.2 | 240.9 | 268.6 | 293.5 | 321.3 | 349.0 | 376.7 |
| Fit residual | −0.8 | masked | −1.1 | +0.4 | −0.3 | −1.1 | −0.3 | +0.1 | +0.3 | faint | +1.6 | +0.7 | +0.6 |

The loud floor-3 sound starts at the floor-3 arrival (± 1 s), so `anchor.offset_seconds = 0`.
The `calibrate` tool (section 15.3) reproduces this fit automatically.

---

## 5. Architecture

```
                 ┌──────────── shabbat-elevator-tracker (one Python process) ────────────┐
 RTSP / HTTP /   │                                                                        │
 ALSA / file /   │ ffmpeg ─PCM─► Ingest ─frames─► Detector ─candidates─► SyncEngine ─┐    │
 loop ──────────►│  (subprocess)  (stream health)  (features, score)     (state machine)│ │
                 │                                                    ▲     │            │ │
                 │                Calendar (offline) ─expected windows┘     ▼            │ │
                 │                         Timeline model (config)  ◄── anchor/cycle    │ │
                 │                                   │                                  │ │
                 │                     StateStore (memory + data/state.json) ◄──────────┘ │
                 │                                   │                                    │
                 │                aiohttp: REST /api/v1/*, SSE /api/v1/stream, static web │
                 └───────────────────────────────────┬────────────────────────────────────┘
                                                     │
                        ┌────────────────────────────┼──────────────────────────┐
                    Web client                 Home Assistant               Anything else
              (served at / or hosted         custom integration            (curl, Node-RED…)
               elsewhere, API URL setting)   (SSE push + poll fallback)
```

Key idea: the server publishes the **anchor time + cycle length + route timeline**.
Every client computes positions and countdowns locally, so the server pushes only when
something changes (a sync, a status change), not every second.

### Technology

- Python **≥ 3.11** (Debian 12 ships 3.11, Alpine 3.20 ships 3.12). No 3.12+-only
  syntax (PEP 695 generics, `type` aliases).
- Runtime dependencies: `numpy`, `aiohttp`, `PyYAML`, `hdate` (GPL-3.0-or-later, the
  library Home Assistant's Jewish Calendar uses), `astral` (Apache-2.0, makes hdate's
  sun times exact). The `ffmpeg` binary decodes audio.
- Web client: plain HTML/CSS/ES modules, **no build step, no framework, no CDN**.
- Tests: `pytest`, `pytest-asyncio`, `pytest-aiohttp`; web model tests with `node --test`;
  UI layout sweep with `tools/layout-check.js` in a browser (section 23).

## 6. Configuration

One YAML file, default `/config/config.yaml` (override with `--config` or `SET_CONFIG`).
Times in seconds unless named otherwise. Floors are **string labels** (`G`, `P1`, `-1`
all work); their order in `building.floors` is bottom to top and defines distance.

`config.example.yaml` (committed) is the reference building's timetable with a
**placeholder location**. Real configs are git-ignored.

```yaml
building:
  name: "Example building"       # or per UI language: {en: "...", he: "...", fr: "..."}
  floors: ["G","1","2","3","4","5","6","7","8","9","10","11","12"]   # bottom → top
  default_floor: null            # floor preselected in the UI; null = force a choice

elevator:
  route: ["G","2","4","6","8","10","12","11","9","7","5","3","1"]    # circular
  cycle_seconds: 404.4           # measured full cycle; model durations are scaled to it
  adaptive_cycle:
    enabled: true                # refine from consecutive anchors (EMA)
    max_deviation_percent: 2.0
  dwell_seconds: 10              # the common stop time, doors opening → closed
  dwell_overrides: {"G": 60}     # only for floors that really differ (parking floor)
  door_transition_seconds: 2.5   # opening (and closing) time, part of the dwell
  travel:
    leg_overhead_seconds: 12.1   # start + stop per leg
    per_floor_seconds: 2.8       # per floor crossed

anchor:
  floor: "3"                     # floor whose stop the microphone hears
  route_index: null              # which visit, if `floor` appears twice in the route
  offset_seconds: 0.0            # anchor_time = sound_onset − offset

audio:
  source: "rtsp://frigate.local:8554/doorbell_audio"   # anything ffmpeg opens; SET_AUDIO_SOURCE overrides
  auth:                          # the secrets themselves come only from env (section 8.1)
    type: "none"                 # none | basic | bearer | header | frigate
    login_url: null              # frigate: e.g. https://frigate.local:8971/api/login
    tls_verify: true             # false for Frigate's default self-signed certificate
  sample_rate: 16000
  stall_seconds: 20
  reconnect_max_backoff_seconds: 60

detector:
  band_hz: [600, 2200]
  threshold_db: 25.0
  release_db: 15.0
  min_event_seconds: 2.0
  max_event_seconds: 30.0
  signature: null                # optional 80-value spectral template from `calibrate`
  min_similarity: 0.80           # used only if signature is set
  background_window_seconds: 30
  background_percentile: 20

sync:
  gate_seconds: 8.0
  confirm_window_cycles: 3
  pause_after_missed: 3
  stale_after_seconds: 7200

calendar:
  enabled: true
  latitude: 32.0781              # Givat Shmuel city centre (city level, owner's choice)
  longitude: 34.8503
  elevation_m: 50
  timezone: "Asia/Jerusalem"
  israel: true                   # false → diaspora (two-day Yom Tov, diaspora parsha schedule)
  candle_lighting_minutes: 18    # before sunset (Jerusalem custom: 40)
  havdalah_minutes: 0            # 0 = nightfall, sun 8.5° below the horizon; N = sunset + N min
  elevator_window:               # when the Shabbat elevator is expected to run
    before_start_minutes: 10     # before candle lighting
    after_end_minutes: 30        # after havdalah

server:
  host: "0.0.0.0"
  port: 8080
  cors_origins: ["*"]
  expose_debug: true             # false hides /api/v1/debug and debug fields
  allow_manual_sync: false       # enables POST /api/v1/sync
  web_root: null                 # null = bundled web client; path overrides; "" disables
  dev_clock_speed: 1.0           # >1 only with a looped file source (section 17.3)

storage:
  data_dir: "/data"
  event_log_days: 14
  save_clips: false              # 20 s WAV around each candidate, for tuning (private data!)

logging:
  level: "INFO"
```

Validation (fail at startup with a clear message):

- floors unique and non-empty; every route stop is a known floor; at least 2 distinct
  stops; no two consecutive identical stops,
- `anchor.floor` in the route; `route_index` required if it appears more than once,
- 60 < `cycle_seconds` < 3600; all durations > 0; `2 × door_transition < min dwell`,
- `detector.band_hz` within `[0, sample_rate/2]`, `release_db < threshold_db`,
- latitude/longitude in range; `dev_clock_speed > 1` only with a file/loop source.

## 7. Timeline model

Pure functions in `shabbat_elevator_tracker/model.py`, mirrored in `web/model.js` (the web
client lives in the package, `shabbat_elevator_tracker/web/`, so pip installs it). Both
are tested against one fixture, `tests/fixtures/timeline_reference.json` (generated by
the Python version), so they cannot disagree.

### 7.1 Building the cycle

1. Rotate `route` to start at the anchor visit.
2. `dwell_i = dwell_overrides.get(floor, dwell_seconds)`;
   `travel_i = leg_overhead + per_floor × |pos(next) − pos(this)|` (wrapping to stop 0).
3. `raw = Σ dwell + Σ travel`; `scale = cycle_seconds / raw`; multiply every duration by
   `scale`. If `|scale − 1| > 0.10`, warn: the config doesn't describe this elevator.
4. Emit, with offsets relative to the anchor arrival (offset 0):
   - `stop`: `{floor, visit, arrive, door_open = arrive + transition, door_close = depart − transition, depart}`,
   - `move`: `{from, to, direction: "up"|"down", start, end}`.
   The last segment ends exactly at the cycle length.

With the section 6 config, arrivals must equal the section 4.3 table to ±0.5 s (the
table used G = 59.75 s; the config rounds it to 60).

### 7.2 Queries

With `anchor_time` (epoch ms) and cycle `C`:

- `phase(now) = (now − anchor_time) mod C` (non-negative).
- `position(now)`: the containing segment; in `move`, `floor_pos = pos(from) + (pos(to) − pos(from)) × progress`.
- `next_arrival(floor, now)`: smallest `t ≥ now` with `(t − anchor_time) mod C` equal to
  an `arrive` of that floor. If the car is there now, also `here_until = depart`.
- `next_arrivals(now)`: every floor → `{arrive_at, depart_at}`; floors not on the route → `null`.

### 7.3 Validity

A prediction is **valid** only if sync state is `locked` and
`now − last_sync_at < stale_after_seconds`. Otherwise the API still returns the timeline
(so clients can draw the shaft) with `prediction.valid = false` and a `reason`.

## 8. Audio ingest

`ingest.py`, modelled on `record_frigate_audio.py`:

- `ffmpeg -hide_banner -nostdin -loglevel warning [-rtsp_transport tcp -timeout 10000000]
  -i <source> -map 0:a:0 -vn -sn -dn -ac 1 -ar <rate> -c:a pcm_s16le -f s16le pipe:1`.
- Source kinds:
  - URL (`rtsp://`, `http(s)://`, …) → live,
  - `alsa:hw:1` style → local device (`-f alsa`),
  - a file path → read at real time (`-re`),
  - `loop:<directory>` → all WAVs in the directory, in time order, endlessly, at real
    time (concat demuxer + `-stream_loop -1 -re`). For testing (section 17.3).
- asyncio subprocess. Timestamp the first sample of a session with wall-clock arrival
  time, then advance by sample count; > 3 s lag ⇒ samples lost ⇒ re-anchor (same rule as
  the recorder).
- Stream status: `connecting` → `connected` (first PCM) → `lost` (stall, exit, spawn
  failure) → `connecting` after backoff 2, 4, 8 … 60 s (reset after 60 s healthy).
  Track `since`, `last_audio_at`, `reconnects_total`, `last_error` (stderr tail with
  credentials redacted as `//***@`).
- 1 s RMS level in dBFS (debug).
- Never log a source URL with credentials.

### 8.1 Authenticated sources

The service's own API has no password, but the audio source may (the owner's Frigate is
not reachable unauthenticated). Secrets are **only** read from the environment, never
from the config file, and never appear in logs, `/debug`, `/health` or diagnostics:

| `audio.auth.type` | Env vars | Effect |
|---|---|---|
| `none` | — | URL as is. `${VAR}` in `audio.source` is still expanded from the environment. |
| `basic` | `SET_STREAM_USERNAME`, `SET_STREAM_PASSWORD` | RTSP: inserted into the URL (percent-encoded), e.g. go2rtc's `rtsp: username/password`. HTTP(S): `Authorization: Basic …` header. |
| `bearer` | `SET_STREAM_TOKEN` | HTTP(S): `Authorization: Bearer …`. |
| `header` | `SET_STREAM_HEADERS` | HTTP(S): raw extra headers, `Name: value` lines separated by `\n`. |
| `frigate` | `SET_STREAM_USERNAME`, `SET_STREAM_PASSWORD` | Before every (re)connect: `POST login_url` with `{"user","password"}`; the `frigate_token` cookie is sent to ffmpeg as `Cookie:`. For Frigate's authenticated port (8971). |

Recommended for Frigate: set go2rtc RTSP credentials (`go2rtc: rtsp: username/password`
in the Frigate config) and use `basic` with the RTSP restream, the same stream the
recorder used. ffmpeg receives credentials on its command line, which is visible only to
processes in the same container.

## 9. Detector and sync

### 9.1 Features (per 0.1 s frame)

- 2048-sample Hann window, hop 1600 (0.1 s at 16 kHz); scale for other rates.
- Power spectrum summed into 100 Hz bands (80 at 16 kHz) → `10·log10(p + 1e-12)`.
- Background per band: 20th percentile over the last `background_window_seconds`,
  updated every 1 s (ring buffer of 1 s band medians).
- Excess: `clip(dB − background, 0, 40)`; score = mean excess over `detector.band_hz`.

### 9.2 Candidates

- Open at score ≥ `threshold_db`; close after score < `release_db` for ≥ 1 s.
- Keep if `min_event_seconds ≤ duration ≤ max_event_seconds`.
- `{onset (first frame ≥ threshold), duration, peak_db, mean_db, similarity?}`; if a
  signature is set and `similarity < min_similarity` → rejected `"signature"`.
- `anchor_time = onset − offset_seconds`.

### 9.3 Why gating

By day, loud sounds that are not the elevator are common (section 4.2). The anchor is
unambiguous only because it repeats every cycle, so a candidate is trusted only when it
matches the expected phase or is confirmed one to three cycles later.

### 9.4 State machine

States: `idle`, `searching`, `provisional`, `locked`, `paused`.

```
           window opens                 candidate              2nd candidate at k·C ± gate (k ≤ 3)
  idle ───────────────► searching ─────────────► provisional ─────────────────────────► locked
   ▲   (calendar)            ▲                     │ pending expired (> 3·C + gate)        │
   │                         └─────────────────────┘                                       │
   │                                    paused ◄── missed ≥ pause_after_missed (stream ok) ┤
   │                                      │ candidate → provisional (same confirm rule)     │
   └──── window closed and no sync for 2·C (from any state) ◄─────────────────────────────┘
```

- **idle**: outside the calendar's expected window (section 10) and no recent sync.
  Detection keeps running; a confirmed pair of candidates still locks (the calendar
  never overrides what the microphone hears). `reason: "outside_shabbat"`, with
  `next_window_start`.
- **searching**: inside the window (or calendar disabled), no usable anchor.
- **provisional**: has pending candidates (keep ≤ 20). A new candidate at `k·C ± gate`
  (k = 1…3) from any pending one locks to the **newer** of the pair. Pending older than
  `3·C + gate` is dropped; none left → back to searching/idle.
- **locked**: `expected = last_anchor + n·C`. Candidate within `±gate` → **sync**
  (`last_anchor` updated, `missed = 0`, adaptive cycle updated). Others → `unmatched`
  (logged only). `expected + gate` passes with no sync and stream `connected` →
  `missed += 1` (not counted while the stream is lost). `missed ≥ pause_after_missed` →
  **paused**.
- **paused**: prediction invalid (`reason: "elevator_paused"`). Re-lock needs a
  confirmed pair, because the phase after a pause is arbitrary (94 s shift observed).
- **Adaptive cycle**: on a sync `n` cycles (n ≤ 3) after the previous one,
  `C ← 0.8·C + 0.2·(interval/n)`, clamped to `cycle_seconds × (1 ± max_deviation%)`.
- Stream lost: state kept; predictions stay valid until stale and are flagged
  `degraded: "audio_lost"`.

Every transition goes to the event log and the SSE stream.

### 9.5 Manual sync (optional)

`POST /api/v1/sync {"floor": "3", "event": "arrive", "at": <epoch ms, default now>}`, only
if `allow_manual_sync`. Converts to anchor time via the timeline and locks (source
`manual`). For buildings without a microphone, and for tests.

## 10. Calendar (offline)

`calendar.py`, using **`hdate`** (with `astral` installed for exact sun times). No network
access. hdate is what Home Assistant's Jewish Calendar integration uses, so the times
match what the owner already sees in HA.

- **Rest days**: Saturday, or `HDateInfo(date, diaspora).is_yom_tov` (hdate knows Israel
  vs diaspora two-day Yom Tov).
- **Spans**: consecutive rest days merge into one span (e.g. two-day Rosh Hashana
  followed by Shabbat). `start` = candle lighting on the eve (sunset −
  `candle_lighting_minutes`); `end` = `Zmanim(last_day).havdalah` (nightfall at 8.5°, or
  sunset + `havdalah_minutes`).
- **Why sunset**: only as the base of candle lighting (and of havdalah in minutes mode);
  it is not shown in the UI.
- **Issur melacha**: `calendar.now.issur_melacha` = `Zmanim(today).issur_melacha_in_effect(now)`,
  the same boolean HA exposes; in the API, the UI and as an HA binary sensor.
- **Expected elevator window** = `[start − before_start_minutes, end + after_end_minutes]`.
- **Per day info**: Hebrew date (day, month id, year), parsha id of the coming Shabbat
  (hdate `Parasha` enum name, lower-cased, e.g. `chayei_sara`; combined parshiot have
  their own ids), holiday ids (hdate holiday names, e.g. `rosh_hashana_i`, `chanukah`).
  The UI translates ids through its strings files and hides unknown ids.
- Times are computed in `calendar.timezone`, returned as UTC ISO strings.
- Reference check (Givat Shmuel): Fri 2026-11-06 candle lighting **16:30**, the elevator
  started 16:31; Sat havdalah **17:25**, still running at 17:26.

## 11. HTTP API

Base `/api/v1`. JSON, UTF-8. Timestamps as ISO-8601 UTC with milliseconds
(`"2026-11-07T14:32:10.123Z"`) plus `_ms` epoch twins where clients compute with them.
CORS per `cors_origins`. Every response includes `server_time` and `clock_rate`
(1.0 except in dev speed-up mode).

### 11.1 `GET /api/v1/state` — the one endpoint clients need

```json
{
  "api_version": 1,
  "instance_id": "6f1c…",
  "server_time": "2026-11-07T14:32:10.123Z", "server_time_ms": 1794061930123, "clock_rate": 1.0,
  "building": {"name": "Example building", "floors": ["G","1","…","12"], "default_floor": null},
  "stream": {"state": "connected", "since": "…", "last_audio_at": "…", "level_dbfs": -57.2},
  "sync": {"state": "locked", "last_sync_at": "…", "last_sync_at_ms": 1794061800000,
           "anchor_floor": "3", "missed": 0, "source": "audio"},
  "cycle": {"seconds": 404.38, "configured_seconds": 404.4, "adaptive": true},
  "timeline": {
    "anchor_time_ms": 1794061800000,
    "stops": [{"floor":"3","visit":0,"arrive":0,"door_open":2.5,"door_close":7.5,"depart":10.0}],
    "moves": [{"from":"3","to":"1","direction":"down","start":10.0,"end":27.7}]
  },
  "prediction": {
    "valid": true, "reason": null,
    "degraded": null,
    "position": {"kind":"move","floor_pos":4.37,"from":"4","to":"6","direction":"up"},
    "next_arrivals": {"G": {"arrive_at":"…","depart_at":"…"}}
  },
  "calendar": {
    "now": {"in_rest_span": true, "issur_melacha": true, "in_elevator_window": true},
    "current_span": {"start":"…","end":"…","days":[{"date":"2026-11-07","ids":["shabbat"]}]},
    "next_span": {"start":"…","end":"…","window_start":"…","window_end":"…","days":[]},
    "parsha_ids": ["chayei_sara"],
    "hebrew_date": {"day": 27, "month_id": "marcheshvan", "year": 5787, "day_he": "…", "year_he": "…"}
  }
}
```

`reason` values: `no_sync`, `elevator_paused`, `stale`, `outside_shabbat`.

### 11.2 Other endpoints

| Method/path | Purpose |
|---|---|
| `GET /api/v1/floors/{floor}` | `{floor, next_arrive_at, next_depart_at, here_now, seconds_until}`. 404 for unknown floor. |
| `GET /api/v1/stream` | **Server-Sent Events**: `state` (full 11.1 body) on connect and on every change; `ping` every 15 s; `retry: 5000`. |
| `GET /api/v1/health` | `{"ok": true, "version", "api_version", "instance_id", "stream", "sync"}`. 200 whenever the process is up. |
| `GET /api/v1/calendar?days=30` | Rest spans and expected windows for the next N days. |
| `GET /api/v1/events?limit=100` | Recent event-log entries, newest first. |
| `GET /api/v1/debug` | Detector internals: current score, background per band, last 20 candidates with accept/reject reason, pending list, next expected anchor, adaptive-cycle history, ffmpeg stderr tail. 404 if `expose_debug: false`. |
| `POST /api/v1/sync` | Manual sync (9.5). 404 unless enabled. |
| `GET /api/v1/config/public` | Building, route, model, calendar settings (never the audio source). |

With `expose_debug: false`, `stream.level_dbfs` and debug-only fields are omitted.

### 11.3 Versioning

Additive changes keep `api_version: 1`; breaking changes → `/api/v2`. The web client
and HA integration check `api_version` and say "server too old/new" instead of failing
silently.

### 11.4 Push vs poll

Push (SSE) is primary: the interesting changes are rare and irregular (a sync every
~6.7 min, status changes). Polling `/state` every 30–60 s is a valid fallback with
correct results, because predictions are absolute timestamps. SSE rather than
WebSocket: one-way, passes plain proxies and Cloudflare Tunnel, built-in browser
reconnect, trivial in aiohttp.

### 11.5 Clock skew

Clients keep `offset = server_time_ms − local receive time` (minimum-RTT sample of the
last 5 responses) and compute `now = server_time_at_receipt + (local_now −
local_at_receipt) × clock_rate`. Shown in debug.

### 11.6 Internet exposure

Planned: Cloudflare Tunnel with HTTPS. Set `expose_debug: false`, keep
`allow_manual_sync: false`; what remains is read-only. No password.

## 12. Web client

Static files in `web/`, served at `/` by default: `index.html`, `styles.css`, `app.js`,
`model.js`, `api.js` (REST + SSE + clock offset), `wake.js`, `shaft.js`, `i18n.js`,
`i18n/en.json`, `i18n/he.json`, `i18n/fr.json`, `config.js` (default API base; empty =
same origin).

### 12.1 Connecting

- API base: `?api=<url>` (then saved) → saved value → `config.js` → same origin.
- Settings: "API server" field + "Test" button (`/health`).
- SSE; after 3 failures, poll `/state` every 30 s and retry SSE every 5 min. Header dot:
  green = live; amber = server reachable but audio lost or not locked; red = server
  unreachable.
- An `https://` page can't call an `http://` API (mixed content); the Test button
  explains this when it is the cause.

### 12.2 Content (all sizes)

- **Floor selector**: a one-time choice. Until a floor is chosen (no implicit default
  unless `building.default_floor`) the page shows only a big "Choose your floor" grid;
  afterwards it shows "Next arrival at floor **N**" with a pencil button that reopens the
  grid (Cancel / Esc closes it). Saved in `localStorage` per API base. ≥ 44 px targets;
  mouse, touch and keyboard.
- **Next arrival at my floor**: clock time (`14:38:52`) **and** countdown (`3:12`). At the
  floor: "Here now — doors close in 0:06". Ticks every 250 ms locally.
- **Last sync** ("Synced 2 min ago, 14:31:40") and **audio status** ("Audio: live" /
  "Audio lost 4 min ago").
- **Shabbat card**: candle lighting and havdalah of the current/next span, parsha,
  holiday names, Hebrew date; when idle: "Shabbat elevator expected from Fri 16:18".
- Invalid prediction → one clear line instead of the countdown ("Waiting for the first
  sync", "Elevator appears paused", "Outside Shabbat — expected from …", "Last sync too
  old").
- Footer: credits (section 2), project link, version.

### 12.3 Size tiers (container queries + `clamp()` type)

| Tier | Viewport | Layout |
|---|---|---|
| `xs` | width < 360 px **or** height < 500 px | Text only: floor, big countdown, arrival time, one status line, compact Shabbat line. Gear for settings. In landscape (e.g. a small phone on its side): two columns, countdown on one side, status and candle lighting / havdalah on the other, no footer; everything fits without scrolling. |
| `md` | between | Text block + horizontal **route strip** (stops as chips, car position highlighted, my floor marked) + Shabbat card. |
| `lg` | width ≥ 900 **and** height ≥ 600 | Text panel + **shaft illustration**: vertical SVG shaft, labelled floors, car moving smoothly, doors sliding open/closed by `door_open`/`door_close`, direction arrow, my floor highlighted, long G stop visible; route strip; Shabbat card. |

Test sizes: 320×568, 375×667, 390×844, 844×390, 432×295, 568×320, 740×360, 932×430,
768×1024, 1024×768, 1366×768, 1920×1080, 2560×1440 — no horizontal scroll and nothing
clipped, in both themes and all three languages (Hebrew in RTL); short landscape sizes
also without vertical scroll.

### 12.4 Theme

Auto (`prefers-color-scheme`) / Light / Dark, saved. Colours are CSS custom properties;
the illustration uses tokens/`currentColor`. Text contrast ≥ 4.5:1.

### 12.5 Language

- Toggle in settings and header: Auto (from `navigator.languages`) / English / Hebrew /
  French, saved. Each strings file holds its own display name and `"dir"`.
- Hebrew sets `<html lang="he" dir="rtl">`. Layout uses logical CSS properties
  (`margin-inline-start`, `inset-inline-end`, …) so RTL needs no separate stylesheet. The
  shaft stays vertical; panels mirror. Countdown digits stay LTR (`<bdi dir="ltr">`).
- Dates/times via `Intl.DateTimeFormat(lang)`; 24 h by default, 12 h toggle. In Hebrew
  the Hebrew date uses Hebrew numerals for day and year (28 Cheshvan 5787 is written
  with the letters for 28 and 787; computed by hdate, see 11.1).
- The server sends **ids** (parsha, holidays, months, states, reasons); every
  user-visible name comes from the strings files. Each file contains: UI strings, 54
  parsha names, holiday names, Hebrew month names.
- A unit test asserts that all three files have exactly the same keys.

### 12.6 Keep the screen on

- `navigator.wakeLock.request('screen')` in a secure context (HTTPS or `localhost`);
  re-request on `visibilitychange`.
- Fallback for plain-HTTP LAN use and older iOS: a tiny muted looping inline video (data
  URI, < 10 KB) started on the first tap/click.
- Settings shows the active mode ("Screen kept on (Wake Lock)" / "(video fallback)" /
  "Tap anywhere to keep the screen on").
- README documents it: over HTTPS (e.g. Cloudflare Tunnel) Wake Lock works in all modern
  browsers; on `http://<LAN IP>` only the video fallback is available, and it's less
  reliable on some browsers.

### 12.7 Advanced / debug toggle

Off by default, saved. Shows: sync state and source, anchor time, cycle (current vs
configured), missed count, pending candidates, clock offset/RTT/rate, transport (SSE or
poll), stream details (dBFS, reconnects), last 10 events, calendar raw data, raw JSON of
`/state`. If the server hides debug, only client-side items show.

### 12.8 Accessibility

Semantic HTML; `aria-live="polite"` on the status line (not on the ticking countdown);
`prefers-reduced-motion` → no animation (position jumps).

## 13. Home Assistant integration

### 13.1 Location

Same repository: `custom_components/shabbat_elevator_tracker/` plus root `hacs.json`.
HACS accepts this (one integration per repo, other files ignored). The API contract and
the integration stay in lockstep, versioned together.

### 13.2 Push or poll

**Push, with polling fallback** (`iot_class: local_push`). The coordinator holds an SSE
connection through HA's shared aiohttp session; each `state` event updates it. On
disconnect: reconnect with backoff, and poll `/state` every 60 s until SSE is back.
Predictions are absolute timestamps, so HA never needs per-second updates.

### 13.3 Setup

- Config flow: URL → validate with `/health` and `api_version`. Unique id = server
  `instance_id` (UUID created once in the data dir).
- Your floor is chosen in the setup flow; *Configure* (options flow) shows the same floor
  form to change it. One floor per integration entry, no per-floor entities.
- Optional zeroconf (`_shabbat-elevator._tcp`), low priority.

### 13.4 Entities (one device per server)

| Entity | Type | Notes |
|---|---|---|
| `sensor.<name>_next_arrival` | timestamp | Home floor; attributes `floor`, `depart_at`. Unavailable when the prediction is invalid. |
| `sensor.<name>_last_sync` | timestamp | |
| `sensor.<name>_sync_state` | enum | idle / searching / provisional / locked / paused |
| `sensor.<name>_cycle_length` | duration (s) | |
| `binary_sensor.<name>_audio_stream` | connectivity | |
| `binary_sensor.<name>_elevator_running` | running | on when locked |
| `binary_sensor.<name>_shabbat_elevator_window` | — | on inside the expected elevator window; attributes start/end |
| `binary_sensor.<name>_issur_melacha` | — | on from candle lighting to havdalah (hdate) |
| `sensor.<name>_car_position` | text | Estimated floor or "3→1", 5 s local timer, **disabled by default** (recorder churn). |
| Event `shabbat_elevator_tracker_sync` | bus | `{anchor_time, cycle_seconds}` on each sync |

Diagnostics download (redacted), `translations/en.json`, `he.json`, `fr.json`;
`manifest.json` with `version`, `codeowners`, `iot_class`, `config_flow: true`.

The integration's README (`custom_components/shabbat_elevator_tracker/README.md`) has the
entity list, a ready **dashboard** (YAML, sections view: next arrival at my floor,
relative and clock time, hidden while not running; sync state and last sync; issur
melacha, elevator window, running, audio; 24 h history) and an automation example
(notify 60 s before arrival at my floor). The device name follows the building name in
Home Assistant's language.

## 14. Deployment

### 14.1 Docker

- `alpine:3.20` + `apk add python3 py3-numpy py3-aiohttp py3-yaml ffmpeg tini`; pip
  installs the pure-Python deps (`hdate`, `astral`) and the package. Measured: 96 MB
  compressed download, ~280 MB on disk (mostly ffmpeg's codec libraries); multi-arch
  (amd64, arm64).
- Non-root `app` (uid 1000). Volumes `/config` (read-only ok) and `/data`.
- `HEALTHCHECK` → `/api/v1/health`; `ENTRYPOINT ["tini","--"]`,
  `CMD ["python3","-m","shabbat_elevator_tracker","serve"]`.
- `docker-compose.yml` example (`SET_AUDIO_SOURCE`, `TZ`, port 8080).
- GHCR publishing by GitHub Action on tag — later, after the first push.

### 14.2 Proxmox LXC

Two scripts and a step-by-step guide, `docs/proxmox.md`:

- `deploy/proxmox/create-lxc.sh` — run **on the Proxmox host** as root. Arguments with
  defaults: `--ctid` (next free), `--hostname shabbat-elevator`, `--storage local-lvm`,
  `--bridge vmbr0`, `--ip dhcp` (or `10.0.0.50/24 --gw 10.0.0.1`), `--cores 1`,
  `--memory 256`, `--disk 2`. Downloads the Debian 12 template with `pveam` if missing,
  `pct create` (unprivileged, `onboot 1`), starts it, `pct push`es and runs `install.sh`,
  prints the URL. Idempotent where possible; `--dry-run` prints the commands.
- `deploy/lxc/install.sh` — run **inside** the container (or any Debian/Ubuntu host):
  `apt install python3 python3-venv ffmpeg`, user `shabbat-elevator`, venv in
  `/opt/shabbat-elevator-tracker`, `pip install` from a release tarball or git tag,
  config at `/etc/shabbat-elevator-tracker/config.yaml` (from the example if absent),
  data in `/var/lib/shabbat-elevator-tracker`, systemd unit (`Restart=always`,
  `ProtectSystem=strict`, `ReadWritePaths=/var/lib/shabbat-elevator-tracker`). Also
  `update.sh`.
- The guide covers: running the script, editing the config (audio source, location),
  `journalctl -u shabbat-elevator-tracker`, opening the web UI, adding the HA
  integration, updating, and backup (`/etc` + `/var/lib`).

### 14.3 Resource targets

< 5 % of one core and < 120 MB RSS (ffmpeg included) for `serve`.

## 15. CLI tools

`shabbat-elevator-tracker <command>` (also `python -m shabbat_elevator_tracker`):

### 15.1 `serve`

The service.

### 15.2 `record`

Port of `record_frigate_audio.py` (epoch-ms file names, contiguous 30-min WAVs,
`.wav.part` repair, size cap, `--rolling`). New: `--hours 22:30-06:30` records only in
that local-time window (helps keep recordings free of conversations).

### 15.3 `calibrate <dir>` (offline)

Reads recorder WAVs and writes a text report and `calibration.yaml` (suggested
`detector`, `cycle_seconds`, travel parameters). This is the section 4 analysis:

1. Continuity and level per file (gaps, clipping, dropouts, dBFS).
2. Features (9.1) → per-second max excess per band.
3. Activity: 90-min sliding windows, autocorrelation over lags 300–1800 s; report where
   the cycle is present.
4. Cycle: lag maximising mean autocorrelation over the first 40 multiples (0.01 s steps).
5. Anchor: fold active windows by the cycle; strongest phase in the band with the
   highest folded contrast; report band, level, duration, hourly detection rate, and
   random-window false-trigger rate at several thresholds; recommend `threshold_db` and
   a `signature`.
6. Pauses and phase jumps (hourly phase vs. a reference).
7. With `--config`: fold at 0.1 s relative to the anchor across all bands, find the
   faint events of other floors, match them to the route, and least-squares fit
   `leg_overhead_seconds`, `per_floor_seconds`, the dwell overrides, and a common
   remote-sound offset, with `dwell_seconds` fixed from the config (dwell and travel are
   not separable by audio). Print the model-vs-observed table (as in 4.3).

Limits: 29 h of audio in < 5 min, < 1 GB RAM (file by file, keep per-second features).

### 15.4 `replay <dir>`

Feeds recordings through the **live** ingest → detector → sync code at full speed with a
simulated clock (timestamps from file names + sample counts). Prints transitions and
syncs, plus a summary. `--expected <file>` compares to ground truth and exits non-zero
on failure (section 17).

### 15.5 `synth`

Generates synthetic test audio (section 17.1) from a config: route, cycle, anchor sound,
approach sounds for other floors, background noise, distractors, pauses and phase
jumps. Deterministic with `--seed`.

### 15.6 Claude skill (optional, later)

`skills/calibrate-elevator/SKILL.md` wrapping `record` → `calibrate` → config → `replay`
for new buildings. Not part of v1.

## 16. Repository layout

```
shabbat-elevator-tracker/
├─ README.md  LICENSE (GPL-3.0)  pyproject.toml  config.example.yaml
├─ Dockerfile  docker-compose.yml  docker-compose.dev.yml  .dockerignore  hacs.json
├─ shabbat_elevator_tracker/
│  ├─ __main__.py  cli.py  config.py  model.py  features.py  detector.py  sync.py
│  ├─ calendar.py  ingest.py  store.py  server.py  service.py  clock.py
│  ├─ recorder.py  calibrate.py  replay.py  synth.py
│  └─ data/parsha_ids.py  data/holiday_ids.py
│  └─ web/  (+ web/i18n/en.json he.json fr.json)   # inside the package
├─ custom_components/shabbat_elevator_tracker/
├─ deploy/proxmox/create-lxc.sh  deploy/lxc/install.sh update.sh shabbat-elevator-tracker.service
├─ tools/git-hooks/pre-push      # blocks pushing audio / test-data (already in place)
├─ docs/  DESIGN.md  api.md  proxmox.md  screenshots/
├─ tests/
│  ├─ fixtures/timeline_reference.json  fixtures/synthetic/ (generated, tiny)
│  ├─ test_*.py  web/*.test.mjs  ha/ (tools/run-ha-tests.sh)
│  └─ recordings/test_replay_reference.py   # skipped unless test-data/ exists
└─ test-data/                    # LOCAL ONLY, git-ignored, never pushed
   ├─ recordings/  (12 × 30 min WAV, Sat 00:53–06:53)
   └─ expected.json
```

`clock.py` holds the `Clock` abstraction (real, simulated for replay, scaled for dev
speed-up). All time reads in the service go through it.

## 17. Testing

### 17.1 Tier 0 — synthetic (committed, runs in CI)

`synth` builds short deterministic signals: floor-3 anchor bursts (600–4 000 Hz, ~10 s
envelope, +35 dB), low-band approach sounds 7.5 s before other arrivals (+5 dB), pink
background at −60 dBFS, Poisson distractor bursts (doors, speech-band modulated noise),
a pause with a phase jump, a stream gap. Tests feed them through features → detector →
sync. Generated on the fly in tests (no audio committed except, if useful, a few
< 100 KB clips under `tests/fixtures/synthetic/`, which the push hook allows).

### 17.2 Tier 1 — real recordings, fast (local only)

- `test-data/recordings/`: Sat 00:53–06:53 from the reference site — chosen because it
  contains ~2 h running, the 3 h pause, and the resume, and almost no human activity
  (2 non-elevator loud sounds in 6 h). Daytime recordings are not kept (conversations).
- `test-data/expected.json`: ground truth from the offline analysis: 26 anchor onsets
  (00:57:33 … 06:46:27), cycle 404.5 s (σ 0.18), pause 02:58:53 → 06:06:00, and the 2
  non-anchor loud candidates (02:25:03, 06:41:15).
- `pytest -m recordings` (auto-skipped when `test-data/` is absent, e.g. in CI) runs
  `replay` at full speed and asserts:
  - every expected anchor synced within ±1.5 s, except at most 2 needed for the
    confirmations after start and after the pause,
  - **zero** syncs off-phase by more than the gate; the 2 non-anchor candidates are
    `unmatched`,
  - `paused` from ≤ 3 cycles after 02:58:53 until the re-lock, re-locked by 06:20,
  - adaptive cycle within 404.2–404.8 s,
  - runtime < 60 s.
- `calibrate` on the same data reports cycle 404.5 ± 0.3 s, an anchor band overlapping
  600–2 200 Hz, the pause, and the 4.3 fit (± 1.5 s per floor).

### 17.3 Tier 2 — pseudo real time (local only)

- **Direct loop**: `audio.source: "loop:test-data/recordings"`. ffmpeg plays the
  recordings endlessly at real time; timestamps are wall-clock now, so the web client
  and HA see a live-looking elevator. `--loop-start 06:00` starts at a time of day
  (skips into the files). The loop seam is a phase jump: a good re-sync test.
- **Docker loop** (`docker-compose.dev.yml`, service `loop`): one container reading
  `test-data/recordings` as a loop, web client on port 8099. `LOOP_SPEED` (default 10)
  sets the clock speed; Home Assistant needs 1, because it shows arrivals against its
  own real clock. `TRACKER_CONFIG` picks a local config. Both come from a git-ignored
  `.env`.
- **Home Assistant** (`docker-compose.dev.yml`, service `homeassistant`): the integration
  mounted read-only, the dashboard in YAML mode, published on 127.0.0.1 only with a
  password-less login for this machine (`tools/ha-dev/configuration.yaml`).
  `tools/ha-dev/setup.py` onboards, adds the integration (floor 3) and writes the
  dashboard with the device's entity IDs.
- **RTSP loop** (`docker-compose.dev.yml`): `mediamtx` + an ffmpeg publisher looping the
  recordings to `rtsp://mediamtx:8554/elevator`, and the service consuming it. Stopping
  the publisher tests "audio lost" and reconnect.
- **Speed-up**: `server.dev_clock_speed: 10` with a loop source plays audio 10× faster on
  a scaled clock; the API reports `clock_rate: 10`, and the web client runs its countdown
  at that rate, so a full cycle can be watched in ~40 s. Dev only; refused for live sources.
- Calendar in tier 2: the loop runs "now", which may be outside Shabbat; the dev config
  sets `calendar.enabled: false` or a `--fake-now` for the calendar only.

### 17.4 Other tests

- **Model**: section 4.3 table (±0.5 s), wrap-around, duplicate floors in a route,
  floors not on the route, phase arithmetic. JS vs Python fixture ±1 ms.
- **Sync**: table-driven with a fake clock: lock after a confirmed pair; ignore
  off-phase loud events; paused after 3 misses; re-lock after a 94 s phase jump; stream
  loss doesn't count misses; idle outside the window; adaptive clamp.
- **Calendar**: known dates (fixtures computed once and checked by hand against a
  printed luach): 2026-11-06/07 Shabbat (parsha Chayei Sarah, Israel), Rosh Hashana +
  Shabbat merge, Israel vs diaspora Pesach and Shavuot, candle lighting 18 vs 40 min,
  havdalah degrees vs minutes, DST transition weekends.
- **Server**: aiohttp test client; `/state` schema; SSE event on a sync; CORS;
  `expose_debug: false`; `/sync` 404 when disabled.
- **UI**: layout sweep (tools/layout-check.js) at all 12.3 sizes × light/dark × en/he/fr × {valid, waiting, paused,
  idle, audio lost, server down}: no horizontal overflow, key elements visible;
  screenshots to `docs/screenshots/`. Strings-file key parity test. Manual phone check
  for wake lock.
- **HA**: `pytest-homeassistant-custom-component`: config flow success/failure,
  entities, SSE update, polling fallback, unavailable when invalid.
- **Lint**: `ruff`; `node --check` for JS.

## 18. Privacy (public repository)

- Never commit: recordings, clips, `test-data/`, real configs (`config.yaml`), the real
  location, hostnames/IPs/stream URLs with credentials, `state.json`/`events.jsonl`.
- `.gitignore` covers these; `tools/git-hooks/pre-push` refuses any push containing
  audio files or `test-data/` paths (enabled with
  `git config core.hooksPath tools/git-hooks`, already set in the owner's clone;
  CONTRIBUTING notes it).
- The reference building's **timetable** (floors, route, timings, cycle) is not
  sensitive and is the committed example. The example location is the centre of Givat
  Shmuel (city level, owner's choice in rev 3), never a street address.
- Commit author e-mail: the GitHub no-reply address, set repo-locally in the owner's
  clone (`git config user.email <id>+YuvalWS@users.noreply.github.com`). Before the
  first push, check `git log --format='%ae %ce' | sort -u` shows only that address.
- `save_clips` writes real audio; the README warns about it.

## 19. Proxmox setup instructions (deliverable)

When implementation is done, the owner receives `docs/proxmox.md` and
`deploy/proxmox/create-lxc.sh` (section 14.2) and a short walkthrough: one command on the
Proxmox host, edit two config values, open the URL, add the HA integration.

## 20. Milestones

Each ends with its tests green. All nine are implemented in v0.1 (uncommitted until the
owner asks); the first push and tag wait for the owner.

1. **Skeleton**: `pyproject.toml`, layout, `config.py` + validation +
   `config.example.yaml`, `model.py` + tests (4.3 table), `clock.py`, CLI stub.
2. **Offline audio**: `features.py`, `detector.py`, `sync.py`, `synth.py` + tier 0 tests;
   `replay`; tier 1 acceptance on `test-data/`.
3. **Calendar**: `calendar.py` + id tables + tests.
4. **Calibrate + record**: tier 1 calibrate acceptance.
5. **Live service**: `ingest.py` (incl. `loop:`), `store.py`, `service.py`, `server.py`
   (REST + SSE); tier 2 direct loop; then one real cycle on the live go2rtc stream.
6. **Web client**: text tiers, strings files (en/he/fr) + RTL, theme, wake lock, Shabbat
   card, debug panel; then the `lg` shaft illustration; layout checks; speed-up mode.
7. **Packaging**: Dockerfile, compose files (incl. dev RTSP loop), LXC + Proxmox
   scripts, `docs/proxmox.md`; build and run both ways.
8. **Home Assistant integration** + tests + translations + README automation example +
   `hacs.json`.
9. **Docs polish**: README with screenshots, `docs/api.md`; first push and tag only when
   the owner says so.

## 21. Open questions

1. **Elevator window padding**: the elevator started 3 min after candle lighting and was
   still running 3 min after nightfall when the recording ended. Defaults are 10 min
   before / 30 min after; adjust if the building's schedule is known.
2. **12 h / 24 h** default (24 h assumed).

## 22. Implementation model

Sonnet can implement this if it follows the milestones in order and is held to the
acceptance tests (tier 1 on real recordings is the key one). Worth a stronger-model
review: milestone 2 (sync edge cases), milestone 6's `lg` illustration (screenshots),
milestone 8 (HA conventions change between releases; check current developer docs).

## 23. Implementation notes (v0.1)

Changes made while implementing, with the reason:

- **Pair confirmation also compares the sounds.** Two candidates confirm each other only
  if their mean band-excess spectra correlate ≥ `sync.confirm_min_similarity` (0.90,
  Pearson) and their durations differ by ≤ `confirm_max_duration_ratio` (1.6×), within
  `confirm_tolerance_seconds` (3 s, +1 s per extra cycle) instead of the gate. Reference
  site: anchor-to-anchor correlation ≥ 0.983, other loud sounds ≤ 0.84. Synthetic tests
  showed random distractors pairing up during the night pause without this.
- **Learned reference sound.** Every accepted sync updates a reference spectrum (EMA,
  persisted in `state.json`). Locked syncs must correlate ≥ `sync.sync_min_similarity`
  (0.80) with it, and new pairs ≥ 0.90. If three timing-consistent pairs fail only the
  reference check, the reference is assumed outdated and the next pair may lock.
- **Best match in a slot.** If a sound inside the gate is followed by one closer to the
  expected time (a door slam 8 s before the elevator, seen at 02:25 in the reference
  recordings), the later one replaces the sync (`replaces` in the event).
- **Re-phase while locked.** If expected stops are being missed and two off-phase
  candidates confirm each other, the lock moves to the new phase without waiting for
  `paused` (handles small phase steps such as a held door).
- **Miss deadline** = expected + gate + `max_event_seconds` + 2 s, because a candidate is
  reported only when its sound ends.
- **Idle has priority over provisional** in the reported state, so weekday noise doesn't
  show as "confirming"; candidates are still collected and can lock.
- **File and loop sources are paced by the reader**, not ffmpeg's `-readrate` (which ran
  at ~1.8× instead of 10× on Windows). ffmpeg decodes at full speed; pipe back-pressure
  throttles it.
- **Live band sums avoid BLAS.** Matrix products on 1–10 frames made numpy start its
  thread pool on every chunk (240 % CPU); `np.add.reduceat` for small batches and
  `*_NUM_THREADS=1` brought the live service to ~1.3 % of a core and 48 MB RAM.
- **Calibrate keeps the configured dwell times** by default (`--fit-dwell-overrides` to
  fit them): with the lead time of the faint floor sounds as a free parameter, fitting G's
  dwell too is under-determined (it gave G 62 s, travel 14.5 + 1.4/floor). With dwell
  fixed, fitted arrivals match section 4.3 within 0.9 s on every floor; the
  leg-overhead/per-floor split itself is weakly determined (14.3 + 1.6/floor vs 12.1 +
  2.8), which barely changes arrivals because most legs cross two floors.
- **Parsha** shown is that of the Shabbat in the displayed span (after havdalah it is
  already next week's). The **Hebrew date** advances at sunset.
- **URL parameters** `?floor=3&lang=he&theme=dark&clock=12` preset (and save) the web
  client's settings, e.g. for a wall tablet's bookmark.
- **UI checks**: instead of Playwright, `tools/layout-check.js` loads the page in
  same-origin iframes of every test size × theme × language and reports horizontal
  overflow and clipped elements (54 combinations, none failing in v0.1).
- **Docker** doesn't set `SET_CONFIG`: `/config/config.yaml` is used when present,
  otherwise defaults + `SET_*` variables.
- **Reference results (v0.1)**: tier 1 replay 24/26 anchors synced (the other 2 are the
  confirmations), no false syncs, pause detected 03:19:49, re-locked 06:12:54, cycle
  404.45 s, ~30 s for 6 h; calibrate 404.5 s (σ 0.18), band 200–4000 Hz, 29 s.

### 23.1 Rev 5 (2026-11-08)

- **One-time floor picker** (12.2) and **short landscape layout** (12.3): the tier
  threshold moved from height < 420 to < 500 px so landscape phones up to ~930 px wide
  get the compact two-column layout instead of a scrolling `md` page.
- **Hebrew numerals** for the Hebrew date in the Hebrew UI (12.5): the API's
  `hebrew_date` also carries `day_he` / `year_he` from hdate's `hebrew_number` (year
  without the thousands).
- **Per-language building name**: `building.name` may be `{lang: name}`; the web client
  and the HA device pick the current language, then `en`, then the first one.
- **Restore ignores a future snapshot**: a `state.json` whose times are ahead of the clock
  (clock moved back, or saved by a sped-up dev clock and kept in the image's `/data`
  volume) is not restored, apart from the cycle and the reference sound. Seen as "synced
  just now" with a sync time hours ahead.
- **Env overrides keep clock times as strings**: `SET_CFG__audio__loop_start=00:56` was
  read by YAML 1.1 as the base-60 number 56.
- **HA: one floor.** The per-floor sensors and the "additional floors" option are gone:
  the floor is picked in the setup flow and changed under *Configure* with the same form.
- **Plainer status wording**: "Sensor OK" / "Sensor offline" (Hebrew: "sensor OK")
  instead of "Audio: live"; the picker asks for *your* floor.

