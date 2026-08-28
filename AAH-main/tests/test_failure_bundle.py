"""Failure-bundle, cleanup, redaction, and retention tests (NO MOCKS).

Covers AC3 (cleanup producers), AC5 (bundle fields), AC6 (log redaction),
AC7 (bounded cleanup scope), AC8 (retention fail-closed). Real git projects,
real worktrees, real attestation secret + governance CLI, real pytest
subprocesses that emit seeded fake secrets, and real on-disk bundle inspection.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._isolation_helpers import commit_worktree, make_isolated_project

from aah.core.build import evidence
from aah.core.build.evidence import (
    EvidenceError,
    _validate_write_scope,
    read_evidence_retention,
    write_attested_result,
    write_failure_bundle,
)
from aah.core.build.ensure_infra import cleanup_namespace
from aah.core.build.run_feature_tests import COMMAND_PREFIX, run_feature_tests


# --- test bodies ----------------------------------------------------------

_PASS_BODY = "def test_TC1_ok():\n    assert True\n"

_FAIL_BODY = "def test_TC1_fail():\n    assert False, 'deliberate failure'\n"

_SECRET_FAIL_BODY = (
    "def test_TC1_secret_fail():\n"
    "    print('AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE')\n"
    "    print('password=hunter2')\n"
    "    print('Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789')\n"
    "    print('-----BEGIN RSA PRIVATE KEY-----')\n"
    "    print('MIIEowIBAAKCAQEA1234567890abcdefINLINEKEYMATERIAL')\n"
    "    print('-----END RSA PRIVATE KEY-----')\n"
    "    assert False, 'fail after leaking secrets'\n"
)


def _run(state: dict, **kwargs) -> dict:
    return run_feature_tests(
        state["feature_id"],
        state["project"],
        subject_path=state["worktree"],
        subject_branch=state["branch"],
        subject_sha=state["sha"],
        actor="implementer",
        attempt_id="attempt-001",
        wave=0,
        **kwargs,
    )


# --- AC3: cleanup producers record all outcomes + leftovers ----------------


def _assert_cleanup_shape(cleanup: dict) -> None:
    # Mirrors verify.py _stop_server cleanup shape so consumers read it uniformly.
    for key in ("transport", "action", "removed", "leftover", "errors", "stopped", "timestamp"):
        assert key in cleanup, f"cleanup missing {key}: {cleanup}"
    assert isinstance(cleanup["removed"], list)
    assert isinstance(cleanup["leftover"], list)
    assert isinstance(cleanup["errors"], list)
    assert isinstance(cleanup["stopped"], bool)


def test_runner_cleanup_all_outcomes(tmp_path):
    """AC3: pass AND fail outcomes both record a structured cleanup block via
    the runner's finally path; the cleanup producer records removed/leftover/
    errors deterministically (interruption via an empty/bad namespace)."""
    # PASS outcome — cleanup still runs and is recorded.
    passing = make_isolated_project(tmp_path / "p", test_body=_PASS_BODY, feature_id="F001")
    res_pass = _run(passing)
    assert res_pass["passed"] is True
    assert "cleanup" in res_pass, res_pass
    _assert_cleanup_shape(res_pass["cleanup"])

    # FAIL outcome — cleanup runs even though the test failed (finally).
    failing = make_isolated_project(tmp_path / "f", test_body=_FAIL_BODY, feature_id="F002")
    res_fail = _run(failing)
    assert res_fail["passed"] is False
    assert "cleanup" in res_fail
    _assert_cleanup_shape(res_fail["cleanup"])

    # Interruption / leftover producer: the cleanup function itself is a real
    # producer. With no docker it fails safe (transport none, nothing removed);
    # an empty run_id is recorded as an error, never a silent success.
    producer = cleanup_namespace("aah-nonexistent-run-xyz", passing["project"])
    _assert_cleanup_shape(producer)
    bad = cleanup_namespace("", passing["project"])
    _assert_cleanup_shape(bad)
    assert bad["errors"], "empty run_id must be recorded as an error"


# --- AC5: bundle identifies SHA/argv/cwd/adapter/seed/tool versions/cleanup -


def test_bundle_fields(tmp_path):
    """AC5: a failing run writes reproduction.json with the required identity
    fields, tool versions, cleanup evidence, and per-log sha256."""
    state = make_isolated_project(tmp_path, test_body=_FAIL_BODY, feature_id="F001")
    res = _run(state)
    assert res["passed"] is False

    bundles = state["project"] / ".aah" / "build" / "failure-bundles"
    manifests = list(bundles.rglob("reproduction.json"))
    assert len(manifests) == 1, f"expected one bundle, got {manifests}"
    manifest = json.loads(manifests[0].read_text())

    assert manifest["subject"]["commit_sha"] == state["sha"]
    assert manifest["execution"]["argv"], "argv must be recorded"
    assert manifest["execution"]["cwd"] is not None
    assert "adapter" in manifest["execution"]
    assert "seed" in manifest["execution"]
    tv = manifest["tool_versions"]
    assert tv["python"] and "pytest" in tv and "docker" in tv
    assert manifest["cleanup"] is not None
    _assert_cleanup_shape(manifest["cleanup"])
    # Per-log sha256 present for every retained stream.
    assert manifest["logs"], "logs index must not be empty"
    for name, entry in manifest["logs"].items():
        assert entry["sha256"] and len(entry["sha256"]) == 64
        assert (manifests[0].parent / entry["file"]).is_file()


# --- AC6: seeded secrets absent from retained logs -------------------------


def test_logs_redacted(tmp_path):
    """AC6: seeded AWS key / password / bearer / PEM are ABSENT from retained
    logs and replaced with ***REDACTED***."""
    state = make_isolated_project(tmp_path, test_body=_SECRET_FAIL_BODY, feature_id="F001")
    res = _run(state)
    assert res["passed"] is False

    bundles = state["project"] / ".aah" / "build" / "failure-bundles"
    log_files = list(bundles.rglob("*.log"))
    assert log_files, "expected at least one retained log"
    blob = "\n".join(p.read_text() for p in log_files)

    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "hunter2" not in blob
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in blob
    assert "INLINEKEYMATERIAL" not in blob
    assert "***REDACTED***" in blob


def test_provisional_cli_scopes_credentials_and_never_writes_evidence(tmp_path):
    secret = "opaque-provisional-secret"
    test_body = (
        "import os\n"
        "def test_F001_TC1_scoped_secret():\n"
        "    assert 'UNRELATED_DOTENV_SECRET' not in os.environ\n"
        "    assert 'UNRELATED_AMBIENT_SECRET' not in os.environ\n"
        "    assert False, f\"credential={os.environ['FEATURE_TOKEN']}\"\n"
    )
    state = make_isolated_project(tmp_path, test_body=test_body, feature_id="F001")
    for checkout in (state["project"], state["worktree"]):
        contract = checkout / ".aah" / "plan" / "features" / "F001.md"
        text = contract.read_text(encoding="utf-8")
        contract.write_text(
            text.replace("test_config:\n", "required_env:\n  - FEATURE_TOKEN\ntest_config:\n"),
            encoding="utf-8",
        )
    (state["project"] / ".env").write_text(
        f"FEATURE_TOKEN={secret}\nUNRELATED_DOTENV_SECRET=dotenv-private\n",
        encoding="utf-8",
    )
    env = dict(state["cli_env"])
    env["UNRELATED_AMBIENT_SECRET"] = "ambient-private"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "aah.core.build.run_feature_tests",
            "--feature-id",
            "F001",
            "--project-path",
            str(state["project"]),
            "--subject-path",
            str(state["worktree"]),
            "--provisional",
        ],
        cwd=state["project"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert secret not in output
    assert "dotenv-private" not in output
    assert "ambient-private" not in output
    assert "***REDACTED***" in output
    assert '"mode": "provisional"' in proc.stdout
    assert '"evidence_eligible": false' in proc.stdout
    assert not (
        state["project"] / ".aah" / "build" / "test-results" / "F001.json"
    ).exists()


def test_official_junit_result_and_attestation_never_persist_required_secret(tmp_path):
    secret = "opaque-official-junit-secret"
    test_body = (
        "import os\n"
        "def test_F001_TC1_secret_failure():\n"
        "    token = os.environ['FEATURE_TOKEN']\n"
        "    assert False, f'credential={token}'\n"
    )
    state = make_isolated_project(tmp_path, test_body=test_body, feature_id="F001")
    for checkout in (state["project"], state["worktree"]):
        contract = checkout / ".aah" / "plan" / "features" / "F001.md"
        text = contract.read_text(encoding="utf-8")
        contract.write_text(
            text.replace("test_config:\n", "required_env:\n  - FEATURE_TOKEN\ntest_config:\n"),
            encoding="utf-8",
        )
    commit_worktree(state, "declare feature credential")
    (state["project"] / ".env").write_text(
        f"FEATURE_TOKEN={secret}\n",
        encoding="utf-8",
    )

    result = _run(state)
    assert result["passed"] is False
    assert secret not in json.dumps(result)
    assert "***REDACTED***" in json.dumps(result)

    output = state["project"] / ".aah" / "build" / "test-results" / "F001.json"
    write_attested_result(
        result,
        output,
        project_path=state["project"],
        command=COMMAND_PREFIX + ["--feature-id", "F001"],
        artifact_name="F001 test results",
    )

    persisted = output.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "***REDACTED***" in persisted


# --- AC7: cleanup / write cannot target paths outside the namespace --------


def test_cleanup_scope_bounded(tmp_path):
    """AC7: write/delete targets outside the .aah/worktrees scope are refused
    and nothing is written; a sentinel outside scope survives."""
    project = tmp_path / "project"
    (project / ".aah").mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keepme.txt"
    sentinel.write_text("do-not-touch", encoding="utf-8")

    subject = {"commit_sha": "deadbeef", "branch": "b", "rel_path": "."}
    execution = {"argv": ["x"], "cwd": ".", "project_root": str(project)}
    logs = {"stderr": "boom"}
    retention = read_evidence_retention(project)

    # (1) absolute out-of-namespace target
    with pytest.raises(EvidenceError):
        write_failure_bundle(
            run_id="r1", subject=subject, execution=execution, cleanup=None,
            logs=logs, retention=retention, out_dir=Path("/tmp/outside"),
        )
    # (2) project root itself is not a write scope
    with pytest.raises(EvidenceError):
        write_failure_bundle(
            run_id="r2", subject=subject, execution=execution, cleanup=None,
            logs=logs, retention=retention, out_dir=project,
        )
    # (3) parent-traversal escape
    with pytest.raises(EvidenceError):
        write_failure_bundle(
            run_id="r3", subject=subject, execution=execution, cleanup=None,
            logs=logs, retention=retention, out_dir=project / ".aah" / ".." / ".." / "outside",
        )
    # The scope gate itself refuses these three targets directly.
    for bad in (Path("/tmp/outside"), project, project / ".aah" / ".." / ".." / "outside"):
        with pytest.raises(EvidenceError):
            _validate_write_scope(bad, project)

    # A legitimate .aah target IS allowed.
    good = write_failure_bundle(
        run_id="ok", subject=subject, execution=execution, cleanup=None,
        logs=logs, retention=retention, out_dir=project / ".aah" / "build" / "failure-bundles",
    )
    assert good.is_dir()

    # The sentinel outside scope is untouched.
    assert sentinel.read_text() == "do-not-touch"

    # cleanup_namespace performs NO filesystem deletion — only docker calls.
    result = cleanup_namespace("aah-somerun", project)
    _assert_cleanup_shape(result)
    assert sentinel.read_text() == "do-not-touch"


def test_bundle_uses_fixed_stream_cap_and_cached_tool_versions(tmp_path):
    project = tmp_path / "project"
    out_dir = project / ".aah" / "build" / "failure-bundles"
    out_dir.mkdir(parents=True)
    subject = {"commit_sha": "deadbeef", "branch": "b", "rel_path": "."}
    execution = {"argv": ["x"], "cwd": ".", "project_root": str(project)}
    retention = read_evidence_retention(project)
    evidence._tool_versions.cache_clear()

    first = write_failure_bundle(
        run_id="first",
        subject=subject,
        execution=execution,
        cleanup=None,
        logs={f"stream-{i}": "bounded" for i in range(65)},
        retention=retention,
        out_dir=out_dir,
    )
    second = write_failure_bundle(
        run_id="second",
        subject=subject,
        execution=execution,
        cleanup=None,
        logs={"stderr": "again"},
        retention=retention,
        out_dir=out_dir,
    )

    manifest = json.loads((first / "reproduction.json").read_text(encoding="utf-8"))
    assert len(manifest["logs"]) == 64
    assert len(manifest["dropped_log_streams"]) == 1
    assert (second / "reproduction.json").exists()
    cache = evidence._tool_versions.cache_info()
    assert cache.misses == 1 and cache.hits >= 1

# --- AC8: retention policy fail-closed -------------------------------------


def test_evidence_retention_policy_fail_closed(tmp_path):
    """AC8: retention is fixed, local-only, and cannot authorize deletion."""
    state = make_isolated_project(tmp_path, test_body=_PASS_BODY, feature_id="F001")
    project = state["project"]

    fixed = read_evidence_retention(project)
    assert fixed["source"] == "fixed"
    assert fixed["local_log_days"] == 30
    assert fixed["allow_destructive_cleanup"] is False

    # A legacy policy cannot change runtime behavior and is not deleted.
    policy_path = project / ".aah" / "plan" / "evidence-retention.yaml"
    policy_path.write_text(
        "local_log_days: 0\nallow_destructive_cleanup: true\n", encoding="utf-8"
    )
    assert read_evidence_retention(project) == fixed
    assert policy_path.exists()

    with pytest.raises(EvidenceError):
        _validate_write_scope(Path("/tmp/outside"), project)
