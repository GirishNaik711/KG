#!/usr/bin/env python3
"""
RAPIDS Demo — GitHub Repo Cloner.

Clones a GitHub RAPIDS project into the demo-imports workspace folder,
validates it is a RAPIDS project with tags, and returns JSON.

Usage:
  python demo-clone-repo.py <github_url>
    [--workspace-root <path>]
    [--workspace <name>]     # default: demo-imports

Output JSON:
  success=true:  { "success": true, "path": "...", "name": "...", "workspace": "...", "tag_count": N }
  success=false: { "success": false, "error": "..." }
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from aah.core.common.git_utils import run_git

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _out(data):
    print(json.dumps(data, indent=2))


def _error(msg):
    _out({"success": False, "error": msg})
    sys.exit(0)


def find_workspace_root():
    """Find workspace_root from rapids-config.yaml, walking up from cwd."""
    cwd = os.getcwd()
    while cwd != os.path.dirname(cwd):
        config_path = os.path.join(cwd, "rapids-config.yaml")
        if os.path.isfile(config_path):
            try:
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
                workspace_root = config.get("workspace_root", "")
                if workspace_root:
                    if not os.path.isabs(workspace_root):
                        workspace_root = os.path.join(cwd, workspace_root)
                    return os.path.abspath(workspace_root)
            except Exception:
                pass
        cwd = os.path.dirname(cwd)
    return None


def count_tags(dest):
    """Return number of git tags in the cloned repo."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list"],
            capture_output=True, text=True, timeout=10,
            cwd=dest
        )
        return len([t for t in result.stdout.strip().split("\n") if t.strip()])
    except Exception:
        return 0


def main():
    parser = argparse.ArgumentParser(description="Clone a RAPIDS GitHub project for demo")
    parser.add_argument("github_url", help="GitHub repository URL (https:// or git@)")
    parser.add_argument("--workspace-root", default="", help="Path to workspace root directory")
    parser.add_argument("--workspace", default="demo-imports", help="Workspace subfolder name (default: demo-imports)")
    parser.add_argument("--branch", default="", help="Branch to clone (default: repo default branch)")
    parser.add_argument("--list-branches", action="store_true", help="List remote branches without cloning")
    args = parser.parse_args()

    url = args.github_url.strip()

    # Security: reject anything that isn't a plain https:// or git@ URL
    if not (url.startswith("https://") or url.startswith("git@")):
        _error("Invalid URL — must start with https:// or git@")

    # Reject shell metacharacters to prevent injection
    for ch in [";", "&", "|", "`", "$", "(", ")", "<", ">", "\n", "\r"]:
        if ch in url:
            _error(f"Invalid URL — contains disallowed character: {ch!r}")

    # --list-branches mode: query remote without cloning
    if args.list_branches:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            _error(f"Could not list branches: {result.stderr.strip()}")
        branches = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                ref = parts[1].strip()
                if ref.startswith("refs/heads/"):
                    branches.append(ref[len("refs/heads/"):])
        _out({"success": True, "branches": branches})
        return

    # Extract project name from URL
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not name:
        _error("Could not determine project name from URL")

    # Resolve workspace root
    workspace_root = args.workspace_root
    if not workspace_root:
        workspace_root = find_workspace_root()
    if not workspace_root:
        _error("Could not determine workspace_root — pass --workspace-root or run from a RAPIDS harness directory")

    dest = os.path.join(workspace_root, args.workspace, name)
    dest = os.path.abspath(dest)

    # Idempotent: already cloned and is a RAPIDS project
    if os.path.isdir(dest):
        if os.path.isfile(os.path.join(dest, ".rapids", "manifest.yaml")):
            tag_count = count_tags(dest)
            _out({
                "success": True,
                "path": dest,
                "name": name,
                "workspace": args.workspace,
                "tag_count": tag_count,
                "demo_ready": tag_count > 0,
            })
            return
        else:
            _error(f"Destination already exists but is not a RAPIDS project: {dest}")

    # Create parent workspace directory if needed
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # Clone
    clone_args = ["clone", "--", url, dest]
    if args.branch:
        clone_args = ["clone", "--branch", args.branch, "--", url, dest]
    result = run_git(clone_args, check=False, timeout=120)
    if result.returncode != 0:
        _error(f"git clone failed: {result.stderr.strip()}")

    # Validate RAPIDS structure
    if not os.path.isfile(os.path.join(dest, ".rapids", "manifest.yaml")):
        shutil.rmtree(dest, ignore_errors=True)
        _error("Cloned successfully but this does not appear to be a RAPIDS project (missing .rapids/manifest.yaml)")

    # Count tags and report readiness (no error — caller decides what to do)
    tag_count = count_tags(dest)
    _out({
        "success": True,
        "path": dest,
        "name": name,
        "workspace": args.workspace,
        "tag_count": tag_count,
        "demo_ready": tag_count > 0,
    })


if __name__ == "__main__":
    main()
