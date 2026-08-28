#!/usr/bin/env python3
"""
stderr presentation for orchestrator results.

Pure presentation: a dict goes in, a string comes out. Nothing here reads the
filesystem, runs git, or decides anything — the orchestrator has already decided
by the time a result reaches this module. That one-way dependency is why this
module imports nothing from ``orchestrator.py``; ``orchestrator.py`` imports
``format_checkpoint_banner`` back for its CLI.

Each action gets one renderer returning ``(title, body_lines)``; ``_BANNERS``
maps the action name to it and ``format_checkpoint_banner`` frames the result.
An action with no entry gets no banner — that is the deliberate default, so a
new action is silent on stderr rather than crashing the CLI.
"""

from __future__ import annotations

from typing import Callable

_BANNER_LINE = "═" * 61

# Renderer contract: (result, wave) -> (title, body_lines). ``wave`` is passed
# in because every title needs it and every renderer would otherwise re-read it.
Renderer = Callable[[dict, object], "tuple[str, list[str]]"]


def _merge_tier_to_integration(result: dict, wave: object) -> tuple[str, list[str]]:
    features = result.get("features", [])
    tier = result.get("tier", 0)
    total_tiers = result.get("total_tiers", 1)
    next_tier = result.get("next_tier", tier + 1)
    return (
        f"MERGE TIER {tier + 1}/{total_tiers} TO INTEGRATION — Wave {wave}",
        [
            f"Tier {tier} complete. Merge features before starting tier {next_tier}.",
            f"Features to merge: {', '.join(features)}",
            "",
            f"Command: aah run core.git_ops.merge_wave_to_integration merge-feature --wave {wave} --feature-id <FXXX>",
        ],
    )


def _advance_tier(result: dict, wave: object) -> tuple[str, list[str]]:
    tier = result.get("current_tier", 0)
    next_tier = result.get("next_tier", tier + 1)
    total_tiers = result.get("total_tiers", 1)
    return (
        f"ADVANCE TIER — Wave {wave}, Tier {tier + 1} → {next_tier + 1}/{total_tiers}",
        [
            f"Tier {tier} merged to integration. Advancing to tier {next_tier}.",
            "",
            "Next: dispatch tier features (branches from updated integration/wave-N).",
        ],
    )


def _merge_features_to_integration(result: dict, wave: object) -> tuple[str, list[str]]:
    features = result.get("features", [])
    return (
        f"MERGE TO INTEGRATION — Wave {wave}",
        [
            f"Features not yet merged to integration branch: {', '.join(features)}",
            "",
            "Merge each feature before running regression:",
            f"Command: aah run core.git_ops.merge_wave_to_integration merge-feature --wave {wave} --feature-id <FXXX>",
        ],
    )


def _checkout_integration(result: dict, wave: object) -> tuple[str, list[str]]:
    expected = result.get("expected_branch", f"integration/wave-{wave}")
    actual = result.get("actual_branch", "unknown")
    return (
        f"CHECKOUT INTEGRATION — Wave {wave}",
        [
            f"Verification requires {expected}; current branch is {actual}.",
            "",
            f"Command: {result.get('command', f'git checkout {expected}')}",
        ],
    )


def _run_regression(result: dict, wave: object) -> tuple[str, list[str]]:
    return (
        f"SYSTEM CHECKPOINT — Wave {wave}",
        [
            f"All features in wave {wave} merged to integration.",
            "",
            "Next: Run regression suite",
            f"Command: aah run core.build.run_regression_suite --wave {wave}",
        ],
    )


def _run_system_checkpoint(result: dict, wave: object) -> tuple[str, list[str]]:
    keep_running = result.get("keep_running", False)
    body = [
        "Regression suite passed.",
        "",
        "Next: System checkpoint (runtime validation)",
        "Dispatch: aah-runtime-validator subagent",
    ]
    if keep_running:
        body.append("")
        body.append("KEEP_RUNNING: true (user review follows — app stays alive after validation)")
    return f"SYSTEM CHECKPOINT — Wave {wave}", body


def _fix_runtime_validation(result: dict, wave: object) -> tuple[str, list[str]]:
    failures = result.get("failures", {})
    return (
        f"SYSTEM CHECKPOINT FAILED — Wave {wave}",
        [
            "Runtime validation failed. Fix required before merge.",
            "",
            f"Failed checks: {', '.join(failures.keys()) if isinstance(failures, dict) else str(failures)}",
            "",
            "Route to aah-fix, then call next-action for a fresh checkpoint.",
        ],
    )


def _user_review(result: dict, wave: object) -> tuple[str, list[str]]:
    checkpoint_id = result.get("checkpoint_id", "")
    covered = result.get("covered_features", [])
    access_path = result.get("access_info_path", "")
    return (
        f"USER REVIEW CHECKPOINT — Wave {wave} ({checkpoint_id})",
        [
            "All automated checks passed. User approval required before merge.",
            "",
            f"Covered features: {', '.join(covered)}",
            "",
            "System is running (kept alive from system checkpoint).",
            "",
            "MANDATORY STEPS (execute in order — do NOT skip any):",
            f"1. Read access info from: {access_path} — get the URL/port",
            "2. Display feature summary (read feature YAMLs + QA reports)",
            "3. Show CONCRETE testing guide with actual commands (curl, docker exec, etc.)",
            "   — Every command must be copy-paste ready with expected output",
            "   — Do NOT use generic steps like 'System starts successfully'",
            "4. Ask user: 'Approve wave N' / 'Request changes' / 'Stop implementation'",
            "",
            "If access info file is missing, dispatch aah-runtime-validator to verify/start the system.",
        ],
    )


def _merge(result: dict, wave: object) -> tuple[str, list[str]]:
    return (
        f"WAVE COMPLETE — Wave {wave} Ready to merge",
        [
            "All tests + regression + runtime validation passed.",
            "Artifact gate satisfied.",
            "",
            f"Command: aah run core.git_ops.promote_to_develop  --wave {wave}",
        ],
    )


def _run_qa(result: dict, wave: object) -> tuple[str, list[str]]:
    features = result.get("features", [])
    return (
        f"QA GATE — Wave {wave}",
        [
            f"Features awaiting QA evaluation: {', '.join(features)}",
            "",
            "Run QA evaluator for each feature before proceeding.",
        ],
    )


def _no_signal(result: dict, wave: object) -> tuple[str, list[str]]:
    features = result.get("features", [])
    recreate = result.get("recreate_evidence", False)
    if recreate:
        body = [
            f"Blocked: QA for features: {', '.join(features)}",
            "",
            "Why: Evidence is missing, stale, or subject checkout cannot be proven.",
            "This means one or more of:",
            "  - Feature worktree is missing or not a registered git worktree",
            "  - Feature worktree branch doesn't match expected feature/* branch",
            "  - Feature worktree HEAD SHA has advanced past the tested commit",
            "  - Feature worktree has uncommitted changes (dirty tree)",
            "  - Feature-test evidence is missing or attestation failed",
            "",
            "Action: Re-run feature tests in their worktrees to produce fresh",
            "evidence before QA can proceed. QA must NEVER run against stale or",
            "unproven checkouts.",
        ]
    else:
        body = [
            f"Blocked: QA for features: {', '.join(features)}",
            "",
            result.get("reason", "No reason provided"),
        ]
    return f"QA BLOCKED (NO SIGNAL) — Wave {wave}", body


def _run_project_standards(result: dict, wave: object) -> tuple[str, list[str]]:
    return (
        f"STANDARDS GATE (whole codebase) — Wave {wave}",
        [
            "Final wave: run the standards check ONCE over the entire codebase.",
            "",
            f"Subject: {result.get('subject_branch', f'integration/wave-{wave}')}",
            f"Why now: {result.get('signal_reason', 'evidence not usable')}",
            "",
            f"Command: {result.get('command', '')}",
        ],
    )


def _fix_standards(result: dict, wave: object) -> tuple[str, list[str]]:
    failures = result.get("failures", {})
    body = [
        "Whole-codebase standards reported a blocking verdict.",
        "",
    ]
    for check in ("linting", "static_analysis"):
        output = failures.get(check) if isinstance(failures, dict) else None
        if not output:
            continue
        body.append(f"{check}:")
        # Verbatim tool output — the first lines are what a developer would read.
        body.extend(f"  {line}" for line in str(output).splitlines()[:10])
        body.append("")
    body.extend([
        "Hand to Skill(\"aah-fix\"): lint is mechanically repairable on the",
        "integration branch (direct-repair path). A genuine security finding may",
        "escalate through the decision loop instead — that is intended.",
        "",
        "aah-fix must delete the standards evidence after committing the repair",
        "so next-action re-runs the gate cleanly.",
    ])
    return f"STANDARDS FAILED — Wave {wave} (route: aah-fix)", body


def _generate_wave_summary(result: dict, wave: object) -> tuple[str, list[str]]:
    return (
        f"WAVE SUMMARY — Wave {wave}",
        [
            f"Generate the wave {wave} completion summary before merge.",
            "",
            f"Command: {result.get('command', f'aah run core.build.wave_summary --wave {wave}')}",
            "",
            "After it writes, call next-action to resume the orchestrator loop.",
        ],
    )


def _generate_artifacts(result: dict, wave: object) -> tuple[str, list[str]]:
    n_missing = len(result.get("missing", []))
    body = [
        f"{n_missing} required artifact(s) missing.",
        "",
        "Missing artifacts:",
    ]
    for item in result.get("missing", [])[:10]:
        fid = item.get("feature_id", f"wave-{wave}")
        artifact = item.get("artifact", "unknown")
        body.append(f"  - {fid}: {artifact}")
    if len(result.get("missing", [])) > 10:
        body.append(f"  ... and {len(result['missing']) - 10} more")
    body.append("")
    body.append("Generate missing artifacts before merge can proceed.")
    return f"ARTIFACT GATE — Wave {wave}", body


def _fix_regression(result: dict, wave: object) -> tuple[str, list[str]]:
    return (
        f"REGRESSION FAILED — Wave {wave}",
        [
            "Regression suite failed. Fix before merging.",
            "",
            f"Failures: {len(result.get('failures', []))}",
        ],
    )


def _update_expertise(result: dict, wave: object) -> tuple[str, list[str]]:
    features = result.get("features", [])
    return (
        f"EXPERTISE UPDATE — Wave {wave} ({len(features)} features)",
        [
            f"All features passed: {', '.join(features)}",
            "",
            f"Domains affected: {', '.join(result.get('domains_affected', []))}",
            "Follow aah-expertise skill: update expertise.yaml + domains/*.yaml + consumption_views.",
        ],
    )


def _raise_feedback(result: dict, wave: object) -> tuple[str, list[str]]:
    failures = result.get("failures", {})
    return (
        f"DESIGN FEEDBACK — Wave {wave} (route: aah-fix)",
        [
            "Post-wave design failure — targets already-built feature(s).",
            "",
            f"Failed checks: {', '.join(failures.keys()) if isinstance(failures, dict) else str(failures)}",
            "",
            "Hand to Skill(\"aah-fix\"): it rebuilds the affected feature(s) in the",
            "CURRENT wave (before promote); the orchestrator re-verifies over the",
            "fixed code, and only then does the wave promote. Nothing survives promote.",
        ],
    )


def _human_review_required(result: dict, wave: object) -> tuple[str, list[str]]:
    feature_id = result.get("feature_id", "?")
    trigger = result.get("trigger", "unknown")
    attempts = result.get("attempts_total", 0)
    rework_count = result.get("rework_count", 0)
    body = [
        f"Feature: {feature_id}",
        f"Trigger: {trigger}",
        f"Total attempts: {attempts} (rework count: {rework_count})",
        "",
        "QA has reached a state requiring human decision:",
    ]
    if trigger == "rework_cap":
        body.append(f"  - Feature has reached the rework cap ({rework_count} attempts)")
        body.append("  - No further automated QA runs will occur")
    elif trigger == "qa_verdict":
        qa_state = result.get("qa_state", {})
        current_verdict = qa_state.get("current_verdict", "unknown")
        body.append(f"  - Latest QA verdict: {current_verdict}")
    body.extend([
        "",
        "Evidence:",
        f"  Authoritative attempt: {result.get('authoritative_attempt', 'N/A')}",
        "",
        "Ask the user once: Approve feature / Request changes.",
        "Record the selected response with the action command, then call next-action.",
    ])
    return f"HUMAN REVIEW REQUIRED — Wave {wave} ({feature_id})", body


_BANNERS: dict[str, Renderer] = {
    "merge_tier_to_integration": _merge_tier_to_integration,
    "advance_tier": _advance_tier,
    "merge_features_to_integration": _merge_features_to_integration,
    "checkout_integration": _checkout_integration,
    "run_regression": _run_regression,
    "run_system_checkpoint": _run_system_checkpoint,
    "fix_runtime_validation": _fix_runtime_validation,
    "user_review": _user_review,
    "merge": _merge,
    "run_qa": _run_qa,
    "no_signal": _no_signal,
    "run_project_standards": _run_project_standards,
    "fix_standards": _fix_standards,
    "generate_wave_summary": _generate_wave_summary,
    "generate_artifacts": _generate_artifacts,
    "fix_regression": _fix_regression,
    "update_expertise": _update_expertise,
    "raise_feedback": _raise_feedback,
    "human_review_required": _human_review_required,
}


def format_checkpoint_banner(result: dict) -> str | None:
    """Format a CLI checkpoint banner for stderr based on orchestrator result.

    Returns a formatted banner string, or None if the action doesn't warrant one.
    """
    renderer = _BANNERS.get(result.get("action"))
    if renderer is None:
        return None
    title, body = renderer(result, result.get("wave", "?"))

    lines = [
        _BANNER_LINE,
        f"  {title}",
        _BANNER_LINE,
    ]
    for line in body:
        lines.append(f"  {line}" if line else "")
    lines.append(_BANNER_LINE)
    return "\n".join(lines)
