#!/usr/bin/env python3
"""
Merge completed worktrees from a wave into an integration branch.

Creates integration/wave-N branch off develop, merges each completed
worktree's branch. Exit 0 = success, Exit 2 = merge conflicts.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.git_utils import (
    GitError,
    branch_contains_all_commits,
    branch_exists,
    checkout_branch,
    code_subject_identity,
    commit_aah_state,
    current_branch,
    find_feature_commits,
    is_ancestor,
    merge_branch,
    rev_parse,
    run_git,
)
from aah.core.common.io_utils import read_json, read_yaml


def _has_lost_fields(pre_merge_data: dict, post_merge_data: dict) -> bool:
    """Check if critical fields were present pre-merge but absent post-merge."""
    if "implementation_reasoning" in pre_merge_data:
        if "implementation_reasoning" not in post_merge_data:
            return True

    pre_ku = pre_merge_data.get("knowledge_used", {})
    post_ku = post_merge_data.get("knowledge_used", {})
    if "codemap_context" in pre_ku and "codemap_context" not in post_ku:
        return True
    if "expertise" in pre_ku and "expertise" not in post_ku:
        return True

    return False


def _reconcile_feature_files(
    project_path: Path, aah_path: Path, feature_id: str,
) -> list[str]:
    """
    After a merge, check other features' .md files for field loss and restore them.

    Compares pre-merge (HEAD~1) vs post-merge (current) for each .md file that
    does NOT belong to the just-merged feature. If critical fields were lost,
    restores the pre-merge content for that file.

    Returns list of restored filenames (for logging).
    """
    from aah.core.common.feature_utils import parse_feature_frontmatter

    features_dir = aah_path / "plan" / "features"
    if not features_dir.is_dir():
        return []

    # Verify HEAD~1 exists (not the first commit)
    check = run_git(["rev-parse", "--verify", "HEAD~1"], cwd=project_path, check=False)
    if check.returncode != 0:
        return []

    restored = []
    fid_upper = feature_id.upper()

    for feature_file in features_dir.glob("*.md"):
        # Skip the feature that was just merged — its file is authoritative
        stem_upper = feature_file.stem.upper()
        if stem_upper == fid_upper or stem_upper.startswith(fid_upper + "-") or stem_upper.startswith(fid_upper + "_"):
            continue

        rel_path = feature_file.relative_to(project_path)

        # Get pre-merge content
        pre_result = run_git(
            ["show", f"HEAD~1:{rel_path}"],
            cwd=project_path, check=False,
        )
        if pre_result.returncode != 0 or not pre_result.stdout.strip():
            continue

        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
                tmp.write(pre_result.stdout)
                tmp_path = Path(tmp.name)
            pre_data = parse_feature_frontmatter(tmp_path)
            tmp_path.unlink(missing_ok=True)
            post_data = parse_feature_frontmatter(feature_file)
        except Exception:
            continue

        if not pre_data or not post_data:
            continue

        if _has_lost_fields(pre_data, post_data):
            feature_file.write_text(pre_result.stdout, encoding="utf-8")
            restored.append(feature_file.name)

    return restored

# Runtime files written by the check_intel_update hook — never meant to be committed.
# If a project was created before these were added to GITIGNORE_BASE they will appear
# as tracked/dirty files and block every git checkout.
_INTEL_RUNTIME_FILES = [
    ".aah/codebase-intel/pending-changes.json",
    ".aah/codebase-intel/.staleness-warned",
]


def _fix_untracked_intel_files(project_path: Path) -> bool:
    """
    Detect and auto-fix the case where intel runtime files are git-tracked,
    blocking checkout.  Returns True if any fix was applied.

    What this does:
      1. git rm --cached <file>   — removes from index without touching disk
      2. Appends the pattern to .gitignore so git never tracks it again
      3. Commits both changes so the working tree is clean before the caller
         retries the checkout

    Claude Code can call this transparently; the printed messages explain every
    action taken so the developer can audit the change.
    """
    gitignore_path = project_path / ".gitignore"

    # Which of the runtime files are currently tracked by git?
    result = run_git(["ls-files", "--"] + _INTEL_RUNTIME_FILES, cwd=project_path, check=False)
    tracked = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    if not tracked:
        return False

    print(
        "\n[AAH auto-fix] Detected git-tracked intel runtime files that block checkout.\n"
        "These files are written by the check_intel_update hook at runtime and should\n"
        "never be committed. Applying fix automatically:\n",
        file=sys.stderr,
    )

    # Step 1 — remove from git index (keeps file on disk)
    for rel_path in tracked:
        run_git(["rm", "--cached", rel_path], cwd=project_path, check=False)
        print(f"  git rm --cached {rel_path}", file=sys.stderr)

    # Step 2 — add to .gitignore if not already present
    existing_content = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    existing_lines = {line.strip() for line in existing_content.splitlines()}
    additions = []
    for rel_path in _INTEL_RUNTIME_FILES:
        if rel_path not in existing_lines:
            additions.append(rel_path)

    if additions:
        block = "\n# AAH intel runtime state — written by hooks, never commit\n"
        block += "\n".join(additions) + "\n"
        with open(gitignore_path, "a", encoding="utf-8") as fh:
            fh.write(block)
        run_git(["add", str(gitignore_path)], cwd=project_path, check=False)
        for line in additions:
            print(f"  .gitignore += {line}", file=sys.stderr)

    # Step 3 — commit so the tree is clean
    staged = run_git(["diff", "--cached", "--name-only"], cwd=project_path, check=False)
    if staged.stdout.strip():
        run_git(
            ["commit", "-m", "chore: untrack intel runtime files, add to .gitignore"],
            cwd=project_path,
            check=False,
        )
        print(
            "\n  Committed fix. Retrying checkout...\n",
            file=sys.stderr,
        )

    return True


def _find_feature_branch(project_path: Path, aah_path: Path, feature_id: str) -> str | None:
    """
    Find the branch that contains a feature's work.

    Search order:
    1. Direct name match: feature/<id> or bare <id>
    2. Worktree-agent branches with commits referencing this feature
    3. feature-commits.json manifest SHA → branch containment lookup
    """
    # 1. Direct name match
    for candidate in [f"feature/{feature_id}", feature_id]:
        if branch_exists(candidate, cwd=project_path):
            return candidate

    # 2. Search worktree-* branches for commits referencing this feature
    result = run_git(
        ["branch", "--list", "worktree-*", "--format=%(refname:short)"],
        cwd=project_path, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        for branch in result.stdout.strip().split("\n"):
            branch = branch.strip()
            if not branch:
                continue
            log_result = run_git(
                ["log", branch, "--oneline", f"--grep={feature_id}", "-1"],
                cwd=project_path, check=False,
            )
            if log_result.stdout.strip():
                return branch

    # 3. Fallback: feature-commits.json manifest → branch containment
    manifest_path = aah_path / "build" / "feature-commits.json"
    if manifest_path.exists():
        import json as _json
        data = _json.loads(manifest_path.read_text(encoding='utf-8'))
        shas = data.get(feature_id, [])
        if shas:
            for sha in shas[:3]:
                ref_result = run_git(
                    ["branch", "--contains", sha, "--format=%(refname:short)"],
                    cwd=project_path, check=False,
                )
                if ref_result.returncode == 0 and ref_result.stdout.strip():
                    for b in ref_result.stdout.strip().split("\n"):
                        b = b.strip()
                        if b and b not in ("develop", "main") and not b.startswith("integration/"):
                            return b

    return None




def _preserve_reasoning_to_develop(
    project_path: Path,
    aah_path: Path,
    feature_id: str,
    source_branch: str,
) -> bool:
    """
    Copy ## Implementation Reasoning section from feature branch to develop.

    Feature branch is authoritative — may contain revisions added during QA fixes.
    Reads the raw markdown from the feature branch and replaces the section on
    develop using append_section (accumulate=False to replace, not duplicate).
    Returns True if develop was updated, False otherwise.
    """
    from aah.core.common.feature_utils import find_feature_file, append_section

    features_dir = aah_path / "plan" / "features"
    feature_file = find_feature_file(features_dir, feature_id)
    if not feature_file:
        return False

    rel_path = feature_file.relative_to(project_path)

    result = run_git(
        ["show", f"{source_branch}:{rel_path}"],
        cwd=project_path, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False

    branch_content = result.stdout
    # Extract ## Implementation Reasoning section lines from branch content
    in_section = False
    section_lines: list[str] = []
    for line in branch_content.splitlines():
        if line.startswith("## Implementation Reasoning"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            section_lines.append(line)

    if not section_lines:
        return False

    develop_file = project_path / rel_path
    if not develop_file.exists():
        return False

    # Strip trailing blank lines
    while section_lines and not section_lines[-1].strip():
        section_lines.pop()

    append_section(develop_file, "Implementation Reasoning", section_lines, accumulate=False)
    return True


def _resolve_base_branch(
    project_path: Path,
    develop: str,
    feature_commits: dict[str, list[str]],
) -> tuple[str | None, str | None]:
    """
    Determine which branch should be the base for the integration branch.

    Strategy: pick the first branch from a preference order
    (develop, main) that already contains every wave feature's commits.
    If `develop` qualifies, use it (the happy path). If only `main`
    qualifies, we've detected a branch inversion — return main plus a
    warning so the caller can surface it.

    Returns (base_branch, warning). base_branch is None if no candidate
    branch contains all the work — that's a hard failure.
    """
    all_shas = [sha for shas in feature_commits.values() for sha in shas]

    candidates = [develop]
    if "main" not in candidates:
        candidates.append("main")

    for candidate in candidates:
        if not branch_exists(candidate, cwd=project_path):
            continue
        if branch_contains_all_commits(candidate, all_shas, cwd=project_path):
            warning = None
            if candidate != develop:
                warning = (
                    f"Branch inversion detected: wave feature commits are "
                    f"reachable from '{candidate}' but not from '{develop}'. "
                    f"Integration branch will be based on '{candidate}' to "
                    f"preserve the work, but the project's branching model is "
                    f"broken — '{develop}' should be the integration target. "
                    f"Fix: `git checkout {develop} && git merge --ff-only {candidate}` "
                    f"and rewind '{candidate}' to its pre-wave tip."
                )
            return candidate, warning

    return None, None


def recreate_integration_branch(project_path: Path, wave_num: int) -> dict:
    """Delete and recreate integration/wave-N from develop HEAD.

    Called during rework when a wave's integration branch has stale code.
    Only acts if integration branch exists AND has not been promoted to develop.

    Returns:
        {"action": "recreated"|"none", "branch": str, "from": str, "reason": str}
    """
    aah_path = project_path / ".aah"
    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    branch_config = manifest.get("branching_config", {})
    develop = branch_config.get("develop_branch", "develop")
    integration_prefix = branch_config.get("integration_prefix", "integration/wave-")
    integration_branch = f"{integration_prefix}{wave_num}"

    if not branch_exists(integration_branch, cwd=project_path):
        return {"action": "none", "reason": "Integration branch does not exist"}

    # Check if already promoted (develop contains integration HEAD)
    integration_head = rev_parse(integration_branch, cwd=project_path)
    if integration_head:
        contains_result = run_git(
            ["branch", "--contains", integration_head, "--format=%(refname:short)"],
            cwd=project_path, check=False,
        )
        if contains_result.returncode == 0 and develop in contains_result.stdout.split():
            return {"action": "none", "reason": "Already promoted to develop"}

    # Delete and recreate from develop HEAD
    original = current_branch(cwd=project_path)
    try:
        # Can't delete a branch we're on — switch to develop first
        checkout_branch(develop, cwd=project_path)
        run_git(["branch", "-D", integration_branch], cwd=project_path)
        checkout_branch(integration_branch, cwd=project_path, create=True)

        return {"action": "recreated", "branch": integration_branch, "from": develop}
    finally:
        try:
            checkout_branch(original, cwd=project_path)
        except Exception:
            pass


def ensure_integration_branch(project_path: Path, wave_num: int) -> dict:
    """
    Ensure integration/wave-N branch exists, creating it from develop HEAD if not.

    Returns {"created": bool, "branch": str}.
    """
    aah_path = project_path / ".aah"
    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    branch_config = manifest.get("branching_config", {})
    develop = branch_config.get("develop_branch", "develop")
    integration_prefix = branch_config.get("integration_prefix", "integration/wave-")
    integration_branch = f"{integration_prefix}{wave_num}"

    if branch_exists(integration_branch, cwd=project_path):
        # An existing integration branch is only reusable if it already contains
        # the current develop HEAD. A branch left over from a prior run was cut
        # from an OLD develop (it predates this wave's dependencies) — reusing it
        # silently poisons the wave with a stale base. Detect that and refresh
        # from develop HEAD instead of blindly returning created=False.
        develop_head = rev_parse(develop, cwd=project_path)
        if develop_head is None or is_ancestor(
            develop_head, integration_branch, cwd=project_path,
        ):
            return {"created": False, "branch": integration_branch}

        # Stale: fast-forward the integration branch up to develop HEAD.
        original = current_branch(cwd=project_path)
        try:
            checkout_branch(integration_branch, cwd=project_path)
            run_git(["merge", "--ff-only", develop], cwd=project_path, check=False)
        finally:
            try:
                checkout_branch(original, cwd=project_path)
            except Exception:
                pass
        refreshed = is_ancestor(develop_head, integration_branch, cwd=project_path)
        return {
            "created": False,
            "branch": integration_branch,
            "refreshed": refreshed,
            "stale": not refreshed,
        }

    original = current_branch(cwd=project_path)
    try:
        checkout_branch(develop, cwd=project_path)
        checkout_branch(integration_branch, cwd=project_path, create=True)
    finally:
        try:
            checkout_branch(original, cwd=project_path)
        except Exception:
            pass

    return {"created": True, "branch": integration_branch}


def _ensure_feature_marked_passing(
    project_path: Path, feature_id: str, integration_branch: str,
) -> str | None:
    """Assert the invariant: commits on integration ⇒ ``passes=true`` there.

    ``passes`` in feature-list.json is the ONE condition the orchestrator reads
    for feature completion, and while per-feature QA is disabled this module is
    its only writer. Writing it solely on the path that performs the merge left a
    hole: a merge landed by ``aah-merge-resolver``, by hand, or by an interrupted
    earlier run puts the code on integration with the flag still false, and the
    documented recovery ("re-run merge-feature") hit the already-present early
    return — so the orchestrator re-dispatched an already-merged feature forever.
    Called from BOTH exits now, so the invariant holds however the merge landed.

    The write must happen ON the integration branch: ``run_regression_suite``
    reads feature-list.json from there. Reading the file after the checkout is
    also what makes "which branch's copy?" unambiguous.

    Returns None on success, or an error string. Never raises — bookkeeping must
    not turn a landed merge into a reported merge failure.
    """
    fl_path = project_path / ".aah" / "feature-list.json"
    if not fl_path.exists():
        return "feature-list.json not found"

    from aah.core.common.feature_list import (
        get_feature_by_id,
        load_feature_list,
        update_feature_status,
    )

    original = current_branch(cwd=project_path)
    switched = original != integration_branch
    try:
        if switched:
            checkout_branch(integration_branch, cwd=project_path)
        feature = get_feature_by_id(load_feature_list(fl_path), feature_id)
        if feature is None:
            # A silent skip here is how an unflagged feature stalls a wave with
            # no stated reason, so this is reported rather than swallowed.
            return f"'{feature_id}' is not in feature-list.json"
        if feature.get("passes"):
            return None
        update_feature_status(fl_path, feature_id, True, skip_test_check=True)
        commit_aah_state(
            project_path,
            f"chore: mark {feature_id} as passing on integration branch",
        )
        return None
    except GitError as exc:
        return str(exc)
    finally:
        if switched:
            try:
                checkout_branch(original, cwd=project_path)
            except Exception:
                pass


def merge_feature_to_integration(
    project_path: Path, wave_num: int, feature_id: str,
) -> dict:
    """
    Merge a single feature branch into integration/wave-N after QA pass.

    Idempotent: if the feature's commits are already on the integration
    branch, returns early with success — still asserting the ``passes`` flag,
    which is what makes the re-run-after-resolver recovery actually recover.

    Returns {"success": bool, "merged": str, "conflicts": list,
             "status_marked": bool}.
    """
    aah_path = project_path / ".aah"
    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    branch_config = manifest.get("branching_config", {})
    integration_prefix = branch_config.get("integration_prefix", "integration/wave-")
    integration_branch = f"{integration_prefix}{wave_num}"

    if not branch_exists(integration_branch, cwd=project_path):
        return {
            "success": False,
            "merged": feature_id,
            "conflicts": [],
            "error": f"Integration branch '{integration_branch}' does not exist. "
                     f"Run ensure-integration first.",
        }

    # Idempotent check: are the feature's commits already on integration?
    # NOTE: post_impl_comment is NOT called here (or after the merge below).
    # The orchestrator drives it as a first-class action so the comment fires
    # regardless of how the merge landed (script, aah-merge-resolver, manual).
    #
    # Checked BEFORE the source-branch lookup below: the invariant this path
    # asserts ("code on integration ⇒ marked passing") does not need a source
    # branch, and a feature branch cleaned up after an earlier merge would
    # otherwise fail the lookup and return an error without ever marking the
    # feature — the same deadlock by a different route.
    feature_shas = find_feature_commits(feature_id, cwd=project_path)
    if feature_shas and branch_contains_all_commits(
        integration_branch, feature_shas, cwd=project_path,
    ):
        # "Already merged" and "marked passing" are two separate facts. Only the
        # merge below used to write the second one, so returning here without it
        # left resolver-landed merges permanently incomplete. Assert it.
        error = _ensure_feature_marked_passing(
            project_path, feature_id, integration_branch,
        )
        result = {
            "success": True,
            "merged": feature_id,
            "conflicts": [],
            "already_present": True,
            "status_marked": error is None,
        }
        if error:
            result["status_error"] = error
        return result

    # Find the feature's branch
    source_branch = _find_feature_branch(project_path, aah_path, feature_id)
    if source_branch is None:
        return {
            "success": False,
            "merged": feature_id,
            "conflicts": [],
            "error": f"No branch found for feature '{feature_id}'.",
        }

    original = current_branch(cwd=project_path)
    error: str | None = None
    try:
        try:
            checkout_branch(integration_branch, cwd=project_path)
        except GitError as checkout_err:
            # If the failure mentions one of our runtime files, auto-fix and retry once.
            err_str = str(checkout_err)
            if any(f in err_str for f in _INTEL_RUNTIME_FILES):
                if _fix_untracked_intel_files(project_path):
                    checkout_branch(integration_branch, cwd=project_path)
                else:
                    raise
            else:
                raise
        try:
            merge_branch(source_branch, cwd=project_path, no_ff=True)
        except GitError as e:
            run_git(["merge", "--abort"], cwd=project_path, check=False)
            return {
                "success": False,
                "merged": feature_id,
                "conflicts": [{"feature": feature_id, "error": str(e)}],
            }

        # Reconcile other features' YAMLs that may have lost fields during merge
        restored = _reconcile_feature_files(project_path, aah_path, feature_id)
        if restored:
            run_git(["add", ".aah/plan/features/"], cwd=project_path, check=False)
            run_git(
                ["commit", "-m", f"chore: reconcile feature YAMLs after {feature_id} merge"],
                cwd=project_path, check=False,
            )

        # After successful merge, update feature status on integration branch
        # so run_regression_suite finds passing features when it runs here.
        # Same writer as the already-present path above: one owner of the flag.
        error = _ensure_feature_marked_passing(
            project_path, feature_id, integration_branch,
        )

        # Preserve implementation_reasoning from feature branch to develop
        develop = branch_config.get("develop_branch", "develop")
        original_for_reasoning = current_branch(cwd=project_path)
        try:
            checkout_branch(develop, cwd=project_path)
            updated = _preserve_reasoning_to_develop(
                project_path, aah_path, feature_id, source_branch,
            )
            if updated:
                commit_aah_state(
                    project_path,
                    f"chore: preserve implementation_reasoning from {source_branch}",
                )
        except Exception:
            pass  # Non-fatal — field may still be lost but no worse than before
        finally:
            try:
                checkout_branch(original_for_reasoning, cwd=project_path)
            except Exception:
                pass

    finally:
        try:
            checkout_branch(original, cwd=project_path)
        except Exception:
            pass

    result = {
        "success": True,
        "merged": feature_id,
        "conflicts": [],
        "status_marked": error is None,
    }
    if error:
        result["status_error"] = error
    return result


def merge_wave_to_integration(project_path: Path, wave_num: int) -> dict:
    """
    Create an integration branch that contains all of a wave's feature work
    and verify (pre- and post-flight) that the work is actually present.

    This used to be a naive "merge each feature branch" loop that could
    silently report success with an empty integration branch. The rewrite:

    1. Scans `git log --all` for every commit whose subject matches
       `feat(FNNN):` (or fix/refactor/test/chore/perf) for each feature
       in the wave. This is the authoritative signal for "where does
       this feature live?"
    2. Picks the integration base by finding the first branch from
       [develop, main] that already contains every wave commit. If
       develop qualifies, great. If only main does, we detected a
       branch inversion and surface it as a warning.
    3. Merges any per-feature worktree branches that exist (for the
       happy-path worktree workflow where features are in isolated
       refs that don't touch develop yet).
    4. POSTFLIGHT: verifies the final integration branch HEAD contains
       every single feature commit. If it doesn't, the merge is a lie
       and we return success=False with the specific missing shas.

    Returns a result dict:
      {success, integration_branch, wave, feature_commits, merged,
       conflicts, warnings, missing}
    """
    aah_path = project_path / ".aah"

    manifest_path = aah_path / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    branch_config = manifest.get("branching_config", {})
    develop = branch_config.get("develop_branch", "develop")
    integration_prefix = branch_config.get("integration_prefix", "integration/wave-")
    integration_branch = f"{integration_prefix}{wave_num}"

    waves_path = aah_path / "plan" / "waves.json"
    if not waves_path.exists():
        return {"success": False, "error": "waves.json not found"}

    from aah.core.plan.compute_waves import flatten_waves
    waves_data = read_json(waves_path)
    waves = flatten_waves(waves_data)
    if wave_num >= len(waves):
        return {
            "success": False,
            "error": f"Wave {wave_num} not found (total: {len(waves)})",
        }

    wave_features = waves[wave_num]

    # --- Pre-merge safety: verify runtime validation and regression passed ---
    # Result files carry an HMAC attestation. Verify it before
    # trusting overall_passed/passed — a hand-written {"passed": true} must not
    # sail through. A stale-secret result (for example, a new clone or a manual
    # key replacement) BLOCKS with an instruction to re-run the gate.
    # Normal session restarts preserve the key and keep valid evidence usable.
    #
    # Both gates are CADENCE-SCOPED, using the SAME helpers the orchestrator and
    # verify.py use — never a local re-derivation of the rule:
    #   * runtime    → checkpoint waves (odd 0-indexed, plus the last)
    #   * regression → the last wave only
    # A wave outside a gate's cadence produces no such evidence, so requiring it
    # here while the other two layers gate would block the merge forever. The
    # attestation, subject-binding, and no_signal rejections below are UNCHANGED
    # for the waves where each gate does apply.
    from aah.core.common.verified_artifacts import (
        ArtifactState,
        load_attested_artifact,
    )
    from aah.core.git_ops._attest_gate import record_gate_audit
    from aah.core.build.orchestrator import is_checkpoint_wave, is_last_wave
    from aah.core.build.verify import (
        REGRESSION_PREFIX,
        RUNTIME_RESULTS_PREFIXES,
    )

    if is_checkpoint_wave(aah_path, wave_num):
        runtime_path = aah_path / "build" / "runtime-results" / f"wave-{wave_num}-all.json"
        runtime_artifact = load_attested_artifact(
            runtime_path, project_path, RUNTIME_RESULTS_PREFIXES, "json"
        )
        if runtime_artifact.state is not ArtifactState.VERIFIED:
            if runtime_artifact.state is ArtifactState.MISSING:
                return {
                    "success": False,
                    "error": f"Cannot merge: runtime validation results missing for wave {wave_num}. "
                             f"Dispatch: aah-runtime-validator subagent for wave {wave_num}",
                    "wave": wave_num,
                }
            if runtime_artifact.state is ArtifactState.STALE_SECRET:
                record_gate_audit(
                    aah_path, gate="merge", artifact="runtime",
                    verdict="reverify_required", reason=runtime_artifact.reason,
                    wave=wave_num,
                )
                return {
                    "success": False,
                    "error": f"Cannot merge: runtime result attestation requires reverification "
                             f"(project key mismatch, {runtime_artifact.reason}) — re-run runtime "
                             f"validation for wave {wave_num} to regenerate fresh signed evidence.",
                    "wave": wave_num,
                }
            record_gate_audit(
                aah_path, gate="merge", artifact="runtime", verdict="refuse",
                reason=runtime_artifact.reason, wave=wave_num,
            )
            return {
                "success": False,
                "error": f"Cannot merge: runtime result attestation failed "
                         f"({runtime_artifact.reason}) — "
                         f"re-run runtime validation for wave {wave_num}.",
                "wave": wave_num,
            }
        runtime_data = runtime_artifact.payload
        assert runtime_data is not None
        if not runtime_data.get("overall_passed"):
            return {
                "success": False,
                "error": f"Cannot merge: runtime validation FAILED for wave {wave_num}. Fix failures first.",
                "wave": wave_num,
            }

    if is_last_wave(aah_path, wave_num):
        regression_path = aah_path / "build" / "test-results" / "regression-latest.json"
        regression_artifact = load_attested_artifact(
            regression_path, project_path, REGRESSION_PREFIX, "json"
        )
        if regression_artifact.state is not ArtifactState.VERIFIED:
            if regression_artifact.state is ArtifactState.MISSING:
                return {
                    "success": False,
                    "error": f"Cannot merge: regression results missing. Run regression first.",
                    "wave": wave_num,
                }
            if regression_artifact.state is ArtifactState.STALE_SECRET:
                record_gate_audit(
                    aah_path, gate="merge", artifact="regression",
                    verdict="reverify_required", reason=regression_artifact.reason,
                    wave=wave_num,
                )
                return {
                    "success": False,
                    "error": "Cannot merge: regression result attestation requires "
                             "reverification (project key mismatch) — re-run regression "
                             "suite to regenerate fresh signed evidence.",
                    "wave": wave_num,
                }
            record_gate_audit(
                aah_path, gate="merge", artifact="regression", verdict="refuse",
                reason=regression_artifact.reason, wave=wave_num,
            )
            return {
                "success": False,
                "error": f"Cannot merge: regression result attestation failed "
                         f"({regression_artifact.reason}) — "
                         f"re-run regression suite.",
                "wave": wave_num,
            }
        reg_data = regression_artifact.payload
        assert reg_data is not None
        # Check status first (no_signal is a legitimate outcome)
        # Gate on status (source of truth), not passed alone. no_signal is a
        # vacuous gate (no passing features to regress) — NEVER satisfied.
        # Preserved verbatim for the last wave: with the per-feature evidence
        # gate no longer invoked, this run is the first automated step that
        # reads any test outcome, so it must not be weakened.
        reg_status = reg_data.get("status", "pass" if reg_data.get("passed") else "fail")
        if reg_status == "no_signal":
            return {
                "success": False,
                "error": "Cannot merge: regression produced no signal (no passing features "
                         "to regress) — not a satisfied gate.",
                "wave": wave_num,
            }

        # Not no_signal — verify subject binding before trusting passed/fail status
        # Regression evidence must bind to integration/wave-N's current HEAD
        subject = reg_data.get("subject")
        if not isinstance(subject, dict):
            record_gate_audit(
                aah_path, gate="merge", artifact="regression", verdict="refuse",
                reason="missing_subject_binding", wave=wave_num,
            )
            return {
                "success": False,
                "error": "Cannot merge: regression evidence missing subject binding.",
                "wave": wave_num,
            }

        expected_branch = f"integration/wave-{wave_num}"
        if subject.get("branch") != expected_branch:
            record_gate_audit(
                aah_path, gate="merge", artifact="regression", verdict="refuse",
                reason="subject_branch_mismatch", wave=wave_num,
                expected_subject={"branch": expected_branch},
                recorded_subject={"branch": subject.get("branch")},
            )
            return {
                "success": False,
                "error": f"Cannot merge: regression evidence branch mismatch "
                         f"(expected {expected_branch}, got {subject.get('branch')}).",
                "wave": wave_num,
            }

        # The runner records the .aah/.claude-excluding content identity, so the
        # gate must hash the branch tip the same way (not as a raw git SHA).
        current_sha = code_subject_identity(cwd=project_path, ref=expected_branch)
        if current_sha is None or subject.get("commit_sha") != current_sha:
            recorded_sha = subject.get("commit_sha", "unknown")
            record_gate_audit(
                aah_path, gate="merge", artifact="regression", verdict="refuse",
                reason="subject_sha_mismatch", wave=wave_num,
                expected_subject={"branch": expected_branch, "commit_sha": current_sha},
                recorded_subject={"branch": expected_branch, "commit_sha": recorded_sha},
            )
            return {
                "success": False,
                "error": f"Cannot merge: regression evidence SHA mismatch "
                         f"(expected {current_sha[:8] if current_sha else 'unknown'}, "
                         f"got {recorded_sha[:8] if recorded_sha else 'unknown'}).",
                "wave": wave_num,
            }

        # Subject verified — check pass/fail
        if reg_status != "pass" and not reg_data.get("passed"):
            return {
                "success": False,
                "error": f"Cannot merge: regression suite FAILED. Fix failures first.",
                "wave": wave_num,
            }

    # No pre-merge artifact-completeness scan. Completeness is the verifier's job
    # at promotion, and the feature merges below are what PRODUCE the evidence it
    # later reads — demanding a complete set before merging inverted that order.
    # The regression subject check above stays.

    # --- Step 1: find feature commits across all refs ---
    # resolve_feature_commits unions git-log grep results with the
    # .aah/build/feature-commits.json manifest, which is the
    # authoritative fallback for features whose commits don't follow
    # the `feat(FNNN):` subject convention.
    feature_commits: dict[str, list[str]] = {
        fid: find_feature_commits(fid, cwd=project_path)
        for fid in wave_features
    }
    features_without_commits = [
        fid for fid, shas in feature_commits.items() if not shas
    ]
    if features_without_commits:
        return {
            "success": False,
            "error": (
                f"No feat({{id}}): commits found for features: "
                f"{features_without_commits}. Did aah-feature-implementer "
                f"actually commit anything? Check worktree branches."
            ),
            "wave": wave_num,
            "feature_commits": feature_commits,
        }

    # --- Step 2: resolve the integration base branch ---
    base_branch, inversion_warning = _resolve_base_branch(
        project_path, develop, feature_commits,
    )
    if base_branch is None:
        return {
            "success": False,
            "error": (
                f"No candidate base branch ({develop!r} or 'main') "
                f"contains all wave {wave_num} feature commits. The work "
                f"is probably still on isolated worktree branches that "
                f"need merging first. Feature commit locations: "
                f"{feature_commits}"
            ),
            "wave": wave_num,
            "feature_commits": feature_commits,
        }

    warnings: list[str] = []
    if inversion_warning:
        warnings.append(inversion_warning)

    # Commit any pending .aah/ state so branch switches don't fail.
    # This happens on the CURRENT branch, which may not be ideal, but
    # the alternative is stashing — and stash-pop across divergent
    # branches is flaky. Leaving the guard to the scaffold fix.
    commit_aah_state(
        project_path,
        f"chore: commit AAH state before wave {wave_num} merge",
    )

    original_branch = current_branch(cwd=project_path)

    try:
        # --- Step 3: create (or reset) integration branch from base ---
        if branch_exists(integration_branch, cwd=project_path):
            # Reuse — but verify it already descends from base; if not,
            # reset it to base first so we don't inherit stale state.
            checkout_branch(integration_branch, cwd=project_path)
            base_sha = rev_parse(base_branch, cwd=project_path)
            if base_sha and not branch_contains_all_commits(
                integration_branch, [base_sha], cwd=project_path,
            ):
                # Integration branch does not descend from base — reset it
                run_git(
                    ["reset", "--hard", base_branch],
                    cwd=project_path, check=False,
                )
        else:
            checkout_branch(base_branch, cwd=project_path)
            checkout_branch(integration_branch, cwd=project_path, create=True)

        # --- Step 4: merge any feature worktree branches ---
        # Branch-based merges only matter if a feature's commits are NOT
        # yet reachable from the integration branch. Find those and merge
        # their owning branch.
        merged: list[str] = []
        conflicts: list[dict] = []
        merged_branches: set[str] = set()

        for feature_id in wave_features:
            shas = feature_commits[feature_id]
            if branch_contains_all_commits(
                integration_branch, shas, cwd=project_path,
            ):
                # Already present from the base branch — no merge needed.
                continue

            source_branch = _find_feature_branch(
                project_path, aah_path, feature_id,
            )
            if source_branch is None:
                conflicts.append({
                    "feature": feature_id,
                    "error": (
                        f"Feature commits {shas[:2]}… are not in base "
                        f"'{base_branch}' and no worktree branch was found."
                    ),
                })
                continue

            if source_branch in merged_branches:
                merged.append(feature_id)
                continue

            try:
                merge_branch(source_branch, cwd=project_path, no_ff=True)
                merged.append(feature_id)
                merged_branches.add(source_branch)
            except GitError as e:
                run_git(["merge", "--abort"], cwd=project_path, check=False)
                conflicts.append({"feature": feature_id, "error": str(e)})

        # --- Step 4b: sync feature-list.json from YAMLs after all merges ---
        # Ensures the merged feature-list.json is consistent with all merged
        # YAMLs before final verification.
        features_dir = aah_path / "plan" / "features"
        fl_path = aah_path / "feature-list.json"
        if features_dir.is_dir():
            try:
                from aah.core.common.feature_list import sync_features_from_yaml, validate_json_matches_yamls
                sync_features_from_yaml(features_dir, fl_path)
                # Verify sync produced correct result
                drift_errors = validate_json_matches_yamls(features_dir, fl_path)
                if drift_errors:
                    warnings.extend([f"Post-sync drift: {e}" for e in drift_errors])
                commit_aah_state(
                    project_path,
                    f"chore: sync feature-list.json from YAMLs after wave {wave_num} merge",
                )
            except Exception:
                pass  # Non-fatal — postflight will catch any real issues

        # --- Step 5: POSTFLIGHT verify every feature commit is now in
        # integration. This is the non-negotiable truth check. ---
        missing: dict[str, list[str]] = {}
        for feature_id, shas in feature_commits.items():
            for sha in shas:
                if not branch_contains_all_commits(
                    integration_branch, [sha], cwd=project_path,
                ):
                    missing.setdefault(feature_id, []).append(sha)

        integration_head = rev_parse(integration_branch, cwd=project_path)

        if missing or conflicts:
            return {
                "success": False,
                "integration_branch": integration_branch,
                "integration_head": integration_head,
                "base_branch": base_branch,
                "wave": wave_num,
                "feature_commits": feature_commits,
                "merged": merged,
                "conflicts": conflicts,
                "missing": missing,
                "warnings": warnings,
                "error": (
                    "Postflight verification failed: integration branch "
                    "does not contain all wave feature commits."
                    if missing else "Merge conflicts during wave integration."
                ),
            }

        return {
            "success": True,
            "integration_branch": integration_branch,
            "integration_head": integration_head,
            "base_branch": base_branch,
            "wave": wave_num,
            "feature_commits": feature_commits,
            "merged": merged,
            "conflicts": [],
            "missing": {},
            "warnings": warnings,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "wave": wave_num}

    finally:
        try:
            checkout_branch(original_branch, cwd=project_path)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge wave worktrees to integration branch")
    parser.add_argument("--project-path", type=Path, default=None)
    # Backward-compat: top-level --wave for callers that don't use subcommands
    parser.add_argument("--wave", type=int, default=None, help="Wave number (backward compat)")
    sub = parser.add_subparsers(dest="command")

    merge_p = sub.add_parser("merge-wave", help="Merge all wave worktrees to integration branch")
    merge_p.add_argument("--wave", type=int, required=True)
    merge_p.add_argument("--project-path", type=Path, default=None)

    ensure_p = sub.add_parser("ensure-integration", help="Create integration/wave-N from develop if missing")
    ensure_p.add_argument("--wave", type=int, required=True)
    ensure_p.add_argument("--project-path", type=Path, default=None)

    recreate_p = sub.add_parser("recreate-integration", help="Delete and recreate integration/wave-N from develop HEAD (rework)")
    recreate_p.add_argument("--wave", type=int, required=True)
    recreate_p.add_argument("--project-path", type=Path, default=None)

    feat_p = sub.add_parser("merge-feature", help="Merge one feature branch into integration/wave-N")
    feat_p.add_argument("--wave", type=int, required=True)
    feat_p.add_argument("--feature-id", required=True)
    feat_p.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    # Backward compat: no subcommand + --wave → merge-wave
    command = args.command
    if command is None:
        if args.wave is not None:
            command = "merge-wave"
        else:
            parser.print_help()
            sys.exit(1)

    if command == "recreate-integration":
        result = recreate_integration_branch(project_path, args.wave)
        json.dump(result, sys.stdout, indent=2)
        print()
        if result["action"] == "recreated":
            print(f"Recreated {result['branch']} from {result['from']}", file=sys.stderr)
        else:
            print(f"No action: {result['reason']}", file=sys.stderr)
        sys.exit(0)

    elif command == "ensure-integration":
        result = ensure_integration_branch(project_path, args.wave)
        json.dump(result, sys.stdout, indent=2)
        print()
        action = "Created" if result["created"] else "Already exists"
        print(f"{action}: {result['branch']}", file=sys.stderr)
        sys.exit(0)

    elif command == "merge-feature":
        result = merge_feature_to_integration(project_path, args.wave, args.feature_id)
        json.dump(result, sys.stdout, indent=2)
        print()
        if result["success"]:
            extra = " (already present)" if result.get("already_present") else ""
            print(f"Merged {args.feature_id} into integration/wave-{args.wave}{extra}", file=sys.stderr)
            # The passes flag is the orchestrator's only completion signal, so a
            # merge that landed the code but could not mark the feature is NOT a
            # clean success — say so loudly, or the wave stalls with no reason why.
            if not result.get("status_marked"):
                print(
                    f"  WARNING: {args.feature_id} is NOT marked passing "
                    f"({result.get('status_error', 'unknown reason')}). The "
                    "orchestrator will treat it as incomplete and re-dispatch it.",
                    file=sys.stderr,
                )
            sys.exit(0)
        else:
            print(f"Merge failed for {args.feature_id}: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(2)

    else:  # merge-wave (default)
        result = merge_wave_to_integration(project_path, args.wave)
        json.dump(result, sys.stdout, indent=2)
        print()
        if result["success"]:
            print(
                f"Wave {args.wave} merged to {result['integration_branch']} "
                f"(base={result.get('base_branch')}, head={result.get('integration_head', '?')[:8]})",
                file=sys.stderr,
            )
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}", file=sys.stderr)
            sys.exit(0)
        else:
            print(f"Wave {args.wave} merge failed: {result.get('error', 'unknown')}", file=sys.stderr)
            for c in result.get("conflicts", []) or []:
                print(f"  conflict: {c.get('feature')}: {c.get('error')}", file=sys.stderr)
            if result.get("missing"):
                print(f"  missing feature commits: {result['missing']}", file=sys.stderr)
            for warning in result.get("warnings", []):
                print(f"  warning: {warning}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
