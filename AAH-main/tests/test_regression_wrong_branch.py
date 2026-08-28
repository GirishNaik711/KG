"""Regression producer preconditions, drift detection, and the non-refire invariant.

A producer that refuses to run MUST still write attested evidence recording that
refusal. A refusal that exits without writing leaves the orchestrator reading
MISSING, which re-dispatches the same producer identically, forever — the exact
symptom issue #85 reported.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from aah.core.build import run_regression_suite as rrs
from aah.core.common.io_utils import write_yaml
from tests.support.aah_project import AAHProjectBuilder

WAVE = 0
BRANCH = f"integration/wave-{WAVE}"


@pytest.fixture
def stub_infra(monkeypatch):
    """Skip Docker/deps setup so the preconditions under test are the only gate."""
    monkeypatch.setattr(
        "aah.core.build.ensure_infra.ensure_test_environment",
        lambda project_path, **kw: {"ready": True, "message": "", "setup_status": "ok"},
    )


@pytest.fixture
def project(tmp_path):
    """A real git-backed project on integration/wave-0 with one passing feature."""
    builder = (
        AAHProjectBuilder.create(tmp_path, name="reg-proj", branch=BRANCH)
        .dirs("build/test-results")
        .secret()
        .manifest(project_name="reg-proj", stack_choices={"primary": "python"})
        .waves([["F001"]])
        .feature("F001")
        .file("src/app.py", "print('hi')\n")
        .file("tests/test_app.py", "def test_ok():\n    assert True\n")
    )
    (builder.aah / "feature-list.json").write_text(
        json.dumps({"features": [{"id": "F001", "passes": True}]}), encoding="utf-8",
    )
    builder.commit()
    return builder.path


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
