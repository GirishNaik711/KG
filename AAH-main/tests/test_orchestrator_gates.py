"""Integration tests for the orchestrator's pre-merge gates.

Covers _check_expertise_update_wave + _validate_expertise_artifacts +
_log_gate_decision: the tightened domain-files validator, content-bound
freshness, and the audit log.

NO MOCKS — real git_repo fixture, real file I/O, subprocess where needed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from aah.core.build import orchestrator as orch
from aah.core.build.orchestrator import (
    _check_expertise_update_wave,
    _log_gate_decision,
    _validate_expertise_artifacts,
)
from aah.core.build.verification_evidence import domain_files_problem
from aah.core.build.wave_markers import (
    write_expertise_marker,
    write_expertise_outcome,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def gates_project(git_repo):
    """A project with .aah skeleton, manifest, expertise.yaml, one
    domain file, and integration/wave-0 branch checked out + committed.
    """
    aah_root = git_repo / ".aah"
    (aah_root / "build").mkdir(parents=True, exist_ok=True)
    (aah_root / "codebase-intel" / "domains").mkdir(parents=True, exist_ok=True)
    (aah_root / "audit").mkdir(parents=True, exist_ok=True)

    (aah_root / "manifest.yaml").write_text(
        "project_name: gates-test\nproject_type: greenfield\ncurrent_phase: implement\n"
    )
    (aah_root / "codebase-intel" / "expertise.yaml").write_text(
        "consumption_views:\n  style_guide: 'use snake_case'\n  known_concerns: ''\n"
    )
    (aah_root / "codebase-intel" / "domains" / "auth.yaml").write_text(
        "domain: auth\npatterns:\n  - 'jwt'\n"
    )

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "checkout", "-b", "integration/wave-0"],
                   cwd=git_repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "seed"],
                   cwd=git_repo, check=True, capture_output=True, env=env)
    return git_repo


def _commit_empty(project_path: Path, msg: str = "advance") -> None:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "--allow-empty", "-m", msg],
                   cwd=project_path, check=True, capture_output=True, env=env)


def _commit_source_change(project_path: Path, msg: str = "advance") -> None:
    """Advance HEAD with a real (non-.aah/) source change.

    The expertise gate binds to code_subject_identity, which excludes
    .aah/.claude bookkeeping — so only a real source change moves it. An
    empty or .aah/-only commit deliberately does NOT (issue #131).
    """
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    (project_path / "app.py").write_text("print('post-marker feature')\n")
    subprocess.run(["git", "add", "-A", "--", "app.py"],
                   cwd=project_path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", msg],
                   cwd=project_path, check=True, capture_output=True, env=env)


# ---------------------------------------------------------------------------
# C2 — audit log
# ---------------------------------------------------------------------------


class TestGateDecisionAuditLog:
    def test_log_appends_jsonl(self, gates_project):
        aah_root = gates_project / ".aah"
        _log_gate_decision(aah_root, gate="expertise", wave=0,
                           decision="pass", reason="r1")
        _log_gate_decision(aah_root, gate="codemap", wave=0,
                           decision="refire", reason="r2")
        log = aah_root / "audit" / "gate-decisions.jsonl"
        lines = log.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["gate"] == "expertise"
        assert first["decision"] == "pass"
        assert first["reason"] == "r1"
        assert first["wave"] == 0
        assert "ts" in first
        second = json.loads(lines[1])
        assert second["gate"] == "codemap"

    def test_log_swallows_io_error(self, gates_project, monkeypatch):
        """A log write failure must NEVER propagate — the gate is the
        priority, the log is best-effort."""
        aah_root = gates_project / ".aah"
        # Make the audit dir un-writable to provoke an OSError.
        # On WSL this isn't fully reliable, so we monkeypatch open instead.
        import builtins
        original_open = builtins.open

        def boom(path, *a, **kw):
            if "gate-decisions.jsonl" in str(path):
                raise OSError("disk full")
            return original_open(path, *a, **kw)
        monkeypatch.setattr(builtins, "open", boom)
        # Must not raise.
        _log_gate_decision(aah_root, gate="expertise", wave=0,
                           decision="pass", reason="boom-test")


# ---------------------------------------------------------------------------
# B8 — tightened domain-files check
# ---------------------------------------------------------------------------


class TestDomainFilesProblem:
    def test_returns_none_when_valid(self, gates_project):
        domains = gates_project / ".aah" / "codebase-intel" / "domains"
        assert domain_files_problem(domains) is None

    def test_directory_missing(self, gates_project):
        domains = gates_project / ".aah" / "codebase-intel" / "domains-not-here"
        assert domain_files_problem(domains) == "directory missing"

    def test_no_domain_files(self, gates_project):
        domains = gates_project / ".aah" / "codebase-intel" / "domains"
        for f in domains.glob("*.yaml"):
            f.unlink()
        assert domain_files_problem(domains) == "no domain files"

    def test_empty_file_caught(self, gates_project):
        """A 0-byte yaml must fail validation; the existence-only
        ``any(...glob)`` check would otherwise accept it."""
        domains = gates_project / ".aah" / "codebase-intel" / "domains"
        (domains / "billing.yaml").write_text("")
        result = domain_files_problem(domains)
        assert result is not None
        assert "empty file" in result
        assert "billing.yaml" in result

    def test_unparseable_caught(self, gates_project):
        domains = gates_project / ".aah" / "codebase-intel" / "domains"
        (domains / "bad.yaml").write_text("{ this is: not [ valid yaml")
        result = domain_files_problem(domains)
        assert result is not None
        assert "unparseable" in result or "empty mapping" in result

    def test_empty_mapping_caught(self, gates_project):
        domains = gates_project / ".aah" / "codebase-intel" / "domains"
        (domains / "blank.yaml").write_text("---\n")  # parses to None
        result = domain_files_problem(domains)
        assert result is not None
        assert "empty mapping" in result or "unparseable" in result


# ---------------------------------------------------------------------------
# Expertise gate refires when marker stale (regression coverage)
# ---------------------------------------------------------------------------


class TestExpertiseGateFreshness:


    def test_gate_dispatches_once_per_product_identity(self, gates_project):
        """A product-code change permits EXACTLY ONE new attempt.

        The gate is non-blocking (issue #117): it dispatches once per code
        identity and records a durable outcome, so it neither blocks nor loops.
        """
        aah_root = gates_project / ".aah"
        write_expertise_marker(gates_project, 0, summary={})

        # Fresh marker + valid artifacts → pass, no dispatch.
        assert _check_expertise_update_wave(aah_root, ["F001"], 0) is None

        # Advance HEAD with a real source change (moves content identity).
        _commit_source_change(gates_project, "post-marker work")

        action = _check_expertise_update_wave(aah_root, ["F001"], 0)
        assert action is not None, (
            "a product-code change must permit one new expertise attempt"
        )
        assert action["action"] == "update_expertise"

        # The attempt is recorded as a warning outcome bound to the NEW identity,
        # so a restart at that same identity does NOT dispatch again.
        write_expertise_outcome(
            gates_project, 0, outcome="warning", reason="skill failed"
        )
        assert _check_expertise_update_wave(aah_root, ["F001"], 0) is None

    def test_stale_marker_never_blocks_and_is_not_deleted(self, gates_project):
        """A stale marker records a warning instead of blocking the wave.

        The marker is deliberately NOT unlinked: the recorded outcome is what
        suppresses a restart re-dispatch, and deleting the marker would defeat it.
        """
        aah_root = gates_project / ".aah"
        marker = aah_root / "build" / "wave-0-expertise-updated.json"
        write_expertise_marker(gates_project, 0, summary={})
        # Mutate expertise.yaml without touching the marker.
        expertise = aah_root / "codebase-intel" / "expertise.yaml"
        expertise.write_text(expertise.read_text() + "\n# drift\n")

        # First call at this identity: one attempt is offered, never a block.
        action = _check_expertise_update_wave(aah_root, ["F001"], 0)
        assert action is not None
        assert action["action"] == "update_expertise"
        assert marker.exists(), "the gate must not unlink the marker"

        # A recorded outcome at the same identity suppresses re-dispatch.
        write_expertise_outcome(gates_project, 0, outcome="warning", reason="drift")
        assert _check_expertise_update_wave(aah_root, ["F001"], 0) is None
        assert marker.exists()

    def test_audit_log_records_pass(self, gates_project):
        """Every gate evaluation appends an audit line."""
        aah_root = gates_project / ".aah"
        write_expertise_marker(gates_project, 0, summary={})

        _check_expertise_update_wave(aah_root, ["F001"], 0)

        log = aah_root / "audit" / "gate-decisions.jsonl"
        assert log.exists()
        lines = log.read_text(encoding="utf-8").strip().split("\n")
        # At least one line for the expertise gate.
        expertise_lines = [json.loads(l) for l in lines if l]
        expertise_lines = [l for l in expertise_lines if l.get("gate") == "expertise"]
        assert len(expertise_lines) >= 1
        assert expertise_lines[-1]["decision"] == "pass"

    def test_audit_log_records_the_first_dispatch(self, gates_project):
        aah_root = gates_project / ".aah"
        # No marker and no recorded outcome → one dispatch for this identity.
        action = _check_expertise_update_wave(aah_root, ["F001"], 0)
        assert action is not None

        log = aah_root / "audit" / "gate-decisions.jsonl"
        last = json.loads(log.read_text().strip().split("\n")[-1])
        assert last["gate"] == "expertise"
        assert last["decision"] == "dispatch"


# ---------------------------------------------------------------------------
# B8 integration: validator rejects empty domain file even when marker exists
# ---------------------------------------------------------------------------


class TestValidateExpertiseArtifactsTightened:
    def test_rejects_empty_domain_yaml(self, gates_project):
        """A 0-byte domain yaml used to pass any(...glob); now it doesn't."""
        aah_root = gates_project / ".aah"
        # Hand-write a marker because this test calls the artifact validator
        # directly rather than the freshness gate.
        marker = aah_root / "build" / "wave-0-expertise-updated.json"
        marker.write_text(json.dumps({"wave": 0}) + "\n")

        # Add an empty domain file alongside the seeded valid one.
        (aah_root / "codebase-intel" / "domains" / "blank.yaml").write_text("")

        action = _validate_expertise_artifacts(aah_root, 0)
        assert action is not None
        assert action["action"] == "update_expertise"
        assert "blank.yaml" in action["reason"] or "empty" in action["reason"]
        # The validator must NOT unlink the marker (issue #117): the caller
        # records a durable outcome instead, and deleting the marker would defeat
        # that suppression on restart.
        assert marker.exists()
