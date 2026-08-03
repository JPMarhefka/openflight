#!/usr/bin/env python3
"""Collect raw OPS243 speed-triggered I/Q captures as SigMF recordings."""

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openflight.ops243 import OPS243Radar  # noqa: E402
from openflight.rolling_buffer import RollingBufferProcessor, SpeedTriggeredCapture  # noqa: E402
from openflight.sigmf_capture import DEFAULT_TEMPLATE_PATH, write_sigmf_recording  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="OPS243 serial port (auto-detect when omitted)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "openflight_sessions" / "ops243_sigmf",
        help="Directory for .sigmf-data/.sigmf-meta pairs",
    )
    parser.add_argument(
        "--trigger-speed",
        type=float,
        default=25.0,
        help="Save a capture when outbound speed is strictly greater than this mph value",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the continuous speed-triggered collector until Ctrl-C."""
    args = build_parser().parse_args(argv)
    if args.trigger_speed < 0:
        raise SystemExit("--trigger-speed must be non-negative")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    radar = OPS243Radar(port=args.port)
    processor = RollingBufferProcessor()
    trigger = SpeedTriggeredCapture(min_trigger_speed_mph=args.trigger_speed)
    saved = 0
    failed = 0

    try:
        radar.connect()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"[SIGMF] Ready: port={radar.port}, threshold>{args.trigger_speed:g} mph, "
            f"output={args.output_dir}. Press Ctrl-C to stop."
        )
        while True:
            result = trigger.wait_for_raw_capture(radar, processor, timeout=30.0)
            if result is None:
                continue
            if result.capture is None:
                failed += 1
                print(
                    f"[SIGMF] Trigger {result.trigger_speed_mph:.1f} mph: "
                    f"not saved ({result.failure_reason}, {result.response_bytes} UART bytes)"
                )
                continue

            saved += 1
            data_path, _ = write_sigmf_recording(
                args.output_dir,
                DEFAULT_TEMPLATE_PATH,
                saved,
                result,
                configuration={
                    "trigger": "speed",
                    "trigger_threshold": args.trigger_speed,
                    "pre_trigger_segments": 0,
                    "effective_pre_trigger_segments": 0,
                    "effective_post_trigger_segments": 32,
                    "effective_buffer_segments": 32,
                    "effective_sample_rate_ksps": 30,
                },
            )
            print(
                f"[SIGMF] Saved #{saved}: {data_path.name} "
                f"({result.capture.num_samples} I/Q pairs, trigger {result.trigger_speed_mph:.1f} mph)"
            )
    except KeyboardInterrupt:
        print(f"\n[SIGMF] Stopped. Saved {saved} capture(s); {failed} invalid UART response(s).")
        return 0
    finally:
        radar.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
