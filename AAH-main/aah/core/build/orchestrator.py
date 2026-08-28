#!/usr/bin/env python3
"""
Deterministic implementation orchestrator.

Eliminates LLM reasoning about state by computing exactly what needs to happen
next. The LLM calls one command and gets a direct instruction.

Commands:
  next-action    — What should happen RIGHT NOW? Returns one clear action.
  wave-readiness — Is this wave done? What's blocking?
  test-summary   — Consolidated test results across all features.
  frontier       — What's available, blocked, and why?
"""

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.build.codemap_refresh import COMMAND_PREFIX as CODEMAP_REFRESH_PREFIX
from aah.core.build.orchestrator_banners import format_checkpoint_banner
from aah.core.build.orchestrator_reports import (
    check_wave_readiness,
    format_qa_summary,
    get_frontier_with_reasons,
    get_qa_report,
    get_test_summary,
)
from aah.core.build.qa_routing import (
    # Unused while PER_FEATURE_QA_ENABLED is False — the wave-completion term it
    # backed is gone. KEPT, not deleted: it is half the one-line diff that
    # re-arms the per-feature QA step, and dropping it would make "disabled"
    # look like "removed".
    _feature_qa_final_approved,  # noqa: F401
    _feature_qa_state_action,
    _features_needing_qa,
)
from aah.core.build.qa_evidence import latest_human_decision
from aah.core.common.audit import append_gate_decision
from aah.core.common.dag import dag_from_json, get_execution_frontier
from aah.core.common.feature_list import load_feature_list
from aah.core.common.git_utils import (
    GitError,
    branch_contains_all_commits,
    branch_exists,
    current_branch,
    find_feature_commits,
    rev_parse,
)
from aah.core.common.io_utils import read_json, read_yaml, write_json
from aah.core.common.progress import load_progress, update_progress
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact
from aah.core.git_ops.merge_wave_to_integration import ensure_integration_branch
from aah.core.git_ops.setup_wave_worktrees import (
    restore_missing_feature_worktrees,
    setup_wave_worktrees,
)
from aah.core.common.feature_utils import (
    flatten_wave_features,
    load_feature_data,
)
from aah.core.common.manifest import rollout_mode
from aah.core.build.update_impl_state import get_wave_context
from aah.core.build.verification_evidence import (
    expertise_artifacts_problem,
    expertise_marker_fresh,
)
from aah.core.build.verify import (
    FEATURE_TEST_PREFIX,
    QUALITY_PREFIX,
    RUNTIME_RESULTS_PREFIXES,
    # Unused while PER_FEATURE_QA_ENABLED is False — the other half of the
    # removed wave-completion term. KEPT for the same reason as
    # _feature_qa_final_approved above; the function itself is untouched and
    # still used by feature_list.update_feature_status's own guard.
    feature_has_passed_attested_tests,  # noqa: F401
    read_attested,
    read_fresh_feature_evidence,
    read_regression_evidence,
    resolve_feature_subject,
    runtime_profile_evidence_validation,
    standards_evidence_passed,
    subject_identity,
)

MAX_PARALLEL_BATCH = 4

# §3 (5.1.1) — the per-feature QA step is DISABLED.
#
# Two things stop being invoked when this is False:
#   1. the `aah-qa-evaluator` dispatch (the `run_qa` gate in compute_next_action);
#   2. the per-feature attested-evidence gate (`feature_has_passed_attested_tests`
#      in `wave_completed`, and both per-feature calls in
#      `verify._check_features`).
#
# DISABLED MEANS NOT CALLED — the logic is NOT deleted. Every module, agent file,
# hook, and CLI remains in place; only the call sites stop invoking them, so
# re-arming the step is a matter of flipping this back to True and restoring the
# two call sites it guards. This flag is deliberately a module constant, not a
# manifest rollout flag: it is a release decision, not a per-project one, and an
# operator must not be able to half-enable it (arming one layer while another
# stays gated is what makes a wave hang).
#
# What is given up: "does this passing test prove its AC, or is it hollow?" now
# falls to the implementer's own Self-QA, and a feature whose tests failed merges
# anyway. The signal appears at the odd-wave runtime checkpoint or the last-wave
# regression instead of at the feature that caused it.
PER_FEATURE_QA_ENABLED = False
# A current-wave rework that keeps failing verify must escalate to a human after
# this many attempts rather than looping the rebuild→re-verify cycle forever
# (mirror of qa_routing.MAX_QA_REWORK_ATTEMPTS, which caps per-feature QA rework).
MAX_REWORK_ATTEMPTS = 3


def _rework_attempts_for_wave(aah_path: Path, wave: int) -> int:
    """Return the max attempt count across active reworks whose entries live in
    the given wave (used by the checkpoint gate's retry cap).

    In the current-wave rework model, a built-feature checkpoint failure routes
    to a rework entry placed in the CURRENT wave. Each rebuild→re-verify loop
    bumps the entry's ``attempts`` (see rework.trigger_rework). This reads the
    highest attempt count for any in-progress rework so the gate can escalate
    once it exceeds MAX_REWORK_ATTEMPTS.
    """
    rework_state_path = aah_path / "build" / "rework-state.json"
    if not rework_state_path.exists():
        return 0
    try:
        state = read_json(rework_state_path)
    except Exception:
        return 0
    attempts = 0
    for r in state.get("active_reworks", []):
        if r.get("status") != "in_progress":
            continue
        attempts = max(attempts, int(r.get("attempts", 1)))
    return attempts


def _record_regression_failure_attempt(
    aah_path: Path, wave: int, regression: dict
) -> int:
    """Count each distinct verified failing regression artifact once."""
    path = aah_path / "build" / "regression-repair-state.json"
    signature = ((regression.get("attestation") or {}).get("signature"))
    if not isinstance(signature, str) or not signature:
        return MAX_REWORK_ATTEMPTS

    try:
        state = read_json(path) if path.exists() else {"waves": {}}
        if not isinstance(state, dict):
            return MAX_REWORK_ATTEMPTS
        waves = state.setdefault("waves", {})
        if not isinstance(waves, dict):
            return MAX_REWORK_ATTEMPTS
        record = waves.setdefault(str(wave), {"failure_signatures": []})
        if not isinstance(record, dict):
            return MAX_REWORK_ATTEMPTS
        signatures = record.setdefault("failure_signatures", [])
        if not isinstance(signatures, list):
            return MAX_REWORK_ATTEMPTS
        if signature not in signatures:
            signatures.append(signature)
        record["attempts"] = len(signatures)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(state, path)
        return len(signatures)
    except (OSError, TypeError, ValueError):
        return MAX_REWORK_ATTEMPTS


def _clear_regression_failure_attempts(aah_path: Path, wave: int) -> None:
    """Retire a wave's retry budget only after verified regression passes."""
    path = aah_path / "build" / "regression-repair-state.json"
    if not path.exists():
        return
    try:
        state = read_json(path)
        waves = state.get("waves") if isinstance(state, dict) else None
        if not isinstance(waves, dict) or str(wave) not in waves:
            return
        waves.pop(str(wave), None)
        write_json(state, path)
    except (OSError, TypeError, ValueError):
        return


def _reverify_action(
    evidence_type: str,
    *,
    command: str,
    reason: str,
    wave: int | None = None,
    features: list[str] | None = None,
    feature_id: str | None = None,
) -> dict:
    """Construct a reverify_evidence action dict.

    The command must be the SAME writer the gate expects, so re-running it
    then re-calling next-action advances.
    """
    action_dict = {
        "action": "reverify_evidence",
        "evidence_type": evidence_type,
        "command": command,
        "reason": reason,
    }
    if wave is not None:
        action_dict["wave"] = wave
    if features is not None:
        action_dict["features"] = features
    if feature_id is not None:
        action_dict["feature_id"] = feature_id
    return action_dict


def _read_runtime_result_or_action(
    project_path: Path,
    aah_path: Path,
    wave: int,
    path: Path,
    missing_reason: str,
) -> tuple[dict | None, dict | None]:
    """Read runtime evidence or return its fail-closed recovery action."""
    artifact = load_attested_artifact(
        path, project_path, RUNTIME_RESULTS_PREFIXES, "json"
    )
    if artifact.state is ArtifactState.VERIFIED:
        return artifact.payload, None
    # A rotated secret and a missing artifact have the same remedy: the
    # aah-runtime-validator agent is the only producer of runtime evidence,
    # and re-signing requires the results blob only it holds.
    reason = missing_reason
    if artifact.state is ArtifactState.STALE_SECRET:
        reason = (
            f"Runtime evidence signature is stale ({artifact.reason}). "
            "Re-run runtime validation."
        )
    return None, {
        "action": "run_system_checkpoint",
        "wave": wave,
        # Kept running because a checkpoint wave is also a user-review wave —
        # the two cadences are paired by construction (is_checkpoint_wave and
        # determine_checkpoints.place_user_review_checkpoints encode the same
        # odd-plus-last rule), so the UCR always has a live system to show.
        "keep_running": True,
        "reason": reason,
    }


# ─── content-bound expertise freshness ───────────────────────


def _log_gate_decision(
    aah_path: Path,
    *,
    gate: str,
    wave: int,
    decision: str,
    reason: str,
) -> None:
    """Append one structured line to .aah/audit/gate-decisions.jsonl.

    Always-on so every content-bound freshness decision is auditable.

    decision is one of:
      - "pass"            : gate returned None (allow merge)
      - "refire"          : gate returned an action that will re-run the work
      - "error"           : gate could not evaluate (e.g. branch missing)

    Best-effort: a write failure is swallowed silently. The orchestrator
    must not block on log I/O.
    """
    append_gate_decision(
        aah_path,
        gate=gate,
        wave=wave,
        decision=decision,
        reason=reason,
    )


def _rollout(aah_path: Path, flag_name: str) -> str:
    """Return a manifest rollout mode without environment overrides."""
    return rollout_mode(aah_path, flag_name).value


# ─── orchestrator-internal verify ────────────────────────




# ─── runtime-profile gate ──────────────────────────


def _runtime_profile_rollout(aah_path: Path) -> str:
    """Return the runtime_profile_v1 rollout mode: off | report_only | enforce.

    Reads manifest.yaml features.runtime_profile_v1. ``True`` → enforce,
    ``"report_only"`` → report_only, everything else (including ``False`` and a
    missing/malformed manifest) → off. The manifest default is ``report_only``.

    MANIFEST-ONLY: there is NO environment override. Unlike the freshness /
    verify-internal flags, no ``AAH_*`` env var can raise OR lower the runtime
    profile state ("no environment bypass"). An operator flipping an env
    var can never downgrade an enforced runtime profile to report_only/off.
    """
    return _rollout(aah_path, "runtime_profile_v1")


def _runtime_profile_gate(
    project_path: Path,
    aah_path: Path,
    current_wave: int,
    runtime_data: dict,
) -> dict | None:
    """Gate a PASSING verify result against the bound runtime
    profile's ``required_checks``.

    Called ONLY on the pass path (verify's ``overall_passed`` is True): a
    present-but-failed check must still route by ``fix_category`` upstream, so
    this gate only ever DOWNGRADES a pass, never upgrades a failure
    (``no_signal ≠ pass`` is never weakened).

    Behaviour by rollout mode:
      * off         → ``None`` (today's behaviour; profile ignored).
      * report_only → always log a ``report_only`` gate-decision audit record,
        then return ``None`` (legacy verifier result stays display-only; zero
        behaviour change).
      * enforce     → collect problems; block (via the ``user_confirm`` action)
        if any, else ``None``.

    The set of required checks and the profile hash come from a SINGLE source —
    ``resolve_runtime_verification_profile`` — so the gate cannot drift from
    what runtime evidence embeds. ``runtime_profile.read_confirmed`` re-derives
    every bound hash.

    Problems that block under enforce:
      * runtime_evidence block absent from the result.
      * profile unresolved (the profile resolver returns None).
      * confirmation not ``confirmed`` (``read_confirmed``).
      * evidence.integration_sha != current HEAD (SHA binding).
      * profile_hash drift (evidence binding vs freshly resolved profile).
      * required_checks-set drift (evidence vs freshly resolved profile).
      * any required check not PRESENT-AND-PASSED in ``checks`` (a
        ``not_applicable`` skip counts as satisfied; absent / failed / no_signal
        does not).

    Resolution/confirmation exceptions are fail-closed: report_only → None,
    enforce → block.
    """
    mode = _runtime_profile_rollout(aah_path)
    if mode == "off":
        return None

    try:
        validation = runtime_profile_evidence_validation(project_path, runtime_data)
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        if mode == "report_only":
            _log_gate_decision(
                aah_path, gate="runtime_profile", wave=current_wave,
                decision="report_only",
                reason=f"resolution error (report_only, not enforced): {exc}",
            )
            return None
        return {
            "action": "user_confirm",
            "wave": current_wave,
            "runtime_profile_state": mode,
            "failures": {},
            "reason": (
                f"Runtime profile could not be evaluated ({exc}). Enforced "
                "runtime_profile_v1 fails closed — operator/environment action required."
            ),
        }

    problems = validation["problems"]
    resolved_required = validation["required_checks"]

    if mode == "report_only":
        _log_gate_decision(
            aah_path, gate="runtime_profile", wave=current_wave,
            decision="report_only",
            reason=(
                f"problems={problems or 'none'} "
                f"required_checks={resolved_required}"
            ),
        )
        return None

    # enforce
    if not problems:
        _log_gate_decision(
            aah_path, gate="runtime_profile", wave=current_wave,
            decision="pass",
            reason=f"all required checks present-and-passed: {resolved_required}",
        )
        return None

    _log_gate_decision(
        aah_path, gate="runtime_profile", wave=current_wave,
        decision="refire", reason=f"enforced block: {problems}",
    )
    # Reuse the single user_confirm action contract (operator/environment
    # action). Do NOT hijack raise_feedback / fix_runtime_validation — a runtime
    # profile block is an operator concern, not a code-fix or design-issue.
    return {
        "action": "user_confirm",
        "wave": current_wave,
        "runtime_profile_state": mode,
        "failures": {p.split(":", 1)[0]: p for p in problems},
        "reason": (
            "Enforced runtime_profile_v1 gate blocked the passing verify result: "
            f"{problems[0]}. All problems: {problems}. Operator/environment action required."
        ),
    }


# ─── next-action ─────────────────────────────────────────────────────


def _migrate_checkpoint_cadence(project_path: Path, current_wave: int) -> dict | None:
    """Run the one-time 5.1.1 checkpoint-cadence migration (§6a).

    Projects planned before 5.1.1 carry a ``checkpoint-config.yaml`` whose
    ``user_review_checkpoints`` list one review after EVERY wave. This rewrites
    it once, on resume, with the odd-plus-last cadence, preserving checkpoints
    for the current and earlier waves so an already-approved UCR is never
    clobbered.

    Version-gated and idempotent: once the marker is current this is a config
    read and nothing more. Never raises — a migration must not be able to break
    resume, so any failure degrades to "no migration" and the loop continues on
    the existing config.
    """
    try:
        from aah.core.plan.determine_checkpoints import migrate_checkpoint_config

        return migrate_checkpoint_config(project_path, current_wave)
    except Exception as exc:  # noqa: BLE001 — never block the loop on migration
        print(
            f"warning: checkpoint-config migration skipped ({exc})",
            file=sys.stderr,
        )
        return None


def compute_next_action(project_path: Path) -> dict:
    """Determine the single next action, running the resume migration first.

    Thin wrapper over ``_next_action`` so the one-time checkpoint-cadence
    migration runs before any cadence gate reads ``checkpoint-config.yaml``, and
    so its notice can be attached to whichever action comes back — the inner
    function has ~30 return points and threading a field through all of them
    would be a change to every one of them.
    """
    project_path = Path(project_path)
    aah_path = project_path / ".aah"
    progress_path = aah_path / "claude-progress.json"
    progress = load_progress(progress_path if progress_path.exists() else None)
    notice = _migrate_checkpoint_cadence(
        project_path, progress.get("current_wave") or 0
    )

    action = _next_action(project_path)
    if notice:
        # Surfaced to the operator by the build skill; also audited so the
        # forced runtime re-run is explainable after the fact.
        action["migration_notice"] = notice
        _log_gate_decision(
            aah_path, gate="checkpoint_config_migration",
            wave=progress.get("current_wave") or 0,
            decision="migrated", reason=str(notice.get("notice", "")),
        )
        print(f"⚙ {notice.get('notice', '')}", file=sys.stderr)
    return action


def _next_action(project_path: Path) -> dict:
    """
    Determine the SINGLE next action the orchestrator should take.

    Returns exactly ONE of:
      - dispatch_team / dispatch_parallel
      - run_qa (feature_id)
      - run_project_standards (wave — whole codebase, last wave only)
      - fix_standards (wave — whole-codebase standards failed)
      - merge_features_to_integration (wave, features)
      - checkout_integration (wave, command)
      - run_regression (wave — cumulative suite, last wave only)
      - run_system_checkpoint (wave — runtime/system validation, checkpoint
        waves only: odd 0-indexed waves plus the last)
      - fix_runtime_validation (wave, failures — system checkpoint failed)
      - raise_feedback (wave, design issue)
      - user_confirm (wave, unfixable issue)
      - user_review (wave, UCR checkpoint — blocks merge until user approves)
      - merge (wave)
      - complete
      - error (reason)
    """
    aah_path = project_path / ".aah"

    # Load state
    progress = load_progress(aah_path / "claude-progress.json" if (aah_path / "claude-progress.json").exists() else None)
    current_wave = progress.get("current_wave") or 0
    current_tier = progress.get("current_tier") or 0

    fl_path = aah_path / "feature-list.json"
    fl_data = load_feature_list(fl_path if fl_path.exists() else None)
    features = fl_data.get("features", [])
    completed = {f["id"] for f in features if f.get("passes", False)}

    # Load waves
    waves_path = aah_path / "plan" / "waves.json"
    if not waves_path.exists():
        return {"action": "error", "reason": "waves.json not found. Run /aah-plan first."}

    waves_data = read_json(waves_path)
    # Keep the RAW (nested) wave structure — do NOT pre-flatten. The tier
    # split below (wave_raw[0] is-a-list check) needs the per-wave tier lists
    # intact; pre-flattening would collapse every wave to a single tier and
    # make total_tiers always report 1. Every downstream consumer that selects
    # a wave re-flattens it via flatten_wave_features(), so the raw structure
    # is safe everywhere else (len(waves), indexing, enumerate).
    waves = waves_data.get("waves", [])

    if not waves:
        return {"action": "error", "reason": "No waves defined."}

    # Check if previous wave's feature branches need cleanup (recovery path).
    # Uses the audit log written by _cleanup_feature_branches — one JSON read
    # instead of N git subprocess calls on every next-action invocation.
    if current_wave > 0:
        cleanup_log = aah_path / "audit" / "branch-cleanup-log.json"
        if cleanup_log.exists():
            events = read_json(cleanup_log).get("cleanup_events", [])
            already_cleaned = any(e.get("wave") == current_wave - 1 for e in events)
        else:
            already_cleaned = False
        if not already_cleaned:
            return {
                "action": "cleanup_branches",
                "wave": current_wave - 1,
                "reason": f"Wave {current_wave - 1} branch cleanup not recorded. Clean up before starting wave {current_wave}.",
                "command": f"aah run core.git_ops.promote_to_develop --wave {current_wave - 1} --cleanup-branches",
            }

    # Wave summary gate — ensure the previous wave's summary was generated
    # before moving on to the next wave's work.
    if current_wave > 0:
        prev_wave = current_wave - 1
        summary_path = aah_path / "build" / "wave-summaries" / f"wave-{prev_wave}-summary.md"
        if not summary_path.exists():
            prev_wave_raw = waves[prev_wave] if prev_wave < len(waves) else []
            prev_wave_fids = flatten_wave_features(prev_wave_raw)
            return {
                "action": "generate_wave_summary",
                "wave": prev_wave,
                "features": prev_wave_fids,
                "reason": f"Wave {prev_wave} summary not yet generated. Generate before starting wave {current_wave}.",
                "command": f"aah run core.build.wave_summary --wave {prev_wave}",
            }

    # Rework retirement (forward-relocation model).
    #
    # In the forward model, aah-plan places superseding rework entries (e.g.
    # F2-rework-01) into a forward wave, and the NORMAL wave walk below builds
    # them — there is no backward re-entry, no revert, and no sequential
    # rework dispatch. The only bookkeeping here is retiring an active-rework
    # record once every feature it named has reached passes=true. That keeps
    # `rework status` accurate for the aah-fix post-rework follow-up without
    # perturbing the forward walk.
    rework_state_path = aah_path / "build" / "rework-state.json"
    if rework_state_path.exists():
        rework_state = read_json(rework_state_path)
        active_reworks = [
            r for r in rework_state.get("active_reworks", [])
            if r.get("status") == "in_progress"
        ]
        retired_any = False
        for rework in active_reworks:
            affected = rework.get("affected_features", [])
            if affected and all(fid in completed for fid in affected):
                rework["status"] = "resolved"
                rework["resolved_at"] = datetime.now(timezone.utc).isoformat()
                retired_any = True
        if retired_any:
            from aah.core.common.io_utils import write_json as _wj
            _wj(rework_state, rework_state_path)

    # NOTE: there is NO deferred-feedback machinery. A built-feature defect
    # discovered at the checkpoint HOLDS the wave open and is rebuilt in the
    # CURRENT wave (the design_issue branch below routes to aah-fix, which
    # creates a current-wave rework entry); the orchestrator re-verifies before
    # promote. Nothing survives promote.

    # Find current wave — handle post-completion re-entry
    # After plan re-entry adds new waves, or if current_wave points past
    # the last wave, scan for the first wave with incomplete features.
    if current_wave >= len(waves):
        # Scan all waves for first with incomplete features (post-completion re-entry)
        resumed_wave = None
        for wave_idx, w in enumerate(waves):
            w_fids = flatten_wave_features(w)
            if not all(fid in completed for fid in w_fids):
                resumed_wave = wave_idx
                break
        if resumed_wave is None:
            return {"action": "complete", "reason": "All waves finished.", "completed_features": len(completed), "total_features": len(features)}
        # Update current_wave to the first incomplete wave
        current_wave = resumed_wave
        progress_path = aah_path / "claude-progress.json"
        if progress_path.exists():
            update_progress(progress_path, wave=current_wave)

    wave_raw = waves[current_wave]

    # Flat list of every feature ID in the wave (handles dict/nested/flat).
    wave_feature_ids = flatten_wave_features(wave_raw)

    if len(wave_feature_ids) > 1:
        recovery = restore_missing_feature_worktrees(
            project_path,
            current_wave,
            [feature_id for feature_id in wave_feature_ids if feature_id in completed],
        )
        if not recovery["success"]:
            return {
                "action": "no_signal",
                "wave": current_wave,
                "signal_reason": "feature_worktree_unavailable",
                "reason": "Could not restore feature worktree: "
                + "; ".join(recovery["errors"]),
            }

    # Tier split: detect nested tier format [[tier0], [tier1], ...]; dict and
    # flat waves collapse to a single tier holding all features.
    if isinstance(wave_raw, list) and wave_raw and isinstance(wave_raw[0], list):
        wave_tiers = wave_raw
    else:
        wave_tiers = [wave_feature_ids]

    total_tiers = len(wave_tiers)
    # Clamp current_tier to valid range
    if current_tier >= total_tiers:
        current_tier = total_tiers - 1

    current_tier_features = wave_tiers[current_tier]

    # A feature is complete for the current wave when ``passes=true`` in
    # feature-list.json. That is the ONE condition.
    #
    # Two terms were REMOVED here, and both had to go together:
    #
    #   * ``feature_has_passed_attested_tests`` — the per-feature attested
    #     evidence gate;
    #   * ``_feature_qa_final_approved`` — the QA evaluator's verdict.
    #
    # Both are now DISABLED (not deleted — see the QA gate further down and
    # ``verify._check_features``): the evaluator is never dispatched and the
    # per-feature evidence gate is never read. Keeping either term would mean no
    # feature in any wave ever completes, because the step that produces what it
    # requires is no longer invoked — the wave would sit at "features remaining"
    # forever.
    #
    # The ``passes`` flag is now written by the merge itself:
    # ``merge_feature`` calls ``update_feature_status(..., True,
    # skip_test_check=True)`` after a successful per-feature merge. So merging IS
    # what marks a feature passing, and no new writer was needed.
    #
    # What this gives up: a feature whose tests failed merges anyway. The signal
    # appears at the odd-wave runtime checkpoint or the last-wave regression, not
    # at the feature that caused it. Accepted deliberately.
    #
    # Standards was never a term here either. The gate is project-scoped and
    # runs once, in the last wave before regression (see
    # _project_standards_gate). A per-feature term — even one conditioned on
    # "is last wave" — would demand per-feature evidence the project-level run
    # never produces, and no feature would ever be complete, so the wave would
    # never reach merge.
    wave_completed = {f for f in wave_feature_ids if f in completed}
    wave_remaining = [f for f in wave_feature_ids if f not in wave_completed]

    # Tier-level logic: check if current tier is complete but more tiers remain
    tier_completed = {f for f in current_tier_features if f in wave_completed}
    tier_remaining = [f for f in current_tier_features if f not in tier_completed]

    if not tier_remaining and current_tier < total_tiers - 1:
        # Current tier is complete but more tiers remain — merge tier then advance
        unmerged_tier = _features_not_merged_to_integration(
            project_path, aah_path, current_wave, current_tier_features
        )
        if unmerged_tier:
            return {
                "action": "merge_tier_to_integration",
                "wave": current_wave,
                "tier": current_tier,
                "total_tiers": total_tiers,
                "features": unmerged_tier,
                "next_tier": current_tier + 1,
                "reason": f"Tier {current_tier} complete. Merge features before starting tier {current_tier + 1}.",
                "command": "aah run core.git_ops.merge_wave_to_integration merge-feature --wave <N> --feature-id <FXXX>",
            }
        # All tier features merged — advance to next tier
        return {
            "action": "advance_tier",
            "wave": current_wave,
            "current_tier": current_tier,
            "next_tier": current_tier + 1,
            "total_tiers": total_tiers,
            "reason": f"Tier {current_tier} merged. Advancing to tier {current_tier + 1}.",
        }

    # Check: are all features in this wave done?
    if not wave_remaining:
        # QA owns the semantic verdict over verified, subject-bound
        # feature-test evidence. The structural AC-to-test join is proven
        # elsewhere: at planning time and inside the scoped feature-test run.
        unmerged = _features_not_merged_to_integration(
            project_path, aah_path, current_wave, wave_feature_ids
        )

        # Verify features are merged to integration branch (computed above).
        if unmerged:
            return {
                "action": "merge_features_to_integration",
                "wave": current_wave,
                "features": unmerged,
                "reason": f"Features not yet merged to integration branch: {unmerged}. Merge before running regression.",
                "command": "aah run core.git_ops.merge_wave_to_integration merge-feature --wave <N> --feature-id <FXXX>",
            }

        # Project impl-reasoning comments to the tracker (post-merge gate).
        #
        # Merge preserved reasoning to develop; the tracker should now reflect
        # it. This gate consults GitHub itself (not local state) to decide who
        # still needs posting, so branch switches, worktree teardowns, and
        # resolver-driven merges cannot cause the effect to be lost. Bounded
        # retries: after _IMPL_COMMENT_RETRY_CAP attempts, escalate to a
        # blocked action so a human can intervene.
        needs_post, post_error = _features_needing_impl_comment_post(
            aah_path, wave_feature_ids,
        )
        if post_error is not None:
            return {
                "action": "blocked",
                "wave": current_wave,
                "reason": (
                    f"Impl-reasoning post gate could not query tracker "
                    f"({post_error}). Resolve connectivity/auth, then re-run."
                ),
            }
        if needs_post:
            attempts = _load_impl_comment_attempts(aah_path)
            over_budget = [
                fid for fid in needs_post
                if int(attempts.get(fid, 0)) >= _IMPL_COMMENT_RETRY_CAP
            ]
            if over_budget:
                return {
                    "action": "blocked",
                    "wave": current_wave,
                    "features": over_budget,
                    "reason": (
                        f"Impl-reasoning post exceeded retry cap "
                        f"({_IMPL_COMMENT_RETRY_CAP}) for {over_budget}. "
                        "Investigate manually or clear "
                        ".aah/build/impl-comment-attempts.json to retry."
                    ),
                }
            _record_impl_comment_attempt(aah_path, needs_post)
            return {
                "action": "post_impl_comment",
                "wave": current_wave,
                "features": needs_post,
                "reason": (
                    "Merged features whose impl-reasoning is not yet on the "
                    f"tracker: {needs_post}. Post before advancing to "
                    "regression."
                ),
                "command": (
                    "aah run core.build.post_impl_comment "
                    "--feature-id <FXXX>"
                ),
            }

        # All verification evidence is produced and consumed on the wave's
        # integration branch.  Correct the root checkout before reading any
        # regression/checkpoint artifacts so evidence that exists only on the
        # integration branch is not mistaken for missing and re-dispatched.
        manifest_path = aah_path / "manifest.yaml"
        manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
        integration_prefix = manifest.get("branching_config", {}).get(
            "integration_prefix", "integration/wave-"
        )
        integration_branch = f"{integration_prefix}{current_wave}"
        try:
            actual_branch = current_branch(cwd=project_path)
        except GitError as exc:
            return {
                "action": "blocked",
                "wave": current_wave,
                "reason": f"Cannot determine the current branch: {exc}",
            }
        if actual_branch != integration_branch:
            return {
                "action": "checkout_integration",
                "wave": current_wave,
                "expected_branch": integration_branch,
                "actual_branch": actual_branch,
                "command": f"git checkout {integration_branch}",
                "reason": (
                    f"Verification must run on {integration_branch}; "
                    f"current branch is {actual_branch}."
                ),
            }

        # Whole-codebase standards gate — the project's ONE standards run, in
        # the last wave, on integration/wave-N, BEFORE regression. A no-op on
        # every earlier wave. Placed here because the integration branch cuts
        # from develop HEAD, so integration/wave-<last> holds every prior wave's
        # merged code plus this one's; and because promote (which destroys
        # worktrees) has not run yet.
        standards_gate = _project_standards_gate(
            project_path, aah_path, current_wave
        )
        if standards_gate is not None:
            return standards_gate

        # ─── regression — LAST WAVE ONLY ────────────────────────────────
        #
        # Regression runs ONCE, on the final wave, over the whole cumulative
        # suite. Safe because the last integration branch cuts from develop
        # HEAD, so integration/wave-<last> contains every promoted wave — one
        # run covers the project. This mirrors _project_standards_gate's
        # is_last_wave guard exactly: on every earlier wave the whole block —
        # the trigger AND the status/fix_regression routing that reads the
        # artifact — is a no-op, NOT a missing-evidence failure. Gating the
        # trigger alone would leave the routing below reading an artifact
        # nothing produces.
        #
        # Order on the last wave is standards → regression → system checkpoint.
        # Do NOT reorder: a lint or security repair changes the tree, and
        # regression evidence is SHA-bound, so running regression first would
        # guarantee a stale artifact.
        if is_last_wave(aah_path, current_wave):
            regression, reg_reason = read_regression_evidence(
                aah_path, project_path, current_wave
            )
            if regression is None:
                if reg_reason == "stale_secret":
                    return _reverify_action(
                        "regression",
                        command=f"aah run core.build.run_regression_suite --wave {current_wave}",
                        reason=reg_reason,
                        wave=current_wave,
                    )
                _log_gate_decision(
                    aah_path, gate="regression", wave=current_wave,
                    decision="refire", reason=reg_reason,
                )
                return {
                    "action": "run_regression",
                    "wave": current_wave,
                    "signal_reason": reg_reason,
                    "reason": (
                        f"Regression evidence is not usable ({reg_reason}). All "
                        f"{len(wave_feature_ids)} features in wave {current_wave} are "
                        "merged to integration, and this is the final wave — run "
                        "the cumulative regression suite."
                    ),
                }

            # Status BEFORE passed. `passed: false` alone cannot distinguish "the
            # suite failed" from "the suite never ran", and those route differently:
            # dispatching aah-fix for a suite that never ran sends it to repair a
            # test failure that does not exist.
            reg_status = regression.get(
                "status", "pass" if regression.get("passed") else "fail"
            )
            if reg_status in ("no_signal", "skipped_wrong_branch"):
                signal_reason = regression.get("signal_reason") or reg_status
                _log_gate_decision(
                    aah_path, gate="regression", wave=current_wave,
                    decision="no_signal", reason=signal_reason,
                )
                return {
                    "action": "no_signal",
                    "wave": current_wave,
                    "signal_reason": signal_reason,
                    "reason": regression.get(
                        "message", "Regression did not render a verdict."
                    ),
                }
            if reg_status == "fail":
                attempts = _record_regression_failure_attempt(
                    aah_path, current_wave, regression
                )
                if attempts >= MAX_REWORK_ATTEMPTS:
                    return {
                        "action": "human_review_required",
                        "wave": current_wave,
                        "trigger": "regression_repair_cap",
                        "attempts": attempts,
                        "failures": regression.get("failures", [])[:5],
                        "reason": (
                            "Regression remains failed after "
                            f"{attempts} verified attempts. Human review required."
                        ),
                    }
                return {
                    "action": "fix_regression",
                    "wave": current_wave,
                    "attempts": attempts,
                    "failures": regression.get("failures", [])[:5],
                    "route": "aah-fix",
                    "repair_mode": "current_wave_repair",
                    "source_artifact": str(
                        (aah_path / "build" / "test-results" / "regression-latest.json")
                        .relative_to(project_path)
                    ),
                    "source_integration_sha": rev_parse(
                        f"integration/wave-{current_wave}", cwd=project_path
                    ),
                    "reason": "Regression failed. Fix before merging.",
                }
            _clear_regression_failure_attempts(aah_path, current_wave)

        # ─── system checkpoint (runtime validation) — CHECKPOINT WAVES ──
        #
        # Reached on EVERY wave. This block used to hang off the regression
        # `else:` — with regression now last-wave-only that nesting would have
        # silently skipped the runtime checkpoint on every earlier wave. It is
        # de-nested here and gated on its own cadence instead: odd 0-indexed
        # waves plus the last (is_checkpoint_wave). On a non-checkpoint wave the
        # cascade falls straight through to the wave-summary / UCR / merge
        # decision below.
        #
        # This is the one place the regression move and the cadence change
        # physically collide; the gates are written once, deliberately, so
        # orchestrator, verify, and the merge gate cannot disagree.
        if is_checkpoint_wave(aah_path, current_wave):
            runtime_result_path = aah_path / "build" / "runtime-results" / f"wave-{current_wave}-all.json"

            # Authenticate and read runtime evidence once, before consulting
            # fix_category. Without this, a forged result could hijack rework.
            runtime_data, recovery = _read_runtime_result_or_action(
                project_path,
                aah_path,
                current_wave,
                runtime_result_path,
                "Runtime results missing or unverifiable "
                f"({runtime_result_path.name}). Re-run system checkpoint "
                "to produce a fresh attested result.",
            )
            if recovery is not None:
                return recovery

            # This routing probe owns only subject freshness. Full runtime-
            # criteria validation remains in verify._check_runtime_bindings at
            # the promotion boundary.
            #
            # Matched against the integration branch's CURRENT subject identity,
            # not against regression's. Regression evidence exists only on the
            # last wave now, but read_regression_evidence already binds it to
            # this same identity, so the last-wave comparison is unchanged while
            # the probe keeps working on every other checkpoint wave.
            runtime_sha = (runtime_data or {}).get("subject", {}).get("commit_sha")
            current_subject = subject_identity(project_path, ref=integration_branch)
            if not runtime_sha or not current_subject or runtime_sha != current_subject:
                return {
                    "action": "run_system_checkpoint",
                    "wave": current_wave,
                    "keep_running": True,
                    "reason": (
                        f"Runtime results are stale for the current "
                        f"{integration_branch} subject. Re-run the system "
                        "checkpoint."
                    ),
                }

            if runtime_data is not None:
                if not runtime_data.get("overall_passed"):
                    failures = {k: v for k, v in runtime_data.get("checks", {}).items() if not v.get("passed")}
                    fix_category = runtime_data.get("fix_category", "build")

                    if fix_category == "design_issue":
                        # Current-wave rework model: design failures target
                        # built features. HOLD THE WAVE OPEN and route to
                        # aah-fix, which rebuilds the affected feature(s) in
                        # the CURRENT wave; the orchestrator re-verifies
                        # before promote. Escalate to a human once the rework
                        # exceeds the retry cap. The wave NEVER falls through
                        # to promote on an unresolved design_issue.
                        attempts = _rework_attempts_for_wave(aah_path, current_wave)
                        if attempts >= MAX_REWORK_ATTEMPTS:
                            _log_gate_decision(
                                aah_path, gate="system_checkpoint", wave=current_wave,
                                decision="escalate-human",
                                reason=f"current-wave rework exceeded cap ({MAX_REWORK_ATTEMPTS} attempts)",
                            )
                            return {
                                "action": "human_review_required",
                                "wave": current_wave,
                                "failures": failures,
                                "trigger": "rework_cap",
                                "attempts": attempts,
                                "reason": (
                                    f"Current-wave rework has reached the rework cap "
                                    f"({MAX_REWORK_ATTEMPTS} attempts) and still fails "
                                    "verify. Human review required."
                                ),
                            }
                        return {
                            "action": "raise_feedback",
                            "wave": current_wave,
                            "failures": failures,
                            "target_phase": "plan",
                            "route": "aah-fix",
                            "reason": (
                                "System checkpoint design_issue (targets built features). "
                                "Hand to aah-fix → rebuild in the CURRENT wave → re-verify "
                                "before promote."
                            ),
                        }
                    elif fix_category == "user_required":
                        return {
                            "action": "user_confirm",
                            "wave": current_wave,
                            "failures": failures,
                            "reason": "System checkpoint failure cannot be auto-fixed. User input required.",
                        }
                    else:
                        return {
                            "action": "fix_runtime_validation",
                            "wave": current_wave,
                            "failures": failures,
                            "route": "aah-fix",
                            "repair_mode": "current_wave_repair",
                            "source_artifact": str(runtime_result_path.relative_to(project_path)),
                            "source_integration_sha": rev_parse(
                                f"integration/wave-{current_wave}", cwd=project_path
                            ),
                            "reason": "System checkpoint failed. Route it through aah-fix, then run next-action again.",
                        }
                else:
                    gate = _runtime_profile_gate(
                        project_path, aah_path, current_wave, runtime_data
                    )
                    if gate is not None:
                        return gate

        # Verification passed via agent-dispatched runtime validator (or the
        # wave is not a checkpoint wave, in which case there is nothing to
        # verify here). Post-checkpoint gates apply.
        #
        # No artifact-completeness scan here. Presence, attestation, freshness,
        # and the verdict are all owned by verify.verify_wave_evidence, which
        # runs once at the promotion boundary; a pre-verifier presence scan only
        # checked that the same files exist a moment before the verifier did.

        # Wave summary gate — generate the CURRENT wave's completion summary
        # BEFORE promote advances the pointer. This makes the summary a true
        # end-of-wave artifact (written while still in the wave) and gives the
        # user something to read at the UCR checkpoint below. The lazy backstop
        # near the top of this function (prev_wave, current_wave > 0) remains as
        # a safety net for interrupted/legacy runs but normally never fires.
        summary_path = aah_path / "build" / "wave-summaries" / f"wave-{current_wave}-summary.md"
        if not summary_path.exists():
            return {
                "action": "generate_wave_summary",
                "wave": current_wave,
                "features": wave_feature_ids,
                "reason": f"Wave {current_wave} summary not yet generated. Generate before merge.",
                "command": f"aah run core.build.wave_summary --wave {current_wave}",
            }

        # Artifact gate passed — check for user review checkpoint before merge
        ucr = _check_user_review_checkpoint(aah_path, current_wave)
        if ucr:
            return ucr

        # Codemap refresh gate — re-index codemap.db against the merged wave code
        # BEFORE the expertise update reads it. Sits after the summary/checkpoint
        # and before expertise so Tier 2 reflects shipped code, not the wave's
        # starting state. Deterministic: expertise cannot run on a stale codemap.
        codemap_action = _check_codemap_gate(project_path, aah_path, current_wave)
        if codemap_action:
            return codemap_action

        # Wave-level expertise update (final learnings from shipped code)
        expertise_wave_action = _check_expertise_update_wave(aah_path, wave_feature_ids, current_wave)
        if expertise_wave_action:
            return expertise_wave_action

        # No verifier call here. promote_to_develop runs the single full
        # verification pass, and only there is its report race-checked
        # (verification_report_is_current) immediately before the branch
        # operation. A second identical read-only pass here proved nothing the
        # promotion pass does not, and its result was never race-checked.

        # All gates passed — merge
        return {
            "action": "merge",
            "wave": current_wave,
            "cleanup_branches": True,
            "reason": (
                f"Wave {current_wave} complete. Every gate that applies to this "
                "wave's cadence passed (regression on the last wave, runtime "
                "validation on checkpoint waves), plus the summary. Promotion "
                "runs the single full-evidence verification pass before "
                "changing branches."
            ),
            "command": f"aah run core.git_ops.promote_to_develop --wave {current_wave} --cleanup-branches",
        }

    # No per-feature standards gate here. Standards is a project-scoped check
    # that fires ONCE per project, in the last wave before regression — see
    # _project_standards_gate at the post-merge cascade above.

    # ─── per-feature QA gate — DISABLED (§3) ────────────────────────────
    #
    # DISABLED MEANS NOT CALLED, NOT DELETED. Everything below this line — the
    # whole gate, `_features_needing_qa`, `_feature_qa_state_action`, the
    # human-review / rework / `run_qa` routing, `aah-qa-evaluator`,
    # `write_qa_report`, `qa_routing`, `qa_evidence`, the `feature-qa-*` operator
    # CLIs, the hooks — remains in place and unmodified. Only the entry condition
    # changed, so re-arming the step is a matter of flipping
    # PER_FEATURE_QA_ENABLED back to True. NOTHING replaces it: no new action, no
    # new gate, no new state file, no retry counter.
    #
    # The implementer agent's own Self-QA is accepted as the per-feature quality
    # step, and the implementer is not modified — it still runs
    # `run_feature_tests` and still signs the result. Nothing consumes it.
    #
    # `_features_needing_qa` is short-circuited rather than called-and-ignored so
    # the disabled step reads no state at all.
    features_needing_qa = (
        _features_needing_qa(project_path, aah_path, wave_remaining)
        if PER_FEATURE_QA_ENABLED
        else []
    )
    if features_needing_qa:
        # Real subject-descriptor gate: resolve each feature's QA subject
        # (worktree or root) and verify evidence freshness.
        wctx = get_wave_context(project_path, current_wave)
        total_features_in_wave = wctx.get("total_features_in_wave", 2)

        # First pass: resolve subject descriptors for all features
        subject_map = {}
        failed_subjects = []
        for fid in features_needing_qa:
            subject_result = resolve_feature_subject(
                project_path, fid, total_features_in_wave
            )
            if not subject_result.get("ok"):
                failed_subjects.append({
                    "feature_id": fid,
                    "reason": subject_result.get("reason", "Unknown failure"),
                })
            else:
                subject_map[fid] = {
                    "subject_path": subject_result["subject_path"],
                    "subject_branch": subject_result["subject_branch"],
                    "subject_sha": subject_result["subject_sha"],
                }

        if failed_subjects:
            reasons = "; ".join(
                f"{item['feature_id']}: {item['reason']}"
                for item in failed_subjects
            )
            return {
                "action": "no_signal",
                "wave": current_wave,
                "features": features_needing_qa,
                "recreate_evidence": True,
                "reason": f"QA subject resolution failed: {reasons}",
            }

        # Second pass: verify feature-test evidence freshness for each subject
        stale_features = []
        for fid in features_needing_qa:
            subject = subject_map[fid]
            result_path = aah_path / "build" / "test-results" / f"{fid}.json"
            evidence = read_fresh_feature_evidence(
                result_path,
                project_path,
                FEATURE_TEST_PREFIX,
                subject_path=Path(subject["subject_path"]),
                feature_id=fid,
                expected_branch=subject["subject_branch"],
                expected_sha=subject["subject_sha"],
            )
            if evidence is None:
                stale_features.append(fid)

        if stale_features:
            fid = stale_features[0]
            subject = subject_map[fid]
            command = (
                "aah run core.build.run_feature_tests "
                f"--feature-id {shlex.quote(fid)} "
                f"--project-path {shlex.quote(str(project_path))} "
                f"--subject-path {shlex.quote(str(subject['subject_path']))} "
                f"--subject-branch {shlex.quote(str(subject['subject_branch']))} "
                f"--subject-sha {shlex.quote(str(subject['subject_sha']))}"
            )
            return {
                "action": "run_feature_tests",
                "wave": current_wave,
                "feature_id": fid,
                "subject_map": {fid: subject},
                "command": command,
                "reason": (
                    f"Feature-test evidence for {fid} is stale for its current "
                    "clean subject. Re-run that feature test before QA."
                ),
            }

        # All subjects resolved and evidence fresh. Route authoritative attempt
        # state by priority: human review > rework > fresh QA > approved.
        human_review_actions = []
        rework_actions = []
        run_qa_features = []

        for fid in features_needing_qa:
            subject = subject_map[fid]
            state_action = _feature_qa_state_action(
                project_path, aah_path, fid, subject
            )

            if isinstance(state_action, dict):
                state_action["wave"] = current_wave
                human_review_actions.append(state_action)
            elif state_action == "rework":
                from aah.core.build.qa_evidence import derive_qa_state

                qa_state = derive_qa_state(project_path, fid)
                latest_attempt_path = (
                    aah_path / "build" / "qa-results" / fid
                    / f"attempt-{qa_state['current_attempt']:03d}.json"
                )
                qa_findings = []
                if latest_attempt_path.exists():
                    try:
                        latest_data = read_json(latest_attempt_path)
                        qa_findings = latest_data.get("issues", [])
                    except Exception:
                        pass

                human = latest_human_decision(
                    project_path, fid, qa_state.get("current_attempt")
                )
                human_rationale = None
                if human and human.get("decision") == "request_rework":
                    human_rationale = human.get("rationale")

                rework_actions.append({
                    "feature_id": fid,
                    "subject": subject,
                    "qa_findings": qa_findings,
                    "human_rationale": human_rationale,
                })
            elif state_action == "run_qa":
                run_qa_features.append(fid)

        if human_review_actions:
            return human_review_actions[0]

        if rework_actions:
            rework = rework_actions[0]
            return {
                "action": "rework_qa_feedback",
                "wave": current_wave,
                "feature_id": rework["feature_id"],
                "subject_map": {rework["feature_id"]: rework["subject"]},
                "qa_findings": rework["qa_findings"],
                "human_rationale": rework["human_rationale"],
                "reason": (
                    f"Feature {rework['feature_id']} requires rework based on QA "
                    "feedback. Dispatch implementer to address issues."
                ),
            }

        if run_qa_features:
            from aah.core.build.qa_evidence import derive_qa_state

            qa_state_map = {}
            for fid in run_qa_features:
                state = derive_qa_state(project_path, fid)
                qa_state_map[fid] = {
                    "attempts_total": state["attempts_total"],
                    "rework_count": state["rework_count"],
                    "current_attempt": state["current_attempt"],
                }

            return {
                "action": "run_qa",
                "features": run_qa_features,
                "wave": current_wave,
                "reason": (
                    f"Features need independent subject-bound QA review: "
                    f"{run_qa_features}."
                ),
                "subject_map": {fid: subject_map[fid] for fid in run_qa_features},
                "qa_state": qa_state_map,
            }

        # All features approved; fall through to the next workflow gate.

    # Check: which features are implementable?
    # Scope to current tier features only for dispatch
    dispatch_candidates = tier_remaining if tier_remaining else wave_remaining
    dag_path = aah_path / "plan" / "dag.json"
    if dag_path.exists():
        dag_data = read_json(dag_path)
        G = dag_from_json(dag_data)
        available = [f for f in get_execution_frontier(G, completed) if f in dispatch_candidates]
    else:
        available = dispatch_candidates

    if not available:
        return {
            "action": "wait",
            "wave": current_wave,
            "tier": current_tier,
            "total_tiers": total_tiers,
            "blocked": dispatch_candidates,
            "reason": f"Features {dispatch_candidates} are blocked by incomplete dependencies.",
        }

    # Dispatch implementation
    context = get_wave_context(project_path, current_wave, tier_num=current_tier)

    # Ensure integration branch exists before dispatching agents.
    # This is a no-op if the branch already exists.
    integration_result = ensure_integration_branch(project_path, current_wave)
    integration_branch = integration_result["branch"]

    # Batch: limit parallel features to MAX_PARALLEL_BATCH
    all_dispatch_features = context.get("features", [])
    batch = all_dispatch_features[:MAX_PARALLEL_BATCH]

    # Pre-create worktrees — all tier features execute in parallel
    feature_ids = [f["id"] for f in batch]
    worktree_map = {}
    worktree_errors = []
    subject_map = {}
    worktree_result = setup_wave_worktrees(project_path, current_wave, feature_ids)
    if worktree_result.get("success"):
        # worktree_map carries the FULL descriptor
        # {path, branch, created, sha} instead of a bare path. The named
        # consumer (aah-build SKILL.md) reads worktree_map[fid]["path"].
        worktree_map = worktree_result.get("worktrees", {})
        # Derive the per-feature subject descriptor straight from the
        # worktree descriptor — no new git calls. Every normal tier dispatch
        # is parallel, so each feature's subject is its own worktree.
        subject_map = {
            fid: {
                "subject_path": desc["path"],
                "subject_branch": desc["branch"],
                "subject_sha": subject_identity(Path(desc["path"])),
            }
            for fid, desc in worktree_map.items()
        }
    worktree_errors = worktree_result.get("errors", [])

    # Detect frontend features in the batch
    features_dir = aah_path / "plan" / "features"
    has_frontend = False
    for fid in feature_ids:
        fdata = load_feature_data(features_dir, fid)
        if fdata and fdata.get("layer") == "frontend":
            has_frontend = True
            break

    # Record dispatched features as in-progress
    progress_path = aah_path / "claude-progress.json"
    if progress_path.exists():
        update_progress(progress_path, in_progress=feature_ids)

    warnings = []
    if has_frontend:
        warnings.append("Frontend features detected — confirm Playwright system deps before dispatching agents (Step 5a)")

    return {
        "action": "dispatch_parallel",
        "wave": current_wave,
        "tier": current_tier,
        "total_tiers": total_tiers,
        "features": batch,
        "batch_info": {
            "batch_size": len(batch),
            "total_in_wave": len(all_dispatch_features),
            "total_remaining": len(all_dispatch_features),
            "is_final_batch": len(all_dispatch_features) <= MAX_PARALLEL_BATCH,
        },
        "contract_path": context.get("contract_path", ""),
        "project_dir": str(project_path),
        "strategy": "parallel",
        "integration_branch": integration_branch,
        "integration_created": integration_result["created"],
        "worktree_map": worktree_map,
        "worktree_errors": worktree_errors,
        "subject_map": subject_map,
        "has_frontend_features": has_frontend,
        "warnings": warnings,
        "reason": f"Wave {current_wave}, Tier {current_tier + 1}/{total_tiers}: dispatching {len(batch)}/{len(all_dispatch_features)} feature(s).",
    }


def _check_user_review_checkpoint(aah_path: Path, current_wave: int) -> dict | None:
    """Check if a user review checkpoint (UCR) is required after this wave.

    Reads checkpoint-config.yaml for UCR definitions. If a UCR is defined
    for after_wave == current_wave and hasn't been approved yet, returns
    an action dict requesting user review. Otherwise returns None.

    Approval is recorded by validate_checkpoint.py approve command at:
      .aah/build/checkpoint-results/wave-N-approval.json
    with {"approved": true, "approved_by": "...", "approved_at": "..."}

    Gated on ``is_checkpoint_wave``: a pre-5.1.1 config lists a UCR after EVERY
    wave, so the stored ``after_wave`` alone would ask for a review on a wave
    with no running system. ``verify._check_user_review`` gates on the same
    helper.
    """
    if not is_checkpoint_wave(aah_path, current_wave):
        return None

    config_path = aah_path / "plan" / "checkpoint-config.yaml"
    if not config_path.exists():
        return None

    try:
        config = read_yaml(config_path)
    except Exception:
        return None

    # Handle both top-level and nested structure
    if "checkpoint_configuration" in config:
        checkpoint_config = config["checkpoint_configuration"]
    else:
        checkpoint_config = config

    ucr_list = checkpoint_config.get("user_review_checkpoints", [])
    if not ucr_list:
        return None

    # Find UCR that triggers after this wave
    for ucr in ucr_list:
        after_wave = ucr.get("after_wave")
        if after_wave != current_wave:
            continue

        ucr_id = ucr.get("id", f"UCR-wave-{current_wave}")

        # Check if already approved (validate_checkpoint.py writes here)
        approval_path = aah_path / "build" / "checkpoint-results" / f"wave-{current_wave}-approval.json"
        if approval_path.exists():
            try:
                approval_data = read_json(approval_path)
                if approval_data.get("approved"):
                    return None  # Already approved, proceed to merge
            except Exception:
                pass

        # UCR required but not yet approved — block merge
        # Fields align with SKILL.md step 14 (User Review Checkpoint):
        #   - covered_features → step 1 (banner)
        #   - testing_steps → step 3 (testing guide: per-feature steps with expected outcomes)
        #   - success_criteria → step 3 (what to look for)
        #   - access_info_path → step 3 (read URL/port from this file)
        # NOTE: Do NOT include fields like "demo_checklist" that suggest a separate demo flow.
        #       The SKILL.md defines exactly 3 choices: Approve / Request changes / Stop.
        return {
            "action": "user_review",
            "wave": current_wave,
            "checkpoint_id": ucr_id,
            "covered_features": ucr.get("covered_features", []),
            "testing_steps": ucr.get("demo_checklist", []),  # Repurposed: items for user's testing guide
            "success_criteria": ucr.get("success_criteria", []),
            "access_info_path": str(aah_path / "build" / "checkpoint-results" / f"wave-{current_wave}-user-review-access.json"),
            "reason": f"User review checkpoint {ucr_id} required after wave {current_wave}. System is running. Read access info, present testing guide with concrete commands, then ask user: Approve / Request changes / Stop.",
            "approve_command": f"aah run core.build.validate_checkpoint approve --wave {current_wave}",
        }

    return None


def _features_not_merged_to_integration(
    project_path: Path, aah_path: Path, wave_num: int, wave_feature_ids: list[str],
) -> list[str]:
    """Check which wave features have NOT been merged to integration/wave-N.

    Returns list of feature IDs that still need to be merged. Empty list means
    all features are on the integration branch.
    """
    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    branch_config = manifest.get("branching_config", {})
    integration_prefix = branch_config.get("integration_prefix", "integration/wave-")
    integration_branch = f"{integration_prefix}{wave_num}"

    if not branch_exists(integration_branch, cwd=project_path):
        # Integration branch doesn't exist yet — all features are unmerged
        return list(wave_feature_ids)

    unmerged = []
    for fid in wave_feature_ids:
        feature_shas = find_feature_commits(fid, cwd=project_path)
        if not feature_shas:
            # No commits found for this feature — treat as unmerged
            unmerged.append(fid)
        elif not branch_contains_all_commits(integration_branch, feature_shas, cwd=project_path):
            unmerged.append(fid)

    return unmerged


def _check_codemap_gate(project_path: Path, aah_path: Path, current_wave: int) -> dict | None:
    """Ensure codemap.db is re-indexed against the wave's merged code before the
    expertise update reads it.

    Fresh iff the attested ``wave-{N}-codemap.json`` verifies AND its
    ``head_sha_post`` equals the current ``integration/wave-{N}`` HEAD — i.e. the
    refresh ran after the last commit landed. Non-blocking by construction: a
    failed refresh (e.g. codemap-scale not installed) still stamps
    ``head_sha_post`` with the current HEAD, so the gate advances after one
    attempt instead of looping.
    """
    head_branch = f"integration/wave-{current_wave}"
    artifact_path = (
        aah_path / "build" / "runtime-results" / f"wave-{current_wave}-codemap.json"
    )
    artifact = load_attested_artifact(
        artifact_path, project_path, CODEMAP_REFRESH_PREFIX, "json"
    )
    if artifact.state is ArtifactState.VERIFIED:
        head_now = rev_parse(head_branch, cwd=project_path) or ""
        if artifact.payload.get("head_sha_post") == head_now:
            _log_gate_decision(
                aah_path, gate="codemap", wave=current_wave,
                decision="pass", reason="fresh against integration HEAD",
            )
            return None

    _log_gate_decision(
        aah_path, gate="codemap", wave=current_wave,
        decision="dispatch", reason=f"stale/missing ({artifact.state.value})",
    )
    return {
        "action": "refresh_codemap",
        "wave": current_wave,
        "reason": (
            f"Wave {current_wave} codemap is stale — re-index the merged wave "
            "code before the expertise update reads it."
        ),
        "command": f"aah run core.build.codemap_refresh --wave {current_wave}",
    }


def _check_expertise_update_wave(aah_path: Path, wave_feature_ids: list[str], current_wave: int) -> dict | None:
    """Attempt the wave expertise update ONCE per product identity.

    Non-blocking by design: expertise is context for the next wave, not proof
    that this one works. A fresh marker represents success; a warning outcome
    bound to the current code identity prevents a failed attempt from looping.
    A genuine product-code change permits exactly one new attempt.

    The marker MUST have been produced by ``aah.core.build.wave_markers``.
    A hand-written stub or marker from a prior wave fails the content-hash +
    HEAD-SHA freshness check.
    """
    project_path = aah_path.parent
    from aah.core.build.wave_markers import expertise_outcome_matches_identity

    marker = aah_path / "build" / f"wave-{current_wave}-expertise-updated.json"

    if marker.exists():
        # Freshness is bound to artifact content + integration HEAD.
        # is_expertise_marker_fresh handles missing fields, head drift,
        # and any out-of-band edit to expertise.yaml or domains/*.yaml.
        fresh, reason = expertise_marker_fresh(marker, project_path, current_wave)
        if fresh and _validate_expertise_artifacts(aah_path, current_wave) is None:
            _log_gate_decision(
                aah_path, gate="expertise", wave=current_wave,
                decision="pass", reason="fresh + artifacts valid",
            )
            return None
        # Stale or invalid: warn and offer one new attempt. Do NOT delete the
        # marker; the next successful attempt replaces it atomically.
        _log_gate_decision(
            aah_path, gate="expertise", wave=current_wave,
            decision="warning", reason=f"freshness/artifacts: {reason or 'invalid'}",
        )

    if expertise_outcome_matches_identity(project_path, current_wave):
        # A failed attempt was already recorded for this product identity.
        return None

    # Not yet attempted at this identity — dispatch once (collect context for
    # the action payload).
    all_files = []
    all_domains = set()
    for fid in wave_feature_ids:
        files = _get_feature_files_touched(aah_path, fid)
        all_files.extend(files)
        all_domains.update(_identify_affected_domains(files))

    _log_gate_decision(
        aah_path, gate="expertise", wave=current_wave,
        decision="dispatch",
        reason="no success marker or warning for this product identity",
    )
    return {
        "action": "update_expertise",
        "features": wave_feature_ids,
        "wave": current_wave,
        "all_files_touched": list(set(all_files)),
        "domains_affected": sorted(all_domains),
        "reason": f"Wave {current_wave} complete. Update expertise from all {len(wave_feature_ids)} features.",
        "skill": "aah-expertise",
        "guidance": (
            "Follow aah-expertise skill — wave-level update:\n"
            f"1. Extract learnings from ALL features: {wave_feature_ids}\n"
            "2. Update .aah/codebase-intel/expertise.yaml (Tier 1)\n"
            "3. Update/create .aah/codebase-intel/domains/*.yaml (Tier 2)\n"
            "4. Update consumption_views (keep valid, add new, revise stale, remove resolved)\n"
            f"5. Write marker via: aah run core.build.wave_markers "
            f"write-expertise --project-path . --wave {current_wave} --summary <json>"
        ),
    }


def _validate_expertise_artifacts(aah_path: Path, current_wave: int) -> dict | None:
    """Validate that expertise artifacts actually exist and are populated.

    Returns an action dict if validation fails, or None if artifacts are valid.

    Do not trust the marker alone — verify the artifacts. This does NOT unlink
    the marker: the caller records a durable outcome instead, and deleting the
    marker would defeat that suppression.
    """
    problem = expertise_artifacts_problem(aah_path)
    if problem is None:
        return None

    _code, detail = problem
    return {
        "action": "update_expertise",
        "wave": current_wave,
        "reason": (
            f"Expertise marker exists but artifacts are invalid (wave "
            f"{current_wave}): {detail}. Re-run expertise update."
        ),
        "skill": "aah-expertise",
        "guidance": (
            "Previous expertise update produced missing, empty, or stale "
            "artifacts. Follow the full aah-expertise skill procedure."
        ),
    }


def _get_feature_files_touched(aah_path: Path, feature_id: str) -> list[str]:
    """Get list of files modified by a feature from its file or commit records."""
    features_dir = aah_path / "plan" / "features"
    data = load_feature_data(features_dir, feature_id)
    if data:
        return data.get("file_scope", [])
    return []


def _identify_affected_domains(files_touched: list[str]) -> list[str]:
    """Map modified files to domain names based on directory structure."""
    affected = set()
    for file in files_touched:
        parts = Path(file).parts
        meaningful = [p for p in parts if p not in ("src", "lib", "pkg", "app", "tests", "test")]
        if meaningful:
            affected.add(meaningful[0])
    return list(affected)


_IMPL_COMMENT_RETRY_CAP = 3


def _impl_comment_attempts_path(aah_path: Path) -> Path:
    return aah_path / "build" / "impl-comment-attempts.json"


def _load_impl_comment_attempts(aah_path: Path) -> dict:
    p = _impl_comment_attempts_path(aah_path)
    if not p.exists():
        return {}
    try:
        return read_json(p) or {}
    except Exception:  # noqa: BLE001
        return {}


def _record_impl_comment_attempt(aah_path: Path, feature_ids: list[str]) -> dict:
    """Increment the retry counter for each feature; return the updated map."""
    from aah.core.common.io_utils import write_json as _wj
    attempts = _load_impl_comment_attempts(aah_path)
    for fid in feature_ids:
        attempts[fid] = int(attempts.get(fid, 0)) + 1
    p = _impl_comment_attempts_path(aah_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _wj(attempts, p)
    return attempts


def _features_needing_impl_comment_post(
    aah_path: Path, wave_feature_ids: list[str],
) -> tuple[list[str], str | None]:
    """Wave features whose impl-reasoning comment is NOT yet on the tracker.

    Returns a (needs_post, error) tuple.
      - needs_post: feature IDs whose tracker comment is "absent". These need a
        post_impl_comment action to run.
      - error: transient failure sentinel ("offline" or "error") the caller can
        surface as a blocked action. When present, needs_post is empty.

    Features that reasonably can't be posted yet (no ledger entry, VC disabled)
    are treated as "advance safe" and omitted from needs_post; the gate does not
    block on them because there is no tracker to project to.
    """
    from aah.core.build.tracker_state import has_impl_comment_on_tracker

    project_path = aah_path.parent
    needs_post: list[str] = []
    for fid in wave_feature_ids:
        # Only consider features whose local .md already has reasoning to post.
        # If the ## Implementation Reasoning section is missing, there is
        # nothing to project; the feature-implementer is responsible for
        # writing it upstream, not this gate.
        yaml_path = aah_path / "plan" / "features" / f"{fid}.md"
        if not yaml_path.exists():
            continue
        try:
            content = yaml_path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        if "## Implementation Reasoning" not in content:
            continue

        verdict = has_impl_comment_on_tracker(fid, project_path)
        if verdict == "absent":
            needs_post.append(fid)
        elif verdict in ("offline", "error"):
            # Transient — surface as blocked, don't advance past the gate.
            return [], verdict
        # "present", "no_ledger", "vc_disabled" → advance safe (nothing to do).
    return needs_post, None


# ─── project-scoped standards gate ───────────────────────────────────


def is_last_wave(aah_path: Path, wave: int) -> bool:
    """Whether ``wave`` is the final wave of the project.

    Read from waves.json, which is the authority on wave count. Do NOT infer
    this from ``current_wave`` plus remaining features: rework of a built feature
    lands in the CURRENT wave (holding it open), and only brand-new work is
    planned into a forward wave, so the remaining-feature count is not a
    reliable proxy for "is this the last wave" in either direction.
    """
    waves_path = aah_path / "plan" / "waves.json"
    try:
        data = read_json(waves_path)
    except (OSError, ValueError):
        return False
    waves = data.get("waves") if isinstance(data, dict) else None
    if not isinstance(waves, list) or not waves:
        return False
    return wave == len(waves) - 1


def is_checkpoint_wave(aah_path: Path, wave: int) -> bool:
    """Whether ``wave`` fires the system (runtime) checkpoint and the UCR.

    The checkpoint fires after every 2 waves, and always on the last wave. Waves
    are 0-indexed, so that lands on the odd positions — an even rule would fire on
    wave 0, a checkpoint after ONE wave of work. An empty wave doesn't count
    (``place_rework_entry`` can pad with ``[]``), but the last wave always fires,
    so a single-wave project gets everything on wave 0.

    Read from the LIVE waves.json every call — never cached — so appending a wave
    self-corrects the set (3 waves ``{1,2}`` → 4 waves ``{1,3}``). Odd wave counts
    give a consecutive pair at the end (5 → ``{1,3,4}``); that is expected.

    Single source of truth: ``verify.py`` and ``merge_wave_to_integration.py``
    import this rather than re-deriving the rule.
    ``determine_checkpoints.place_user_review_checkpoints`` encodes the same rule
    at plan time and must not drift. Error handling mirrors ``is_last_wave``.
    """
    waves_path = aah_path / "plan" / "waves.json"
    try:
        data = read_json(waves_path)
    except (OSError, ValueError):
        return False
    waves = data.get("waves") if isinstance(data, dict) else None
    if not isinstance(waves, list) or not waves:
        return False

    last = len(waves) - 1
    if wave == last:
        return True
    if wave < 0 or wave > last or wave % 2 != 1:
        return False
    return bool(flatten_wave_features(waves[wave]))


def _project_standards_gate(
    project_path: Path, aah_path: Path, current_wave: int
) -> dict | None:
    """Whole-codebase standards gate — ONCE per project, before regression.

    Returns None on every wave except the last (a no-op, NOT a
    missing-evidence failure), and None on the last wave once the evidence
    verifies and passes. Otherwise returns the one action that advances:

      * missing / unverifiable / stale evidence → ``run_project_standards``
      * a completed run with no verdict         → ``no_signal``
      * a blocking verdict                      → ``fix_standards``

    A passing verdict emits NOTHING — the cascade proceeds to regression, and
    ``aah-fix`` is never invoked on the happy path.
    """
    if not is_last_wave(aah_path, current_wave):
        return None

    from aah.core.build.quality_checks import project_standards_artifact

    artifact = project_standards_artifact(aah_path, current_wave)
    rel_artifact = str(artifact.relative_to(project_path))
    branch = f"integration/wave-{current_wave}"
    command = (
        "aah run core.build.quality_checks run-project "
        f"--project-path . --wave {current_wave}"
    )

    def _run(reason: str, signal_reason: str) -> dict:
        _log_gate_decision(
            aah_path, gate="project_standards", wave=current_wave,
            decision="refire", reason=signal_reason,
        )
        return {
            "action": "run_project_standards",
            "wave": current_wave,
            "subject_branch": branch,
            "artifact": rel_artifact,
            "signal_reason": signal_reason,
            "command": command,
            "reason": reason,
        }

    result = read_attested(artifact, project_path, QUALITY_PREFIX)
    if result is None:
        return _run(
            "No verified whole-codebase standards evidence for the final wave. "
            f"Run the standards check over {branch} before regression.",
            "missing_or_unverified",
        )

    # Subject binding: the evidence must describe the tree being promoted. The
    # repair commit moves the subject identity, so this is also what makes the
    # post-repair re-run land cleanly instead of reporting staleness.
    current_subject = subject_identity(project_path, ref=branch)
    recorded = result.get("subject") if isinstance(result.get("subject"), dict) else {}
    if current_subject is None:
        return _run(
            f"Could not resolve the current subject of {branch}; re-run the "
            "whole-codebase standards check.",
            "subject_unresolvable",
        )
    if recorded.get("commit_sha") != current_subject:
        return _run(
            "Whole-codebase standards evidence is bound to a subject that is no "
            f"longer current on {branch}. Re-run it.",
            "stale_subject",
        )

    if standards_evidence_passed(result):
        _log_gate_decision(
            aah_path, gate="project_standards", wave=current_wave,
            decision="pass", reason="whole-codebase standards passed",
        )
        return None

    if str(result.get("status")) == "no_signal":
        _log_gate_decision(
            aah_path, gate="project_standards", wave=current_wave,
            decision="no_signal", reason="completed run rendered no verdict",
        )
        return {
            "action": "no_signal",
            "wave": current_wave,
            "artifact": rel_artifact,
            "signal_reason": "project_standards_no_signal",
            "reason": (
                "Whole-codebase standards completed but could not render a "
                f"verdict. Inspect {rel_artifact} and resolve the unavailable "
                "tool or environment before retrying."
            ),
        }

    # Failing verdict → repair, NOT blocked. A whole-codebase `ruff --fix` is
    # mechanical and qualifies for aah-fix's direct-repair fast path; a genuine
    # bandit finding may legitimately escalate through the decision loop
    # instead, which is intended behaviour rather than the route failing.
    _log_gate_decision(
        aah_path, gate="project_standards", wave=current_wave,
        decision="refire", reason="blocking verdict",
    )
    findings = result.get("findings") if isinstance(result.get("findings"), dict) else {}
    return {
        "action": "fix_standards",
        "wave": current_wave,
        "route": "aah-fix",
        "repair_mode": "current_wave_repair",
        "source_artifact": rel_artifact,
        # Load-bearing: step 1 of the direct-repair path REQUIRES the branch to
        # match this SHA and stops otherwise. Omit it and aah-fix cannot take
        # the fast path.
        "source_integration_sha": rev_parse(branch, cwd=project_path),
        "failures": {
            "linting": findings.get("linting", ""),
            "static_analysis": findings.get("static_analysis", ""),
        },
        "checks": {
            "linting": result.get("linting", {}),
            "static_analysis": result.get("static_analysis", {}),
        },
        "reverify_command": command,
        "reason": (
            "Whole-codebase standards reported a blocking verdict on "
            f"{branch}. Route it through aah-fix (repair, delete "
            f"{rel_artifact}, then next-action re-runs the gate)."
        ),
    }


# ─── CLI ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AAH build orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("next-action", help="What should happen right now?")

    wr_p = sub.add_parser("wave-readiness", help="Is wave ready to merge?")
    wr_p.add_argument("--wave", type=int, required=True)

    sub.add_parser("test-summary", help="Consolidated test results")
    sub.add_parser("frontier", help="Available and blocked features with reasons")

    qa_p = sub.add_parser("qa-report", help="Read and display QA evaluation report for a feature")
    qa_p.add_argument("--feature-id", required=True)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path()

    if args.command == "next-action":
        result = compute_next_action(project_path)
        # Print checkpoint banner to stderr for CLI visibility
        banner = format_checkpoint_banner(result)
        if banner:
            print(banner, file=sys.stderr)
    elif args.command == "wave-readiness":
        result = check_wave_readiness(project_path, args.wave)
    elif args.command == "test-summary":
        result = get_test_summary(project_path)
    elif args.command == "frontier":
        result = get_frontier_with_reasons(project_path)
    elif args.command == "qa-report":
        result = get_qa_report(project_path, args.feature_id)
        print(format_qa_summary(result), file=sys.stderr)

    json.dump(result, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
