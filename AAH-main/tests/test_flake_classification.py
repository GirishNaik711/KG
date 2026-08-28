"""Diagnostic-rerun flake classification (NO MOCKS, no clock monkeypatch).

AC4: a test that deterministically FAILS ONCE THEN PASSES (via a real on-disk
marker file, not a patched clock) is classified nondeterministic_failure, and
the ORIGINAL gate stays failing/blocking — there is no retry-to-green. A test
that always fails is reproducible_failure.
"""

from __future__ import annotations

from pathlib import Path

from tests._isolation_helpers import make_isolated_project

from aah.core.build.run_feature_tests import run_feature_tests


# Fail-once-then-pass: the first invocation creates a sentinel and fails; every
# later invocation sees the sentinel and passes. The marker lives under the
# checkout's .aah/ (an EPHEMERAL path — not part of the evaluated source/test
# surface, so it does not trip assert_subject_unchanged) yet persists across
# the two runs of the same subject. No monkeypatched clock — real on-disk state.
_FAIL_ONCE_BODY = (
    "from pathlib import Path\n"
    "def test_TC1_flaky():\n"
    "    marker = Path(__file__).resolve().parent.parent / '.aah' / 'build' / '.ran_once'\n"
    "    marker.parent.mkdir(parents=True, exist_ok=True)\n"
    "    if not marker.exists():\n"
    "        marker.write_text('ran')\n"
    "        assert False, 'first run fails'\n"
    "    assert True\n"
)

_ALWAYS_FAIL_BODY = "def test_TC1_always_fail():\n    assert False, 'always'\n"


def _run(state: dict, *, diagnostic_rerun: bool) -> dict:
    return run_feature_tests(
        state["feature_id"],
        state["project"],
        subject_path=state["worktree"],
        subject_branch=state["branch"],
        subject_sha=state["sha"],
        actor="implementer",
        attempt_id="attempt-001",
        wave=0,
        diagnostic_rerun=diagnostic_rerun,
    )


def test_no_retry_to_green(tmp_path):
    """AC4: fail-once-then-pass stays a FAILING gate but is classified
    nondeterministic_failure — the diagnostic never flips passed to True."""
    state = make_isolated_project(tmp_path, test_body=_FAIL_ONCE_BODY, feature_id="F001")

    res = _run(state, diagnostic_rerun=True)

    # Original gate stays failed — NO retry-to-green.
    assert res["passed"] is False, res
    assert res.get("status") != "pass"

    diag = res.get("diagnostic")
    assert diag is not None, "diagnostic rerun must be recorded"
    assert diag["classification"] == "nondeterministic_failure", diag
    assert diag["first_exit"] != 0
    assert diag["rerun_exit"] == 0  # the rerun passed, but the gate does NOT flip


def test_reproducible_failure(tmp_path):
    """AC4 companion: an always-failing test is reproducible_failure and stays
    blocking; exactly one rerun occurs."""
    state = make_isolated_project(tmp_path, test_body=_ALWAYS_FAIL_BODY, feature_id="F002")

    res = _run(state, diagnostic_rerun=True)

    assert res["passed"] is False
    diag = res.get("diagnostic")
    assert diag is not None
    assert diag["classification"] == "reproducible_failure", diag
    assert diag["first_exit"] != 0
    assert diag["rerun_exit"] != 0


def test_no_diagnostic_without_flag(tmp_path):
    """Without --diagnostic-rerun, no rerun happens (bounded subprocess count)."""
    state = make_isolated_project(tmp_path, test_body=_ALWAYS_FAIL_BODY, feature_id="F003")
    res = _run(state, diagnostic_rerun=False)
    assert res["passed"] is False
    assert res.get("diagnostic") is None
