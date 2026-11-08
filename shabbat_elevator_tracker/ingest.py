"""Live audio ingest through an ffmpeg subprocess (section 8).

Sources: rtsp/http(s)/any ffmpeg URL, `alsa:<device>`, a file path (played at real time),
or `loop:<directory>` (recorder WAVs in time order, endlessly, at real time x clock rate).
Credentials for the source come only from the environment (section 8.1) and are never
logged.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import os
import re
import tempfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import numpy as np

from .clock import Clock
from .config import expand_env_refs

log = logging.getLogger(__name__)

LAG_RESYNC_SECONDS = 3.0
_CRED_RE = re.compile(r"//[^@/\s]*@")
_SECRET_HEADER_RE = re.compile(r"(?i)(authorization|cookie):[^\r\n]*")


def redact(text: str) -> str:
    text = _CRED_RE.sub("//***@", text or "")
    text = _SECRET_HEADER_RE.sub(lambda m: m.group(1) + ": ***", text)
    return re.sub(r"(?i)(frigate_token=)[^;\s]+", r"\1***", text)


class AuthError(RuntimeError):
    pass


def _url_with_userinfo(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def frigate_login(login_url: str, user: str, password: str, tls_verify: bool = True) -> str:
    """POST to Frigate's /api/login; returns the frigate_token cookie value."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s, s.post(
        login_url, json={"user": user, "password": password}, ssl=None if tls_verify else False
    ) as r:
        if r.status != 200:
            raise AuthError(f"Frigate login failed: HTTP {r.status}")
        tok = r.cookies.get("frigate_token")
        if tok is None:
            raise AuthError("Frigate login succeeded but returned no frigate_token cookie")
        return tok.value


async def resolve_source(audio_cfg: dict, env: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Return (input url, extra ffmpeg input options) with credentials applied."""
    env = dict(os.environ) if env is None else env
    src = expand_env_refs(audio_cfg.get("source") or "", env)
    auth = audio_cfg.get("auth") or {}
    typ = auth.get("type", "none")
    opts: list[str] = []
    scheme = src.split("://", 1)[0].lower() if "://" in src else ""
    is_http = scheme in ("http", "https")
    user, pwd = env.get("SET_STREAM_USERNAME", ""), env.get("SET_STREAM_PASSWORD", "")
    headers: list[str] = []
    if typ == "basic":
        if not user:
            raise AuthError("auth type 'basic' needs SET_STREAM_USERNAME (and SET_STREAM_PASSWORD)")
        if is_http:
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            headers.append(f"Authorization: Basic {token}")
        else:
            src = _url_with_userinfo(src, user, pwd)
    elif typ == "bearer":
        tok = env.get("SET_STREAM_TOKEN", "")
        if not tok:
            raise AuthError("auth type 'bearer' needs SET_STREAM_TOKEN")
        headers.append(f"Authorization: Bearer {tok}")
    elif typ == "header":
        raw = env.get("SET_STREAM_HEADERS", "").replace("\\n", "\n")
        headers += [h.strip() for h in raw.splitlines() if h.strip()]
        if not headers:
            raise AuthError("auth type 'header' needs SET_STREAM_HEADERS")
    elif typ == "frigate":
        if not user:
            raise AuthError("auth type 'frigate' needs SET_STREAM_USERNAME and SET_STREAM_PASSWORD")
        login_url = expand_env_refs(auth.get("login_url") or "", env)
        tok = await frigate_login(login_url, user, pwd, bool(auth.get("tls_verify", True)))
        headers.append(f"Cookie: frigate_token={tok}")
    if headers:
        if not is_http:
            raise AuthError(f"auth type '{typ}' sends HTTP headers and needs an http(s) source")
        opts += ["-headers", "".join(h + "\r\n" for h in headers)]
    if is_http and not auth.get("tls_verify", True):
        opts += ["-tls_verify", "0"]
    return src, opts


def loop_playlist(directory: str | Path, start_hhmm: str | None = None, tz: str = "UTC",
                  out_dir: str | Path | None = None) -> Path:
    """ffmpeg concat list of recorder WAVs, rotated to start at a local time of day."""
    from .replay import recording_files

    files = recording_files(directory)
    if not files:
        raise FileNotFoundError(f"loop: no recorder WAVs (<epoch_ms>.wav) in {directory}")
    inpoint = 0.0
    if start_hhmm:
        from zoneinfo import ZoneInfo

        hh, mm = (int(x) for x in start_hhmm.split(":"))
        z = ZoneInfo(tz)
        best = 0
        for i, (_p, t0) in enumerate(files):
            local = dt.datetime.fromtimestamp(t0, z)
            target = local.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
            nxt = files[i + 1][1] if i + 1 < len(files) else t0 + 1800
            if t0 <= target < nxt:
                best, inpoint = i, target - t0
                break
        files = files[best:] + files[:best]
    out = Path(out_dir or tempfile.gettempdir()) / "set_loop_playlist.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (p, _t) in enumerate(files):
        lines.append(f"file '{p.resolve().as_posix()}'")
        if i == 0 and inpoint > 0:
            lines.append(f"inpoint {inpoint:.3f}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def is_paced_source(src: str) -> bool:
    """File and loop sources: the reader paces them to the (possibly scaled) clock."""
    if src.startswith("loop:") or src.startswith("file://"):
        return True
    return "://" not in src and not src.startswith("alsa:")


def ffmpeg_command(src: str, extra: list[str], sample_rate: int, rate: float = 1.0,
                   playlist: Path | None = None) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]
    # file and loop sources are decoded at full speed and paced by the reader (AudioIngest),
    # which also handles the dev speed-up; ffmpeg's -readrate is unreliable on some platforms
    if playlist is not None:
        cmd += ["-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i", str(playlist)]
    elif src.startswith("alsa:"):
        cmd += ["-f", "alsa", "-i", src[len("alsa:"):]]
    elif "://" in src and not src.startswith("file://"):
        if src.lower().startswith(("rtsp://", "rtsps://")):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        else:
            cmd += ["-rw_timeout", "15000000"]
        cmd += extra + ["-i", src]
    else:
        cmd += ["-i", src[len("file://"):] if src.startswith("file://") else src]
    cmd += ["-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
    return cmd


class StreamStatus:
    def __init__(self) -> None:
        self.state = "connecting"
        self.since: float | None = None
        self.last_audio_at: float | None = None
        self.reconnects_total = 0
        self.last_error: str | None = None
        self.level_dbfs: float | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)

    def as_dict(self, debug: bool = True) -> dict:
        from .calendar import iso

        d = {
            "state": self.state,
            "since": iso(self.since),
            "last_audio_at": iso(self.last_audio_at),
            "last_audio_at_ms": int(self.last_audio_at * 1000) if self.last_audio_at else None,
            "reconnects_total": self.reconnects_total,
        }
        if debug:
            d["level_dbfs"] = None if self.level_dbfs is None else round(self.level_dbfs, 1)
            d["last_error"] = self.last_error
        return d


class AudioIngest:
    """Runs ffmpeg, keeps it alive, timestamps PCM and hands it to `on_audio`."""

    def __init__(self, audio_cfg: dict, clock: Clock, on_audio: Callable[[np.ndarray, float, bool], None],
                 on_status: Callable[[StreamStatus], None] | None = None, tz: str = "UTC",
                 data_dir: str | None = None) -> None:
        self.cfg = audio_cfg
        self.clock = clock
        self.on_audio = on_audio  # (samples, t_first_sample, new_session)
        self.on_status = on_status
        self.sr = int(audio_cfg["sample_rate"])
        self.status = StreamStatus()
        self.tz = tz
        self.data_dir = data_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()

    def _set_state(self, state: str, error: str | None = None) -> None:
        if error is not None:
            self.status.last_error = redact(error)[-500:]
        if state != self.status.state:
            self.status.state = state
            self.status.since = self.clock.now()
            log.info("audio stream %s%s", state, f": {self.status.last_error}" if error else "")
            if self.on_status:
                self.on_status(self.status)

    async def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.returncode is None:
            self._proc.kill()

    async def run(self) -> None:
        backoff = 2.0
        max_backoff = float(self.cfg.get("reconnect_max_backoff_seconds", 60))
        while not self._stop.is_set():
            self._set_state("connecting")
            started = self.clock.now()
            try:
                await self._run_once()
                err = "ffmpeg exited"
            except AuthError as e:
                err = str(e)
            except FileNotFoundError as e:
                err = f"cannot start: {e}"
            except Exception as e:  # noqa: BLE001 - keep the service alive
                err = f"{type(e).__name__}: {e}"
            if self._stop.is_set():
                break
            self._set_state("lost", err)
            self.status.reconnects_total += 1
            if (self.clock.now() - started) / max(self.clock.rate, 1e-9) > 60:
                backoff = 2.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, max_backoff)

    async def _run_once(self) -> None:
        src_cfg = self.cfg.get("source") or ""
        playlist = None
        rate = self.clock.rate
        if src_cfg.startswith("loop:"):
            playlist = loop_playlist(src_cfg[5:], self.cfg.get("loop_start"), self.tz, self.data_dir)
            src, extra = src_cfg, []
        else:
            if not src_cfg:
                raise AuthError("no audio source configured (audio.source or SET_AUDIO_SOURCE)")
            src, extra = await resolve_source(self.cfg)
        cmd = ffmpeg_command(src, extra, self.sr, rate, playlist)
        self._paced = is_paced_source(src_cfg)
        log.info("starting ffmpeg for %s", redact(src))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stderr_task = asyncio.create_task(self._read_stderr(self._proc))
        try:
            await self._pump(self._proc)
        finally:
            if self._proc.returncode is None:
                self._proc.kill()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
                await asyncio.wait_for(stderr_task, 2)  # drain to EOF so the pipes close
                await asyncio.wait_for(self._proc.stdout.read(), 2)
                transport = getattr(self._proc, "_transport", None)
                if transport is not None:
                    transport.close()  # Windows proactor otherwise closes it after the loop
            except (TimeoutError, asyncio.CancelledError):
                stderr_task.cancel()

    async def _read_stderr(self, proc) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = redact(line.decode(errors="replace").rstrip())
            if text:
                self.status.stderr_tail.append(text)
                self.status.last_error = text[-500:]

    async def _pump(self, proc) -> None:
        assert proc.stdout is not None
        stall = float(self.cfg.get("stall_seconds", 20)) / max(self.clock.rate, 1e-9)
        chunk_bytes = int(self.sr * 0.1) * 2 * max(1, int(round(self.clock.rate)))
        t0: float | None = None
        n = 0
        leftover = b""
        level_acc: list[float] = []
        while not self._stop.is_set():
            try:
                data = await asyncio.wait_for(proc.stdout.read(chunk_bytes), timeout=stall)
            except TimeoutError:
                self.status.last_error = f"no audio for {stall:.0f} s"
                return
            if not data:
                return
            data = leftover + data
            cut = len(data) - (len(data) % 2)
            leftover = data[cut:]
            samples = np.frombuffer(data[:cut], dtype="<i2")
            if len(samples) == 0:
                continue
            now = self.clock.now()
            new_session = False
            if t0 is None:
                t0 = now - len(samples) / self.sr
                n = 0
                new_session = True
                self._set_state("connected")
            elif getattr(self, "_paced", False):
                # pace a file/loop source: wait until the clock reaches the end of this chunk
                target = t0 + (n + len(samples)) / self.sr
                if target > now:
                    await asyncio.sleep((target - now) / max(self.clock.rate, 1e-9))
                    now = self.clock.now()
            else:
                expected_end = t0 + (n + len(samples)) / self.sr
                if abs(now - expected_end) > LAG_RESYNC_SECONDS * max(1.0, self.clock.rate):
                    t0 = now - len(samples) / self.sr
                    n = 0
                    new_session = True
            t_first = t0 + n / self.sr
            n += len(samples)
            self.status.last_audio_at = now
            x = samples.astype(np.float32) / 32768.0
            level_acc.append(float(np.mean(x * x)))
            if len(level_acc) >= 10:
                self.status.level_dbfs = 10 * np.log10(np.mean(level_acc) + 1e-12)
                level_acc = []
            self.on_audio(samples, t_first, new_session)
