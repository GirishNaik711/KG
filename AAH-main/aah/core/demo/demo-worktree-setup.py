#!/usr/bin/env python3
"""
RAPIDS Demo Worktree Setup.

Creates a git worktree from a git tag for a specific phase. The tagged commit
already has the correct state — no seeding needed. Faster and more accurate.

Usage:
    python3 demo-worktree-setup.py <project-path> <phase> --tag <tag-name> [<worktree-base-dir>]

    <phase> can be: onboarding, research, analysis, plan, implement, deploy, sustain

Examples:
    python3 demo-worktree-setup.py projects/my-project research --tag rapids/my-project/onboarding
    python3 demo-worktree-setup.py projects/my-project analysis --tag rapids/my-project/research/complete
"""

import json
import os
import shutil
import subprocess
import sys

import yaml

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PHASE_ORDER = ["research", "analysis", "plan", "implement", "deploy", "sustain"]
ALL_PHASES = ["onboarding"] + PHASE_ORDER

# Files and directories from the harness repo that must be available in
# every demo worktree so that Claude Code can discover skills, settings,
# and helper scripts.
HARNESS_LINKS = {
    "dirs":  [".claude", "scripts"],
    "files": ["CLAUDE.md", "rapids-config.yaml"],
}

# Exclude .venv and bytecode when copying harness dirs on Windows.
# .venv contains deeply nested package paths (e.g. networkx test files) that
# exceed Windows' 260-char limit; the demo invokes scripts via
# `uvx --from aah python`, which uses the installed aah tool env, so a local
# .venv copy is not needed.
_COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", "*.pyo", "worktrees")


def _safe_rmtree(path):
    """Remove a directory tree on any platform.

    Handles two Windows-specific failure modes:
    1. Read-only files (WinError 5) — chmod before retry.
    2. Directory in use by another process (WinError 32) — fall back to
       `cmd /c rmdir /s /q` which can remove directories that Python's
       shutil cannot (e.g. stale terminal CWDs).
    """
    if not os.path.exists(path):
        return

    def _on_error(func, error_path, exc_info):
        import stat as _stat
        try:
            os.chmod(error_path, _stat.S_IWRITE)
            func(error_path)
        except OSError:
            pass  # will be caught by the outer try/except

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_on_error)
        else:
            shutil.rmtree(path, onerror=_on_error)
    except OSError:
        # Last resort: delegate to shell for locked/nested dirs (Windows cmd, Unix rm -rf)
        if sys.platform == "win32":
            result = subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", os.path.normpath(path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0 and os.path.exists(path):
                raise RuntimeError(
                    f"Could not remove '{path}'. Close any terminals or editors "
                    f"that have this directory open, then retry.\n{result.stderr}"
                )
        else:
            result = subprocess.run(
                ["rm", "-rf", path],
                capture_output=True, text=True,
            )
            if result.returncode != 0 and os.path.exists(path):
                raise RuntimeError(
                    f"Could not remove '{path}'.\n{result.stderr}"
                )


def load_json(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_json(path, data):
    """Save data as JSON to a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_text(path):
    """Load a text file, returning empty string on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def save_text(path, content):
    """Save text content to a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _find_harness_root():
    """Locate the RAPIDS harness repository root.

    Resolution order:
    1. $CLAUDE_PROJECT_DIR (set by Claude Code)
    2. Walk up from this script's directory looking for .claude/skills/
    """
    # Try CLAUDE_PROJECT_DIR first
    env_root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env_root and os.path.isdir(os.path.join(env_root, ".claude", "skills")):
        return env_root

    # Walk up from the script location
    candidate = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if os.path.isdir(os.path.join(candidate, ".claude", "skills")):
            return candidate
        candidate = os.path.dirname(candidate)

    return None


def _reset_phase_state(worktree_path, phase):
    """Reset the current phase to not-started in a demo worktree.

    The tagged commit has the phase already completed. This function rolls
    back state so the phase can be executed from scratch during the demo,
    while keeping all prior phase artifacts intact.

    Resets:
    - .rapids/{phase}/ artifact directories (current + later phases)
    - completed_artifacts in manifest.yaml (current + later phases)
    - current_phase in manifest.yaml (set to target phase)
    - Activity statuses in activity-plan.yaml (current + later phases → pending)
    - claude-progress.json (reset to target phase, clear feature progress)

    Commits the reset to the demo branch so the worktree starts clean.
    """
    rapids_dir = os.path.join(worktree_path, ".rapids")
    if not os.path.isdir(rapids_dir):
        return

    # Determine which phases to reset: current + all later ones
    if phase in PHASE_ORDER:
        phase_idx = PHASE_ORDER.index(phase)
        phases_to_reset = set(PHASE_ORDER[phase_idx:])
    else:
        return  # onboarding handled separately

    # 1. Remove artifact directories for phases being reset
    for p in phases_to_reset:
        phase_dir = os.path.join(rapids_dir, p)
        if os.path.isdir(phase_dir):
            shutil.rmtree(phase_dir)

    # 2. Update manifest.yaml
    manifest_path = os.path.join(rapids_dir, "manifest.yaml")
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}

        # Remove completed_artifacts for phases being reset
        if "completed_artifacts" in manifest:
            manifest["completed_artifacts"] = [
                a for a in manifest["completed_artifacts"]
                if not any(a.startswith(f"{p}/") for p in phases_to_reset)
            ]

        # Set current_phase to the target demo phase
        manifest["current_phase"] = phase

        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

    # 3. Update activity-plan.yaml
    activity_plan_path = os.path.join(rapids_dir, "activity-plan.yaml")
    if os.path.isfile(activity_plan_path):
        with open(activity_plan_path, "r", encoding="utf-8") as f:
            plan = yaml.safe_load(f) or {}

        activities = plan.get("activities", [])
        if isinstance(activities, list):
            for act_data in activities:
                if isinstance(act_data, dict) and act_data.get("phase") in phases_to_reset:
                    act_data["status"] = "pending"
        elif isinstance(activities, dict):
            for act_id, act_data in activities.items():
                if isinstance(act_data, dict) and act_data.get("phase") in phases_to_reset:
                    act_data["status"] = "pending"

        with open(activity_plan_path, "w", encoding="utf-8") as f:
            yaml.dump(plan, f, default_flow_style=False, sort_keys=False)

    # 4. Reset feature-list.json (drives the orchestrator's wave decisions)
    feature_list_path = os.path.join(rapids_dir, "feature-list.json")
    if os.path.isfile(feature_list_path):
        fl = load_json(feature_list_path) or {}
        for feat in fl.get("features", []):
            if isinstance(feat, dict):
                feat["status"] = "pending"
                feat["passes"] = False
        save_json(feature_list_path, fl)

    # 5. Reset claude-progress.json
    progress_path = os.path.join(rapids_dir, "claude-progress.json")
    if os.path.isfile(progress_path):
        progress = load_json(progress_path) or {}
        progress["current_phase"] = phase
        progress["current_feature"] = None
        progress["current_wave"] = None
        progress["features_completed"] = []
        progress["features_total"] = 0
        progress["last_action"] = f"Demo reset — {phase} phase ready to execute"
        progress["next_steps"] = f"Execute {phase} phase from scratch"
        progress["blockers"] = []
        progress["session_history"] = []
        save_json(progress_path, progress)

    # 6. Commit the state reset so git status is clean
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, text=True, cwd=worktree_path
    )
    subprocess.run(
        ["git", "commit", "-m", f"Reset {phase} phase state for demo"],
        capture_output=True, text=True, cwd=worktree_path
    )

    print(f"  Reset {phase} state for demo (prior phases intact)")


def _link_harness_into_worktree(worktree_path, copy_claude_dir=False):
    """Symlink harness .claude/, scripts/, CLAUDE.md, and rapids-config.yaml
    into a demo worktree so that Claude Code can discover skills and settings.

    Args:
        copy_claude_dir: If True, copy .claude/ instead of symlinking.
            Used for onboarding (plain directory) where a .claude/ symlink
            causes Claude Code to follow it to the harness root and reset
            cwd there, breaking config resolution.

    Existing CLAUDE.md written by _write_worktree_claude_md is preserved —
    harness CLAUDE.md content is appended to it.
    """
    harness_root = _find_harness_root()
    if not harness_root:
        print("Warning: Could not locate harness root — skills may not be available in worktree")
        return

    for dirname in HARNESS_LINKS["dirs"]:
        src = os.path.join(harness_root, dirname)
        dst = os.path.join(worktree_path, dirname)
        if not os.path.isdir(src) or os.path.exists(dst):
            continue
        if dirname == ".claude" and copy_claude_dir:
            shutil.copytree(src, dst, ignore=_COPY_IGNORE)
        elif sys.platform == "win32":
            shutil.copytree(src, dst, ignore=_COPY_IGNORE)
        else:
            os.symlink(src, dst)

    for fname in HARNESS_LINKS["files"]:
        src = os.path.join(harness_root, fname)
        dst = os.path.join(worktree_path, fname)
        if not os.path.isfile(src):
            continue
        if fname == "CLAUDE.md" and os.path.isfile(dst):
            # Append harness CLAUDE.md to the worktree-specific one
            with open(src, "r", encoding="utf-8") as f:
                harness_content = f.read()
            with open(dst, "a", encoding="utf-8") as f:
                f.write("\n\n" + harness_content)
        elif not os.path.exists(dst):
            shutil.copy2(src, dst)


def find_git_root(cwd=None):
    """Find the git repository root directory.

    Args:
        cwd: Directory to run the git command from. If provided, finds
             the git root for the repo containing that directory.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
            cwd=cwd
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        print(f"Error: Not inside a git repository (cwd={cwd}).")
        sys.exit(1)


def create_worktree_from_tag(git_root, worktree_path, tag_name):
    """Create a git worktree from a specific tag in detached HEAD mode.

    Uses --detach so NO named branch is created. This prevents demo
    worktrees from polluting the git branch list (main, develop, etc.).
    Commits in the worktree go to a detached HEAD and become unreachable
    garbage once the worktree is removed.
    """
    # Clean up if worktree already exists at this path
    if os.path.exists(worktree_path):
        subprocess.run(
            ["git", "worktree", "remove", worktree_path, "--force"],
            capture_output=True, text=True, cwd=git_root
        )
    if os.path.exists(worktree_path):
        _safe_rmtree(worktree_path)

    # Prune stale worktree entries
    subprocess.run(
        ["git", "worktree", "prune"],
        capture_output=True, text=True, cwd=git_root
    )

    # Create worktree in detached HEAD mode — no branch created
    subprocess.run(
        ["git", "worktree", "add", "--detach", worktree_path, tag_name],
        capture_output=True, text=True, check=True, cwd=git_root
    )


def setup_onboarding_directory(project_path, worktree_base_dir):
    """Create a plain empty directory for onboarding — treated as a blank workspace.

    Onboarding demos the full workspace initialization flow:
    /rapids-init-workspace → /rapids-new-project → /rapids-research.
    A plain directory with only harness symlinks and no .rapids/ state
    ensures the onboarding sequence starts from a clean slate.
    """
    project_name = os.path.basename(os.path.abspath(project_path))
    onboarding_path = os.path.join(worktree_base_dir, "onboarding")

    # Clean up if directory exists from a previous run (best-effort — may fail on OneDrive).
    if os.path.exists(onboarding_path):
        _safe_rmtree(onboarding_path)
    os.makedirs(onboarding_path, exist_ok=True)

    # Write CLAUDE.md, link harness files (copy .claude/ for onboarding)
    _write_worktree_claude_md(onboarding_path, project_name, "onboarding")
    _link_harness_into_worktree(onboarding_path, copy_claude_dir=True)
    # Write demo config AFTER linking so it always wins over any stale harness copy
    _write_demo_rapids_config(onboarding_path, phase="onboarding")

    print(f"Onboarding directory ready (blank slate)")
    print(onboarding_path)
    return onboarding_path


def setup_worktree_from_tag(project_path, phase, tag_name, worktree_base_dir):
    """Create a worktree from a tag with phase state reset for demo.

    The tagged commit has the phase already completed. After checkout,
    the current phase state is reset to not-started while prior phase
    artifacts are kept intact, so the phase can be demoed from scratch.
    """
    project_path = os.path.abspath(project_path)
    project_name = os.path.basename(project_path)
    git_root = find_git_root(cwd=project_path)

    worktree_path = os.path.join(worktree_base_dir, phase)

    print(f"Creating worktree from tag: {tag_name}")
    print(f"Worktree: {worktree_path} (detached HEAD)")

    # Step 1: Create worktree from tag (detached HEAD — no branch created)
    create_worktree_from_tag(git_root, worktree_path, tag_name)

    # Step 2: Reset phase state so it can be executed from scratch
    if phase in PHASE_ORDER:
        _reset_phase_state(worktree_path, phase)

    # Step 3: Write CLAUDE.md boundary constraint
    _write_worktree_claude_md(worktree_path, project_name, phase)

    # Step 4: Link harness .claude/, scripts/, config into the worktree
    _link_harness_into_worktree(worktree_path)

    # Step 5: Write demo-specific rapids-config.yaml
    _write_demo_rapids_config(worktree_path)

    # Step 6: Commit all modifications so git status is clean
    # Without this, Claude sees unstaged changes and may run 'git restore'
    # which would undo the state reset from the tagged commit.
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, text=True, cwd=worktree_path
    )
    subprocess.run(
        ["git", "commit", "-m", f"Demo worktree setup: {phase} phase"],
        capture_output=True, text=True, cwd=worktree_path
    )

    print(f"{phase.title()} worktree ready (from tag {tag_name})")
    print(worktree_path)
    return worktree_path


def _write_demo_rapids_config(worktree_path, phase=None):
    """Write a rapids-config.yaml that points directly at the worktree.

    For onboarding: the directory IS the workspace root with no active project.
    For other phases: resolve_active_project_path() computes
    workspace_root / active_workspace / active_project → this worktree.
    """
    abs_path = os.path.abspath(worktree_path)

    if phase == "onboarding":
        # Onboarding is a blank workspace — no active project yet
        config = {
            "framework_root": None,
            "workspace_root": abs_path,
            "active_workspace": None,
            "active_project": None,
            "settings": {
                "default_complexity_tier": "moderate",
            },
        }
        config_path = os.path.join(worktree_path, "rapids-config.yaml")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f"  Wrote demo rapids-config.yaml → {abs_path}")
        return

    # Other phases: point config at the worktree as the active project
    # Split: .../worktrees/research → workspace_root=..., workspace=worktrees, project=research
    project_name = os.path.basename(abs_path)
    workspace_name = os.path.basename(os.path.dirname(abs_path))
    workspace_root = os.path.dirname(os.path.dirname(abs_path))

    config = {
        "framework_root": None,
        "workspace_root": workspace_root,
        "active_workspace": workspace_name,
        "active_project": project_name,
        "settings": {
            "default_complexity_tier": "moderate",
        },
    }
    config_path = os.path.join(worktree_path, "rapids-config.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"  Wrote demo rapids-config.yaml → {abs_path}")


def _write_worktree_claude_md(worktree_path, project_name, phase):
    """Write a CLAUDE.md that scopes Claude to this worktree directory."""
    phase_label = "Onboarding" if phase == "onboarding" else phase.title()

    if phase == "onboarding":
        content = f"""# RAPIDS Demo — Onboarding (Blank Workspace)

This is a blank workspace directory for the RAPIDS onboarding demo.

## Workspace Setup Flow

This directory is an empty workspace. Follow these steps to demo the full
onboarding flow:

1. **Initialize workspace**: `/rapids-init-workspace demo-workspace`
2. **Create project**: `/rapids-new-project demo-app --stack python-fastapi`
3. **Run intake**: `/rapids-research`

## Directory Boundary

**This directory (`{worktree_path}`) is the workspace root.**

- There is NO existing project or `.rapids/` state here.
- Do NOT search for or access files outside this directory.
- Do NOT read state from parent directories.
"""
    else:
        content = f"""# RAPIDS Demo Worktree — {phase_label}

This is an isolated RAPIDS demo worktree for the **{phase_label}** phase.

## CRITICAL: Project Root Boundary

**This directory (`{worktree_path}`) is the project root.**

- ALL project state lives in `.rapids/` within THIS directory.
- The primary state file is `.rapids/manifest.yaml`.
- Phase artifacts are in `.rapids/research/`, `.rapids/analysis/`, `.rapids/plan/`, etc.
- **NEVER** read state files from parent directories or any path above this directory.
- **NEVER** search for or access files outside `{worktree_path}`.
- If a file is not found within this directory, treat it as non-existent.

## CRITICAL: Git Isolation — Detached HEAD Demo Worktree

**This worktree runs in detached HEAD mode. It shares the git database
with the main repository. ALL git operations MUST be fully isolated.**

- **ONLY** commit directly in this worktree (detached HEAD commits are OK)
- **NEVER** create ANY named branches (no `demo/`, `integration/`, `feature/` branches)
- **NEVER** checkout, merge to, or modify `main`, `develop`, or any
  `integration/wave-*` branch
- **NEVER** run `git_ops.merge_wave_to_integration`,
  `git_ops.promote_to_develop`, or `git_ops.validate_main_merge`
- **NEVER** push any branch or commit to a remote
- **SKIP** all branching strategy steps (worktree-per-feature, integration
  branches, wave merges, promotion to develop)
- For the implement phase: implement ALL features directly here —
  commit each feature directly, do NOT create feature branches or integration
  branches
- For all phases: commit artifacts directly — no branch ceremony needed
- If a RAPIDS script attempts a branch operation, skip that script and
  commit directly instead
"""
    save_text(os.path.join(worktree_path, "CLAUDE.md"), content)


def main():
    if len(sys.argv) < 3:
        print("Usage: demo-worktree-setup.py <project-path> <phase> --tag <tag-name> [<worktree-base-dir>]")
        print(f"Phases: {', '.join(ALL_PHASES)}")
        print("  --tag   Use a git tag as the worktree source (required)")
        sys.exit(1)

    # Parse arguments
    positional = []
    tag_name = None
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--tag" and i + 1 < len(sys.argv):
            tag_name = sys.argv[i + 1]
            i += 2
        else:
            positional.append(sys.argv[i])
            i += 1

    if len(positional) < 2:
        print("Usage: demo-worktree-setup.py <project-path> <phase> --tag <tag-name> [<worktree-base-dir>]")
        sys.exit(1)

    project_path = positional[0]
    phase = positional[1].lower()

    if phase not in ALL_PHASES:
        print(f"Error: Unknown phase '{phase}'.")
        print(f"Valid phases: {', '.join(ALL_PHASES)}")
        sys.exit(1)

    if not os.path.isdir(project_path):
        print(f"Error: Project path not found: {project_path}")
        sys.exit(1)

    # Worktree base directory: default to .claude/worktrees/ under the project
    if len(positional) >= 3:
        worktree_base_dir = os.path.abspath(positional[2])
    else:
        worktree_base_dir = os.path.join(os.path.abspath(project_path), ".claude", "worktrees")

    os.makedirs(worktree_base_dir, exist_ok=True)

    if phase == "onboarding":
        # Onboarding: plain empty directory, no git, no state
        setup_onboarding_directory(project_path, worktree_base_dir)
    else:
        # Other phases: git worktree from tag with state reset
        if not tag_name:
            print("Error: --tag is required for non-onboarding phases.")
            sys.exit(1)
        setup_worktree_from_tag(project_path, phase, tag_name, worktree_base_dir)


if __name__ == "__main__":
    main()
