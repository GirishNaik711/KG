"""Verify that expected_command_prefix gating works correctly.

In Phase 2 the orchestrator's regression-result reader will pass
expected_command_prefix=["aah-run", "aah.core.build.run_regression_suite"]
to ensure the file was produced by the right writer (not, e.g., a
qa-report writer).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from aah.core.common import attestation
from aah.core.common.attestation import REASON_COMMAND_MISMATCH, SECRET_REL_PATH
from aah.core.common.io_utils import read_json


def _seed_secret(project_path: Path) -> bytes:
    secret_path = project_path / SECRET_REL_PATH
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    secret_path.write_bytes(secret)
    return secret


def _write(project_path: Path, command: list[str]) -> Path:
    out = project_path / ".aah" / "build" / "test-results" / "regression-latest.json"
    attestation.write_attested(
        {"passed": True, "summary": {"total": 47}},
        out,
        project_path=project_path,
        command=command,
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=10,
    )
    return out


class TestCommandPrefix:
    def test_no_prefix_means_any_command_passes(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, ["aah-run", "aah.core.build.run_regression_suite"])
        loaded = read_json(out)
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok is True
        assert reason == ""

    def test_matching_prefix_accepted(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, ["aah-run", "aah.core.build.run_regression_suite", "--project-path", "."])
        loaded = read_json(out)
        ok, reason = attestation.verify(
            loaded,
            project_path=rapids_project,
            expected_command_prefix=["aah-run", "aah.core.build.run_regression_suite"],
        )
        assert ok is True
        assert reason == ""

    def test_non_matching_prefix_rejected(self, rapids_project):
        _seed_secret(rapids_project)
        # File written by qa report path...
        out = _write(rapids_project, ["aah-run", "aah.core.build.write_qa_report"])
        loaded = read_json(out)
        # ...but read with the regression prefix expectation.
        ok, reason = attestation.verify(
            loaded,
            project_path=rapids_project,
            expected_command_prefix=["aah-run", "aah.core.build.run_regression_suite"],
        )
        assert ok is False
        assert reason == REASON_COMMAND_MISMATCH

    def test_prefix_longer_than_command_rejected(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, ["aah-run"])
        loaded = read_json(out)
        ok, reason = attestation.verify(
            loaded,
            project_path=rapids_project,
            expected_command_prefix=["aah-run", "aah.core.build.run_regression_suite"],
        )
        assert ok is False
        assert reason == REASON_COMMAND_MISMATCH

    def test_exact_match_accepted(self, rapids_project):
        _seed_secret(rapids_project)
        out = _write(rapids_project, ["aah-run", "x"])
        loaded = read_json(out)
        ok, reason = attestation.verify(
            loaded,
            project_path=rapids_project,
            expected_command_prefix=["aah-run", "x"],
        )
        assert ok is True
