#!/usr/bin/env python3
"""
Brownfield redeploy detection and change analysis.

Detects prior deployment outputs and identifies which services changed
since last deploy (via git diff against deploy tag).

Usage:
    aah run core.deploy.brownfield_redeploy detect --project-path <path>
    aah run core.deploy.brownfield_redeploy changed --project-path <path>
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml


def detect_prior_deployment(project_path: Path) -> dict | None:
    """
    Check if a prior serverless deployment exists.

    Reads .aah/deploy/serverless-outputs.yaml (or cloudrun-outputs.yaml
    or ecs-express-outputs.yaml for backward compat).

    Returns deployment outputs dict or None if no prior deployment.
    """
    aah_dir = project_path / ".aah" / "deploy"

    # Check for outputs files (in priority order)
    output_files = [
        aah_dir / "serverless-outputs.yaml",
        aah_dir / "cloudrun-outputs.yaml",
        aah_dir / "ecs-express-outputs.yaml",
    ]

    for output_file in output_files:
        if output_file.exists():
            try:
                data = read_yaml(output_file)
                data["_source_file"] = str(output_file.name)
                return data
            except Exception:
                continue

    return None


def detect_changed_services(project_path: Path, prior_outputs: dict) -> dict:
    """
    Git diff since last deploy tag to identify changed services.

    Uses the deploy git tag (set at end of deploy phase) to determine
    what changed since last successful deployment.

    Returns:
        {
            "changed_services": [str],     # services with code changes
            "unchanged_services": [str],   # services with no changes
            "new_services": [str],         # services that didn't exist last deploy
            "removed_services": [str],     # services from last deploy that no longer exist
            "has_changes": bool,
            "deploy_tag": str | None,
            "files_changed": int,
        }
    """
    from aah.core.deploy.service_discovery import discover_services

    # Find last deploy tag
    deploy_tag = _find_deploy_tag(project_path)

    # Get current services
    current_services = discover_services(project_path)
    current_names = {s["name"] for s in current_services}
    current_paths = {s["name"]: s["path"] for s in current_services}

    # Get previously deployed services
    prior_services = prior_outputs.get("services", {})
    if isinstance(prior_services, list):
        prior_names = {s.get("name", "") for s in prior_services}
    elif isinstance(prior_services, dict):
        prior_names = set(prior_services.keys())
    else:
        prior_names = set()

    # If no deploy tag, treat everything as changed
    if not deploy_tag:
        return {
            "changed_services": sorted(current_names),
            "unchanged_services": [],
            "new_services": [],
            "removed_services": [],
            "has_changes": True,
            "deploy_tag": None,
            "files_changed": -1,  # Unknown
        }

    # Get changed files since tag
    changed_files = _git_diff_files(project_path, deploy_tag)

    # Determine which services were affected
    changed_services = set()
    for svc_name, svc_path in current_paths.items():
        # A service is "changed" if any file in its path was modified
        svc_path_prefix = svc_path if svc_path != "." else ""
        for changed_file in changed_files:
            if svc_path_prefix == "":
                # Main backend (root): changes in src/ count
                if changed_file.startswith("src/") or changed_file in ("pyproject.toml", "requirements.txt"):
                    changed_services.add(svc_name)
                    break
            else:
                # Named service: changes in its folder count
                if changed_file.startswith(f"{svc_path_prefix}/"):
                    changed_services.add(svc_name)
                    break

    unchanged_services = current_names - changed_services
    new_services = current_names - prior_names
    removed_services = prior_names - current_names

    # New services are always "changed" (need deployment)
    changed_services.update(new_services)
    unchanged_services -= new_services

    return {
        "changed_services": sorted(changed_services),
        "unchanged_services": sorted(unchanged_services),
        "new_services": sorted(new_services),
        "removed_services": sorted(removed_services),
        "has_changes": len(changed_services) > 0,
        "deploy_tag": deploy_tag,
        "files_changed": len(changed_files),
    }


def _find_deploy_tag(project_path: Path) -> str | None:
    """Find the most recent aah-deploy git tag."""
    try:
        r = subprocess.run(
            ["git", "tag", "--list", "*aah-deploy*", "--sort=-creatordate"],
            capture_output=True, text=True, timeout=10,
            cwd=str(project_path),
        )
        if r.returncode == 0 and r.stdout.strip():
            # Return most recent tag
            return r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


def _git_diff_files(project_path: Path, since_tag: str) -> list[str]:
    """Get list of files changed since a git tag."""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", since_tag, "HEAD"],
            capture_output=True, text=True, timeout=15,
            cwd=str(project_path),
        )
        if r.returncode == 0:
            return [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]
    except Exception:
        pass
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Brownfield redeploy detection")
    sub = parser.add_subparsers(dest="command", required=True)

    det_p = sub.add_parser("detect", help="Detect prior deployment")
    det_p.add_argument("--project-path", type=Path, required=True)

    chg_p = sub.add_parser("changed", help="Detect changed services since last deploy")
    chg_p.add_argument("--project-path", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "detect":
        prior = detect_prior_deployment(args.project_path)
        if prior:
            json.dump({"has_prior_deployment": True, "outputs": prior}, sys.stdout, indent=2)
        else:
            json.dump({"has_prior_deployment": False, "outputs": None}, sys.stdout, indent=2)
        print()

    elif args.command == "changed":
        prior = detect_prior_deployment(args.project_path)
        if not prior:
            json.dump({
                "error": "No prior deployment found",
                "has_changes": True,
                "changed_services": [],
            }, sys.stdout, indent=2)
            print()
            sys.exit(0)

        result = detect_changed_services(args.project_path, prior)
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
