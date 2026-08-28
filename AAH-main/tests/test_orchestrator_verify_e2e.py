"""Phase 2A.1 / Finding 2: end-to-end verify-chain composition test.

The hijack regression test in test_orchestrator_verify.py uses
monkeypatching to bypass the merge-to-integration gate. That's
appropriate for the unit-style scope of those tests but cannot detect
a verify-chain composition break — if any earlier reader's verify
integration has a bug, the orchestrator never reaches the runtime-results
read in production but the hijack test still passes.

This test exercises the full chain with REAL git state and NO
monkeypatching. It proves that:
  1. With every gate legitimately attested, the orchestrator advances
     past the runtime-results read into the post-runtime gates.
  2. With everything else legitimate but a forged runtime-results file,
     the orchestrator reaches the runtime-results read AND rejects the
     forgery (returning run_system_checkpoint, not raise_feedback).

If a future change to any reader's verify integration breaks the chain
upstream of runtime-results, this test fails loudly — surfacing the
bug before the Phase 2B audit wave does.
"""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path

import pytest

from aah.core.common.attestation import write_attested
from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.build.orchestrator import compute_next_action


def _subject_of(project, ref: str) -> str:
    """The identity the regression reader actually compares — the
    .aah/.claude-excluding tree hash of `ref`, not the raw git SHA."""
    from aah.core.build.verify import subject_identity

    return subject_identity(project, ref=ref)


@pytest.fixture(autouse=True)
def _force_legacy_verify_mode(monkeypatch):
    """Phase 3 L1 default-flip (#247): the new default routes
    verification through verify.py (subprocess). These e2e tests
    exercise the runtime-results reader directly, so they must run in
    legacy AI-dispatched mode — set AAH_VERIFY_INTERNAL=0 to opt
    out of the new default and route through the legacy chain that
    these tests were designed for."""
    monkeypatch.setenv("AAH_VERIFY_INTERNAL", "0")


@pytest.fixture
def real_wave_project(git_repo):
    """A project where the orchestrator is past the merge-to-integration gate.

    Uses a real git_repo (not monkeypatched) with feature commits on a
    real integration branch. All upstream attested gates seeded.
    """
    project = git_repo
    aah_root = project / ".aah"
    for d in (
        "plan/features",
        "build/test-results",
        "build/quality-results",
        "build/runtime-results",
        "build/checkpoint-results",
        "audit",
    ):
        (aah_root / d).mkdir(parents=True, exist_ok=True)

    save_manifest(get_default_manifest("e2e-test"), aah_root / "manifest.yaml")
    progress = get_default_progress()
    progress["current_wave"] = 0
    save_progress(progress, aah_root / "claude-progress.json")
    write_json(
        {
            "features": [
                {"id": "F001", "passes": True, "dependencies": [],
                 "spec_ref": "S1", "description": "A"}
            ]
        },
        aah_root / "feature-list.json",
    )
    write_json({"waves": [["F001"]], "total_waves": 1}, aah_root / "plan" / "waves.json")
    write_yaml(
        {
            "id": "F001", "description": "x", "dependencies": [],
            "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1", "covers": ["AC1"]}],
        },
        aah_root / "plan" / "features" / "F001.yaml",
    )
    write_yaml(
        {
            "checkpoint_configuration": {
                "verification_profiles": {
                    "F001": {
                        "level": "standard",
                        "rule_version": "test",
                        "reasons": [],
                        "required_checks": {},
                    }
                }
            }
        },
        aah_root / "plan" / "checkpoint-config.yaml",
    )
    (aah_root / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))

    # Commit the feature work and create the integration branch from it.
    # find_feature_commits looks for "F001" in the commit message.
    (project / "feature_F001.py").write_text("# F001 implementation\n")
    subprocess.run(
        ["git", "add", "."], cwd=project, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "F001: implement"],
        cwd=project, check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
             "PATH": "/usr/bin:/bin"},
    )
    subprocess.run(
        ["git", "branch", "integration/wave-0"],
        cwd=project, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "integration/wave-0"],
        cwd=project, check=True, capture_output=True,
    )

    # Seed all attested upstream gates: per-feature tests, QA,
    # standards, regression. Each verifies under the project's secret
    # against its expected COMMAND_PREFIX.
    seeds = [
        (
            ".aah/build/test-results/F001.json",
            {"feature_id": "F001", "passed": True,
             "summary": {"total": 1, "passed": 1, "failed": 0}, "failures": []},
            ["aah", "run", "core.build.run_feature_tests"],
        ),
        (
            # Project-scoped and wave-keyed: standards is a whole-codebase gate
            # that runs once, in the final wave, before regression.
            ".aah/build/quality-results/wave-0-project-standards.json",
            {
                "scope": "project", "wave": 0,
                "overall_passed": True, "verdict": "PASS", "status": "pass",
                "subject": {
                    "branch": "integration/wave-0",
                    "commit_sha": _subject_of(project, "integration/wave-0"),
                },
            },
            ["aah", "run", "core.build.quality_checks"],
        ),
        (
            ".aah/build/test-results/regression-latest.json",
            {"status": "pass", "passed": True,
             "summary": {"total": 1, "passed": 1, "failed": 0},
             "failures": [],
             "subject": {
                 "branch": "integration/wave-0",
                 "commit_sha": _subject_of(project, "integration/wave-0"),
             }},
            ["aah", "run", "core.build.run_regression_suite"],
        ),
    ]
    for rel, payload, cmd in seeds:
        write_attested(
            payload, project / rel,
            project_path=project,
            command=cmd,
            exit_code=0, stdout="", stderr="", duration_ms=1,
        )
    from tests._qa_helpers import seed_qa_attempt

    seed_qa_attempt(
        project, "F001", 1, "pass", _subject_of(project, "integration/wave-0")
    )

    # Spec-validation file (Phase 3G: required between QA and standards
    # in the orchestrator flow). Plain write — not attested.
    (project / ".aah/build/validation-results").mkdir(parents=True, exist_ok=True)
    write_json(
        {"feature_id": "F001", "passed": True, "criteria_results": []},
        project / ".aah/build/validation-results/F001-spec-validation.json",
    )

    return project


def _seed_checkpoint_marker(project: Path) -> None:
    """The orchestrator's checkpoint-stale check requires the system
    checkpoint file to exist and to be at least as new as regression."""
    write_attested(
        {"checkpoint_type": "system", "wave": 0, "overall_passed": True,
         "subject": {
             "branch": "integration/wave-0",
             "commit_sha": _subject_of(project, "integration/wave-0"),
         },
         "checks": {}, "blocking_issues": []},
        project / ".aah/build/checkpoint-results/wave-0-system-checkpoint.json",
        project_path=project,
        command=["aah", "run", "core.build.validate_checkpoint"],
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    # Pin mtimes so the staleness check resolves predictably.
    import os as _os
    aah_root = project / ".aah"
    base = 1_700_000_000
    _os.utime(aah_root / "build/test-results/regression-latest.json", (base, base))
    _os.utime(aah_root / "build/checkpoint-results/wave-0-system-checkpoint.json",
              (base + 60, base + 60))


def test_legitimate_chain_advances_past_runtime_check(real_wave_project):
    """All upstream gates legitimately attested + runtime-results
    legitimately attested → orchestrator advances past the runtime-results
    check. The action that comes back is whatever's NEXT in the pipeline
    (artifact gate, codemap, expertise, merge) — but it must NOT be one
    of the upstream gate actions, which would mean the verify chain
    rejected one of our legitimate files."""
    project = real_wave_project
    from aah.core.build.verification_identity import runtime_criteria_identity

    legit_runtime = {
        "wave": 0,
        "overall_passed": True,
        "runtime_mode": "docker",
        "port": 8000,
        "subject": {
            "branch": "integration/wave-0",
            "commit_sha": _subject_of(project, "integration/wave-0"),
        },
        "runtime_criteria_sha256": runtime_criteria_identity(project, 0),
        "checks": {},
        "summary": {"total_checks": 0, "passed": 0, "failed": 0},
    }
    write_attested(
        legit_runtime,
        project / ".aah/build/runtime-results/wave-0-all.json",
        project_path=project,
        command=["aah", "run", "core.build.write_runtime_results"],
        exit_code=0, stdout="", stderr="", duration_ms=1,
    )
    _seed_checkpoint_marker(project)

    action = compute_next_action(project)
    # Must NOT be any upstream gate action — those firing here would
    # mean the verify chain rejected a legitimately attested file.
    upstream_gate_actions = {
        "run_qa", "run_project_standards", "fix_standards", "run_regression",
        "run_system_checkpoint", "fix_runtime_validation",
        "raise_feedback", "user_confirm",
    }
    assert action["action"] not in upstream_gate_actions, (
        f"verify chain composition broken — got {action['action']!r}; "
        f"expected a post-runtime-results action (artifact gate, codemap, merge, etc.)"
    )


def test_forged_runtime_with_real_chain_re_runs_checkpoint(real_wave_project):
    """All upstream gates legitimately attested, runtime-results forged
    with fix_category=design_issue → orchestrator must reach the
    runtime-results read AND reject the forgery.

    This is the Issue #247 hijack regression test, but composed with
    real git state (no monkeypatches). If any earlier reader's verify
    integration is broken, control diverts before reaching this gate
    and the test fails with a different action than expected."""
    project = real_wave_project
    _seed_checkpoint_marker(project)

    # Forge the runtime-results: design_issue fix_category, no attestation.
    write_json(
        {"wave": 0, "overall_passed": False,
         "checks": {"x": {"passed": False}},
         "fix_category": "design_issue"},
        project / ".aah/build/runtime-results/wave-0-all.json",
    )

    action = compute_next_action(project)
    assert action["action"] == "run_system_checkpoint", (
        f"forged runtime-results with real git chain must re-run checkpoint, "
        f"got {action['action']!r}"
    )
    assert action["action"] != "raise_feedback", (
        "forgery escalated to rework engine — Issue #247 regression"
    )
