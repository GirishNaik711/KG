"""Functional tests for validated runtime environment assembly (NO MOCKS).

Real .env files on disk, real parse_env_file, real assemble_runtime_env. Seeds
fake-secret values and asserts names-only, hash-only provenance.
"""

from __future__ import annotations

import hashlib

import pytest

from aah.core.common.readiness import assemble_runtime_env, parse_env_file


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_env_precedence_and_redaction(tmp_path):
    """AC2: deterministic precedence; undeclared/protected blocked; no values in evidence."""
    profile = {"allowed_env_keys": ["APP_MODE", "DB_URL", "FEATURE_FLAG"]}

    # A real project .env on disk.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_MODE=staging\nDB_URL=postgres://fake-secret-value@host/db\n"
    )
    project_env, errors = parse_env_file(
        env_file, profile["allowed_env_keys"], protected_keys=[]
    )
    assert errors == []
    assert project_env == {"APP_MODE": "staging", "DB_URL": "postgres://fake-secret-value@host/db"}

    process_env = {"APP_MODE": "dev", "FEATURE_FLAG": "off", "IGNORED": "x", "PATH": "/bin"}
    cmd_env = {"APP_MODE": "prod"}

    resolved, evidence = assemble_runtime_env(profile, process_env, project_env, cmd_env)

    # Precedence: process < project .env < cmd.env. cmd wins APP_MODE.
    assert resolved["APP_MODE"] == "prod"
    # project .env wins DB_URL (process didn't set it).
    assert resolved["DB_URL"] == "postgres://fake-secret-value@host/db"
    # process-only allowed key survives.
    assert resolved["FEATURE_FLAG"] == "off"
    # Undeclared ambient key filtered; protected PATH never admitted.
    assert "IGNORED" not in resolved
    assert "PATH" not in resolved

    # Evidence records source NAMES + value_sha256 ONLY — never a raw value.
    ev_text = repr(evidence)
    assert "fake-secret-value" not in ev_text
    assert "staging" not in ev_text
    assert evidence["sources"]["APP_MODE"]["final_source"] == "cmd_env"
    assert evidence["sources"]["APP_MODE"]["layers"] == ["process_env", "project_env", "cmd_env"]
    assert evidence["sources"]["DB_URL"]["value_sha256"] == _sha(resolved["DB_URL"])
    assert evidence["sources"]["APP_MODE"]["value_sha256"] == _sha("prod")


def test_env_protected_conflict_fails_closed(tmp_path):
    """A curated layer setting a protected key fails before startup."""
    profile = {"allowed_env_keys": ["APP_MODE"]}
    with pytest.raises(ValueError, match="protected env key"):
        assemble_runtime_env(profile, {}, {"PATH": "/evil"}, {})


def test_env_undeclared_curated_key_fails_closed(tmp_path):
    """A curated layer carrying an undeclared key fails before startup."""
    profile = {"allowed_env_keys": ["APP_MODE"]}
    with pytest.raises(ValueError, match="undeclared env key"):
        assemble_runtime_env(profile, {}, {"SURPRISE": "1"}, {})


def test_env_no_interpolation(tmp_path):
    """AC3: .env parsed without interpolation / command execution."""
    profile_keys = ["LITERAL", "CMD", "BRACED", "QUOTED"]
    env_file = tmp_path / ".env"
    # These MUST be preserved verbatim — no $VAR expansion, no $(...) execution.
    env_file.write_text(
        "LITERAL=$HOME/should/not/expand\n"
        "CMD=$(rm -rf /)\n"
        "BRACED=${OTHER}-suffix\n"
        'QUOTED="literal $VALUE inside quotes"\n'
    )
    parsed, errors = parse_env_file(env_file, profile_keys, protected_keys=[])
    assert errors == []
    assert parsed["LITERAL"] == "$HOME/should/not/expand"
    assert parsed["CMD"] == "$(rm -rf /)"
    assert parsed["BRACED"] == "${OTHER}-suffix"
    # Surrounding quotes stripped, inner content preserved verbatim.
    assert parsed["QUOTED"] == "literal $VALUE inside quotes"


def test_env_rejects_duplicate_malformed_protected_undeclared(tmp_path):
    """AC3: duplicate / malformed / protected / undeclared keys are rejected."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOD=1\n"
        "GOOD=2\n"          # duplicate
        "NOEQUALS\n"        # malformed
        "PATH=/x\n"         # protected
        "UNDECLARED=9\n"    # not in allowlist
    )
    parsed, errors = parse_env_file(
        env_file, declared_keys=["GOOD"], protected_keys=["PATH"]
    )
    joined = "\n".join(errors)
    assert "duplicate key 'GOOD'" in joined
    assert "malformed" in joined
    assert "protected key 'PATH'" in joined
    assert "undeclared key 'UNDECLARED'" in joined
    # Only the first clean GOOD is admitted.
    assert parsed == {"GOOD": "1"}


def test_env_parser_can_filter_unrelated_keys_for_scoped_consumer(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FEATURE_TOKEN=ok\nOTHER_FEATURE_TOKEN=private\n")

    parsed, errors = parse_env_file(
        env_file,
        declared_keys=["FEATURE_TOKEN"],
        protected_keys=[],
        ignore_undeclared=True,
    )

    assert errors == []
    assert parsed == {"FEATURE_TOKEN": "ok"}
