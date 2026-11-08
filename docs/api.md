# HTTP API (v1)

Base path `/api/v1`. JSON, UTF-8, no authentication. Timestamps are ISO-8601 UTC strings
(`"2026-11-07T14:32:10.123Z"`) with `_ms` epoch-millisecond twins where clients compute
with them. CORS follows `server.cors_origins`. Additive changes keep `api_version: 1`.

## `GET /state`

Everything a client needs. Clients compute positions and countdowns themselves from
`timeline` (+ `server_time_ms` and `clock_rate` for clock offset), so the server only
needs to push when something changes.

```json
{
  "api_version": 1, "version": "0.1.0", "instance_id": "6f1c…",
  "server_time": "2026-11-07T11:16:40.000Z", "server_time_ms": 1794061000000, "clock_rate": 1.0,
  "building": {"name": "Example building", "floors": ["G", "1", "…", "12"], "default_floor": null},
  "stream": {"state": "connected", "since": "…", "last_audio_at": "…", "last_audio_at_ms": 0,
             "reconnects_total": 0, "level_dbfs": -63.4, "last_error": null},
  "sync": {"state": "locked", "last_sync_at": "…", "last_sync_at_ms": 0, "anchor_floor": "3",
           "missed": 0, "source": "audio", "paused_since": null,
           "pending": 0, "syncs_total": 12, "next_expected_at": "…"},
  "cycle": {"seconds": 404.45, "configured_seconds": 404.4, "adaptive": true},
  "timeline": {
    "anchor_time_ms": 1794060800000, "cycle_seconds": 404.45,
    "stops": [{"floor": "3", "visit": 0, "arrive": 0.0, "door_open": 2.5, "door_close": 7.5, "depart": 10.0}],
    "moves": [{"from": "3", "to": "1", "direction": "down", "start": 10.0, "end": 27.7}]
  },
  "prediction": {
    "valid": true, "reason": null, "degraded": null,
    "position": {"kind": "move", "from": "4", "to": "6", "direction": "up", "floor_pos": 4.37, "phase": 170.2},
    "next_arrivals": {"G": {"arrive_at": "…", "arrive_at_ms": 0, "depart_at": "…", "depart_at_ms": 0, "here_now": false}}
  },
  "calendar": {
    "enabled": true,
    "now": {"in_rest_span": true, "issur_melacha": true, "in_elevator_window": true},
    "current_span": {"start": "…", "end": "…", "start_ms": 0, "end_ms": 0,
                     "window_start": "…", "window_end": "…",
                     "days": [{"date": "2026-11-07", "ids": ["shabbat"]}]},
    "next_span": {"…": "…"},
    "parsha_ids": ["chayei_sara"], "today_ids": ["shabbat"],
    "hebrew_date": {"day": 27, "month_id": "marcheshvan", "year": 5787, "day_he": "…", "year_he": "…"},
    "timezone": "Asia/Jerusalem"
  },
  "debug_enabled": true
}
```

- `sync.state`: `idle` (outside the expected Shabbat window), `searching`, `provisional`
  (one candidate heard, waiting for confirmation), `locked`, `paused` (expected stops
  missed: the elevator stopped).
- `prediction.reason` when not valid: `no_sync`, `elevator_paused`, `stale`, `outside_shabbat`.
- `prediction.degraded`: `audio_lost` while the stream is down but the last sync is fresh.
- Timeline offsets are seconds after the anchor floor's arrival; floor `t` arrival times
  repeat every `cycle_seconds` from `anchor_time_ms`.
- `calendar.*_ids` are ids (parsha, holiday, month); clients translate them.
- With `server.expose_debug: false`, `stream.level_dbfs`, `stream.last_error`,
  `sync.pending`, `sync.syncs_total` and `sync.next_expected_at` are omitted.

## `GET /stream`

Server-Sent Events. `event: state` with the full `/state` body on connect and on every
change (sync, state transition, stream status, calendar flags); `event: ping` every 15 s;
`retry: 5000`.

## Other endpoints

| Method / path | Returns |
|---|---|
| `GET /floors/{floor}` | `{floor, valid, reason, next_arrive_at, next_depart_at, here_now, here_until, seconds_until, on_route}`; 404 for an unknown floor |
| `GET /health` | `{ok, version, api_version, instance_id, stream: {state}, sync: {state}}` |
| `GET /calendar?days=30` | `{enabled, spans: [...]}`: rest spans and expected elevator windows |
| `GET /events?limit=100` | `{events: [...]}`: recent events, newest first (transitions, syncs, misses, pauses, stream changes; candidates only with debug) |
| `GET /config/public` | building, route and timing model, calendar rules (never the audio source or the location) |
| `GET /debug` | detector score, background, recent candidates, pending, cycle history, ffmpeg stderr tail (404 when `expose_debug: false`) |
| `POST /sync` | manual sync `{"floor": "3", "event": "arrive"|"depart", "at": <epoch ms, default now>}` (404 unless `allow_manual_sync: true`) |

## Clock handling

Compute `server_now = server_time_ms + (local_now − local_receipt) × clock_rate`; take the
offset from the response with the smallest round trip. `clock_rate` is 1 except in the
dev speed-up mode (`server.dev_clock_speed` with a looped recording).
