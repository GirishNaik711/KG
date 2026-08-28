#!/usr/bin/env python3
"""
RAPIDS Project Listing Script.

Discovers all RAPIDS projects across harness projects/ directory and
workspace-based projects, then outputs a compact status table.

Usage:
  python3 aah/tools/list-projects.py          # Human-readable table
  python3 aah/tools/list-projects.py --json   # Machine-readable JSON
"""

import argparse
import json
import os
import sys
from pathlib import Path
import yaml

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')


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
        with open(config_path, "r") as f:
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
        with open(progress_path, "r") as f:
            progress = json.load(f)

        # Load manifest for depth
        depth = "?"
        if os.path.exists(manifest_path):
            with open(manifest_path, "r") as f:
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
            projects.append(summary)

    if not projects:
        if args.json:
            print(json.dumps({"projects": []}, indent=2))
        else:
            print("No projects found. Run /rapids-new-project to create one.")
        return

    # JSON output
    if args.json:
        output = {"projects": projects}
        print(json.dumps(output, indent=2))
        return

    # Human-readable output
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


if __name__ == "__main__":
    main()
