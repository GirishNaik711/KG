"""Functional tests for the fail-closed required_env credential gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aah.core.build.evidence import EvidenceError
from aah.core.build.run_feature_tests import (
    _dotenv_values,
    _missing_required_env,
    _parse_junit_xml,
    _redirected_test_environment,
    _required_env_keys,
    _resolve_required_env,
    _sanitize_test_output,
    _secret_patterns,
)
from aah.core.common.git_utils import run_git
from aah.core.common.redaction import exact_value_patterns, sanitize_structure
from aah.core.common.validators import (
    FEATURE_SCHEMA,
    validate_required_env_keys,
    validate_dict_schema,
)
from aah.core.git_ops.setup_wave_worktrees import setup_wave_worktrees


def _write_feature(features_dir: Path, fid: str, required_env=None) -> None:
    features_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"## Id\n{fid}\n", "## Description\nx\n"]
    if required_env is not None:
        if isinstance(required_env, list):
            rendered = "\n".join(f"- {key}" for key in required_env)
        else:
            rendered = str(required_env)
        lines.append(f"## Required Env\n{rendered}\n")
    (features_dir / f"{fid}.md").write_text("\n".join(lines), encoding="utf-8")


def test_dotenv_uses_only_real_root_file_and_declared_keys(tmp_path):
    (tmp_path / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
    assert _dotenv_values(tmp_path, ["TOKEN"]) == {}

    (tmp_path / ".env").write_text(
        "TOKEN=real\nUNRELATED_SECRET=private\n",
        encoding="utf-8",
    )
    assert _dotenv_values(tmp_path, ["TOKEN"]) == {"TOKEN": "real"}


def test_root_and_subject_requirements_are_additive(tmp_path):
    subject = tmp_path / "wt"
    _write_feature(
        subject / ".aah" / "plan" / "features",
        "F001",
        required_env=["ROOT_KEY", "NEW_KEY"],
    )
    root_yaml = {"id": "F001", "required_env": ["ROOT_KEY"]}
    assert _required_env_keys(root_yaml, "F001", subject) == ["ROOT_KEY", "NEW_KEY"]


def test_empty_subject_list_cannot_remove_root_requirement(tmp_path):
    subject = tmp_path / "wt"
    _write_feature(subject / ".aah" / "plan" / "features", "F001", required_env=[])
    root_yaml = {"id": "F001", "required_env": ["ROOT_KEY"]}
    assert _required_env_keys(root_yaml, "F001", subject) == ["ROOT_KEY"]


@pytest.mark.parametrize("value", ["not-a-list", {"k": "v"}, 42])
def test_malformed_required_env_fails_closed(tmp_path, value):
    with pytest.raises(EvidenceError, match="must be a list"):
        _required_env_keys({"required_env": value}, "F001", tmp_path)


@pytest.mark.parametrize(
    "keys, expected",
    [
        ([""], "must not be empty"),
        (["NOT-VALID"], "invalid environment key"),
        (["TOKEN", "TOKEN"], "duplicate environment key"),
        (["PATH"], "protected environment key"),
        ([123], "expected str"),
    ],
)
def test_required_env_key_validation(keys, expected):
    assert any(expected in error for error in validate_required_env_keys(keys))


def test_feature_schema_runs_required_env_item_validation():
    schema = {"required_env": FEATURE_SCHEMA["required_env"]}
    errors = validate_dict_schema({"required_env": ["PATH"]}, schema, context="F001")
    assert any("protected environment key 'PATH'" in error for error in errors)


def test_root_dotenv_satisfies_gate_and_empty_value_does_not(tmp_path):
    subject = tmp_path / "wt"
    subject.mkdir()
    (tmp_path / ".env").write_text("A=set\nB=\n", encoding="utf-8")
    feature = {"required_env": ["A", "B"]}
    assert _missing_required_env(feature, "F001", tmp_path, subject) == ["B"]


def test_process_env_wins_over_root_dotenv(tmp_path, monkeypatch):
    subject = tmp_path / "wt"
    subject.mkdir()
    monkeypatch.setenv("TOKEN", "from-process")
    (tmp_path / ".env").write_text("TOKEN=from-dotenv\n", encoding="utf-8")
    resolved, missing = _resolve_required_env(
        {"required_env": ["TOKEN"]},
        "F001",
        tmp_path,
        subject,
    )
    assert missing == []
    assert resolved == {"TOKEN": "from-process"}


def test_only_declared_dotenv_values_enter_subprocess(tmp_path, monkeypatch):
    subject = tmp_path / "wt"
    subject.mkdir()
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    (tmp_path / ".env").write_text(
        "TOKEN=real\nUNRELATED_SECRET=private\n",
        encoding="utf-8",
    )
    resolved, missing = _resolve_required_env(
        {"required_env": ["TOKEN"]},
        "F001",
        tmp_path,
        subject,
    )
    env = _redirected_test_environment(tmp_path / "ns", resolved)
    assert missing == []
    assert env["TOKEN"] == "real"
    assert "UNRELATED_SECRET" not in env


def test_unrelated_ambient_secret_does_not_enter_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_AMBIENT_SECRET", "ambient-private")
    monkeypatch.setenv("DECLARED_TOKEN", "declared-private")

    env = _redirected_test_environment(
        tmp_path / "ns",
        {"DECLARED_TOKEN": os.environ["DECLARED_TOKEN"]},
    )

    assert env["DECLARED_TOKEN"] == "declared-private"
    assert "UNRELATED_AMBIENT_SECRET" not in env
    assert env.get("PATH") == os.environ.get("PATH")


def test_malformed_dotenv_fails_closed(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=one\nTOKEN=two\n", encoding="utf-8")
    with pytest.raises(EvidenceError, match="duplicate key 'TOKEN'"):
        _dotenv_values(tmp_path, ["TOKEN"])


def test_resolved_secret_values_are_redacted_from_output():
    output = _sanitize_test_output(
        "connection failed for opaque-short-secret",
        {"TOKEN": "opaque-short-secret"},
    )
    assert "opaque-short-secret" not in output
    assert "REDACTED" in output


def test_junit_fields_are_recursively_redacted(tmp_path):
    secret = "opaque-junit-secret"
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite time="0.1"><testcase name="case-opaque-junit-secret" '
        'classname="suite.opaque-junit-secret" time="0.1">'
        '<failure message="token=opaque-junit-secret">'
        'traceback opaque-junit-secret</failure></testcase></testsuite>',
        encoding="utf-8",
    )

    parsed = _parse_junit_xml(
        junit,
        extra_patterns=tuple(_secret_patterns({"TOKEN": secret})),
    )
    rendered = json.dumps(parsed)

    assert secret not in rendered
    assert "REDACTED" in rendered


def test_short_exact_secret_does_not_corrupt_structural_values():
    sanitized = sanitize_structure(
        {"status": "fail", "message": "credential=fail"},
        extra_patterns=exact_value_patterns(["fail"]),
        preserve_keys={"status"},
    )

    assert sanitized["status"] == "fail"
    assert sanitized["message"] == "credential=***REDACTED***"


def test_setup_worktree_never_exposes_root_dotenv(git_repo):
    aah_path = git_repo / ".aah"
    aah_path.mkdir()
    (aah_path / "manifest.yaml").write_text(
        "branching_config:\n  integration_prefix: integration/wave-\n",
        encoding="utf-8",
    )
    run_git(["branch", "integration/wave-0"], cwd=git_repo)
    (git_repo / ".env").write_text("TOKEN=present\n", encoding="utf-8")

    result = setup_wave_worktrees(git_repo, 0, ["F001"])
    assert result["success"] is True
    worktree = Path(result["worktrees"]["F001"]["path"])
    assert not (worktree / ".env").exists()
    assert not (worktree / ".env").is_symlink()
    assert _dotenv_values(git_repo, ["TOKEN"]) == {"TOKEN": "present"}


def test_setup_removes_only_legacy_root_dotenv_link(git_repo):
    aah_path = git_repo / ".aah"
    aah_path.mkdir()
    (aah_path / "manifest.yaml").write_text(
        "branching_config:\n  integration_prefix: integration/wave-\n",
        encoding="utf-8",
    )
    run_git(["branch", "integration/wave-0"], cwd=git_repo)
    (git_repo / ".env").write_text("TOKEN=present\n", encoding="utf-8")
    first = setup_wave_worktrees(git_repo, 0, ["F001"])
    worktree = Path(first["worktrees"]["F001"]["path"])
    legacy_link = worktree / ".env"
    try:
        legacy_link.symlink_to((git_repo / ".env").resolve())
    except OSError:
        pytest.skip("platform does not permit symlink creation")

    second = setup_wave_worktrees(git_repo, 0, ["F001"])

    assert second["success"] is True
    assert not legacy_link.exists()
    assert not legacy_link.is_symlink()


def test_setup_refuses_unexpected_worktree_dotenv_without_deleting(git_repo):
    aah_path = git_repo / ".aah"
    aah_path.mkdir()
    (aah_path / "manifest.yaml").write_text(
        "branching_config:\n  integration_prefix: integration/wave-\n",
        encoding="utf-8",
    )
    run_git(["branch", "integration/wave-0"], cwd=git_repo)
    first = setup_wave_worktrees(git_repo, 0, ["F001"])
    worktree = Path(first["worktrees"]["F001"]["path"])
    dotenv = worktree / ".env"
    dotenv.write_text("USER_DATA=keep\n", encoding="utf-8")

    second = setup_wave_worktrees(git_repo, 0, ["F001"])

    assert second["success"] is False
    assert "unexpected worktree .env file" in "\n".join(second["errors"])
    assert dotenv.read_text(encoding="utf-8") == "USER_DATA=keep\n"


def test_no_required_env_is_a_noop(tmp_path):
    assert _missing_required_env({}, "F001", tmp_path, tmp_path) == []


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
