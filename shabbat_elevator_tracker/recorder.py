"""`record`: capture the audio source to contiguous WAV files for calibration (section 15.2).

Each file is named after the epoch milliseconds of its first sample; consecutive files
are exactly contiguous. A gap in the stream starts a new file at the right time. Files
are written as `<name>.wav.part` and renamed when closed; a `.part` left by a crash is
repaired on the next start. `--hours 22:30-06:30` records only inside that local-time
window (night recordings are much less likely to contain conversations).

Recordings are private data: keep them out of version control.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path

from .ingest import ffmpeg_command, is_paced_source, redact, resolve_source

HEADER_BYTES = 44
SAMPLE_BYTES = 2
NAME = re.compile(r"\d{13}")
GAP_SECONDS = 3.0
READ_BYTES = 16384

log = logging.getLogger("recorder")


def wav_header(rate: int, data_bytes: int) -> bytes:
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16,
                       1, 1, rate, rate * SAMPLE_BYTES, SAMPLE_BYTES, 16, b"data", data_bytes)


def finalize(part: Path) -> Path | None:
    """Fix the header sizes of a .wav.part and rename it to .wav."""
    size = part.stat().st_size
    data = max(0, size - HEADER_BYTES) // SAMPLE_BYTES * SAMPLE_BYTES
    if data == 0:
        part.unlink()
        return None
    with part.open("r+b") as f:
        rate = struct.unpack("<I", f.read(28)[24:28])[0]
        f.truncate(HEADER_BYTES + data)
        f.seek(0)
        f.write(wav_header(rate, data))
        f.flush()
        os.fsync(f.fileno())
    final = part.with_suffix("")
    part.rename(final)
    return final


def recordings(directory: Path) -> list[Path]:
    return sorted((p for p in directory.glob("*.wav") if NAME.fullmatch(p.stem)), key=lambda p: int(p.stem))


def end_ms(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return int(path.stem) + w.getnframes() * 1000 / w.getframerate()


def parse_hours(spec: str | None) -> tuple[dt.time, dt.time] | None:
    if not spec:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", spec.strip())
    if not m:
        raise ValueError("--hours must look like 22:30-06:30")
    a = dt.time(int(m.group(1)), int(m.group(2)))
    b = dt.time(int(m.group(3)), int(m.group(4)))
    return a, b


def in_hours(window: tuple[dt.time, dt.time] | None, now: dt.datetime) -> bool:
    if window is None:
        return True
    a, b = window
    t = now.time()
    return a <= t < b if a < b else (t >= a or t < b)


class Segment:
    def __init__(self, directory: Path, start_ms: int, rate: int) -> None:
        self.start_ms = start_ms
        self.rate = rate
        self.frames = 0
        self.part = directory / f"{start_ms}.wav.part"
        if self.part.with_suffix("").exists():
            raise FileExistsError(self.part.with_suffix(""))
        self.file = self.part.open("xb")
        self.file.write(wav_header(rate, 0))

    def write(self, pcm: bytes) -> None:
        self.file.write(pcm)
        self.frames += len(pcm) // SAMPLE_BYTES

    def close(self) -> Path | None:
        self.file.close()
        final = finalize(self.part)
        if final:
            log.info("closed %s (%.1f min)", final.name, self.frames / self.rate / 60)
        return final


class Recorder:
    def __init__(self, audio_cfg: dict, output: Path, segment_minutes: float = 30, max_gb: float = 10,
                 rolling: bool = False, min_free_gb: float = 1, hours: str | None = None,
                 tz: str | None = None, ffmpeg: str = "ffmpeg") -> None:
        self.audio_cfg = audio_cfg
        self.out = Path(output)
        self.rate = int(audio_cfg["sample_rate"])
        self.stall = float(audio_cfg.get("stall_seconds", 20))
        self.segment_frames = round(segment_minutes * 60 * self.rate)
        self.segment_bytes = HEADER_BYTES + self.segment_frames * SAMPLE_BYTES
        self.cap = int(max_gb * 1e9)
        self.max_gb = max_gb
        self.rolling = rolling
        self.min_free = int(min_free_gb * 1e9)
        self.min_free_gb = min_free_gb
        self.hours = parse_hours(hours)
        self.tz = None
        if tz:
            from zoneinfo import ZoneInfo

            self.tz = ZoneInfo(tz)
        self.ffmpeg = ffmpeg
        self.stopping = False
        self.reason: str | None = None
        self.exit_code = 0
        self.segment: Segment | None = None
        self.out.mkdir(parents=True, exist_ok=True)
        for part in sorted(self.out.glob("*.wav.part")):
            log.warning("repairing %s left by an unclean stop", part.name)
            finalize(part)
        existing = recordings(self.out)
        self.last_end_ms = end_ms(existing[-1]) if existing else 0
        self.used = sum(p.stat().st_size for p in existing)
        if self.segment_bytes > self.cap:
            raise SystemExit("--max-gb is smaller than a single segment")

    def _now_local(self) -> dt.datetime:
        return dt.datetime.now(self.tz) if self.tz else dt.datetime.now().astimezone()

    def stop(self, reason: str, code: int = 0) -> None:
        if not self.stopping:
            self.stopping, self.reason, self.exit_code = True, reason, code

    def make_room(self) -> bool:
        while self.used + self.segment_bytes > self.cap:
            oldest = recordings(self.out)
            if not self.rolling or not oldest:
                self.stop(f"storage cap of {self.max_gb:g} GB reached")
                return False
            self.used -= oldest[0].stat().st_size
            oldest[0].unlink()
            log.info("deleted %s to stay under the cap", oldest[0].name)
        if shutil.disk_usage(self.out).free < self.segment_bytes + self.min_free:
            self.stop(f"less than {self.min_free_gb:g} GB free on the disk", 1)
            return False
        return True

    def open_segment(self, start_ms: int) -> bool:
        if not self.make_room():
            return False
        self.segment = Segment(self.out, start_ms, self.rate)
        self.used += self.segment_bytes
        return True

    def close_segment(self) -> None:
        if self.segment is None:
            return
        reserved, seg = self.segment_bytes, self.segment
        self.segment = None
        final = seg.close()
        self.used += (final.stat().st_size if final else 0) - reserved
        if final:
            self.last_end_ms = seg.start_ms + seg.frames * 1000 / self.rate

    def _spawn(self) -> subprocess.Popen:
        src, extra = asyncio.run(resolve_source(self.audio_cfg))
        cmd = ffmpeg_command(src, extra, self.rate)
        cmd[0] = self.ffmpeg
        if is_paced_source(src):
            cmd.insert(cmd.index("-i"), "-re")  # a local test file is read at real time
        log.info("connecting to %s", redact(src))
        return subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def session(self) -> float:
        proc = self._spawn()
        chunks: queue.Queue = queue.Queue()

        def reader() -> None:
            data = b"x"
            while data:
                try:
                    data = proc.stdout.read1(READ_BYTES)
                except (OSError, ValueError):
                    data = b""
                chunks.put(data)

        threading.Thread(target=reader, daemon=True).start()
        anchor_ms = None
        frames = 0
        min_lag = None
        carry = b""
        last_data = time.monotonic()
        try:
            while not self.stopping:
                if not in_hours(self.hours, self._now_local()):
                    log.info("outside --hours, closing")
                    break
                try:
                    chunk = chunks.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last_data > self.stall:
                        log.warning("no audio for %ss, reconnecting", self.stall)
                        break
                    continue
                if not chunk:
                    break
                last_data = time.monotonic()
                now_ms = time.time() * 1000
                chunk, carry = carry + chunk, b""
                if len(chunk) % SAMPLE_BYTES:
                    chunk, carry = chunk[:-1], chunk[-1:]
                n = len(chunk) // SAMPLE_BYTES
                chunk_ms = n * 1000 / self.rate
                if anchor_ms is not None:
                    lag = now_ms - (anchor_ms + (frames + n) * 1000 / self.rate)
                    min_lag = lag if min_lag is None else min(min_lag, lag)
                    if lag - min_lag > GAP_SECONDS * 1000:
                        log.warning("%.1fs of audio missing, starting a new file", (lag - min_lag) / 1000)
                        self.close_segment()
                        anchor_ms = None
                if anchor_ms is None:
                    anchor_ms = max(now_ms - chunk_ms, math.ceil(self.last_end_ms))
                    frames, min_lag = 0, None
                pos = 0
                while pos < n and not self.stopping:
                    if self.segment is None and not self.open_segment(round(anchor_ms + frames * 1000 / self.rate)):
                        break
                    take = min(n - pos, self.segment_frames - self.segment.frames)
                    self.segment.write(chunk[pos * SAMPLE_BYTES:(pos + take) * SAMPLE_BYTES])
                    pos += take
                    frames += take
                    if self.segment.frames >= self.segment_frames:
                        self.close_segment()
        finally:
            self.close_segment()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()
        return frames / self.rate

    def run(self) -> int:
        log.info("recording to %s, %d Hz, cap %g GB%s%s", self.out, self.rate, self.max_gb,
                 " (rolling)" if self.rolling else "",
                 f", hours {self.hours[0]:%H:%M}-{self.hours[1]:%H:%M}" if self.hours else "")
        backoff = 2
        while not self.stopping:
            if not in_hours(self.hours, self._now_local()):
                time.sleep(5)
                continue
            try:
                seconds = self.session()
            except Exception as e:  # noqa: BLE001
                log.warning("cannot record: %s", redact(str(e)))
                seconds = 0
            if self.stopping:
                break
            backoff = 2 if seconds >= 60 else min(backoff * 2, 60)
            if in_hours(self.hours, self._now_local()):
                log.warning("stream ended after %.0fs of audio, retrying in %ss", seconds, backoff)
                deadline = time.monotonic() + backoff
                while not self.stopping and time.monotonic() < deadline:
                    time.sleep(0.5)
        log.info("stopped: %s; %.2f GB in %s", self.reason, self.used / 1e9, self.out)
        return self.exit_code
