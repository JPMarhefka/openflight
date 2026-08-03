"""Tests for replaying the kiosk IQ text export."""

import importlib.util
from pathlib import Path

from openflight.sigmf_capture import _iq_text_bytes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "analysis" / "reprocess_sigmf_session.py"
SPEC = importlib.util.spec_from_file_location("reprocess_sigmf_session", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_read_iq_txt_accepts_native_ops243_format(tmp_path):
    path = tmp_path / "iq.txt"
    path.write_bytes(
        _iq_text_bytes(
            list(range(24)),
            list(range(100, 124)),
            sample_time=698.904,
            trigger_time=698.954,
        )
    )

    i_values, q_values, sample_time, trigger_time = MODULE.read_iq_txt_with_metadata(path)

    assert i_values == list(range(24))
    assert q_values == list(range(100, 124))
    assert sample_time == 698.904
    assert trigger_time == 698.954
    assert MODULE.read_iq_txt(path) == (i_values, q_values)


def test_read_iq_txt_keeps_legacy_format_compatible(tmp_path):
    path = tmp_path / "iq.txt"
    path.write_text("I:0,1,2\n\nQ:100,101,102\n", encoding="utf-8")

    assert MODULE.read_iq_txt_with_metadata(path) == (
        [0, 1, 2],
        [100, 101, 102],
        None,
        None,
    )
