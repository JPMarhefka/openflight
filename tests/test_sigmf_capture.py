"""Tests for OPS243 SigMF raw-capture serialization."""

import importlib.util
import json
import struct
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openflight.launch_monitor import ClubType, Shot
from openflight.rolling_buffer import (
    BoardProcessedTimeline,
    BoardSpeedMagnitudeRecord,
    IQCapture,
    RawCaptureResult,
    SpeedTriggeredCaptureResult,
)
from openflight.rolling_buffer.monitor import RollingBufferMonitor
from openflight.shot_visualization import SHOT_PREVIEW_FILENAME
from openflight.sigmf_capture import (
    BOARD_OUTPUT_FILENAME,
    OPS243_CENTER_FREQUENCY_HZ,
    _iq_text_bytes,
    create_kiosk_session_directory,
    update_kiosk_sigmf_metadata,
    write_kiosk_sigmf_hit,
    write_sigmf_recording,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "config" / "ops243-iq.sigmf-meta"


def _result() -> SpeedTriggeredCaptureResult:
    return SpeedTriggeredCaptureResult(
        capture=IQCapture(
            sample_time=100.0,
            trigger_time=100.015,
            i_samples=[0, 2048, 4095],
            q_samples=[4095, 2048, 0],
            board_timeline=BoardProcessedTimeline(
                records=[
                    BoardSpeedMagnitudeRecord(
                        timestamp=100.020,
                        unit="mps",
                        magnitudes=[12.5, 9.0],
                        speeds=[-0.7, 0.2],
                    )
                ],
                metadata=[{"FFTSize": 1024}],
                sample_time=100.0,
                trigger_time=100.015,
                processing_enabled=True,
            ),
        ),
        trigger_speed_mph=42.5,
        trigger_timestamp=1_700_000_000.125,
        response_bytes=1234,
        capture_latency_ms=250.0,
        trigger_threshold_mph=25.0,
        trigger_type="speed",
    )


def test_sigmf_recording_preserves_interleaved_iq_and_metadata(tmp_path):
    """Raw ADC counts must remain unchanged in SigMF ci16_le order."""
    data_path, meta_path = write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, _result())

    assert data_path.with_suffix(".sigmf-meta") == meta_path
    assert struct.unpack("<6h", data_path.read_bytes()) == (0, 4095, 2048, 2048, 4095, 0)
    assert len(data_path.read_bytes()) == 3 * 2 * 2

    metadata = json.loads(meta_path.read_text())
    template = json.loads(TEMPLATE_PATH.read_text())
    assert set(metadata) == {"global", "captures", "annotations"}
    assert metadata["global"]["core:datatype"] == "ci16_le"
    assert metadata["global"]["core:sample_rate"] == 30000
    assert metadata["global"]["core:hw"] == template["global"]["core:hw"]
    assert metadata["global"]["core:recorder"] == template["global"]["core:recorder"]
    assert metadata["global"]["core:version"] == template["global"]["core:version"]
    capture = metadata["captures"][0]
    assert capture["core:sample_start"] == 0
    assert template["captures"][0]["core:frequency"] == OPS243_CENTER_FREQUENCY_HZ
    assert capture["core:frequency"] == template["captures"][0]["core:frequency"]
    assert capture["openflight:sample_count"] == 3
    assert capture["openflight:trigger_type"] == "speed"
    assert capture["openflight:trigger_speed_mph"] == 42.5
    assert capture["openflight:trigger_threshold_mph"] == 25.0


def test_sigmf_recording_includes_effective_capture_configuration(tmp_path):
    """Each raw capture records the settings needed to reproduce its window."""
    configuration = {
        "trigger": "hardware",
        "trigger_threshold": 25.0,
        "pre_trigger_segments": 4,
        "effective_pre_trigger_segments": 4,
        "effective_post_trigger_segments": 28,
        "effective_sample_rate_ksps": 30,
    }

    _, meta_path = write_sigmf_recording(
        tmp_path,
        TEMPLATE_PATH,
        1,
        _result(),
        configuration=configuration,
    )

    capture = json.loads(meta_path.read_text())["captures"][0]
    assert capture["openflight:configuration"] == configuration


def test_hardware_sigmf_recording_omits_unreported_trigger_speed(tmp_path):
    """A board ST trigger has a threshold but no serial speed-reading value."""
    result = RawCaptureResult(
        capture=IQCapture(
            sample_time=100.0,
            trigger_time=100.068,
            i_samples=[0, 2048, 4095],
            q_samples=[4095, 2048, 0],
        ),
        trigger_speed_mph=None,
        trigger_timestamp=1_700_000_000.125,
        response_bytes=1234,
        capture_latency_ms=250.0,
        trigger_threshold_mph=25.0,
        trigger_type="hardware",
    )

    _, meta_path = write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, result)
    capture = json.loads(meta_path.read_text())["captures"][0]

    assert capture["openflight:trigger_type"] == "hardware"
    assert "openflight:trigger_speed_mph" not in capture
    assert capture["openflight:trigger_threshold_mph"] == 25.0


def test_sigmf_recording_rejects_mismatched_or_invalid_adc_values(tmp_path):
    """Invalid raw I/Q data cannot silently become a malformed recording."""
    result = _result()
    assert result.capture is not None
    result.capture.q_samples = [0]
    with pytest.raises(ValueError, match="counts must match"):
        write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, result)

    result.capture.q_samples = [0, 1, 2]
    result.capture.i_samples[0] = 4096
    with pytest.raises(ValueError, match="12-bit"):
        write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, result)
    assert not list(tmp_path.iterdir())


def test_sigmf_recording_never_overwrites_a_same_timestamp_capture(tmp_path):
    """A restarted process with the same timestamp/index keeps prior raw data immutable."""
    first_data, first_meta = write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, _result())
    second_data, second_meta = write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, _result())

    assert first_data != second_data
    assert first_meta != second_meta
    assert first_data.read_bytes() == second_data.read_bytes()
    assert len(list(tmp_path.glob("*.sigmf-data"))) == 2
    assert len(list(tmp_path.glob("*.sigmf-meta"))) == 2


def test_kiosk_hit_directory_contains_sigmf_pair_and_raw_iq_text(tmp_path):
    """One kiosk hit publishes SigMF, raw I/Q, and board-output artifacts."""
    session_dir = create_kiosk_session_directory(tmp_path, 1_700_000_000.0)
    hit_dir, data_path, meta_path, text_path = write_kiosk_sigmf_hit(
        session_dir,
        TEMPLATE_PATH,
        1,
        _result(),
    )

    assert session_dir.name == "session-20231114T221320Z"
    assert hit_dir.name == "hit-0001-20231114T221320-125000Z"
    assert {path.name for path in hit_dir.iterdir()} == {
        "capture.sigmf-data",
        "capture.sigmf-meta",
        "iq.txt",
        BOARD_OUTPUT_FILENAME,
        SHOT_PREVIEW_FILENAME,
    }
    assert (session_dir / "index.html").is_file()
    preview = (hit_dir / SHOT_PREVIEW_FILENAME).read_text()
    assert "Capture-relative time (ms)" in preview
    assert "Absolute speed (mps)" in preview
    assert preview.count('data-array-index="') == 2
    assert "Mag1, time 20.000 ms, speed 0.700 mps" in preview
    assert "speed -0.700 mps" not in preview
    assert "Mag1" in preview and "Mag5" in preview
    assert "highest magnitude → lowest" in preview

    update_kiosk_sigmf_metadata(
        meta_path,
        Shot(
            ball_speed_mph=70.2,
            timestamp=datetime.now(),
            club_speed_mph=66.5,
            club=ClubType.DRIVER,
            spin_rpm=5603.0,
            spin_result_quality="experimental",
        ),
        1,
    )
    index = (session_dir / "index.html").read_text()
    assert SHOT_PREVIEW_FILENAME in index
    assert "Mag1 is highest magnitude and Mag5 is lowest" in index
    assert "Trigger / threshold (OPS243)" in index
    assert "Ball speed (OpenFlight processor)" in index
    assert "Spin (OpenFlight processor)" in index
    assert "Carry (OpenFlight model)" in index
    assert "42.5 mph (speed)" in index
    assert "70.2 mph" in index
    assert "66.5 mph" in index
    assert "5603 RPM (experimental)" in index
    assert "95.5 yd" in index
    assert data_path.parent == meta_path.parent == text_path.parent == hit_dir
    assert struct.unpack("<6h", data_path.read_bytes()) == (0, 4095, 2048, 2048, 4095, 0)
    assert text_path.read_text() == (
        "PIDHDIDKDLDV\n"
        "Hg100.000000\n"
        "Ht100.015000\n"
        '#%{"I":[0,2048,4095,]}\n'
        '#%{"Q":[4095,2048,0,]}\n'
    )
    board_lines = [json.loads(line) for line in (hit_dir / BOARD_OUTPUT_FILENAME).read_text().splitlines()]
    assert board_lines[0]["record_count"] == 1
    assert board_lines[0]["processing_enabled"] is True
    assert board_lines[1]["speed"] == [-0.7, 0.2]
    assert board_lines[1]["magnitude"] == [12.5, 9.0]
    assert board_lines[1]["time_offset_ms"] == pytest.approx(20.0)
    capture_metadata = json.loads(meta_path.read_text())["captures"][0]
    assert capture_metadata["openflight:board_record_count"] == 1
    assert capture_metadata["openflight:board_point_count"] == 2
    assert capture_metadata["openflight:board_unit"] == "mps"
    assert capture_metadata["openflight:board_output_file"] == BOARD_OUTPUT_FILENAME
    assert capture_metadata["openflight:board_preview_file"] == SHOT_PREVIEW_FILENAME
    assert capture_metadata["openflight:board_preview_color_encoding"] == (
        "array_position_1_to_5; Mag1 highest magnitude, Mag5 lowest magnitude"
    )


def test_kiosk_index_uses_hardware_threshold_without_actual_trigger_speed(tmp_path):
    """Hardware-triggered hits show the configured threshold, not a fake speed."""
    result = _result()
    result.trigger_speed_mph = None
    result.trigger_type = "hardware"
    session_dir = create_kiosk_session_directory(tmp_path, 1_700_000_000.0)

    write_kiosk_sigmf_hit(session_dir, TEMPLATE_PATH, 1, result)

    index = (session_dir / "index.html").read_text()
    assert "&gt;25.0 mph (hardware)" in index


def test_kiosk_session_directories_are_unique_for_same_start_second(tmp_path):
    """A rapid kiosk restart cannot collide with a prior timestamped session."""
    first = create_kiosk_session_directory(tmp_path, 1_700_000_000.0)
    second = create_kiosk_session_directory(tmp_path, 1_700_000_000.0)

    assert first.name == "session-20231114T221320Z"
    assert second.name == "session-20231114T221320Z-01"


def test_iq_text_uses_ops243_format_and_wraps_each_channel_after_20_values():
    text = _iq_text_bytes(
        list(range(24)),
        list(range(100, 124)),
        sample_time=698.904,
        trigger_time=698.954,
    ).decode()
    assert text == (
        "PIDHDIDKDLDV\n"
        "Hg698.904000\n"
        "Ht698.954000\n"
        '#%{"I":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,\n'
        "#%20,21,22,23,]}\n"
        '#%{"Q":[100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117,118,119,\n'
        "#%120,121,122,123,]}\n"
    )


def test_kiosk_hit_publish_failure_leaves_no_partial_hit_directory(tmp_path, monkeypatch):
    """Failure while publishing any artifact removes the staging directory."""
    import openflight.sigmf_capture as sigmf_module

    session_dir = create_kiosk_session_directory(tmp_path, 1_700_000_000.0)
    real_replace = sigmf_module.os.replace
    replace_calls = 0

    def fail_hit_directory_publish(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 6:
            raise OSError("hit directory rename failed")
        return real_replace(source, destination)

    monkeypatch.setattr(sigmf_module.os, "replace", fail_hit_directory_publish)

    with pytest.raises(OSError, match="hit directory rename failed"):
        write_kiosk_sigmf_hit(session_dir, TEMPLATE_PATH, 1, _result())

    assert not list(session_dir.iterdir())


def test_kiosk_hit_writes_empty_board_status_for_iq_only_response(tmp_path):
    """Older firmware without board records still gets a valid status file."""
    result = RawCaptureResult(
        capture=IQCapture(
            sample_time=100.0,
            trigger_time=100.015,
            i_samples=[0, 2048, 4095],
            q_samples=[4095, 2048, 0],
        ),
        trigger_speed_mph=42.5,
        trigger_timestamp=1_700_000_000.125,
        response_bytes=1234,
        capture_latency_ms=250.0,
        trigger_threshold_mph=25.0,
    )
    session_dir = create_kiosk_session_directory(tmp_path, 1_700_000_000.0)

    hit_dir, _, meta_path, _ = write_kiosk_sigmf_hit(session_dir, TEMPLATE_PATH, 1, result)

    board_lines = [json.loads(line) for line in (hit_dir / BOARD_OUTPUT_FILENAME).read_text().splitlines()]
    assert board_lines[0]["record_count"] == 0
    assert board_lines[0]["processing_enabled"] is False
    capture_metadata = json.loads(meta_path.read_text())["captures"][0]
    assert capture_metadata["openflight:board_record_count"] == 0
    assert capture_metadata["openflight:board_point_count"] == 0
    assert capture_metadata["openflight:board_output_file"] == BOARD_OUTPUT_FILENAME
    assert capture_metadata["openflight:board_preview_file"] == SHOT_PREVIEW_FILENAME
    assert capture_metadata["openflight:board_preview_color_encoding"].startswith("array_position_1_to_5")


def test_sigmf_write_failure_cleans_temporary_files(tmp_path, monkeypatch):
    """An I/O error while a temporary file is open leaves no capture artifacts."""
    import openflight.sigmf_capture as sigmf_module

    monkeypatch.setattr(sigmf_module.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(OSError, match="full"):
        write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, _result())

    assert not list(tmp_path.iterdir())


def test_sigmf_metadata_publish_failure_cleans_the_data_half(tmp_path, monkeypatch):
    """A failed second rename cannot leave an orphaned .sigmf-data file."""
    import openflight.sigmf_capture as sigmf_module

    real_replace = sigmf_module.os.replace
    replace_calls = 0

    def fail_metadata_publish(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("metadata rename failed")
        return real_replace(source, destination)

    monkeypatch.setattr(sigmf_module.os, "replace", fail_metadata_publish)

    with pytest.raises(OSError, match="metadata rename failed"):
        write_sigmf_recording(tmp_path, TEMPLATE_PATH, 1, _result())

    assert not list(tmp_path.iterdir())


def test_sigmf_cli_parser_accepts_port_output_and_threshold(tmp_path):
    """The SSH collector exposes the documented command-line controls."""
    script_path = PROJECT_ROOT / "scripts" / "hardware-test" / "capture_ops243_sigmf.py"
    spec = importlib.util.spec_from_file_location("capture_ops243_sigmf", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args(
        ["--port", "/dev/ttyACM0", "--output-dir", str(tmp_path), "--trigger-speed", "31"]
    )
    assert args.port == "/dev/ttyACM0"
    assert args.output_dir == tmp_path
    assert args.trigger_speed == 31.0


def test_sigmf_cli_writes_then_cleans_up_on_ctrl_c(tmp_path, monkeypatch, capsys):
    """The standalone collector saves complete data and always releases the port."""
    script_path = PROJECT_ROOT / "scripts" / "hardware-test" / "capture_ops243_sigmf.py"
    spec = importlib.util.spec_from_file_location("capture_ops243_sigmf_ctrl_c", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    radar = MagicMock()
    radar.port = "/dev/ttyACM0"
    trigger = MagicMock()
    trigger.wait_for_raw_capture.side_effect = [_result(), KeyboardInterrupt]
    monkeypatch.setattr(module, "OPS243Radar", lambda **_kwargs: radar)
    monkeypatch.setattr(module, "RollingBufferProcessor", MagicMock)
    monkeypatch.setattr(module, "SpeedTriggeredCapture", lambda **_kwargs: trigger)

    assert module.main(["--port", "/dev/ttyACM0", "--output-dir", str(tmp_path)]) == 0

    assert len(list(tmp_path.glob("*.sigmf-data"))) == 1
    assert len(list(tmp_path.glob("*.sigmf-meta"))) == 1
    radar.disconnect.assert_called_once()
    assert "Stopped. Saved 1 capture(s)" in capsys.readouterr().out


def test_monitor_sigmf_recorder_persists_a_complete_raw_capture(tmp_path):
    """Kiosk's recorder writes a pair without needing a second serial owner."""
    monitor = RollingBufferMonitor(trigger_type="speed", sigmf_capture_dir=tmp_path)
    monitor._start_sigmf_session()

    monitor._record_sigmf_capture(_result())

    assert monitor._sigmf_session_dir is not None
    assert len(list(monitor._sigmf_session_dir.glob("hit-*/capture.sigmf-data"))) == 1
    assert len(list(monitor._sigmf_session_dir.glob("hit-*/capture.sigmf-meta"))) == 1
    assert len(list(monitor._sigmf_session_dir.glob("hit-*/iq.txt"))) == 1
    assert len(list(monitor._sigmf_session_dir.glob(f"hit-*/{BOARD_OUTPUT_FILENAME}"))) == 1


def test_monitor_start_creates_a_new_sigmf_session_and_resets_hit_count(tmp_path, monkeypatch):
    """Every listener start owns a new output directory and hit numbering sequence."""
    import openflight.rolling_buffer.monitor as monitor_module
    import openflight.sigmf_capture as sigmf_module

    class InertThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        def join(self, *_args, **_kwargs):
            return None

    created_sessions = []

    def create_session(_output_dir, _start_timestamp):
        session_dir = tmp_path / f"session-test-{len(created_sessions) + 1}"
        session_dir.mkdir()
        created_sessions.append(session_dir)
        return session_dir

    monkeypatch.setattr(monitor_module.threading, "Thread", InertThread)
    monkeypatch.setattr(sigmf_module, "create_kiosk_session_directory", create_session)
    monitor = RollingBufferMonitor(trigger_type="speed", sigmf_capture_dir=tmp_path)

    monitor.start()
    first_session = monitor._sigmf_session_dir
    monitor._sigmf_capture_index = 4
    monitor.stop()
    monitor.start()

    assert first_session is not None
    assert monitor._sigmf_session_dir is not None
    assert monitor._sigmf_session_dir != first_session
    assert monitor._sigmf_capture_index == 0


def test_monitor_sigmf_write_error_is_isolated(tmp_path, monkeypatch, caplog):
    """A full disk or bad template must not turn a capture into a kiosk crash."""
    import openflight.sigmf_capture as sigmf_module

    monitor = RollingBufferMonitor(trigger_type="speed", sigmf_capture_dir=tmp_path)
    monitor._diagnostic_callback = MagicMock()
    monitor._start_sigmf_session()
    monkeypatch.setattr(
        sigmf_module,
        "write_kiosk_sigmf_hit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    monitor._record_sigmf_capture(_result())

    assert "Failed to save raw capture" in caplog.text
    assert monitor._sigmf_session_dir is not None
    assert not list(monitor._sigmf_session_dir.iterdir())
    assert monitor._diagnostic_callback.call_args.args[0]["reason"] == "sigmf_write_failed"
