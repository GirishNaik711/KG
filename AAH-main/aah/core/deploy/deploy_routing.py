#!/usr/bin/env python3
"""
Detect if a AAH project uses LangGraph framework and cloudless deployment.

Used by the deploy phase to determine whether to route to
cloudless-deploy-engineer or standard deploy-engineer.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml


def is_langgraph_project(project_path: Path) -> bool:
    """
    Detect if project uses LangGraph framework.

    Checks (in order):
    1. decision-registry.yaml: DDR-L2-001 resolved to "langgraph"
    2. manifest.yaml: stack_choices.ai contains "langgraph"
    3. Fallback: scan src/ for langgraph imports
    """
    # Check 1: decision registry
    registry_path = project_path / ".aah" / "decision-registry.yaml"
    if registry_path.exists():
        registry = read_yaml(registry_path)
        for decision in registry.get("decisions", []):
            if decision.get("ddr_id") == "DDR-L2-001":
                resolved = (decision.get("resolved_option", decision.get("resolved")) or "").lower()
                if "langgraph" in resolved:
                    return True
                if resolved and "langgraph" not in resolved:
                    return False

    # Check 2: manifest stack_choices
    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        ai_stack = (manifest.get("stack_choices", {}).get("ai") or "").lower()
        if "langgraph" in ai_stack:
            return True

    # Check 3: fallback — scan source for imports
    src_dir = project_path / "src"
    if src_dir.exists():
        for py_file in src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if "from langgraph" in content or "import langgraph" in content:
                    return True
            except (OSError, PermissionError):
                continue

    return False


def get_backend_platform(project_path: Path) -> str | None:
    """
    The single source of truth for the deploy route: the aah-discuss slug
    `backend-compute-platform` (defined in decisions_guidance.yaml, answered
    during /aah-discuss, stored in .aah/discuss/decision-registry.yaml).

    Options: agentcore-runtime | ecs-express-mode | cloud-run | cloudless-managed
             | self-managed.

    Falls back to the legacy DDR-SHARED-017 (`ddr_id`) only for pre-AAH projects
    that still carry a DDR-keyed registry. AAH projects are slug-keyed.
    """
    slug = get_discuss_slug(project_path, "backend-compute-platform")
    if slug:
        return slug
    # Legacy fallback (pre-AAH DDR registry).
    return get_ddr_resolution(project_path, "DDR-SHARED-017")


def is_cloudless_deployment(project_path: Path) -> bool:
    """Check if the backend platform resolved to cloudless-managed."""
    return get_backend_platform(project_path) == "cloudless-managed"


def get_ddr_resolution(project_path: Path, ddr_id: str) -> str | None:
    """
    Get the resolved option for a DDR from the decision registry.

    Checks both 'resolved_option' and 'resolved' fields (backward compat).
    Returns the resolved value or None if not found/not resolved.
    """
    registry_path = project_path / ".aah" / "decision-registry.yaml"
    if not registry_path.exists():
        return None

    registry = read_yaml(registry_path)
    for decision in registry.get("decisions", []):
        if decision.get("ddr_id") == ddr_id:
            return (decision.get("resolved_option", decision.get("resolved")) or "").lower() or None

    return None


def get_discuss_slug(project_path: Path, slug_id: str) -> str | None:
    """
    Get the resolved value for a slug from the aah-discuss decision registry
    (`.aah/discuss/decision-registry.yaml`).

    This is the AAH-native decision source (replaces the legacy DDR path for the
    AgentCore route). The registry is slug-keyed with two blocks:
      - `decisions[]`    — walk answers   ({slug_id, response, ...})
      - `pre_resolved[]` — inferred/mandatory ({slug_id, value, ...})
    Returns the resolved value (lowercased) or None if not found.
    """
    registry_path = project_path / ".aah" / "discuss" / "decision-registry.yaml"
    if not registry_path.exists():
        return None
    registry = read_yaml(registry_path)
    for decision in registry.get("decisions", []):
        if decision.get("slug_id") == slug_id:
            val = decision.get("response", decision.get("value"))
            return (str(val).lower() or None) if val is not None else None
    for pre in registry.get("pre_resolved", []):
        if pre.get("slug_id") == slug_id:
            val = pre.get("value", pre.get("response"))
            return (str(val).lower() or None) if val is not None else None
    return None


def is_agentcore_deployment(project_path: Path) -> bool:
    """Check if the backend platform resolved to agentcore-runtime (AWS Bedrock
    AgentCore Runtime + Amplify frontend)."""
    return get_backend_platform(project_path) == "agentcore-runtime"


def is_cloudrun_deployment(project_path: Path) -> bool:
    """Check if the backend platform resolved to cloud-run."""
    return get_backend_platform(project_path) == "cloud-run"


def is_ecs_express_deployment(project_path: Path) -> bool:
    """Check if the backend platform resolved to ecs-express-mode."""
    return get_backend_platform(project_path) == "ecs-express-mode"


def get_deployment_route(project_path: Path) -> str:
    """
    Determine the deployment route for this project.

    Reads the aah-discuss slug `backend-compute-platform` (single source of truth),
    with DDR-SHARED-017 as a legacy fallback (see get_backend_platform).

    Returns:
        "agentcore"  - AWS Bedrock AgentCore Runtime backend + Amplify frontend (any framework)
        "cloud-run"  - Serverless pipeline, GCP Cloud Run
        "ecs-express" - Serverless pipeline, AWS ECS Fargate + ALB
        "cloudless"  - Cloudless path (LangGraph + Agent Engine/AgentCore)
        "standard"   - Standard IaC generation (user deploys)
    """
    platform = get_backend_platform(project_path)

    if platform == "agentcore-runtime":
        return "agentcore"
    if platform == "cloud-run":
        return "cloud-run"
    if platform == "ecs-express-mode":
        return "ecs-express"
    if platform == "cloudless-managed":
        return "cloudless"

    return "standard"


def get_target_cloud(project_path: Path) -> str | None:
    """
    Determine the target cloud provider from manifest or registry.

    Returns "aws", "gcp", or None.
    """
    # Infer directly from the backend platform slug (single source of truth).
    platform = get_backend_platform(project_path)
    if platform in ("agentcore-runtime", "ecs-express-mode"):
        return "aws"
    if platform == "cloud-run":
        return "gcp"

    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        cloud = (manifest.get("stack_choices", {}).get("cloud") or "").lower()
        if cloud in ("aws", "gcp"):
            return cloud

    registry_path = project_path / ".aah" / "discuss" / "decision-registry.yaml"
    if registry_path.exists():
        registry = read_yaml(registry_path)

        # Check infrastructure_givens.cloud
        cloud_given = (registry.get("context", {}).get("infrastructure_givens", {}).get("cloud") or "").lower()
        if cloud_given in ("aws", "gcp"):
            return cloud_given
        if "aws" in cloud_given:
            return "aws"
        if "gcp" in cloud_given:
            return "gcp"

    return None


def get_route_config(project_path: Path) -> dict:
    """
    Get the full routing config for this project's deployment route.

    Returns the route entry from routing-config.yaml merged with detected context:
        {
            "route": "cloudless",
            "agent_backend": "cloudless-deploy-engineer",
            "agent_frontend": "frontend-deploy-engineer",
            "frontend_capable": false,
            "frontend_platform": "ecs-express-mode",  # derived from cloud target
            "plan_templates": ["cloudless-feature.yaml", ...],
            "user_callout": "Cloudless deployment is recommended...",
            "cloud_target": "aws",
            ...
        }
    """
    route = get_deployment_route(project_path)
    cloud_target = get_target_cloud(project_path)

    # Load routing config
    config_path = Path(__file__).parent / "routing-config.yaml"
    if not config_path.exists():
        # Fallback: return minimal config if routing-config.yaml not found
        return {"route": route, "cloud_target": cloud_target}

    config = read_yaml(config_path)
    route_entry = config.get("routes", {}).get(route, {})

    # Derive frontend platform when route can't deploy frontend
    frontend_platform = None
    if not route_entry.get("frontend_capable", False) and cloud_target:
        derivation_rules = config.get("frontend_derivation", {}).get("rules", {})
        frontend_platform = derivation_rules.get(cloud_target)
    elif route_entry.get("frontend_capable", False):
        # Same platform for frontend
        if route == "cloud-run":
            frontend_platform = "cloud-run"
        elif route == "ecs-express":
            frontend_platform = "ecs-express-mode"
        elif route == "agentcore":
            frontend_platform = "amplify"

    # Format user callout with context
    user_callout = route_entry.get("user_callout", "")
    cloud_labels = {"aws": "AWS Bedrock AgentCore", "gcp": "GCP Vertex AI Agent Engine"}
    if cloud_target and "{cloud_target_label}" in user_callout:
        user_callout = user_callout.replace(
            "{cloud_target_label}", cloud_labels.get(cloud_target, cloud_target)
        )

    return {
        "route": route,
        "cloud_target": cloud_target,
        "agent_backend": route_entry.get("agent_backend"),
        "agent_frontend": route_entry.get("agent_frontend"),
        "frontend_capable": route_entry.get("frontend_capable", False),
        "frontend_platform": frontend_platform,
        "plan_templates": route_entry.get("plan_templates", []),
        "plan_rules": route_entry.get("plan_rules", []),
        "user_callout": user_callout.strip(),
        "constraints": route_entry.get("constraints", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect LangGraph and deployment route")
    parser.add_argument("--project-path", type=Path, required=True)
    parser.add_argument("command", choices=["detect", "route", "config"], help="What to check")
    args = parser.parse_args()

    if args.command == "detect":
        result = {
            "is_agentcore": is_agentcore_deployment(args.project_path),
            "is_langgraph": is_langgraph_project(args.project_path),
            "is_cloudless": is_cloudless_deployment(args.project_path),
            "is_cloudrun": is_cloudrun_deployment(args.project_path),
            "is_ecs_express": is_ecs_express_deployment(args.project_path),
            "target_cloud": get_target_cloud(args.project_path),
            "backend_deploy_platform": get_ddr_resolution(args.project_path, "DDR-SHARED-017"),
        }
    elif args.command == "route":
        result = {
            "route": get_deployment_route(args.project_path),
            "is_agentcore": is_agentcore_deployment(args.project_path),
            "is_langgraph": is_langgraph_project(args.project_path),
            "is_cloudless": is_cloudless_deployment(args.project_path),
            "is_cloudrun": is_cloudrun_deployment(args.project_path),
            "is_ecs_express": is_ecs_express_deployment(args.project_path),
            "target_cloud": get_target_cloud(args.project_path),
            "backend_deploy_platform": get_ddr_resolution(args.project_path, "DDR-SHARED-017"),
        }
    elif args.command == "config":
        result = get_route_config(args.project_path)

    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
