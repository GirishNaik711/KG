"""Tests for aah.core.build.write_runtime_results.

The deterministic subprocess writer the aah-runtime-validator agent calls in
place of a Write tool. The writer — not the agent — decides the verdict: it
validates the check schema, derives ``overall_passed``, and derives the subject
and runtime-criteria identity locally.

Also covers the issue #90 screenshot writer's relative-source and
raw-preservation behaviour, per the plan's "do not create
test_write_runtime_screenshot.py".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.aah_project import AAHProjectBuilder

from aah.core.common import attestation
from aah.core.common.io_utils import read_json


WAVE = 0
BRANCH = f"integration/wave-{WAVE}"
RUNTIME_PREFIX = ["aah", "run", "core.build.write_runtime_results"]


@pytest.fixture
def project(tmp_path):
    """A real git-backed project on integration/wave-0.

    The writer now requires the correct branch, a clean tree outside
    AAH_STATE_PATHS, and a resolvable subject — so a bare skeleton is no
    longer sufficient.
    """
    import yaml

    builder = (
        AAHProjectBuilder.create(tmp_path, name="rt-proj", branch=BRANCH)
        .dirs("build/runtime-results", "plan/smoke-tests")
        .secret()
        .manifest(project_name="rt-proj", stack_choices={"primary": "python"})
        .waves([["F001"]])
        .feature("F001")
        .file("src/app.py", "print('hi')\n")
    )
    (builder.aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump({"checkpoint_configuration": {"system_checkpoints": {}}}),
        encoding="utf-8",
    )
    builder.commit()
    return builder.path


def _check(name: str, *, passed: bool = True, wave: int = WAVE, **overrides) -> dict:
    payload = {
        "check": name,
        "wave": wave,
        "timestamp": "2026-06-03T00:00:00+00:00",
        "passed": passed,
        "details": {"message": "ok"},
    }
    payload.update(overrides)
    return payload


def sample_checks(**passed_overrides) -> dict:
    """All four core checks, passing unless overridden."""
    return {
        name: _check(name, passed=passed_overrides.get(name, True))
        for name in (
            "module_validation", "startup_validation", "smoke_tests", "health_check",
        )
    }


def _run(
    project_path: Path, *args: str, expect_success: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.build.write_runtime_results",
         "--project-path", str(project_path), *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    if expect_success:
        assert proc.returncode == 0, f"unexpected failure: stderr={proc.stderr!r}"
    return proc


def _write(project_path: Path, checks: dict, *extra: str, **kwargs):
    return _run(
        project_path,
        "--wave", str(WAVE),
        "--results-json", json.dumps(checks),
        *extra,
        **kwargs,
    )


def _loaded(project_path: Path, wave: int = WAVE) -> dict:
    return read_json(
        project_path / ".aah" / "build" / "runtime-results" / f"wave-{wave}-all.json"
    )


class TestRoundtrip:
    def test_writes_attested_result(self, project):
        _write(project, sample_checks(), "--port", "8000", "--duration-ms", "1234")
        loaded = _loaded(project)

        assert loaded["wave"] == WAVE
        assert loaded["overall_passed"] is True
        assert loaded["runtime_mode"] == "docker"
        assert loaded["port"] == 8000
        assert "fix_category" not in loaded
        assert set(loaded["checks"]) == {
            "module_validation", "startup_validation", "smoke_tests", "health_check",
        }
        assert loaded["summary"] == {"total_checks": 4, "passed": 4, "failed": 0}

        ok, reason = attestation.verify(
            loaded, project_path=project, expected_command_prefix=RUNTIME_PREFIX,
        )
        assert ok is True, f"verify failed: {reason}"

    def test_summary_counts_failures(self, project):
        _write(
            project, sample_checks(smoke_tests=False),
            "--fix-category", "design_issue",
        )
        loaded = _loaded(project)
        assert loaded["overall_passed"] is False
        assert loaded["summary"] == {"total_checks": 4, "passed": 3, "failed": 1}
        assert loaded["fix_category"] == "design_issue"










class TestErrorPaths:
    def test_invalid_json_in_results_fails(self, project):
        proc = _run(
            project, "--wave", str(WAVE), "--results-json", "not json",
            expect_success=False,
        )
        assert proc.returncode == 1
        assert "Error parsing --results-json" in proc.stderr

    def test_results_must_be_object_not_array(self, project):
        proc = _run(
            project, "--wave", str(WAVE), "--results-json", "[]",
            expect_success=False,
        )
        assert proc.returncode == 1
        assert "must be a JSON object" in proc.stderr

    def test_missing_required_arg_fails(self, project):
        proc = _run(project, "--wave", str(WAVE), expect_success=False)
        assert proc.returncode != 0
        assert "results-json" in proc.stderr.lower()
