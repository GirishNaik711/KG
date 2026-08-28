#!/usr/bin/env python3
"""
Fast-forward merge integration branch to develop after all tests pass.

Merges integration/wave-N -> develop, cleans up the integration branch,
and atomically updates feature-list.json, claude-progress.json, manifest.yaml.
Exit 0 = success, Exit 2 = blocked.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.feature_list import update_feature_status
from aah.core.common.git_utils import (
    AAH_STATE_PATHS,
    GitError,
    branch_exists,
    checkout_branch,
    commit_aah_state,
    current_branch,
    fast_forward_merge,
    find_feature_commits,
    is_ancestor,
    porcelain_dirt,
    push_to_remote,
    rev_parse,
    run_git,
    tracked_claude_dirt,
)
from aah.core.common.io_utils import read_json, read_yaml, write_json
from aah.core.common.progress import update_progress
from aah.core.git_ops.merge_wave_to_integration import _find_feature_branch


PROTECTED_BRANCH_PREFIXES = ("develop", "main", "integration/")


def _cleanup_feature_branches(
    project_path: Path, aah_path: Path, wave_num: int,
    wave_features: list, integration_branch: str, develop_head: str,
) -> dict:
    """
    Delete feature branches after successful promote to develop.

    Uses safe delete (git branch -d) which refuses to delete branches
    not fully merged. Logs all actions to audit file.
    """
    # Normalize wave_features — may be a dict with "features" key or a plain list
    if isinstance(wave_features, dict):
        feature_ids = wave_features.get("features", [])
    else:
        feature_ids = wave_features

    deleted = []
    skipped = []
    already_deleted: set[str] = set()

    for fid in feature_ids:
        branch = _find_feature_branch(project_path, aah_path, fid)
        if branch is None:
            skipped.append({"branch": f"(not found for {fid})", "reason": "branch not found"})
            continue

        # Never delete protected branches
        if any(branch == p or branch.startswith(p) for p in PROTECTED_BRANCH_PREFIXES):
            skipped.append({"branch": branch, "reason": "protected branch"})
            continue

        # Skip if already deleted by a prior feature in this wave
        if branch in already_deleted:
            continue

        # Get SHA before deletion for audit trail
        sha_result = run_git(
            ["rev-parse", "--short", branch],
            cwd=project_path, check=False,
        )
        sha_at_delete = sha_result.stdout.strip() if sha_result.returncode == 0 else "unknown"

        # Remove worktree first — git refuses to delete a branch checked out
        # in a worktree. Non-fatal: if already removed this is a no-op.
        wt_path = project_path / ".claude" / "worktrees" / fid
        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=project_path, capture_output=True,
            )

        # Safe delete — fails if not fully merged
        result = run_git(
            ["branch", "-d", branch],
            cwd=project_path, check=False,
        )
        if result.returncode == 0:
            deleted.append({"branch": branch, "sha_at_delete": sha_at_delete, "feature_id": fid})
            already_deleted.add(branch)
        else:
            reason = result.stderr.strip() or "delete failed (not fully merged?)"
            skipped.append({"branch": branch, "reason": reason, "feature_id": fid})

    # Log to audit file
    cleanup_event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wave": wave_num,
        "trigger": "promote_to_develop",
        "integration_branch": integration_branch,
        "develop_head": develop_head,
        "branches_deleted": deleted,
        "branches_skipped": skipped,
    }

    audit_path = aah_path / "audit" / "branch-cleanup-log.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    if audit_path.exists():
        audit_data = read_json(audit_path)
    else:
        audit_data = {"cleanup_events": []}

    audit_data["cleanup_events"].append(cleanup_event)
    write_json(audit_data, audit_path)

    return {"deleted": deleted, "skipped": skipped}


def _merge_with_aah_resolution(
    project_path: Path, integration_branch: str, develop: str,
) -> dict:
    """Merge integration into the current branch (develop), resolving .aah/ state.

    Attempts `git merge --no-ff`. On conflict:
      - if every conflicted path is orchestration-owned .aah/ state, resolve each
        by taking the integration side (`checkout --theirs`) and commit;
      - otherwise `git merge --abort` and return an error asking for the resolver,
        so the working tree is NEVER left half-merged.
    Returns {"success": bool, ...}.
    """
    res = run_git(
        ["merge", "--no-ff", "--no-edit", integration_branch],
        cwd=project_path, check=False,
    )
    if res.returncode == 0:
        return {"success": True}

    # Conflict (or other failure). Enumerate unmerged paths.
    conflicts = run_git(
        ["diff", "--name-only", "--diff-filter=U"], cwd=project_path, check=False,
    )
    conflicted = [p for p in conflicts.stdout.splitlines() if p.strip()]
    non_aah = [p for p in conflicted if not p.startswith(".aah/")]

    if not conflicted or non_aah:
        # Genuine source conflict (or a non-conflict merge failure) — never leave
        # a half-merged tree. Abort and surface for the resolver.
        run_git(["merge", "--abort"], cwd=project_path, check=False)
        return {
            "success": False,
            "error": (
                f"Promote merge of {integration_branch} into {develop} hit conflicts "
                f"outside orchestration-owned .aah/ state: {non_aah or conflicted}. "
                f"Merge aborted (tree is clean). Run the aah-merge-resolver on "
                f"{integration_branch} and retry the promote."
            ),
            "conflicts": non_aah or conflicted,
        }

    # All conflicts are .aah/ state — integration side wins (completed wave state).
    for path in conflicted:
        run_git(["checkout", "--theirs", "--", path], cwd=project_path, check=False)
        run_git(["add", "--", path], cwd=project_path, check=False)
    commit_res = run_git(["commit", "--no-edit"], cwd=project_path, check=False)
    if commit_res.returncode != 0:
        run_git(["merge", "--abort"], cwd=project_path, check=False)
        return {
            "success": False,
            "error": (
                f"Failed to complete .aah/-state merge of {integration_branch}: "
                f"{commit_res.stderr.strip()}. Merge aborted."
            ),
            "conflicts": conflicted,
        }
    return {"success": True, "resolved_aah_state": conflicted}


def promote_to_develop(project_path: Path, wave_num: int, cleanup_branches: bool = False) -> dict:
    """
    Promote integration/wave-N to develop.

    Prerequisites: the complete evidence set that applies to THIS wave's cadence
    must verify and pass. There is no separate regression requirement here —
    ``verify_wave_evidence`` is the single authority, and it gates regression on
    the last wave and runtime evidence on checkpoint waves (odd 0-indexed waves
    plus the last). On the last wave that still means the full cumulative
    regression suite must pass.
    """
    aah_path = project_path / ".aah"

    # Load config
    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    branch_config = manifest.get("branching_config", {})
    develop = branch_config.get("develop_branch", "develop")
    integration_prefix = branch_config.get("integration_prefix", "integration/wave-")
    integration_branch = f"{integration_prefix}{wave_num}"

    # Load waves to get feature IDs
    waves_path = aah_path / "plan" / "waves.json"
    wave_features = []
    if waves_path.exists():
        from aah.core.plan.compute_waves import flatten_waves
        waves_data = read_json(waves_path)
        waves = flatten_waves(waves_data)
        if wave_num < len(waves):
            wave_features = waves[wave_num]

    # Check integration branch exists
    if not branch_exists(integration_branch, cwd=project_path):
        return {"success": False, "error": f"Integration branch '{integration_branch}' not found"}

    claude_dirt = tracked_claude_dirt(project_path)
    if claude_dirt:
        return {
            "success": False,
            "error": (
                "Tracked legacy .claude/ files have local changes. Preserve any "
                "needed content, then run `git rm -r --cached -f .claude/` and "
                "commit that migration before retrying; the files remain on disk."
            ),
            "dirty": claude_dirt,
        }

    # Reject any dirt outside AAH_STATE_PATHS — do NOT commit or stash it.
    # Staged, unstaged, or untracked application changes mean the tree is not
    # the subject that was verified, and the checkout below would either fail
    # or silently carry them onto develop. Report only the offending lines, not
    # the .aah/ churn we deliberately ignore.
    dirty = porcelain_dirt(project_path, AAH_STATE_PATHS)
    if dirty:
        return {
            "success": False,
            "error": (
                "Working tree has changes outside "
                f"{', '.join(AAH_STATE_PATHS)} — commit or revert them before "
                "promoting. Promotion never commits or stashes application changes."
            ),
            "dirty": dirty,
        }

    # Commit pending .aah/ state so branch switches don't fail.
    # This may legitimately return False — nothing to commit, or a merge in
    # progress (commit_aah_state refuses mid-merge rather than authoring a merge
    # commit nobody asked for).
    commit_aah_state(project_path, f"chore: commit AAH state before wave {wave_num} promote")

    # The actual gate, not a formality. Because the commit above can no-op, any
    # remaining dirt would break the checkout below. Untracked .claude/ data is
    # local; tracked changes were rejected above.
    residual = porcelain_dirt(project_path, prefixes=(".claude/",))
    if residual:
        return {
            "success": False,
            "error": (
                "AAH state remains uncommitted after the scoped state commit "
                "(a merge may be in progress). Resolve it before promoting — "
                "the checkout to develop would fail."
            ),
            "dirty": residual,
        }

    original_branch = current_branch(cwd=project_path)

    # Final read-only verification of the COMPLETE evidence set, while still on
    # integration/wave-N. Evidence written on one branch is invisible from
    # another, so this MUST happen before the checkout below. It runs nothing and
    # mutates nothing; the report is rechecked immediately before the checkout,
    # which is what replaces a persisted signed promotion gate in this design.
    from aah.core.build.verify import (
        VerificationSystemError,
        WaveVerificationFailed,
        verification_report_is_current,
        verify_wave_evidence,
    )
    from aah.core.git_ops._attest_gate import record_gate_audit

    try:
        report = verify_wave_evidence(project_path, wave_num)
    except WaveVerificationFailed as exc:
        from aah.core.build.verification_contracts import (
            action_from_verification_failure,
        )
        first = exc.report.failures[0]
        record_gate_audit(
            aah_path, gate="promote", artifact="final_verification",
            verdict="refuse", reason=first.code, wave=wave_num,
        )
        return {
            "success": False,
            "error": (
                f"Final evidence verification failed for wave {wave_num}: "
                f"{first.message}"
            ),
            "verification_failures": [f.to_dict() for f in exc.report.failures],
            # The prioritized remediation action, so a promotion failure routes
            # to the check that must re-run instead of looping on promotion.
            # This is the only place a WaveVerificationFailed now surfaces.
            "remediation": action_from_verification_failure(
                first,
                exc.report.failures,
                source_integration_sha=rev_parse(
                    integration_branch, cwd=project_path
                ),
            ),
        }
    except VerificationSystemError as exc:
        record_gate_audit(
            aah_path, gate="promote", artifact="final_verification",
            verdict="refuse", reason="verification_system_error", wave=wave_num,
        )
        return {
            "success": False,
            "error": f"Final evidence verification could not run: {exc}",
        }

    # Gather pre-merge evidence so we can verify the promote actually
    # did something. A ff-merge of an ancestor is a silent no-op, and
    # we never want to report success for a no-op unless it's provably
    # because every feature commit is already on develop.
    integration_head_before = rev_parse(integration_branch, cwd=project_path)
    develop_head_before = rev_parse(develop, cwd=project_path)

    # Collect the feature commits we expect develop to contain after
    # the promote. Empty list means "waves.json is missing" which we
    # already handle permissively below.
    expected_commits: list[str] = []
    for fid in wave_features:
        expected_commits.extend(
            find_feature_commits(fid, cwd=project_path),
        )

    # LAST gate before the irreversible branch operation: nothing the verifier
    # consulted may have moved while we collected the pre-merge evidence above.
    current, drift_reason = verification_report_is_current(project_path, report)
    if not current:
        record_gate_audit(
            aah_path, gate="promote", artifact="final_verification",
            verdict="refuse", reason=f"drift:{drift_reason}", wave=wave_num,
        )
        return {
            "success": False,
            "error": (
                f"Verified evidence drifted before checkout ({drift_reason}). "
                "Nothing was merged — re-run verification and promote again."
            ),
        }

    try:
        # Checkout develop and fast-forward merge
        checkout_branch(develop, cwd=project_path)
        try:
            fast_forward_merge(integration_branch, cwd=project_path)
        except GitError:
            # ff-merge failed (branches diverged) — attempt a real merge, but
            # never leave a half-merged tree. develop and integration
            # legitimately both touch orchestration-owned .aah/ state (each wave
            # commits progress/feature-list/specs/audit on both sides), so
            # conflicts confined to .aah/ are the norm and are resolved
            # deterministically in favor of the integration side (it carries the
            # completed wave state). A conflict in real source is surfaced as an
            # error after aborting the merge — the caller must run the resolver.
            merge_outcome = _merge_with_aah_resolution(
                project_path, integration_branch, develop,
            )
            if not merge_outcome["success"]:
                return merge_outcome

        develop_head_after = rev_parse(develop, cwd=project_path)

        # --- Postflight verification ---
        # 1. develop must now contain integration's tip (unless they
        #    were already equal, which is legitimate only if integration
        #    had nothing to contribute)
        # 2. develop must contain every expected feature commit
        if integration_head_before and not is_ancestor(
            integration_head_before, develop, cwd=project_path,
        ):
            return {
                "success": False,
                "error": (
                    f"Promote failed verification: develop ({develop_head_after[:8] if develop_head_after else '?'}) "
                    f"does not contain integration tip ({integration_head_before[:8]}). "
                    f"The fast-forward silently no-op'd — probably because "
                    f"integration was created from an empty base."
                ),
                "develop_before": develop_head_before,
                "develop_after": develop_head_after,
                "integration_head": integration_head_before,
            }

        missing_features: dict[str, list[str]] = {}
        for fid in wave_features:
            shas = find_feature_commits(fid, cwd=project_path)
            if not shas:
                missing_features[fid] = []
                continue
            for sha in shas:
                if not is_ancestor(sha, develop, cwd=project_path):
                    missing_features.setdefault(fid, []).append(sha)

        if missing_features:
            return {
                "success": False,
                "error": (
                    f"Promote failed verification: '{develop}' does not "
                    f"contain all wave feature commits after merge."
                ),
                "develop_before": develop_head_before,
                "develop_after": develop_head_after,
                "integration_head": integration_head_before,
                "missing": missing_features,
            }

        # Push develop to remote now that the wave is verified on develop
        push_to_remote(project_path, branch=develop, tags=True)

        # Update feature-list.json — mark wave features as passing
        fl_path = aah_path / "feature-list.json"
        if fl_path.exists():
            for fid in wave_features:
                try:
                    update_feature_status(fl_path, fid, True)
                except SystemExit:
                    pass  # Feature might not be in list

        # Update progress
        progress_path = aah_path / "claude-progress.json"
        if progress_path.exists():
            # Move to next wave
            next_wave = wave_num + 1
            waves = read_json(waves_path).get("waves", []) if waves_path.exists() else []
            if next_wave >= len(waves):
                update_progress(
                    progress_path,
                    wave=next_wave,
                    tier=0,
                    next_steps="All waves complete. Run /aah-deploy or /aah-promote-to-main.",
                )
            else:
                update_progress(
                    progress_path,
                    wave=next_wave,
                    tier=0,
                    next_steps=f"Begin wave {next_wave}: features {waves[next_wave]}",
                )

        # Keep integration branch for audit trail — do not delete

        # Cleanup feature branches if requested
        branch_cleanup = None
        if cleanup_branches:
            branch_cleanup = _cleanup_feature_branches(
                project_path, aah_path, wave_num,
                wave_features, integration_branch, develop_head_after,
            )

        result = {
            "success": True,
            "promoted": integration_branch,
            "to": develop,
            "develop_before": develop_head_before,
            "develop_after": develop_head_after,
            "integration_head": integration_head_before,
            "features_marked_passing": wave_features,
        }
        if branch_cleanup:
            result["branch_cleanup"] = branch_cleanup

        return result

    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        try:
            checkout_branch(original_branch, cwd=project_path)
        except Exception:
            pass


def promote_build_to_develop(
    project_path: Path, build_branch: str, cleanup_branches: bool = False,
) -> dict:
    """Promote the lean sequential build branch to develop (wave-free).

    The lean build runs all modules sequentially on ONE shared build branch and
    has no waves/tiers, no runtime evidence, and no waves.json. This verifies the
    cumulative regression evidence (``verify_build_evidence``), then fast-forwards
    (or .aah-resolves) ``build_branch`` -> develop and marks every feature
    passing. It reuses the same git-safety and .aah-resolution machinery as the
    wave-based ``promote_to_develop``.


    ``cleanup_branches`` is accepted for CLI symmetry; the single-worktree build
    has no per-feature branches to delete, so it is a no-op here.
    """
    from aah.core.build.verification_evidence import verify_build_evidence

    aah_path = project_path / ".aah"
    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    develop = manifest.get("branching_config", {}).get("develop_branch", "develop")

    if not branch_exists(build_branch, cwd=project_path):
        return {"success": False, "error": f"Build branch '{build_branch}' not found"}

    claude_dirt = tracked_claude_dirt(project_path)
    if claude_dirt:
        return {
            "success": False,
            "error": (
                "Tracked legacy .claude/ files have local changes. Preserve any "
                "needed content, then run `git rm -r --cached -f .claude/` and "
                "commit that migration before retrying; the files remain on disk."
            ),
            "dirty": claude_dirt,
        }

    dirty = porcelain_dirt(project_path, AAH_STATE_PATHS)
    if dirty:
        return {
            "success": False,
            "error": (
                "Working tree has changes outside "
                f"{', '.join(AAH_STATE_PATHS)} — commit or revert them before "
                "promoting. Promotion never commits or stashes application changes."
            ),
            "dirty": dirty,
        }

    commit_aah_state(project_path, f"chore: commit AAH state before promoting {build_branch}")
    residual = porcelain_dirt(project_path, prefixes=(".claude/",))
    if residual:
        return {
            "success": False,
            "error": (
                "AAH state remains uncommitted after the scoped state commit "
                "(a merge may be in progress). Resolve it before promoting."
            ),
            "dirty": residual,
        }

    original_branch = current_branch(cwd=project_path)

    # Wave-free evidence gate: the cumulative regression suite, bound to the
    # build-branch tip. No standards evidence (the build skill's gate enforces
    # itself) and no runtime evidence (advisory, cleared at the module checkpoint).
    problem = verify_build_evidence(project_path, build_branch)
    if problem:
        code, message = problem
        return {
            "success": False,
            "error": f"Build evidence verification failed: {message}",
            "verification_failures": [{"code": code, "message": message}],
        }

    # Feature set = every feature in feature-list.json (no waves.json).
    fl_path = aah_path / "feature-list.json"
    feature_ids: list[str] = []
    if fl_path.exists():
        fl = read_json(fl_path)
        feature_ids = [
            f.get("id") for f in fl.get("features", [])
            if isinstance(f, dict) and f.get("id")
        ]

    build_head_before = rev_parse(build_branch, cwd=project_path)
    develop_head_before = rev_parse(develop, cwd=project_path)

    try:
        checkout_branch(develop, cwd=project_path)
        try:
            fast_forward_merge(build_branch, cwd=project_path)
        except GitError:
            merge_outcome = _merge_with_aah_resolution(project_path, build_branch, develop)
            if not merge_outcome["success"]:
                return merge_outcome

        develop_head_after = rev_parse(develop, cwd=project_path)

        if build_head_before and not is_ancestor(
            build_head_before, develop, cwd=project_path,
        ):
            return {
                "success": False,
                "error": (
                    f"Promote failed verification: develop does not contain the "
                    f"build branch tip ({build_head_before[:8]}). The fast-forward "
                    f"silently no-op'd."
                ),
                "develop_before": develop_head_before,
                "develop_after": develop_head_after,
                "build_head": build_head_before,
            }

        missing_features: dict[str, list[str]] = {}
        for fid in feature_ids:
            for sha in find_feature_commits(fid, cwd=project_path):
                if not is_ancestor(sha, develop, cwd=project_path):
                    missing_features.setdefault(fid, []).append(sha)
        if missing_features:
            return {
                "success": False,
                "error": (
                    f"Promote failed verification: '{develop}' does not contain "
                    f"all feature commits after merge."
                ),
                "develop_before": develop_head_before,
                "develop_after": develop_head_after,
                "build_head": build_head_before,
                "missing": missing_features,
            }

        push_to_remote(project_path, branch=develop, tags=True)

        if fl_path.exists():
            for fid in feature_ids:
                try:
                    update_feature_status(fl_path, fid, True)
                except SystemExit:
                    pass

        progress_path = aah_path / "claude-progress.json"
        if progress_path.exists():
            update_progress(
                progress_path,
                next_steps="Build complete. Run /aah-deploy or /aah-promote-to-main.",
            )

        return {
            "success": True,
            "promoted": build_branch,
            "to": develop,
            "develop_before": develop_head_before,
            "develop_after": develop_head_after,
            "build_head": build_head_before,
            "features_marked_passing": feature_ids,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        try:
            checkout_branch(original_branch, cwd=project_path)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote integration/build branch to develop")
    parser.add_argument("--wave", type=int, default=None,
                        help="Legacy wave-based promote of integration/wave-N.")
    parser.add_argument("--build-branch", type=str, default=None,
                        help="v2 (lean/sequential): promote this single build branch to develop, "
                             "gated on cumulative regression evidence only.")
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument("--cleanup-branches", action="store_true", default=False,
                        help="Delete feature branches after successful promote (safe delete only)")
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    if args.build_branch:
        result = promote_build_to_develop(
            project_path, args.build_branch, cleanup_branches=args.cleanup_branches,
        )
    else:
        if args.wave is None:
            parser.error("one of --build-branch or --wave is required")
        result = promote_to_develop(project_path, args.wave, cleanup_branches=args.cleanup_branches)
    json.dump(result, sys.stdout, indent=2)
    print()

    if result["success"]:
        print(f"Promoted {result['promoted']} to {result['to']}", file=sys.stderr)
        if result.get("branch_cleanup"):
            cleanup = result["branch_cleanup"]
            deleted = cleanup.get("deleted", [])
            skipped = cleanup.get("skipped", [])
            print(f"  Branch cleanup: {len(deleted)} deleted, {len(skipped)} skipped", file=sys.stderr)
            for d in deleted:
                print(f"    deleted: {d['branch']} ({d['sha_at_delete']})", file=sys.stderr)
            for s in skipped:
                print(f"    skipped: {s['branch']} — {s['reason']}", file=sys.stderr)
        sys.exit(0)
    else:
        print(f"Promotion blocked: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
