"""Phase 1.5 / Finding 4: spec test for the verification_artifact_guard if: glob.

The Claude Code harness owns the actual matcher behind the ``if:`` field
in settings.json hook entries. We can't drive the harness from a unit
test, but we can codify our expectations: the ``if:`` field for the
verification_artifact_guard must (a) match every results-bucket path
across Write/Edit/MultiEdit, and (b) NOT match plan/discuss/manifest/
audit paths.

This is a **spec test, not an integration test.** It uses ``fnmatch`` —
which treats ``*`` and ``**`` as "any characters" — to assert path
coverage. We've verified above that fnmatch's permissiveness behaves
correctly for our path shapes (positive paths match, plausible
look-alikes don't).

If a future settings.json edit drops a bucket, this test fails
immediately. That's the lowest-cost way to close the Finding-4 gap
without owning the harness's matcher.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest


SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"
SETTINGS_DEFAULTS = (
    Path(__file__).resolve().parents[1] / "aah" / "hooks" / "settings-defaults.json"
)


_TOOL_RE = re.compile(r"(Write|Edit|MultiEdit)\((.*?)\)(?:\||$)")


# Pinned in code so a future edit that drops one of these from the
# settings.json if-clause causes a clear "spec violation" failure rather
# than the test silently passing on a degraded clause.
EXPECTED_RESULT_BUCKETS = (
    "test-results",
    "runtime-results",
    "quality-results",
    "checkpoint-results",
    "validation-results",
)


def _verification_clause() -> str:
    """Locate the verification_artifact_guard's if: pattern in settings.json."""
    data = json.loads(SETTINGS.read_text())
    for entry in data["hooks"]["PreToolUse"]:
        # Match the entry by its hook command, not by its matcher,
        # so a future settings.json that splits this across multiple
        # entries continues to work.
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            if "verification_artifact_guard" in cmd:
                if_clause = hook.get("if", "")
                if if_clause:
                    return if_clause
    raise AssertionError(
        "verification_artifact_guard hook with an if: clause not found in settings.json"
    )


def _globs_for_tool(if_clause: str, tool: str) -> list[str]:
    """Extract every Tool(<glob>) clause for ``tool`` from a pipe-separated if: string."""
    return [
        m.group(2)
        for m in _TOOL_RE.finditer(if_clause)
        if m.group(1) == tool
    ]


def _matches_any(globs: list[str], path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


# ---------------------------------------------------------------------------
# Positive coverage — every results-bucket path must match
# ---------------------------------------------------------------------------


POSITIVE_PATHS = [
    "/x/proj/.aah/build/test-results/F001.json",
    "/x/proj/.aah/build/runtime-results/wave-0-all.json",
    "/x/proj/.aah/build/quality-results/F001-static-analysis.json",
    "/x/proj/.aah/build/checkpoint-results/wave-0-system-checkpoint.json",
    "/x/proj/.aah/build/validation-results/F001-spec-validation.json",
    # Subagent worktree path: a aah-feature-implementer running in a worktree
    # writes a result file at this path and the guard MUST still cover it.
    "/x/proj/.claude/worktrees/agent-abc/.aah/build/test-results/F001.json",
]


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", POSITIVE_PATHS)
def test_verification_artifact_globs_cover_positive(tool: str, path: str) -> None:
    if_clause = _verification_clause()
    globs = _globs_for_tool(if_clause, tool)
    assert globs, f"no globs for {tool} in if: clause: {if_clause!r}"
    assert _matches_any(globs, path), (
        f"{tool}({path!r}) is a verification-artifact write but the if: glob "
        f"does not match. Globs for {tool}: {globs!r}"
    )


# ---------------------------------------------------------------------------
# Negative coverage — non-verification paths must NOT match
# ---------------------------------------------------------------------------


NEGATIVE_PATHS = [
    "/x/proj/src/main.py",
    "/x/proj/.aah/plan/features/F001.yaml",
    "/x/proj/.aah/manifest.yaml",
    "/x/proj/.aah/audit/log.json",
    "/x/proj/.aah/codebase-intel/expertise.yaml",
    # Look-alikes that lack a load-bearing path segment:
    "/x/proj/.aah/test-results/F001.json",          # no /implement/
    "/x/proj/test-results/F001.json",                  # no /.aah/
]


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("path", NEGATIVE_PATHS)
def test_verification_artifact_globs_skip_negative(tool: str, path: str) -> None:
    if_clause = _verification_clause()
    globs = _globs_for_tool(if_clause, tool)
    assert not _matches_any(globs, path), (
        f"{tool}({path!r}) is NOT a verification artifact but the if: glob "
        f"matches anyway. Globs for {tool}: {globs!r}"
    )


# ---------------------------------------------------------------------------
# Coverage completeness — every expected bucket appears in the if-clause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("bucket", EXPECTED_RESULT_BUCKETS)
def test_every_results_bucket_present_in_if_clause(tool: str, bucket: str) -> None:
    """If a future edit drops one of the buckets from the if-clause,
    fail loudly here even if the positive/negative path tests happen
    to pass under a degraded clause."""
    if_clause = _verification_clause()
    globs = _globs_for_tool(if_clause, tool)
    assert any(bucket in g for g in globs), (
        f"if-clause for {tool} is missing the {bucket!r} bucket. "
        f"Globs for {tool}: {globs!r}"
    )


# ---------------------------------------------------------------------------
# Settings JSON well-formed
# ---------------------------------------------------------------------------


def test_settings_json_parses() -> None:
    """Catch JSON syntax breakage in settings.json on any edit."""
    json.loads(SETTINGS.read_text())


def test_attestation_secret_is_in_deny_list() -> None:
    """Phase 1 invariant: agents must not be able to read the secret."""
    data = json.loads(SETTINGS_DEFAULTS.read_text())
    deny = data["claude"].get("permissions", {}).get("deny", [])
    assert "Read(**/.aah/build/.attestation-secret)" in deny, (
        "Read deny entry for .aah/build/.attestation-secret missing from "
        "permissions.deny in settings-defaults.json"
    )


# ---------------------------------------------------------------------------
# Phase 3G hook wiring invariants
# ---------------------------------------------------------------------------


def _hooks_in(data: dict, event: str, matcher: str | None = None) -> list[dict]:
    """Flatten all hook entries under hooks[event][*] (optionally filtered
    by top-level matcher) into a single list of {type, command, ...} dicts.
    """
    out = []
    for entry in data.get("hooks", {}).get(event, []):
        if matcher is not None and entry.get("matcher") != matcher:
            continue
        out.extend(entry.get("hooks", []) or [])
    return out


def test_session_start_includes_run_init_check() -> None:
    """Phase 3G Wire 2: run_init_check must run on SessionStart so env
    breakage surfaces early, before users invest 30 min in a doomed wave."""
    data = json.loads(SETTINGS.read_text())
    hooks = _hooks_in(data, "SessionStart")
    commands = [h.get("command", "") for h in hooks]
    assert any("run_init_check" in cmd for cmd in commands), (
        "SessionStart must include aah.core.build.run_init_check. "
        f"Found commands: {commands!r}"
    )


def test_session_start_run_init_check_has_reasonable_timeout() -> None:
    """Defense against: someone removes the timeout and the hook hangs
    on a slow init.sh, blocking SessionStart indefinitely."""
    data = json.loads(SETTINGS.read_text())
    for hook in _hooks_in(data, "SessionStart"):
        if "run_init_check" in hook.get("command", ""):
            timeout = hook.get("timeout")
            assert isinstance(timeout, int) and 5 <= timeout <= 120, (
                f"run_init_check SessionStart hook needs a 5-120s timeout, "
                f"got {timeout!r}"
            )
            return
    pytest.fail("run_init_check hook entry not found")
