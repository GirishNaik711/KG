"""Tests for the stderr banner renderers extracted from orchestrator.py.

The banner had zero coverage while it was an 18-branch elif ladder inside the
orchestrator. It is pure presentation — dict in, string out — so it tests
directly with no project fixture.

What these pin, beyond "it renders":
  - every action the framework can actually emit has a banner, and every banner
    corresponds to an action the framework can emit (both directions, with an
    explicit exemption set that is itself checked for staleness)
  - an unknown action returns None rather than raising, so a new orchestrator
    action is silent on stderr instead of breaking the CLI
  - the frame is applied uniformly and blank body lines stay unindented
"""

from __future__ import annotations

import inspect
import re

from aah.core.build import orchestrator, verification_contracts
from aah.core.build.orchestrator_banners import (
    _BANNER_LINE,
    _BANNERS,
    format_checkpoint_banner,
)


def _emitted_actions() -> set[str]:
    """Every action the framework can hand to the skill.

    TWO sources, and a scan of only the first one lies:

    1. ``"action": "..."`` string literals in the orchestrator and the modules
       its routing was split into.
    2. ``ACTION_*`` constants in ``verification_contracts._ACTION_ORDER``,
       emitted by ``verify.py``'s ``col.fail`` / ``col.read`` calls and surfaced
       through ``action_from_verification_failure`` → ``_run_final_verification``
       → ``compute_next_action``. These reach the skill exactly like the literals
       do and **no literal-only grep sees them** — which is how an earlier
       analysis wrongly concluded a live, merge-blocking action was dead.
    """
    modules = [orchestrator]
    for name in ("qa_routing", "orchestrator_reports"):
        try:
            modules.append(__import__(f"aah.core.build.{name}", fromlist=["x"]))
        except ImportError:  # not yet extracted
            pass

    literals: set[str] = set()
    for module in modules:
        literals |= set(
            re.findall(r'"action":\s*"([a-z_]+)"', inspect.getsource(module))
        )
    return literals | set(verification_contracts._ACTION_ORDER)


# Actions the framework emits with no banner. Not a wish list — this is the
# inventory as extracted, recorded so that adding an action forces a deliberate
# choice about whether the operator needs stderr instructions.
NO_BANNER = {
    "blocked",
    "cleanup_branches",
    "complete",
    "dispatch_parallel",
    "error",
    "post_impl_comment",
    "reverify_evidence",
    "rework_qa_feedback",
    "run_feature_tests",
    "user_confirm",
    "wait",
}


def test_banner_table_matches_the_actions_the_framework_emits():
    """No banner for an action nothing emits; no emitted action without one.

    Both directions matter. Missing a banner silently drops an operator's
    instructions at a blocking gate; a banner for a dead action is a branch
    nobody can reach. The exemption set is checked for staleness too, so a
    removed action cannot leave a permanent lie behind in this file.
    """
    emitted = _emitted_actions()

    assert NO_BANNER <= emitted, (
        "exemption listed for an action the framework no longer emits: "
        f"{sorted(NO_BANNER - emitted)}"
    )
    assert not (set(_BANNERS) - emitted), (
        "banner exists for an action the framework never emits: "
        f"{sorted(set(_BANNERS) - emitted)}"
    )
    assert not (emitted - set(_BANNERS) - NO_BANNER), (
        "framework emits an action with no banner and no explicit exemption: "
        f"{sorted(emitted - set(_BANNERS) - NO_BANNER)}"
    )


def test_action_order_constants_reach_the_inventory():
    """Regression guard for a real analysis error.

    ``run_feature_tests`` appears in no ``"action":`` literal, so a literal-only
    scan calls it dead. It is emitted from ``verify.py`` through
    ``_ACTION_ORDER``. An inventory built from literals alone would miss it and
    every other constant-sourced action — which is exactly how an earlier
    analysis concluded a live merge-blocking action had no callers.
    """
    literals = set(
        re.findall(r'"action":\s*"([a-z_]+)"', inspect.getsource(orchestrator))
    )
    assert "run_feature_tests" not in literals
    assert "run_feature_tests" in verification_contracts._ACTION_ORDER
    assert "run_feature_tests" in _emitted_actions()
    # ...and the constant-only actions are a non-empty set, so the second source
    # is load-bearing rather than a subset of the first.
    assert set(verification_contracts._ACTION_ORDER) - literals


def test_there_are_exactly_eighteen_banner_arms():
    """The extraction ported all 18 arms and invented none."""
    assert len(_BANNERS) == 18


def test_unknown_and_missing_action_return_none():
    assert format_checkpoint_banner({}) is None
    assert format_checkpoint_banner({"action": "not_a_real_action"}) is None
    assert format_checkpoint_banner({"action": None}) is None


def test_every_banner_renders_from_an_empty_payload():
    """Defaults must cover a payload carrying only the action.

    Renderers read the payload with ``.get`` defaults; a missing key must not
    raise, because the banner runs on the CLI path after the decision is made —
    a KeyError here would turn a useful next-action into a traceback.
    """
    for action in _BANNERS:
        out = format_checkpoint_banner({"action": action})
        assert out is not None, action
        lines = out.split("\n")
        assert lines[0] == _BANNER_LINE, action
        assert lines[2] == _BANNER_LINE, action
        assert lines[-1] == _BANNER_LINE, action
        assert "Wave ?" in lines[1], action


def test_frame_and_indentation_contract():
    """Title and body get two spaces; blank body lines stay truly empty."""
    out = format_checkpoint_banner(
        {"action": "run_qa", "wave": 2, "features": ["F001", "F002"]}
    )
    assert out.split("\n") == [
        _BANNER_LINE,
        "  QA GATE — Wave 2",
        _BANNER_LINE,
        "  Features awaiting QA evaluation: F001, F002",
        "",
        "  Run QA evaluator for each feature before proceeding.",
        _BANNER_LINE,
    ]


def test_system_checkpoint_announces_keep_running_only_when_set():
    base = {"action": "run_system_checkpoint", "wave": 0}
    assert "KEEP_RUNNING" not in format_checkpoint_banner(base)
    assert "KEEP_RUNNING" not in format_checkpoint_banner({**base, "keep_running": False})
    assert "KEEP_RUNNING: true" in format_checkpoint_banner({**base, "keep_running": True})


def test_no_signal_explains_recreation_only_for_stale_evidence():
    """``no_signal`` is not a pass — the operator must be told to regenerate
    evidence rather than to retry QA, and the non-recreate arm must surface the
    orchestrator's own reason instead of a generic message."""
    stale = format_checkpoint_banner(
        {
            "action": "no_signal",
            "wave": 1,
            "features": ["F001"],
            "recreate_evidence": True,
        }
    )
    assert "Re-run feature tests in their worktrees" in stale
    assert "QA must NEVER run against stale or" in stale

    plain = format_checkpoint_banner(
        {
            "action": "no_signal",
            "wave": 1,
            "features": ["F001"],
            "reason": "profile hash is stale",
        }
    )
    assert "profile hash is stale" in plain
    assert "Re-run feature tests" not in plain
    assert "No reason provided" in format_checkpoint_banner(
        {"action": "no_signal", "wave": 1}
    )


def test_checkout_integration_names_both_branches():
    """The operator needs to see what branch verification wants AND what they
    are actually on — a bare 'wrong branch' message is not actionable."""
    out = format_checkpoint_banner(
        {
            "action": "checkout_integration",
            "wave": 2,
            "expected_branch": "integration/wave-2",
            "actual_branch": "feature/F001",
        }
    )
    assert "Verification requires integration/wave-2" in out
    assert "current branch is feature/F001" in out
    # Falls back to the conventional branch name and a git command.
    fallback = format_checkpoint_banner({"action": "checkout_integration", "wave": 5})
    assert "integration/wave-5" in fallback
    assert "git checkout integration/wave-5" in fallback


def test_generate_artifacts_truncates_the_missing_list_at_ten():
    missing = [{"feature_id": f"F{i:03d}", "artifact": "tests"} for i in range(13)]
    out = format_checkpoint_banner(
        {"action": "generate_artifacts", "wave": 4, "missing": missing}
    )
    assert "13 required artifact(s) missing." in out
    assert "F009: tests" in out
    assert "F010: tests" not in out
    assert "... and 3 more" in out

    short = format_checkpoint_banner(
        {"action": "generate_artifacts", "wave": 4, "missing": missing[:2]}
    )
    assert "more" not in short
    # A missing feature_id falls back to the wave, not to a KeyError.
    assert "wave-4: tests" in format_checkpoint_banner(
        {"action": "generate_artifacts", "wave": 4, "missing": [{"artifact": "tests"}]}
    )


def test_failure_maps_render_as_dict_keys_or_raw_value():
    """``failures`` arrives as a dict from the runtime gate and as a list from
    regression; both must render without a type check at the call site."""
    as_dict = format_checkpoint_banner(
        {"action": "fix_runtime_validation", "wave": 0, "failures": {"boot": 1, "smoke": 2}}
    )
    assert "Failed checks: boot, smoke" in as_dict
    as_list = format_checkpoint_banner(
        {"action": "raise_feedback", "wave": 0, "failures": ["boot", "smoke"]}
    )
    assert "Failed checks: ['boot', 'smoke']" in as_list


def test_human_review_shows_the_trigger_detail_and_the_evidence_path():
    payload = {
        "action": "human_review_required",
        "wave": 2,
        "feature_id": "F007",
        "trigger": "rework_cap",
        "attempts_total": 5,
        "rework_count": 3,
        "authoritative_attempt": "qa-results/F007/attempt-003.json",
    }
    out = format_checkpoint_banner(payload)
    assert "HUMAN REVIEW REQUIRED — Wave 2 (F007)" in out
    assert "Total attempts: 5 (rework count: 3)" in out
    assert "reached the rework cap (3 attempts)" in out
    assert "Authoritative attempt: qa-results/F007/attempt-003.json" in out
    assert "Ask the user once: Approve feature / Request changes." in out

    verdict = format_checkpoint_banner(
        {**payload, "trigger": "qa_verdict", "qa_state": {"current_verdict": "fail"}}
    )
    assert "Latest QA verdict: fail" in verdict
    assert "rework cap" not in verdict
    # An unrecognized trigger degrades to the header without a detail line.
    other = format_checkpoint_banner({**payload, "trigger": "something_new"})
    assert "Trigger: something_new" in other
    assert "rework cap" not in other and "Latest QA verdict" not in other


def test_human_review_banner_leaks_no_subject_sha():
    """The subject SHA is deliberately absent from the payload and the banner.

    A per-feature review is bound to a QA *attempt*, not to a hash the operator
    copies. Reintroducing it here would re-imply a hash-keyed decision record
    that the recorder does not write.
    """
    out = format_checkpoint_banner(
        {
            "action": "human_review_required",
            "wave": 1,
            "feature_id": "F001",
            "trigger": "qa_verdict",
            "subject_sha": "deadbeefcafebabe",
        }
    )
    assert "deadbeef" not in out
    assert "Subject SHA" not in out


def test_tier_banners_are_one_indexed_for_humans():
    """Tiers are stored zero-indexed but shown one-indexed; the tier numbers in
    the merge command body stay raw so they match what the CLI expects."""
    merge = format_checkpoint_banner(
        {
            "action": "merge_tier_to_integration",
            "wave": 1,
            "tier": 0,
            "total_tiers": 3,
            "next_tier": 1,
            "features": ["F001"],
        }
    )
    assert "MERGE TIER 1/3 TO INTEGRATION — Wave 1" in merge
    assert "Tier 0 complete. Merge features before starting tier 1." in merge

    advance = format_checkpoint_banner(
        {"action": "advance_tier", "wave": 1, "current_tier": 0, "next_tier": 1, "total_tiers": 3}
    )
    assert "Tier 1 → 2/3" in advance
    assert "Tier 0 merged to integration. Advancing to tier 1." in advance


def test_banners_module_is_pure_presentation():
    """No filesystem, no git, no decisions — and nothing imported back from the
    orchestrator, which is what keeps the dependency one-way."""
    src = inspect.getsource(
        __import__("aah.core.build.orchestrator_banners", fromlist=["x"])
    )
    for forbidden in ("import orchestrator", "read_json", "read_yaml", "subprocess", "Path("):
        assert forbidden not in src, forbidden
