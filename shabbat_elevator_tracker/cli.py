"""Command line: serve, record, calibrate, replay, synth, check-config."""

from __future__ import annotations

import os

# numpy's BLAS thread pool costs far more than it saves on this workload (tiny live chunks)
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from . import __version__  # noqa: E402
from .config import ConfigError, load_config  # noqa: E402


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load(args):
    try:
        return load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from e


def cmd_serve(args) -> int:
    cfg = _load(args)
    if args.port:
        cfg.raw["server"]["port"] = args.port
    _setup_logging(cfg.raw["logging"]["level"])
    if not shutil.which("ffmpeg"):
        logging.error("ffmpeg not found on PATH; audio ingest will not work")
    from .server import run_server
    from .service import Service

    svc = Service(cfg)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(run_server(svc))
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass  # Windows
    try:
        loop.run_until_complete(task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        task.cancel()
        try:
            loop.run_until_complete(task)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    finally:
        loop.close()
    return 0


def cmd_record(args) -> int:
    cfg = _load(args)
    _setup_logging("INFO")
    from .recorder import Recorder

    audio = dict(cfg.audio)
    if args.url:
        audio["source"] = args.url
    if args.sample_rate:
        audio["sample_rate"] = args.sample_rate
    ff = shutil.which(args.ffmpeg or "ffmpeg")
    if not ff:
        print("error: ffmpeg not found", file=sys.stderr)
        return 2
    rec = Recorder(audio, Path(args.output), args.segment_minutes, args.max_gb, args.rolling,
                   args.min_free_gb, args.hours, cfg.calendar["timezone"], ff)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: rec.stop("signal"))
    return rec.run()


def cmd_calibrate(args) -> int:
    cfg = _load(args)
    from .calibrate import calibrate, format_report, write_suggestion

    rep = calibrate(args.directory, cfg, progress=not args.quiet, fit=not args.no_fit,
                    fit_overrides=args.fit_dwell_overrides)
    text = format_report(rep, cfg.calendar["timezone"])
    print(text)
    out = Path(args.output)
    if "suggestion" in rep:
        write_suggestion(rep, out)
        print(f"\nwrote {out}")
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    return 0 if "error" not in rep else 1


def cmd_replay(args) -> int:
    cfg = _load(args)
    from .replay import compare_expected, load_expected, run_replay, summary

    res = run_replay(cfg, args.directory, progress=not args.quiet, use_calendar=not args.no_calendar)
    print("\n".join(summary(res, cfg.calendar["timezone"])))
    if args.expected:
        ok, lines = compare_expected(res, load_expected(args.expected), cfg)
        print("\n".join(lines))
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    return 0


def cmd_synth(args) -> int:
    cfg = _load(args)
    from .synth import plan, write_recordings

    pauses = []
    for p in args.pause or []:
        a, b, c = (float(x) for x in p.split(","))
        pauses.append((a, b, c))
    start = dt.datetime.fromisoformat(args.start).timestamp() if args.start else dt.datetime.now().timestamp()
    pl = plan(cfg, args.minutes * 60, seed=args.seed, pauses=pauses,
              distractors_per_hour=args.distractors_per_hour, sample_rate=args.sample_rate)
    paths = write_recordings(args.output, pl, start, seed=args.seed)
    truth = {"anchors": [start + a for a in pl.anchors], "cycle": pl.cycle, "pauses": pl.pauses}
    Path(args.output, "synth_truth.json").write_text(json.dumps(truth, indent=1))
    print(f"wrote {len(paths)} files to {args.output} ({len(pl.anchors)} anchor events)")
    return 0


def cmd_check(args) -> int:
    cfg = _load(args)
    from .calendar import ShabbatCalendar
    from .model import timeline_from_config

    tl = timeline_from_config(cfg)
    print(f"config OK ({cfg.path or 'defaults'})")
    print(f"cycle {tl.cycle} s, model scale {tl.scale:.4f}")
    for w in tl.warnings:
        print("warning:", w)
    print("arrivals after the anchor arrival:")
    for s in tl.stops:
        print(f"  {s.floor:>5}  {s.arrive:7.1f} s  (dwell {s.depart - s.arrive:5.1f} s)")
    cal = ShabbatCalendar.from_config(cfg)
    if cal.enabled:
        import time

        nxt = cal.next_span(time.time()) or cal.current_span(time.time())
        if nxt:
            from zoneinfo import ZoneInfo

            z = ZoneInfo(cfg.calendar["timezone"])
            f = lambda t: dt.datetime.fromtimestamp(t, z).strftime("%a %Y-%m-%d %H:%M")  # noqa: E731
            print(f"next rest span: {f(nxt.start)} -> {f(nxt.end)}; elevator expected {f(nxt.window_start)} -> {f(nxt.window_end)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shabbat-elevator-tracker",
                                description="Predicts Shabbat-elevator arrivals from audio at one floor.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", "-c", help="config file (default: $SET_CONFIG or /config/config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the service (API, web client, ingest)")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("record", help="record the audio source to contiguous WAV files")
    s.add_argument("--output", "-o", default="recordings")
    s.add_argument("--url", help="audio source (default: audio.source / SET_AUDIO_SOURCE)")
    s.add_argument("--hours", help="record only in this local-time window, e.g. 22:30-06:30")
    s.add_argument("--segment-minutes", type=float, default=30)
    s.add_argument("--max-gb", type=float, default=10)
    s.add_argument("--rolling", action="store_true", help="delete the oldest files at the cap")
    s.add_argument("--min-free-gb", type=float, default=1)
    s.add_argument("--sample-rate", type=int)
    s.add_argument("--ffmpeg")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("calibrate", help="find cycle, anchor sound and route timing in recordings")
    s.add_argument("directory")
    s.add_argument("--output", "-o", default="calibration.yaml")
    s.add_argument("--json", help="also write the full report as JSON")
    s.add_argument("--no-fit", action="store_true", help="skip the route timing fit")
    s.add_argument("--fit-dwell-overrides", action="store_true",
                   help="also fit dwell_overrides (default: use the configured dwell times)")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("replay", help="feed recordings through the live pipeline at full speed")
    s.add_argument("directory")
    s.add_argument("--expected", help="ground-truth JSON; exit non-zero on failure")
    s.add_argument("--no-calendar", action="store_true")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("synth", help="write synthetic recordings for testing")
    s.add_argument("--output", "-o", required=True)
    s.add_argument("--minutes", type=float, default=60)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--sample-rate", type=int)
    s.add_argument("--start", help="ISO start time (default: now)")
    s.add_argument("--distractors-per-hour", type=float, default=20)
    s.add_argument("--pause", action="append", help="start_s,length_s,phase_jump_s (repeatable)")
    s.set_defaults(func=cmd_synth)

    s = sub.add_parser("check-config", help="validate the config and print the timeline")
    s.set_defaults(func=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)
