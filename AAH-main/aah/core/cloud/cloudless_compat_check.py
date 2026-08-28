#!/usr/bin/env python3
"""
Validate cloudless compatibility with the selected framework.

Called during analysis phase to ensure DDR-SHARED-017=cloudless-managed
is only selected when DDR-L2-001=langgraph.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml


def check_cloudless_compatibility(project_path: Path) -> dict:
    """
    Validate that cloudless deployment is compatible with selected framework.

    Returns:
        {
            "compatible": bool,
            "framework": str,
            "compute_model": str,
            "issues": [str],
            "warnings": [str],
        }
    """
    registry_path = project_path / ".aah" / "discuss" / "decision-registry.yaml"
    issues = []
    warnings = []

    if not registry_path.exists():
        return {
            "compatible": True,
            "framework": None,
            "compute_model": None,
            "issues": [],
            "warnings": ["No decision registry found - cannot validate compatibility"],
        }

    registry = read_yaml(registry_path)
    decisions = registry.get("decisions", [])

    framework = None
    compute_model = None

    for d in decisions:
        if d.get("ddr_id") == "DDR-L2-001":
            framework = (d.get("resolved_option", d.get("resolved")) or "").lower()
        elif d.get("ddr_id") == "DDR-SHARED-017":
            compute_model = (d.get("resolved_option", d.get("resolved")) or "").lower()

    if compute_model == "cloudless-managed" and framework and "langgraph" not in framework:
        issues.append(
            f"DDR-SHARED-017=cloudless-managed requires DDR-L2-001=langgraph, "
            f"but framework is '{framework}'. Cloudless only supports LangGraph."
        )

    if compute_model == "cloudless-managed" and not framework:
        warnings.append(
            "DDR-SHARED-017=cloudless-managed selected but DDR-L2-001 not yet resolved. "
            "Framework must be langgraph for cloudless to work."
        )

    # Check cloud target is set
    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists() and compute_model == "cloudless-managed":
        manifest = read_yaml(manifest_path)
        cloud = (manifest.get("stack_choices", {}).get("cloud") or "").lower()
        if cloud not in ("aws", "gcp"):
            warnings.append(
                "cloudless-managed selected but target cloud not set in manifest. "
                "Set stack_choices.cloud to 'aws' or 'gcp'."
            )

    return {
        "compatible": len(issues) == 0,
        "framework": framework,
        "compute_model": compute_model,
        "issues": issues,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check cloudless compatibility")
    parser.add_argument("--project-path", type=Path, required=True)
    args = parser.parse_args()

    result = check_cloudless_compatibility(args.project_path)
    json.dump(result, sys.stdout, indent=2)
    print()
    sys.exit(0 if result["compatible"] else 1)


if __name__ == "__main__":
    main()
