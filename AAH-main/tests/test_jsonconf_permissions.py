"""Tests for jsonconf.merge_permissions reconcile behavior.

merge_permissions treats the settings-defaults file as the single source of
truth for the permissions aah manages. On each install/setup it:
  * adds default entries not already present, and
  * prunes entries aah previously added (recorded under `_aah`) that the
    defaults no longer list — without touching entries the user added.

This auto-heals stale rules (e.g. the redundant `Write(<secret>)` deny entries
Claude Code now warns about) on the next `aah setup`, with no migration step.
"""

from __future__ import annotations

import json
from pathlib import Path

from aah.core.install.formats import jsonconf

DEFAULTS_FILE = (
    Path(__file__).resolve().parents[1] / "aah" / "hooks" / "settings-defaults.json"
)


def test_fresh_install_adds_defaults_and_records_them():
    data: dict = {}
    defaults = {"deny": ["Read(**/.env)", "Edit(**/.env)"]}
    allow_added, deny_added = jsonconf.merge_permissions(data, defaults)
    assert data["permissions"]["deny"] == ["Read(**/.env)", "Edit(**/.env)"]
    assert set(deny_added) == {"Read(**/.env)", "Edit(**/.env)"}
    assert allow_added == []
    assert set(data["_aah"]["settings_deny"]) == {"Read(**/.env)", "Edit(**/.env)"}


def test_stale_aah_entry_is_pruned():
    """A rule aah added before but no longer wants is removed on re-merge."""
    data = {
        "permissions": {"deny": ["Write(**/.env)", "Edit(**/.env)", "Read(**/.env)"]},
        "_aah": {"settings_deny": ["Write(**/.env)", "Edit(**/.env)", "Read(**/.env)"]},
    }
    jsonconf.merge_permissions(data, {"deny": ["Read(**/.env)", "Edit(**/.env)"]})
    deny = data["permissions"]["deny"]
    assert "Write(**/.env)" not in deny
    assert "Edit(**/.env)" in deny and "Read(**/.env)" in deny
    assert set(data["_aah"]["settings_deny"]) == {"Edit(**/.env)", "Read(**/.env)"}


def test_user_added_entry_survives_prune():
    """An entry the user added (never recorded under _aah) is never pruned,
    even a Write() rule that aah has stopped shipping."""
    data = {
        "permissions": {"deny": ["Write(**/.env)", "Write(**/mine.txt)"]},
        # aah recorded only its own Write(**/.env); Write(**/mine.txt) is the user's.
        "_aah": {"settings_deny": ["Write(**/.env)"]},
    }
    jsonconf.merge_permissions(data, {"deny": ["Edit(**/.env)"]})
    deny = data["permissions"]["deny"]
    assert "Write(**/.env)" not in deny, "aah-recorded stale rule pruned"
    assert "Write(**/mine.txt)" in deny, "user rule preserved"
    assert "Edit(**/.env)" in deny, "new default added"


def test_user_entry_coinciding_with_stale_default_is_preserved():
    """If the user independently added a rule identical to a stale aah default,
    it is NOT pruned because it was never recorded under _aah."""
    data = {
        "permissions": {"deny": ["Write(**/.env)"]},
        "_aah": {},  # aah never recorded it — it is the user's
    }
    jsonconf.merge_permissions(data, {"deny": ["Edit(**/.env)"]})
    assert "Write(**/.env)" in data["permissions"]["deny"]


def test_reconcile_is_idempotent():
    data: dict = {}
    defaults = {"deny": ["Read(**/.env)", "Edit(**/.env)"], "allow": ["Bash"]}
    jsonconf.merge_permissions(data, defaults)
    snapshot = json.dumps(data, sort_keys=True)
    allow_added, deny_added = jsonconf.merge_permissions(data, defaults)
    assert allow_added == [] and deny_added == []
    assert json.dumps(data, sort_keys=True) == snapshot


def test_pruning_last_managed_entry_drops_empty_field_and_marker_key():
    data = {
        "permissions": {"deny": ["Write(**/.env)"]},
        "_aah": {"settings_deny": ["Write(**/.env)"]},
    }
    jsonconf.merge_permissions(data, {"deny": []})
    # Field removed once empty; marker key removed once nothing is managed.
    assert "deny" not in data.get("permissions", {})
    assert "settings_deny" not in data["_aah"]


def test_shipped_defaults_have_no_write_deny_rules():
    """Guard: the settings-defaults file must express secret-file protection via
    Edit()/Read(), never Write() — Claude Code file checks ignore Write() rules
    and now warn about them. See settings-defaults.json _comment."""
    defaults = json.loads(DEFAULTS_FILE.read_text())
    deny = defaults["claude"]["permissions"]["deny"]
    write_rules = [e for e in deny if e.startswith("Write(")]
    assert not write_rules, f"Write() deny rules must not ship: {write_rules}"


def test_shipped_defaults_keep_edit_coverage_for_secrets():
    """Removing Write() rules must not drop protection: every secret path still
    has an Edit() rule (which covers Write in Claude Code)."""
    defaults = json.loads(DEFAULTS_FILE.read_text())
    deny = set(defaults["claude"]["permissions"]["deny"])
    for path in (
        "**/.env",
        # NOTE: no blanket "**/.env.*" — a negatable-free glob deny there would also
        # block the safe templates (.env.example/.template/.sample) that the framework
        # requires agents to edit. Layer 1 hard-denies only the real-secret variants;
        # secrets_guard.py governs the rest of the .env.* family with a template allowlist.
        "**/.env.local",
        "**/.env.*.local",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.pfx",
        "**/credentials.json",
        "**/secrets.yaml",
        "**/token.json",
        "**/service_account*.json",
        "**/service-account*.json",
    ):
        assert f"Edit({path})" in deny, f"missing Edit({path}) coverage"
