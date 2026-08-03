#!/usr/bin/env python3
"""Reprocess saved OPS243 kiosk IQ captures and enrich SigMF metadata."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def read_iq_txt_with_metadata(
    path: Path,
) -> tuple[list[int], list[int], float | None, float | None]:
    """Read native OPS243 or legacy OpenFlight I/Q text output.

    Native dumps retain the OPS243 command header, ``Hg`` sample-start time,
    ``Ht`` trigger time, and ``#%``-wrapped JSON fragments. The radar's JSON
    arrays use a trailing comma, so that comma is removed before decoding.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    sample_time = None
    trigger_time = None
    fragments: dict[str, list[str]] = {"I": [], "Q": []}
    current_name: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("Hg"):
            sample_time = float(line[2:])
            continue
        if line.startswith("Ht"):
            trigger_time = float(line[2:])
            continue
        if not line.startswith("#%"):
            continue

        fragment = line[2:]
        if fragment.startswith('{"I":['):
            current_name = "I"
        elif fragment.startswith('{"Q":['):
            current_name = "Q"
        if current_name is None:
            continue

        fragments[current_name].append(fragment)
        if fragment.endswith("]}"):
            current_name = None

    if fragments["I"] and fragments["Q"]:
        values: dict[str, list[int]] = {}
        for name in ("I", "Q"):
            payload = "".join(fragments[name]).replace(",]}", "]}", 1)
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid OPS243 {name} array: {error}") from error
            values[name] = [int(value) for value in decoded.get(name, [])]
        i_values, q_values = values["I"], values["Q"]
    else:
        try:
            text = path.read_text(encoding="utf-8")
            i_text, q_text = text.split("\n\nQ:", 1)
        except ValueError as error:
            raise ValueError("I/Q text does not contain native OPS243 or legacy I:/Q: data") from error
        i_values = [
            int(value)
            for value in i_text.removeprefix("I:").replace("\n", ",").split(",")
            if value.strip()
        ]
        q_values = [
            int(value)
            for value in q_text.replace("\n", ",").split(",")
            if value.strip()
        ]

    if len(i_values) != len(q_values):
        raise ValueError(f"I/Q length mismatch: {len(i_values)} != {len(q_values)}")
    return i_values, q_values, sample_time, trigger_time


def read_iq_txt(path: Path) -> tuple[list[int], list[int]]:
    """Read I/Q samples while preserving the original two-value API."""
    i_values, q_values, _, _ = read_iq_txt_with_metadata(path)
    return i_values, q_values


def reprocess_hit(
    hit_dir: Path,
    processor: RollingBufferProcessor,
    ui_record: dict | None = None,
) -> bool:
    meta_path = hit_dir / "capture.sigmf-meta"
    i_path = hit_dir / "iq.txt"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    capture_meta = metadata["captures"][0]
    i_values, q_values, file_sample_time, file_trigger_time = read_iq_txt_with_metadata(i_path)
    sample_time = (
        file_sample_time
        if file_sample_time is not None
        else float(capture_meta.get("openflight:sample_time", 0.0))
    )
    trigger_time = (
        file_trigger_time
        if file_trigger_time is not None
        else float(capture_meta.get("openflight:trigger_time", 0.0))
    )
    capture = IQCapture(
        sample_time=sample_time,
        trigger_time=trigger_time,
        i_samples=i_values,
        q_samples=q_values,
        first_byte_timestamp=capture_meta.get("openflight:host_trigger_timestamp"),
    )
    processed = processor.process_capture(capture)
    if processed is None:
        capture_meta.update(
            {
                "openflight:shot_number": int(re.search(r"hit-(\d+)-", hit_dir.name).group(1)),
                "openflight:reprocessing_status": "no_outbound_signal",
            }
        )
        meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"no usable ball signal {hit_dir}")
        return False

    match = re.search(r"hit-(\d+)-", hit_dir.name)
    shot_number = int(match.group(1)) if match else None
    spin = processed.spin
    fields = {
        "openflight:shot_number": shot_number,
        "openflight:ball_speed_mph": processed.ball_speed_mph,
        "openflight:club_speed_mph": processed.club_speed_mph,
        "openflight:smash_factor": processed.smash_factor,
        "openflight:ball_timestamp_ms": processed.ball_timestamp_ms,
        "openflight:club_timestamp_ms": processed.club_timestamp_ms,
        "openflight:impact_timestamp_ms": processed.impact_timestamp_ms,
        "openflight:impact_source": processed.impact_source,
        "openflight:spin_rpm": spin.spin_rpm if spin else None,
        "openflight:spin_confidence": spin.confidence if spin else None,
        "openflight:spin_quality": spin.quality if spin else None,
        "openflight:spin_snr": spin.snr if spin else None,
        "openflight:spin_modulation_depth": spin.modulation_depth if spin else None,
        "openflight:spin_peak_freq_hz": spin.peak_freq_hz if spin else None,
        "openflight:spin_seam_cycles": spin.seam_cycles if spin else None,
        "openflight:spin_rejection_reason": (
            "unreliable" if spin and not spin.is_reliable else None
        ),
        "openflight:reprocessing_status": "processed",
    }
    if ui_record is not None:
        for name, key in {
            "club_speed_mph": "openflight:club_speed_mph",
            "smash_factor": "openflight:smash_factor",
            "launch_angle_vertical": "openflight:launch_angle_vertical_deg",
            "launch_angle_horizontal": "openflight:launch_angle_horizontal_deg",
            "launch_angle_confidence": "openflight:launch_angle_confidence",
            "carry_spin_adjusted": "openflight:carry_spin_adjusted_yards",
        }.items():
            if ui_record.get(name) is not None:
                fields[key] = ui_record[name]
        if ui_record.get("estimated_carry_yards") is not None:
            fields["openflight:estimated_carry_ui_yards"] = ui_record["estimated_carry_yards"]
        if ui_record.get("spin_rpm") is not None:
            fields["openflight:spin_rpm"] = ui_record["spin_rpm"]
        if ui_record.get("launch_angle_vertical") is not None:
            spin_rpm = ui_record.get("spin_rpm") or 3000.0
            trajectory = simulate(
                LaunchConditions(
                    ball_speed_mph=float(ui_record["ball_speed_mph"]),
                    launch_angle_v=float(ui_record["launch_angle_vertical"]),
                    launch_angle_h=float(ui_record.get("launch_angle_horizontal") or 0.0),
                    spin_rpm=float(spin_rpm),
                    spin_axis_deg=0.0,
                    spin_source="measured" if ui_record.get("spin_rpm") else "club_typical",
                )
            )
            fields.update(
                {
                    "openflight:ballistics_spin_rpm": spin_rpm,
                    "openflight:ballistics_spin_source": "measured" if ui_record.get("spin_rpm") else "club_typical",
                    "openflight:apex_yards": trajectory.apex_yards,
                    "openflight:apex_feet": trajectory.apex_yards * 3.0,
                    "openflight:ballistics_carry_yards": trajectory.carry_yards,
                    "openflight:rollout_yards": trajectory.total_yards - trajectory.carry_yards,
                    "openflight:total_yards": trajectory.total_yards,
                    "openflight:flight_time_s": trajectory.flight_time_s,
                    "openflight:landing_speed_mph": trajectory.landing_speed_mph,
                    "openflight:landing_angle_deg": trajectory.landing_angle_deg,
                    "openflight:lateral_yards": trajectory.lateral_yards,
                }
            )
    capture_meta.update({k: v for k, v in fields.items() if v is not None})
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"updated {hit_dir} -> {processed.ball_speed_mph:.1f} mph")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path, help="session-... directory")
    parser.add_argument("--ui-log", type=Path, help="session JSONL containing shot_detected UI records")
    args = parser.parse_args()

    # SciPy can take a while to load on a cold macOS environment. Print this
    # before importing it so a long startup is not mistaken for stalled work.
    print("Loading signal-processing dependencies...", flush=True)
    global IQCapture, LaunchConditions, RollingBufferProcessor, simulate
    from openflight.ballistics import LaunchConditions, simulate
    from openflight.rolling_buffer.processor import RollingBufferProcessor
    from openflight.rolling_buffer.types import IQCapture

    print("Processing captures...", flush=True)
    processor = RollingBufferProcessor()
    hits = sorted(p for p in args.session.glob("hit-*") if p.is_dir())
    ui_records = []
    if args.ui_log:
        for line in args.ui_log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("type") == "shot_detected":
                ui_records.append(record)
    ui_index = 0
    for hit_dir in hits:
        try:
            # UI records correspond only to accepted shots, while the session
            # directory also contains rejected trigger captures.
            ui_record = ui_records[ui_index] if ui_index < len(ui_records) else None
            if reprocess_hit(hit_dir, processor, ui_record):
                ui_index += 1
        except Exception as error:  # keep processing the remaining shots
            print(f"FAILED {hit_dir}: {error}")


if __name__ == "__main__":
    main()
