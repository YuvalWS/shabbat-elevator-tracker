import asyncio
import base64
import shutil
import wave

import numpy as np
import pytest

from shabbat_elevator_tracker.clock import ScaledClock
from shabbat_elevator_tracker.ingest import (
    AudioIngest,
    AuthError,
    ffmpeg_command,
    loop_playlist,
    redact,
    resolve_source,
)


async def test_none_and_env_expansion():
    src, opts = await resolve_source({"source": "rtsp://${H}:8554/a", "auth": {"type": "none"}}, {"H": "cam"})
    assert src == "rtsp://cam:8554/a" and opts == []


async def test_basic_rtsp_puts_encoded_credentials_in_url():
    env = {"SET_STREAM_USERNAME": "me@x", "SET_STREAM_PASSWORD": "p:w/d"}
    src, opts = await resolve_source({"source": "rtsp://frigate:8554/door", "auth": {"type": "basic"}}, env)
    assert src == "rtsp://me%40x:p%3Aw%2Fd@frigate:8554/door"
    assert opts == []


async def test_basic_http_uses_header():
    env = {"SET_STREAM_USERNAME": "u", "SET_STREAM_PASSWORD": "p"}
    src, opts = await resolve_source({"source": "https://h/s.mp4", "auth": {"type": "basic", "tls_verify": False}}, env)
    assert src == "https://h/s.mp4"
    hdr = opts[opts.index("-headers") + 1]
    assert hdr == "Authorization: Basic " + base64.b64encode(b"u:p").decode() + "\r\n"
    assert "-tls_verify" in opts


async def test_bearer_and_header():
    _, opts = await resolve_source({"source": "http://h/a", "auth": {"type": "bearer"}}, {"SET_STREAM_TOKEN": "tok"})
    assert "Authorization: Bearer tok\r\n" in opts
    _, opts = await resolve_source({"source": "http://h/a", "auth": {"type": "header"}},
                                   {"SET_STREAM_HEADERS": "X-A: 1\\nX-B: 2"})
    assert opts[opts.index("-headers") + 1] == "X-A: 1\r\nX-B: 2\r\n"


async def test_missing_credentials_and_header_on_rtsp():
    with pytest.raises(AuthError):
        await resolve_source({"source": "rtsp://h/a", "auth": {"type": "basic"}}, {})
    with pytest.raises(AuthError):
        await resolve_source({"source": "rtsp://h/a", "auth": {"type": "bearer"}}, {"SET_STREAM_TOKEN": "t"})


async def test_frigate_login(aiohttp_server):
    from aiohttp import web

    seen = {}

    async def login(request):
        seen["body"] = await request.json()
        resp = web.json_response({})
        resp.set_cookie("frigate_token", "jwt123")
        return resp

    app = web.Application()
    app.router.add_post("/api/login", login)
    srv = await aiohttp_server(app)
    url = str(srv.make_url("/api/login"))
    env = {"SET_STREAM_USERNAME": "u", "SET_STREAM_PASSWORD": "p"}
    src, opts = await resolve_source({"source": str(srv.make_url("/api/go2rtc/api/stream.mp4?src=door")),
                                      "auth": {"type": "frigate", "login_url": url}}, env)
    assert seen["body"] == {"user": "u", "password": "p"}
    assert "Cookie: frigate_token=jwt123\r\n" in opts


def test_redact():
    text = "rtsp://u:p@h/x Authorization: Bearer abc Cookie: frigate_token=xyz frigate_token=xyz"
    out = redact(text)
    assert "u:p" not in out and "abc" not in out and "xyz" not in out


def test_ffmpeg_command_kinds(tmp_path):
    c = ffmpeg_command("rtsp://h/a", [], 16000)
    assert "-rtsp_transport" in c and c[-1] == "pipe:1"
    c = ffmpeg_command("alsa:hw:1", [], 16000)
    assert c[c.index("-f") + 1] == "alsa"
    c = ffmpeg_command("loop:x", [], 16000, playlist=tmp_path / "p.txt")
    assert "-stream_loop" in c and "concat" in c


def _write_wav(path, seconds, sr=8000):
    x = (np.random.default_rng(0).standard_normal(int(seconds * sr)) * 300).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())


def test_loop_playlist_rotation(tmp_path):
    t0 = 1794005584141  # Sat 00:53:04 local
    for k in range(3):
        _write_wav(tmp_path / f"{t0 + k * 10_000}.wav", 10)
    pl = loop_playlist(tmp_path, None, "Asia/Jerusalem", tmp_path)
    lines = pl.read_text().splitlines()
    assert lines[0].endswith(f"{t0}.wav'")
    pl = loop_playlist(tmp_path, "00:53", "Asia/Jerusalem", tmp_path)
    assert pl.read_text().splitlines()[0].endswith(f"{t0}.wav'")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_ingest_loop_source_paced_and_scaled(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_wav(rec / "1794005584141.wav", 20)
    clock = ScaledClock(20.0)
    got = []
    ing = AudioIngest({"source": f"loop:{rec.as_posix()}", "sample_rate": 8000, "stall_seconds": 20},
                      clock, lambda x, t, new: got.append((len(x), t, new)), data_dir=str(tmp_path))
    task = asyncio.create_task(ing.run())
    await asyncio.sleep(2.0)  # 2 s real = 40 s virtual at 20x
    await ing.stop()
    await asyncio.wait_for(task, 10)
    assert ing.status.state in ("connected", "connecting", "lost")
    total = sum(n for n, _, _ in got) / 8000
    assert 20 < total < 60, total  # paced to the scaled clock, and looping past the 20 s file
    assert got[0][2] is True
    # timestamps advance with the samples (contiguous session)
    sess = [g for g in got if not g[2]]
    assert sess


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_ingest_reports_lost_for_bad_source(tmp_path):
    clock = ScaledClock(1.0)
    states = []
    ing = AudioIngest({"source": str(tmp_path / "missing.wav"), "sample_rate": 8000,
                       "reconnect_max_backoff_seconds": 1}, clock, lambda *a: None,
                      on_status=lambda s: states.append(s.state))
    task = asyncio.create_task(ing.run())
    await asyncio.sleep(1.5)
    await ing.stop()
    await asyncio.wait_for(task, 10)
    assert "lost" in states
    assert ing.status.reconnects_total >= 1
