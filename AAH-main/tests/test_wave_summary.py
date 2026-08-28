"""Functional tests for wave summary reading the real QA attempt schema.

NO MOCKS. Every test uses a real Git project, real attestation secret, real
CLI subprocess calls (write_qa_report), and real attested artifacts. The reader
under test is aah.core.build.wave_summary.generate().

The QA block reads the append-only attempt tree via derive_qa_state plus human
decisions.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aah.core.build import wave_summary
from aah.core.build.qa_evidence import derive_qa_state
from aah.core.common.attestation import write_attested
from aah.core.common.git_utils import code_subject_identity
from aah.core.common.io_utils import write_json


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_test_results(project: Path, feature_id: str) -> dict:
    """Read the real attested test-results/{fid}.json the runner wrote."""
    import json as _json
    path = project / ".aah" / "build" / "test-results" / f"{feature_id}.json"
    return _json.loads(path.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        v for v in (str(_REPO_ROOT), env.get("PYTHONPATH", "")) if v
    )
    return env


def _feature_md(feature_id: str, test_rel: str) -> str:
    command = f"{sys.executable} -m pytest {test_rel} -s"
    return (
        "---\n"
        f"id: {feature_id}\n"
        "spec_ref: SPEC-001\n"
        f"description: wave summary source for {feature_id}\n"
        "dependencies: []\n"
        "status: pending\n"
        "test_config:\n"
        f'  command: "{command}"\n'
        "acceptance_criteria:\n"
        "  - id: AC1\n"
        "    description: marker present\n"
        "test_cases:\n"
        "  - id: TC1\n"
        "    covers: [AC1]\n"
        "    description: marker validation\n"
        "---\n"
        f"# {feature_id}\n"
    )


@pytest.fixture
def ws_project(tmp_path: Path) -> dict:
    """Real Git project with feature F001 in wave 0, worktree, attestation secret."""
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-b", "develop")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.com")

    (project / ".gitignore").write_text(".aah/build/\n.claude/\n", encoding="utf-8")
    aah = project / ".aah"
    (aah / "plan" / "features").mkdir(parents=True)
    (aah / "plan" / "waves.json").parent.mkdir(parents=True, exist_ok=True)
    (aah / "manifest.yaml").write_text(
        "project_name: ws-test\n"
        "current_phase: build\n",
        encoding="utf-8",
    )

    tests_dir = project / "qa_tests"
    tests_dir.mkdir()
    test_rel = "qa_tests/test_F001.py"
    (aah / "plan" / "features" / "F001.md").write_text(
        _feature_md("F001", test_rel), encoding="utf-8"
    )
    (project / test_rel).write_text(
        "from pathlib import Path\n\n"
        "def test_F001_TC1_marker():\n"
        "    assert Path('marker.txt').read_text().strip().startswith('commit-')\n",
        encoding="utf-8",
    )

    write_json(
        {"features": [{"id": "F001", "spec_ref": "SPEC-001",
                       "description": "Marker", "dependencies": [], "passes": False}]},
        aah / "feature-list.json",
    )
    write_json({"waves": [["F001"]], "total_waves": 1}, aah / "plan" / "waves.json")

    (project / "marker.txt").write_text("commit-base\n", encoding="utf-8")
    _git(project, "add", ".gitignore", ".aah", "qa_tests", "marker.txt")
    _git(project, "commit", "-m", "test: seed wave summary project")

    worktree = project / ".claude" / "worktrees" / "F001"
    _git(project, "worktree", "add", str(worktree), "-b", "feature/F001", "develop")

    build = aah / "build"
    build.mkdir(parents=True)
    (build / ".attestation-secret").write_bytes(secrets.token_bytes(32))

    return {"project": project, "worktree": worktree}


def _run_qa_report(state: dict, *, verdict: str, subject_sha: str | None = None):
    project, worktree = state["project"], state["worktree"]
    subject_sha = subject_sha or code_subject_identity(cwd=worktree)
    crit_verdict = "pass" if verdict == "pass" else "fail"
    args = [
        sys.executable, "-m", "aah.cli", "run", "core.build.write_qa_report",
        "--feature-id", "F001",
        "--verdict", verdict,
        "--criteria-json",
        '[{"id":"AC1","verdict":"' + crit_verdict + '","description":"marker","evidence":"e"}]',
        "--test-command", "pytest qa_tests/test_F001.py",
        "--tests-run", "1",
        "--tests-passed", "1" if verdict == "pass" else "0",
        "--project-path", str(project),
        "--subject-path", str(worktree),
        "--subject-branch", "feature/F001",
        "--subject-sha", subject_sha,
        "--actor", "qa",
    ]
    return subprocess.run(args, cwd=_REPO_ROOT, env=_cli_env(),
                          capture_output=True, text=True)


def _write_human_decision(project: Path, feature_id: str, decision: str, _sha: str, n: int = 1) -> None:
    """Write the simple human-NNN.json produced by validate_checkpoint.

    ``n`` is the append-only decision number; the CLI allocates a fresh one per
    decision (never overwrites), so distinct ``n`` values exercise the reader's
    newest-file selection.
    """
    d = project / ".aah" / "build" / "qa-results" / feature_id
    d.mkdir(parents=True, exist_ok=True)
    write_json({
        "feature_id": feature_id,
        "attempt": 1,
        "decision": decision,
        "recorded_by": "user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rationale": "reviewed",
    }, d / f"human-{n:03d}.json")


def _run_feature_tests_cli(project: Path, worktree: Path, *, diagnostic_rerun: bool = True):
    """Invoke the REAL run_feature_tests runner subprocess (NO MOCKS)."""
    sha = code_subject_identity(cwd=worktree)
    args = [
        sys.executable, "-m", "aah.cli", "run", "core.build.run_feature_tests",
        "--feature-id", "F001",
        "--wave", "0",
        "--project-path", str(project),
        "--subject-path", str(worktree),
        "--subject-branch", "feature/F001",
        "--subject-sha", sha,
        "--actor", "qa",
    ]
    if diagnostic_rerun:
        args.append("--diagnostic-rerun")
    return subprocess.run(args, cwd=_REPO_ROOT, env=_cli_env(),
                          capture_output=True, text=True)


@pytest.fixture
def ws_project_isolated(tmp_path: Path) -> dict:
    """Real Git project with a DETERMINISTICALLY FAILING feature F001 in wave 0,
    isolated test execution enabled.

    Extends the ws_project pattern; the feature's test asserts False so the real
    runner classifies it reproducible_failure and writes a real reproduction
    bundle + cleanup evidence into .aah/build/test-results/F001.json.
    """
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-b", "develop")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.com")

    (project / ".gitignore").write_text(".aah/build/\n.claude/\n", encoding="utf-8")
    aah = project / ".aah"
    (aah / "plan" / "features").mkdir(parents=True)
    (aah / "manifest.yaml").write_text(
        "project_name: ws-test\n"
        "current_phase: build\n"
        "features: {}\n",
        encoding="utf-8",
    )

    tests_dir = project / "qa_tests"
    tests_dir.mkdir()
    test_rel = "qa_tests/test_F001.py"
    (aah / "plan" / "features" / "F001.md").write_text(
        _feature_md("F001", test_rel), encoding="utf-8"
    )
    # Deterministic failure — reproducible on rerun.
    (project / test_rel).write_text(
        "def test_F001_TC1_marker():\n"
        "    assert False, 'deterministic failure'\n",
        encoding="utf-8",
    )

    write_json(
        {"features": [{"id": "F001", "spec_ref": "SPEC-001",
                       "description": "Marker", "dependencies": [], "passes": False}]},
        aah / "feature-list.json",
    )
    write_json({"waves": [["F001"]], "total_waves": 1}, aah / "plan" / "waves.json")

    _git(project, "add", ".gitignore", ".aah", "qa_tests")
    _git(project, "commit", "-m", "test: seed failing wave summary project")

    worktree = project / ".claude" / "worktrees" / "F001"
    _git(project, "worktree", "add", str(worktree), "-b", "feature/F001", "develop")

    build = aah / "build"
    build.mkdir(parents=True)
    (build / ".attestation-secret").write_bytes(secrets.token_bytes(32))

    return {"project": project, "worktree": worktree}


def test_summary_links_reproduction_and_flake(ws_project_isolated):
    """AC2: the wave summary links the reproduction bundle + flake
    classification + cleanup state from REAL runner evidence, without breaking
    the attempt reader.

    NO MOCKS: the real run_feature_tests subprocess writes a real
    test-results/F001.json (failure bundle + diagnostic rerun + cleanup), and a
    real write_qa_report gives derive_qa_state real counts.
    """
    project = ws_project_isolated["project"]
    worktree = ws_project_isolated["worktree"]

    # Real runner: deterministic failure → reproducible_failure + failure bundle.
    # (The aah.cli wrapper normalizes the runner's non-zero test-failure exit, so
    # we assert on the REAL written artifact rather than the wrapped exit code.)
    _run_feature_tests_cli(project, worktree, diagnostic_rerun=True)

    tr = _read_test_results(project, "F001")
    assert tr.get("passed") is False, tr
    assert isinstance(tr.get("diagnostic"), dict), tr
    assert tr["diagnostic"]["classification"] == "reproducible_failure"
    bundle = tr.get("artifacts", {}).get("failure_bundle")
    assert isinstance(bundle, dict) and bundle.get("manifest") == "reproduction.json", tr
    manifest_sha = bundle.get("manifest_sha256")
    assert isinstance(manifest_sha, str) and len(manifest_sha) == 64

    # Real QA report so derive_qa_state has real counts (attempt reader).
    q = _run_qa_report(
        {"project": project, "worktree": worktree},
        verdict="rework_required",
        subject_sha=code_subject_identity(cwd=worktree),
    )
    assert q.returncode == 0, q.stderr
    assert derive_qa_state(project, "F001")["attempts_total"] == 1

    out = wave_summary.generate(project, 0)

    # Reproduction bundle link (manifest name + sha256 prefix).
    assert "reproduction.json" in out
    assert manifest_sha[:12] in out
    # Flake classification link.
    assert "reproducible_failure" in out
    # The attempt reader still renders (real attempt line + verdict).
    assert "attempt(s)" in out
    assert "REWORK_REQUIRED" in out
    # Cleanup state line renders (no-docker → transport:none, counts 0).
    assert "Cleanup:" in out


@pytest.mark.docker
def test_summary_cleanup_counts_real_docker(ws_project_isolated):
    """AC2 docker-gated: with a real Docker daemon the runner's cleanup evidence
    carries removed/leftover counts that the summary renders. Skips cleanly
    without Docker — NEVER fabricates a pass."""
    from aah.core.build.ensure_infra import _is_docker_available
    if not _is_docker_available():
        pytest.skip("requires a working Docker daemon")

    project = ws_project_isolated["project"]
    worktree = ws_project_isolated["worktree"]
    _run_feature_tests_cli(project, worktree, diagnostic_rerun=True)
    tr = _read_test_results(project, "F001")
    assert tr.get("passed") is False, tr
    cleanup = tr.get("cleanup")
    assert isinstance(cleanup, dict)
    # removed/leftover keys present in the namespace-teardown shape.
    assert "removed" in cleanup and "leftover" in cleanup
    out = wave_summary.generate(project, 0)
    assert "Cleanup:" in out
    assert "removed=" in out and "leftover=" in out


def test_attempt_count_and_verdict_and_sha_rendered(ws_project):
    """AC1: real attempt count, outcome, and subject SHA appear in the summary."""
    project = ws_project["project"]
    sha = code_subject_identity(cwd=ws_project["worktree"])

    # Two real attempts on the same SHA.
    for _ in range(2):
        r = _run_qa_report(ws_project, verdict="rework_required", subject_sha=sha)
        assert r.returncode == 0, r.stderr

    state = derive_qa_state(project, "F001")
    assert state["attempts_total"] == 2

    out = wave_summary.generate(project, 0)
    assert "2 attempt(s)" in out
    assert "REWORK_REQUIRED" in out
    assert sha[:8] in out          # subject SHA rendered from real capture


def test_human_decision_rendered(ws_project):
    """AC1: latest human decision (approve / request_rework) appears in the summary."""
    project = ws_project["project"]
    sha = code_subject_identity(cwd=ws_project["worktree"])
    r = _run_qa_report(ws_project, verdict="human_review_required", subject_sha=sha)
    assert r.returncode == 0, r.stderr

    _write_human_decision(project, "F001", "approve", sha, n=1)
    assert "human: approve" in wave_summary.generate(project, 0)

    # Append a later decision (human-002) — newest (sorted[-1]) must win.
    _write_human_decision(project, "F001", "request_rework", sha, n=2)
    assert "human: request_rework" in wave_summary.generate(project, 0)


def test_no_dead_reads(ws_project):
    """AC2: no qa_cycles/overall_result reads remain, and a real verdict never
    renders the old 'UNKNOWN' default."""
    src = (_REPO_ROOT / "aah" / "core" / "build" / "wave_summary.py").read_text()
    assert "qa_cycles" not in src
    assert "overall_result" not in src

    project = ws_project["project"]
    r = _run_qa_report(ws_project, verdict="rework_required")
    assert r.returncode == 0, r.stderr
    out = wave_summary.generate(project, 0)
    assert "UNKNOWN" not in out
