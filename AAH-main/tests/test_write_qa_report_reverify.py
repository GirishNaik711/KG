"""Stale/unbound Tier-1 evidence yields reverify (exit 3), not refuse (bug #7).

A degenerate/stale subject binding (subject_evidence.verified=False) must exit 3
(reverify_required) so the orchestrator regenerates evidence, while a genuine
Tier-1 gap still hard-refuses (exit 2). A pass is never written in either case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aah.core.build import write_qa_report
from aah.core.common.git_utils import code_subject_identity
from tests.support.aah_project import AAHProjectBuilder


def _run(monkeypatch, tmp_path: Path, tier1: dict) -> int:
    # Minimal AAH project so require_project_path resolves.
    builder = AAHProjectBuilder(tmp_path)
    builder.git("init", "-b", "develop")
    builder.git("config", "user.email", "test@example.com")
    builder.git("config", "user.name", "Test User")
    builder.file("README.md", "# test\n").commit("init", ("README.md",))
    (tmp_path / ".aah").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aah" / "manifest.yaml").write_text("project_name: test\n", encoding="utf-8")
    monkeypatch.setattr(
        "aah.core.build.verification_evidence.read_fresh_feature_evidence",
        lambda *a, **k: tier1,
    )
    argv = [
        "write_qa_report", "--feature-id", "F-MOD000-00", "--verdict", "pass",
        "--criteria-json", "[]", "--project-path", str(tmp_path),
        "--subject-path", str(tmp_path), "--subject-branch", "develop",
        "--subject-sha", code_subject_identity(cwd=tmp_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        write_qa_report.main()
    return exc.value.code


def test_stale_unbound_evidence_reverify(tmp_path, monkeypatch):
    tier1 = {
        "passed": False,
        "status": "no_signal",
        "gaps": [],
        "details": {"subject_evidence": {"verified": False, "reason": "SHA mismatch"}},
    }
    assert _run(monkeypatch, tmp_path, tier1) == 3   # reverify_required


def test_genuine_gap_still_refuses(tmp_path, monkeypatch):
    tier1 = {
        "passed": False,
        "status": "fail",
        "gaps": [{"criterion_id": "AC1", "reason": "no test covers AC1"}],
        "details": {"subject_evidence": {"verified": True}},
    }
    assert _run(monkeypatch, tmp_path, tier1) == 2   # hard refuse


def test_qa_pass_report_not_written_on_reverify(tmp_path, monkeypatch):
    tier1 = {
        "passed": False,
        "status": "no_signal",
        "details": {"subject_evidence": {"verified": False, "reason": "empty descriptor"}},
    }
    _run(monkeypatch, tmp_path, tier1)
    attempts = tmp_path / ".aah" / "build" / "qa-results" / "F-MOD000-00"
    assert not attempts.exists(), "no QA attempt may be written on reverify"
