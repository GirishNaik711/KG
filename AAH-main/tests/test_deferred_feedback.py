"""Functional tests (NO MOCKS) for the current-wave rework feedback contract.

The defer-past-promote machinery is GONE. This file asserts:
  - ``has_deferred_for_wave`` no longer exists (deferral subsystem deleted).
  - Uniform ids: every feedback session id is ``UF-<wave>-<seq>`` (auto-incremented).
  - In-progress UX check: find_inprogress_session_for_wave scans by wave.
"""

from pathlib import Path

import pytest

from aah.core.common.io_utils import write_yaml
from aah.core.build import feedback_capture
from aah.core.build.feedback_capture import (
    resolve_feedback_id,
    find_inprogress_session_for_wave,
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".aah" / "build" / "feedback").mkdir(parents=True)
    (tmp_path / ".aah" / "plan" / "features").mkdir(parents=True)
    return tmp_path


def _write_log(project, feedback_id, **fields):
    write_yaml(
        {"feedback_id": feedback_id, "steps": [], **fields},
        project / ".aah" / "build" / "feedback" / f"{feedback_id}.yaml",
    )


# ---------------------------------------------------------------------------
# Deferral subsystem is deleted
# ---------------------------------------------------------------------------


def test_has_deferred_for_wave_removed():
    assert not hasattr(feedback_capture, "has_deferred_for_wave")


def test_orchestrator_has_no_deferral_reader():
    src = Path("aah/core/build/orchestrator.py").read_text()
    assert "has_deferred_for_wave" not in src
    assert "deferred-promote" not in src


# ---------------------------------------------------------------------------
# Auto-incremented ids — UF-<wave>-<seq>
# ---------------------------------------------------------------------------


def test_resolve_feedback_id_first_session(project):
    fid = resolve_feedback_id(project, 6)
    assert fid == "UF-06-01"


def test_resolve_feedback_id_increments(project):
    _write_log(project, "UF-06-01", status="completed", wave=6)
    fid = resolve_feedback_id(project, 6)
    assert fid == "UF-06-02"


def test_resolve_feedback_id_zero_pads_wave(project):
    fid = resolve_feedback_id(project, 3)
    assert fid == "UF-03-01"


def test_resolve_feedback_id_double_digit_wave(project):
    fid = resolve_feedback_id(project, 12)
    assert fid == "UF-12-01"


def test_resolve_feedback_id_separate_per_wave(project):
    _write_log(project, "UF-06-01", status="completed", wave=6)
    _write_log(project, "UF-06-02", status="completed", wave=6)
    # wave 7 starts its own counter
    fid = resolve_feedback_id(project, 7)
    assert fid == "UF-07-01"


# ---------------------------------------------------------------------------
# In-progress UX check — find_inprogress_session_for_wave
# ---------------------------------------------------------------------------


def test_find_inprogress_none_when_absent(project):
    assert find_inprogress_session_for_wave(project, 6) is None


def test_find_inprogress_detects_in_progress(project):
    _write_log(project, "UF-06-01", status="in_progress", wave=6)
    active = find_inprogress_session_for_wave(project, 6)
    assert active is not None
    assert active.name == "UF-06-01.yaml"


def test_find_inprogress_ignores_terminal(project):
    _write_log(project, "UF-06-01", status="completed", wave=6)
    assert find_inprogress_session_for_wave(project, 6) is None


def test_find_inprogress_returns_first_inprogress(project):
    _write_log(project, "UF-06-01", status="completed", wave=6)
    _write_log(project, "UF-06-02", status="in_progress", wave=6)
    active = find_inprogress_session_for_wave(project, 6)
    assert active is not None
    assert active.name == "UF-06-02.yaml"
