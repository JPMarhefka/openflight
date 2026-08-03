"""SigMF serialization for raw OPS243 rolling-buffer captures."""

import json
import logging
import os
import shutil
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Tuple

from .rolling_buffer.trigger import RawCaptureResult
from .rolling_buffer.types import BoardProcessedTimeline
from .shot_visualization import (
    SHOT_PREVIEW_FILENAME,
    board_point_count,
    render_shot_preview_svg,
    update_session_index,
)

OPS243_CENTER_FREQUENCY_HZ = 24_125_000_000
logger = logging.getLogger("openflight.sigmf_capture")
DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "config" / "ops243-iq.sigmf-meta"
BOARD_OUTPUT_FILENAME = "board-speed-magnitude.jsonl"


def recording_basename(capture_index: int, trigger_timestamp: float) -> str:
    """Create a sortable, unique recording base name for one capture."""
    timestamp = datetime.fromtimestamp(trigger_timestamp, timezone.utc)
    return f"ops243-{timestamp.strftime('%Y%m%dT%H%M%S-%fZ')}-{capture_index:04d}"


def create_kiosk_session_directory(output_dir: Path, listener_start_timestamp: float) -> Path:
    """Create one unique, timestamped directory for a kiosk listener run."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.fromtimestamp(listener_start_timestamp, timezone.utc)
    base_name = f"session-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    collision_index = 0
    while True:
        suffix = "" if collision_index == 0 else f"-{collision_index:02d}"
        session_dir = output_dir / f"{base_name}{suffix}"
        try:
            session_dir.mkdir()
            return session_dir
        except FileExistsError:
            collision_index += 1


def _write_temporary(path: Path, content: bytes) -> Path:
    """Write and fsync a hidden sibling file, removing it on every error."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        return temp_path
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _recording_paths(output_dir: Path, capture_index: int, trigger_timestamp: float) -> Tuple[Path, Path]:
    """Return unused pair paths without overwriting an immutable recording."""
    base_name = recording_basename(capture_index, trigger_timestamp)
    collision_index = 0
    while True:
        suffix = "" if collision_index == 0 else f"-{collision_index:02d}"
        data_path = output_dir / f"{base_name}{suffix}.sigmf-data"
        meta_path = output_dir / f"{base_name}{suffix}.sigmf-meta"
        if not data_path.exists() and not meta_path.exists():
            return data_path, meta_path
        collision_index += 1


def _publish_pair_atomically(
    data_path: Path,
    data_content: bytes,
    meta_path: Path,
    meta_content: bytes,
) -> None:
    """Publish a fully written SigMF pair, cleaning up if either half fails."""
    data_temp = _write_temporary(data_path, data_content)
    meta_temp: Path | None = None
    data_published = False
    try:
        meta_temp = _write_temporary(meta_path, meta_content)
        os.replace(data_temp, data_path)
        data_published = True
        os.replace(meta_temp, meta_path)
    except BaseException:
        data_temp.unlink(missing_ok=True)
        if meta_temp is not None:
            meta_temp.unlink(missing_ok=True)
        if data_published:
            data_path.unlink(missing_ok=True)
        raise


def _hit_directory_path(session_dir: Path, capture_index: int, trigger_timestamp: float) -> Path:
    """Return an unused timestamped hit directory within one kiosk session."""
    timestamp = datetime.fromtimestamp(trigger_timestamp, timezone.utc)
    base_name = f"hit-{capture_index:04d}-{timestamp.strftime('%Y%m%dT%H%M%S-%fZ')}"
    collision_index = 0
    while True:
        suffix = "" if collision_index == 0 else f"-{collision_index:02d}"
        hit_dir = session_dir / f"{base_name}{suffix}"
        if not hit_dir.exists():
            return hit_dir
        collision_index += 1


def _publish_hit_atomically(hit_dir: Path, files: dict[str, bytes]) -> None:
    """Publish a complete hit directory or leave no final hit behind."""
    staging_dir = Path(tempfile.mkdtemp(dir=hit_dir.parent, prefix=f".{hit_dir.name}."))
    try:
        for filename, content in files.items():
            destination = staging_dir / filename
            temp_path = _write_temporary(destination, content)
            os.replace(temp_path, destination)
        os.replace(staging_dir, hit_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _interleaved_iq_bytes(i_samples: list[int], q_samples: list[int]) -> bytes:
    """Return unscaled OPS243 ADC values as interleaved ci16_le samples."""
    if len(i_samples) != len(q_samples):
        raise ValueError("I and Q sample counts must match")
    samples = []
    for i_value, q_value in zip(i_samples, q_samples):
        if not 0 <= i_value <= 4095 or not 0 <= q_value <= 4095:
            raise ValueError("OPS243 ADC samples must be 12-bit unsigned values")
        samples.extend((i_value, q_value))
    return struct.pack(f"<{len(samples)}h", *samples)


def _iq_text_bytes(i_samples: list[int], q_samples: list[int]) -> bytes:
    """Return raw ADC samples as separate comma-separated I and Q blocks."""
    if len(i_samples) != len(q_samples):
        raise ValueError("I and Q sample counts must match")
    for i_value, q_value in zip(i_samples, q_samples):
        if not 0 <= i_value <= 4095 or not 0 <= q_value <= 4095:
            raise ValueError("OPS243 ADC samples must be 12-bit unsigned values")
    def wrapped(values: list[int]) -> str:
        lines = [
            ",".join(str(value) for value in values[index : index + 13])
            for index in range(0, len(values), 13)
        ]
        return "\n".join(lines)

    return f"I:{wrapped(i_samples)}\n\nQ:{wrapped(q_samples)}\n".encode("utf-8")


def _sigmf_contents(
    template_path: Path,
    result: RawCaptureResult,
    configuration: Mapping[str, Any] | None = None,
    board_output_file: str | None = None,
) -> Tuple[bytes, bytes]:
    """Build the raw SigMF data and metadata bytes for one complete capture."""
    if result.capture is None:
        raise ValueError("A complete I/Q capture is required for SigMF output")

    capture = result.capture
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if not all(key in template for key in ("global", "captures", "annotations")):
        raise ValueError("SigMF template requires global, captures, and annotations objects")

    trigger_datetime = datetime.fromtimestamp(result.trigger_timestamp, timezone.utc).isoformat()
    trigger_datetime = trigger_datetime.replace("+00:00", "Z")
    metadata: dict[str, Any] = template
    static_capture = dict(template["captures"][0]) if template["captures"] else {}
    static_capture.setdefault("core:frequency", OPS243_CENTER_FREQUENCY_HZ)
    static_capture.update(
        {
            "core:sample_start": 0,
            "core:datetime": trigger_datetime,
            "openflight:sample_time": capture.sample_time,
            "openflight:trigger_time": capture.trigger_time,
            "openflight:host_trigger_timestamp": result.trigger_timestamp,
            "openflight:capture_latency_ms": result.capture_latency_ms,
            "openflight:sample_count": capture.num_samples,
        }
    )
    board_timeline = result.board_timeline or capture.board_timeline
    if board_timeline is None and board_output_file is not None:
        # A valid I/Q-only response still gets an explicit empty/status board
        # artifact, so its SigMF metadata should describe that artifact too.
        board_timeline = BoardProcessedTimeline(
            sample_time=capture.sample_time,
            trigger_time=capture.trigger_time,
        )
    if board_timeline is not None:
        static_capture.update(
            {
                "openflight:board_processing_enabled": board_timeline.processing_enabled,
                "openflight:board_record_count": board_timeline.record_count,
                "openflight:board_point_count": board_point_count(result),
                "openflight:board_unit": board_timeline.units,
                "openflight:board_peak_speed": board_timeline.peak_speed,
                "openflight:board_peak_magnitude": board_timeline.peak_magnitude,
            }
        )
        if board_output_file is not None:
            static_capture["openflight:board_output_file"] = board_output_file
            static_capture["openflight:board_preview_file"] = SHOT_PREVIEW_FILENAME
            static_capture["openflight:board_preview_color_encoding"] = (
                "array_position_1_to_5; Mag1 highest magnitude, Mag5 lowest magnitude"
            )
    if configuration is not None:
        # Keep the complete effective startup configuration with every raw
        # capture.  This is intentionally a custom OpenFlight field rather
        # than a SigMF core field: it describes acquisition provenance, not
        # the signal encoding itself.
        static_capture["openflight:configuration"] = dict(configuration)
    if result.trigger_speed_mph is not None:
        static_capture["openflight:trigger_speed_mph"] = result.trigger_speed_mph
    if result.trigger_threshold_mph is not None:
        static_capture["openflight:trigger_threshold_mph"] = result.trigger_threshold_mph
    metadata["captures"] = [static_capture]
    return (
        _interleaved_iq_bytes(capture.i_samples, capture.q_samples),
        (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _board_timeline_for_result(result: RawCaptureResult) -> BoardProcessedTimeline:
    """Return the parsed board timeline, including an empty fallback."""
    if result.board_timeline is not None:
        return result.board_timeline
    if result.capture is not None and result.capture.board_timeline is not None:
        return result.capture.board_timeline
    return BoardProcessedTimeline(
        sample_time=result.capture.sample_time if result.capture is not None else None,
        trigger_time=result.capture.trigger_time if result.capture is not None else None,
    )


def _board_output_bytes(result: RawCaptureResult) -> bytes:
    """Serialize board speed/magnitude output as graph-friendly JSONL."""
    timeline = _board_timeline_for_result(result)
    header = {
        "type": "ops243_board_speed_magnitude",
        "format_version": 1,
        "processing_enabled": timeline.processing_enabled,
        "sample_time": timeline.sample_time,
        "trigger_time": timeline.trigger_time,
        "record_count": timeline.record_count,
        "unit": timeline.units,
        "metadata": timeline.metadata,
    }
    lines = [json.dumps(header, sort_keys=True)]
    for record in timeline.records:
        payload = {"type": "speed_magnitude", **record.to_dict(timeline.sample_time)}
        lines.append(json.dumps(payload, sort_keys=True))
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_sigmf_recording(
    output_dir: Path,
    template_path: Path,
    capture_index: int,
    result: RawCaptureResult,
    configuration: Mapping[str, Any] | None = None,
) -> Tuple[Path, Path]:
    """Persist one complete raw speed-triggered capture as a SigMF recording."""
    if capture_index < 1:
        raise ValueError("capture_index must be positive")

    output_dir.mkdir(parents=True, exist_ok=True)
    data_path, meta_path = _recording_paths(output_dir, capture_index, result.trigger_timestamp)
    data_content, meta_content = _sigmf_contents(template_path, result, configuration)

    _publish_pair_atomically(
        data_path,
        data_content,
        meta_path,
        meta_content,
    )
    return data_path, meta_path


def write_kiosk_sigmf_hit(
    session_dir: Path,
    template_path: Path,
    capture_index: int,
    result: RawCaptureResult,
    configuration: Mapping[str, Any] | None = None,
) -> Tuple[Path, Path, Path, Path]:
    """Write one kiosk capture as a complete hit directory with all raw artifacts."""
    if result.capture is None:
        raise ValueError("A complete I/Q capture is required for SigMF output")
    if capture_index < 1:
        raise ValueError("capture_index must be positive")

    session_dir.mkdir(parents=True, exist_ok=True)
    hit_dir = _hit_directory_path(session_dir, capture_index, result.trigger_timestamp)
    data_content, meta_content = _sigmf_contents(
        template_path,
        result,
        configuration,
        board_output_file=BOARD_OUTPUT_FILENAME,
    )
    text_content = _iq_text_bytes(result.capture.i_samples, result.capture.q_samples)
    board_output_content = _board_output_bytes(result)
    preview_content = render_shot_preview_svg(
        result,
        title=f"OPS243 board speed / magnitude — {hit_dir.name}",
    )
    _publish_hit_atomically(
        hit_dir,
        {
            "capture.sigmf-data": data_content,
            "capture.sigmf-meta": meta_content,
            "iq.txt": text_content,
            BOARD_OUTPUT_FILENAME: board_output_content,
            SHOT_PREVIEW_FILENAME: preview_content,
        },
    )
    # The hit remains independently usable if an index refresh fails. The
    # index itself is also atomically replaced, so it is never left partial.
    try:
        update_session_index(session_dir)
    except OSError:
        logger.exception("[SIGMF] Failed to refresh session visualization index")
    return (
        hit_dir,
        hit_dir / "capture.sigmf-data",
        hit_dir / "capture.sigmf-meta",
        hit_dir / "iq.txt",
    )


def update_kiosk_sigmf_metadata(meta_path: Path, shot: Any, shot_number: int) -> None:
    """Add the final kiosk/UI shot fields to an existing SigMF metadata file."""
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if not metadata.get("captures"):
        raise ValueError("SigMF metadata has no capture object")

    capture = metadata["captures"][0]
    fields = {
        "openflight:shot_number": shot_number,
        "openflight:ball_speed_mph": shot.ball_speed_mph,
        "openflight:ball_speed_raw_mph": shot.ball_speed_raw_mph,
        "openflight:club_speed_mph": shot.club_speed_mph,
        "openflight:smash_factor": shot.smash_factor,
        "openflight:carry_yards": shot.estimated_carry_yards,
        "openflight:estimated_carry_ui_yards": shot.estimated_carry_yards,
        "openflight:carry_spin_adjusted_yards": shot.carry_spin_adjusted,
        "openflight:launch_angle_vertical_deg": shot.launch_angle_vertical,
        "openflight:launch_angle_horizontal_deg": shot.launch_angle_horizontal,
        "openflight:launch_angle_confidence": shot.launch_angle_confidence,
        "openflight:spin_rpm": shot.spin_rpm,
        "openflight:spin_rpm_measured": shot.spin_rpm_measured,
        "openflight:spin_source": shot.spin_source,
        "openflight:spin_confidence": shot.spin_confidence,
        "openflight:spin_quality": shot.spin_result_quality,
        "openflight:spin_snr": shot.spin_snr,
        "openflight:spin_rejection_reason": shot.spin_rejection_reason,
        "openflight:mode": shot.mode,
    }
    # Reproduce the trajectory values used by the UI when a launch angle is
    # available. These are derived values; raw IQ alone cannot provide angle.
    if shot.launch_angle_vertical is not None:
        try:
            from .ballistics import resolve_launch, simulate

            conditions = resolve_launch(shot)
            if conditions is not None:
                trajectory = simulate(conditions)
                fields.update(
                    {
                        "openflight:ballistics_spin_rpm": conditions.spin_rpm,
                        "openflight:ballistics_spin_source": conditions.spin_source,
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
        except Exception:  # Keep raw capture metadata even if ballistics fails.
            logger.exception("[SIGMF] Failed to calculate trajectory metadata")
    capture.update({key: value for key, value in fields.items() if value is not None})
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The shot number and UI-derived values are added after the raw hit is
    # published, so refresh the session index after enrichment as well.
    try:
        update_session_index(meta_path.parent.parent)
    except OSError:
        logger.exception("[SIGMF] Failed to refresh session visualization index after metadata update")
