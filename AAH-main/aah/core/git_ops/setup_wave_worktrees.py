#!/usr/bin/env python3
"""
Pre-create git worktrees for parallel feature branches in a wave.

The orchestrator calls this once, sequentially, before dispatching parallel
agents — avoiding git index.lock contention from concurrent worktree creation.

Usage:
  aah run core.git_ops.setup_wave_worktrees setup --wave N --project-path $DIR
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from aah.core.common.feature_utils import flatten_wave_features
from aah.core.common.git_utils import (
    GitError,
    branch_exists,
    is_ancestor,
    rev_parse,
    run_git,
)
from aah.core.common.io_utils import read_json, read_yaml
from aah.core.git_ops.ensure_merge_drivers import ensure_merge_config


def setup_wave_worktrees(
    project_path: Path,
    wave_num: int,
    feature_ids: list[str] | None = None,
    base_branch: str | None = None,
) -> dict:
    """
    Pre-create worktrees for all features in a wave.

    Creates `.claude/worktrees/<fid>` with branch `feature/<fid>` off the base
    branch (default: `integration/wave-N`). ``base_branch`` overrides that
    default — used by the forward-relocation model when a rework entry's
    forward wave must branch off the current develop/integration HEAD rather
    than a per-wave integration branch that may not exist yet.
    Skips any worktree that already exists (idempotent on re-run).
    """
    aah_path = project_path / ".aah"

    if feature_ids is None:
        waves_path = aah_path / "plan" / "waves.json"
        if not waves_path.exists():
            return {"success": False, "worktrees": {}, "errors": ["waves.json not found"]}
        from aah.core.plan.compute_waves import flatten_waves
        waves_data = read_json(waves_path)
        waves = flatten_waves(waves_data)
        if wave_num >= len(waves):
            return {"success": False, "worktrees": {}, "errors": [f"Wave {wave_num} not found"]}
        # Handle all wave formats: dict, nested tiers, or flat list
        feature_ids = flatten_wave_features(waves[wave_num])

    if base_branch is None:
        manifest_path = aah_path / "manifest.yaml"
        manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
        integration_prefix = manifest.get("branching_config", {}).get("integration_prefix", "integration/wave-")
        base_branch = f"{integration_prefix}{wave_num}"

    # Assert the merge=ours attributes + driver before any worktree forks. The
    # features about to be dispatched each rewrite the .aah state files, and
    # this wave's merge must resolve those to the integration branch's copy
    # rather than stop on a conflict. Non-fatal: without it the wave still runs,
    # it just drags aah-merge-resolver into bookkeeping files.
    try:
        ensure_merge_config(project_path)
    except (GitError, OSError) as e:
        print(
            f"⚠ could not register git merge config: {e}\n"
            f"  Wave {wave_num} merges may conflict in .aah/ state files.",
            file=sys.stderr,
        )

    worktrees_dir = project_path / ".claude" / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Secrets stay at the project root and cross into feature worktrees only at
    # the scoped test-process boundary.  Older harness versions created a
    # worktree .env symlink; remove only that exact framework-owned link.  Any
    # other .env entry is ambiguous user data and fails closed without deletion.
    root_env = (project_path / ".env").resolve()

    worktrees = {}
    errors = []

    def _secure_worktree_env(wt_path: Path) -> str | None:
        env_path = wt_path / ".env"
        if not env_path.exists() and not env_path.is_symlink():
            return None
        if not env_path.is_symlink():
            return "unexpected worktree .env file; remove it before dispatch"
        try:
            target = env_path.resolve(strict=False)
        except OSError as exc:
            return f"could not inspect worktree .env symlink: {exc}"
        if target != root_env:
            return "unexpected worktree .env symlink target; remove it before dispatch"
        try:
            env_path.unlink()
        except OSError as exc:
            return f"could not remove legacy worktree .env symlink: {exc}"
        return None

    for fid in feature_ids:
        wt_path = worktrees_dir / fid
        branch_name = f"feature/{fid}"

        # Skip if already exists
        if wt_path.exists() and (wt_path / ".git").exists():
            sha = rev_parse("HEAD", cwd=wt_path)
            if sha is None:
                # Fail closed: a broken worktree with an unresolvable HEAD
                # must not yield a partial descriptor. Recording the error
                # flips success False so the orchestrator skips populating
                # its worktree/subject maps.
                errors.append(f"{fid}: could not resolve worktree HEAD")
                continue
            env_error = _secure_worktree_env(wt_path)
            if env_error:
                errors.append(f"{fid}: {env_error}")
                continue
            worktrees[fid] = {
                "path": str(wt_path),
                "branch": branch_name,
                "created": False,
                "sha": sha,
            }
            continue

        try:
            if branch_exists(branch_name, cwd=project_path):
                add_args = ["worktree", "add", str(wt_path), branch_name]
            else:
                add_args = [
                    "worktree", "add", str(wt_path), "-b", branch_name, base_branch,
                ]
            run_git(
                add_args,
                cwd=project_path,
            )
            sha = rev_parse("HEAD", cwd=wt_path)
            if sha is None:
                errors.append(f"{fid}: could not resolve worktree HEAD")
                continue
            env_error = _secure_worktree_env(wt_path)
            if env_error:
                errors.append(f"{fid}: {env_error}")
                continue
            worktrees[fid] = {
                "path": str(wt_path),
                "branch": branch_name,
                "created": True,
                "sha": sha,
            }
        except GitError as e:
            errors.append(f"{fid}: {e}")

    # Ensure the project root's dependencies are ready before agents run, so
    # the test runner is found on the first try without agent retries. Python
    # worktrees additionally share the root's .venv via UV_PROJECT.
    #
    # This used to be a bare `uv sync`, which is Python-only AND requires a
    # pyproject.toml: on a requirements.txt-only project it failed with "No
    # pyproject.toml found" on every wave, and because the result was
    # discarded nobody ever saw it. The per-language commands come from the
    # adapter pack now, and a failure is reported rather than swallowed —
    # non-fatally, since a warm-up miss must not block dispatch (each feature
    # installs again in its own worktree via ensure_test_environment).
    warnings: list[str] = []
    if worktrees:
        try:
            from aah.core.build.lang_checks import detect

            for cmd in detect(project_path).install_deps():
                label = cmd.label or " ".join(cmd.argv)
                proc = subprocess.run(
                    cmd.argv,
                    cwd=str(cmd.cwd or project_path),
                    capture_output=True,
                    text=True,
                    timeout=cmd.timeout_sec,
                )
                if proc.returncode != 0:
                    warnings.append(
                        f"root dependency warm-up failed ({label}): "
                        f"{(proc.stderr or '').strip()[-300:]}"
                    )
                    break
        except Exception as exc:  # noqa: BLE001 — warm-up is best-effort
            warnings.append(f"root dependency warm-up skipped: {exc}")

    result = {"success": len(errors) == 0, "worktrees": worktrees, "errors": errors}
    if warnings:
        result["warnings"] = warnings
    return result


def restore_missing_feature_worktrees(
    project_path: Path,
    wave_num: int,
    feature_ids: list[str],
) -> dict:
    """Restore completed features whose branch survived worktree removal."""
    if not feature_ids:
        return {"success": True, "restored": [], "errors": []}

    worktrees_dir = project_path / ".claude" / "worktrees"
    manifest_path = project_path / ".aah" / "manifest.yaml"
    manifest = read_yaml(manifest_path) if manifest_path.exists() else {}
    integration_prefix = manifest.get("branching_config", {}).get(
        "integration_prefix", "integration/wave-"
    )
    integration_branch = f"{integration_prefix}{wave_num}"
    if not branch_exists(integration_branch, cwd=project_path):
        return {"success": True, "restored": [], "errors": []}

    restore_ids = []
    for feature_id in feature_ids:
        worktree = worktrees_dir / feature_id
        if worktree.exists():
            continue
        branch = f"feature/{feature_id}"
        if not branch_exists(branch, cwd=project_path):
            continue
        if not is_ancestor(branch, integration_branch, cwd=project_path):
            continue
        restore_ids.append(feature_id)

    if not restore_ids:
        return {"success": True, "restored": [], "errors": []}

    result = setup_wave_worktrees(project_path, wave_num, restore_ids)
    return {
        "success": result["success"],
        "restored": list(result.get("worktrees", {})),
        "errors": result.get("errors", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-create worktrees for wave features")
    sub = parser.add_subparsers(dest="command", required=True)

    setup_p = sub.add_parser("setup", help="Create worktrees for all features in a wave")
    setup_p.add_argument("--wave", type=int, required=True)
    setup_p.add_argument("--project-path", type=Path, default=None)
    setup_p.add_argument("--base-branch", type=str, default=None,
                         help="Branch to create feature worktrees off (default: integration/wave-N)")

    args = parser.parse_args()

    if args.command == "setup":
        from aah.core.common.config import require_project_path
        project_path = require_project_path(args.project_path)

        result = setup_wave_worktrees(project_path, args.wave, base_branch=args.base_branch)
        json.dump(result, sys.stdout, indent=2)
        print()

        created = sum(1 for v in result["worktrees"].values() if v.get("created"))
        total = len(result["worktrees"])
        print(f"Wave {args.wave}: {created} created, {total - created} skipped, {total} total", file=sys.stderr)
        for err in result.get("errors", []):
            print(f"  ERROR: {err}", file=sys.stderr)

        sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
