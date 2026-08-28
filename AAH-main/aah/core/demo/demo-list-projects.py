#!/usr/bin/env python3
"""
RAPIDS Project Listing Script.

Displays the RAPIDS ASCII banner and a compact project status table.
Called by the rapids SKILL.md during pre-onboarding to avoid
loading raw state.json files into the LLM context.

Usage:
  python3 scripts/demo-list-projects.py          # Human-readable table
  python3 scripts/demo-list-projects.py --json   # Machine-readable JSON
"""

import argparse
import json
import os
import subprocess
import sys
import glob
from pathlib import Path
import yaml

# Fix console encoding on platforms that need it
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')

# Banner (hardcoded to avoid reading branding.md)
BANNER = """
╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║   ██████╗  █████╗ ██████╗ ██╗██████╗ ███████╗                                 ║
║   ██╔══██╗██╔══██╗██╔══██╗██║██╔══██╗██╔════╝                                 ║
║   ██████╔╝███████║██████╔╝██║██║  ██║███████╗                                 ║
║   ██╔══██╗██╔══██║██╔═══╝ ██║██║  ██║╚════██║                                 ║
║   ██║  ██║██║  ██║██║     ██║██████╔╝███████║                                 ║
║   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═════╝ ╚══════╝                                 ║
║                                                                               ║
║   Robust AI Project Implementation & Deployment System  v1.0                  ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
""".strip()

PHASES = ["research", "analysis", "plan", "implement", "deploy", "sustain"]


def check_project_has_tags(project_path):
    """Return True if the git repo containing project_path has at least one tag."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list"],
            capture_output=True, text=True, timeout=10,
            cwd=project_path
        )
        return bool([t for t in result.stdout.strip().split("\n") if t.strip()])
    except Exception:
        return False


def find_project_root():
    """Find project root via $CLAUDE_PROJECT_DIR or walk up from cwd."""
    root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if root and os.path.isdir(root):
        return root
    # Fallback: walk up from cwd looking for rapids-config.yaml
    cwd = os.getcwd()
    while cwd != os.path.dirname(cwd):
        if os.path.isfile(os.path.join(cwd, "rapids-config.yaml")):
            return cwd
        cwd = os.path.dirname(cwd)
    return os.getcwd()


def load_rapids_config(root):
    """Load rapids-config.yaml to find workspace configuration."""
    config_path = os.path.join(root, "rapids-config.yaml")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def find_workspace_projects(root):
    """Find projects in workspace-based architecture."""
    projects = []
    config = load_rapids_config(root)

    if not config:
        return projects

    workspace_root = config.get("workspace_root", "../rapids-workspaces")
    if not os.path.isabs(workspace_root):
        workspace_root = os.path.join(root, workspace_root)
    workspace_root = os.path.abspath(workspace_root)

    if not os.path.exists(workspace_root):
        return projects

    # Scan all workspaces
    for workspace_name in os.listdir(workspace_root):
        workspace_path = os.path.join(workspace_root, workspace_name)
        if not os.path.isdir(workspace_path):
            continue

        # Scan all projects in workspace
        for project_name in os.listdir(workspace_path):
            project_path = os.path.join(workspace_path, project_name)
            rapids_dir = os.path.join(project_path, ".rapids")

            if not os.path.isdir(rapids_dir):
                continue

            # Check for manifest to confirm it's a RAPIDS project
            if os.path.isfile(os.path.join(rapids_dir, "manifest.yaml")):
                projects.append({
                    "path": project_path,
                    "name": project_name,
                    "workspace": workspace_name
                })

    return projects


def load_project_summary_from_progress(progress_path, manifest_path, project_name):
    """Extract compact summary from claude-progress.json and manifest.yaml."""
    try:
        # Load progress
        with open(progress_path, "r", encoding="utf-8") as f:
            progress = json.load(f)

        # Load manifest for depth
        depth = "?"
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}
                depth = manifest.get("depth", "?")

        phase = progress.get("current_phase", "unknown")

        # Count completed features
        completed = 0
        total = 0
        if "session_history" in progress:
            for session in progress["session_history"]:
                completed += len(session.get("features_completed", []))

        return {
            "name": project_name,
            "phase": phase,
            "depth": depth,
            "completed": completed,
            "total": total if total > 0 else "-",
        }
    except (json.JSONDecodeError, KeyError, TypeError, FileNotFoundError) as e:
        return {
            "name": project_name,
            "phase": "error",
            "depth": "?",
            "completed": 0,
            "total": 0,
            "error": str(e),
        }


def load_project_summary(state_path):
    """Extract compact summary from a state.json file."""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        name = state.get("project_name", os.path.basename(os.path.dirname(state_path)))
        phase = state.get("current_phase", "unknown")
        depth = state.get("depth_level", "?")

        # Count completed/total across all phases
        completed = 0
        total = 0
        phase_status = state.get("phase_status", {})
        for p in PHASES:
            ps = phase_status.get(p, {})
            total += ps.get("activities_total", 0)
            completed += ps.get("activities_completed", 0)

        return {
            "name": name,
            "phase": phase,
            "depth": depth,
            "completed": completed,
            "total": total,
        }
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return {
            "name": os.path.basename(os.path.dirname(state_path)),
            "phase": "error",
            "depth": "?",
            "completed": 0,
            "total": 0,
            "error": str(e),
        }


def find_projects_in_dir(projects_root):
    """Find projects in a projects/ directory (like framework root or harness projects/)."""
    projects = []
    if not os.path.exists(projects_root):
        return projects

    for project_name in os.listdir(projects_root):
        project_path = os.path.join(projects_root, project_name)
        rapids_dir = os.path.join(project_path, ".rapids")

        if not os.path.isdir(rapids_dir):
            continue

        # Check for manifest to confirm it's a RAPIDS project
        progress_path = os.path.join(rapids_dir, "claude-progress.json")
        manifest_path = os.path.join(rapids_dir, "manifest.yaml")

        if os.path.exists(manifest_path):
            projects.append({
                "path": project_path,
                "name": project_name,
                "location": "projects",
                "progress_path": progress_path if os.path.exists(progress_path) else None,
                "manifest_path": manifest_path
            })

    return projects


def main():
    parser = argparse.ArgumentParser(description="List RAPIDS projects")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args()

    root = find_project_root()

    projects = []

    # 1. Scan projects/ folder at harness root (reference/demo projects)
    projects_folder = os.path.join(root, "projects")
    projects_in_folder = find_projects_in_dir(projects_folder)

    for proj_info in projects_in_folder:
        if proj_info["progress_path"]:
            summary = load_project_summary_from_progress(
                proj_info["progress_path"],
                proj_info["manifest_path"],
                proj_info["name"]
            )
        else:
            # No progress file - brownfield or reference project
            summary = {
                "name": proj_info["name"],
                "phase": "reference",
                "depth": "?",
                "completed": 0,
                "total": "-"
            }
        summary["category"] = "RAPIDS"
        summary["workspace"] = "projects"
        summary["path"] = proj_info["path"]
        summary["has_tags"] = check_project_has_tags(proj_info["path"])
        projects.append(summary)

    # 2. Try workspace-based projects
    workspace_projects = find_workspace_projects(root)

    # Process workspace projects
    for proj_info in workspace_projects:
        project_path = proj_info["path"]
        rapids_dir = os.path.join(project_path, ".rapids")

        # Try claude-progress.json (harness style)
        progress_path = os.path.join(rapids_dir, "claude-progress.json")
        manifest_path = os.path.join(rapids_dir, "manifest.yaml")

        if os.path.exists(progress_path):
            summary = load_project_summary_from_progress(
                progress_path,
                manifest_path,
                proj_info["name"]
            )
            summary["category"] = "RAPIDS"
            summary["workspace"] = proj_info["workspace"]
            summary["path"] = project_path
            summary["has_tags"] = check_project_has_tags(project_path)
            projects.append(summary)

    # Fallback: Try legacy .rapids/projects structure
    projects_dir = os.path.join(root, ".rapids", "projects")
    if os.path.exists(projects_dir):
        pattern = os.path.join(projects_dir, "*")
        all_folders = sorted(glob.glob(pattern))

        for folder in all_folders:
            state_path = os.path.join(folder, "state.json")
            if os.path.isfile(state_path):
                summary = load_project_summary(state_path)
                summary["category"] = "RAPIDS"
                summary["workspace"] = "legacy"
                summary["path"] = folder
                summary["has_tags"] = check_project_has_tags(folder)
                projects.append(summary)
            else:
                # Brownfield project
                projects.append({
                    "name": os.path.basename(folder),
                    "category": "Brownfield",
                    "phase": "-",
                    "depth": "-",
                    "completed": 0,
                    "total": 0,
                    "workspace": "legacy",
                    "path": folder,
                    "has_tags": check_project_has_tags(folder)
                })

    if not projects:
        if args.json:
            print(json.dumps({"projects": []}, indent=2))
        else:
            print(BANNER)
            print()
            print("No projects found. Describe your problem to start a new project.")
        return

    # JSON output
    if args.json:
        output = {"projects": projects}
        print(json.dumps(output, indent=2))
        return

    # Human-readable output
    print(BANNER)
    print()
    print(f"Projects ({len(projects)} found)")
    print()
    print(f"  {'#':<3} {'Project':<30} {'Workspace':<15} {'Phase':<12} {'Depth':<8} {'Progress':<10}")
    print(f"  {'--':<3} {'─' * 30}  {'─' * 13}  {'─' * 10}  {'─' * 6}  {'─' * 8}")

    for i, p in enumerate(projects, 1):
        progress = f"{p['completed']}/{p['total']}" if isinstance(p['total'], int) and p['total'] > 0 else "-"
        depth_short = str(p['depth'])[:5] if len(str(p['depth'])) > 5 else str(p['depth'])
        workspace_short = p.get('workspace', '-')[:13]
        err = " (parse error)" if p.get("error") else ""
        print(f"  {i:<3} {p['name']:<30} {workspace_short:<15} {p['phase']:<12} {depth_short:<8} {progress:<10}{err}")

    print()
    print("  Options: [Continue #] [New project] [Status #]")


if __name__ == "__main__":
    main()
