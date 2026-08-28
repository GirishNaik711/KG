#!/usr/bin/env python3
"""
Extract cloud service requirements from the /aah-discuss decision registry.

Reads .aah/discuss/decision-registry.yaml (slug-keyed) and
aah/_resources/access/cloud-service-catalog.yaml (handler registry), cross-references
each resolved slug with catalog entries, and outputs a JSON list of
services that need connectivity testing.

CLI:
    aah run core.cloud.registry_extractor --project-path <path>
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import resolve_framework_root, resolve_project_path
from aah.core.common.io_utils import read_yaml


# ═══════════════════════════════════════════════════════════════════════════════
# Managed deployment service bundles
# ═══════════════════════════════════════════════════════════════════════════════
#
# When a project chooses a managed deployment runtime (cloudless, cloud-run,
# ecs-express), the readiness gate needs to probe a fixed bundle of provider
# services regardless of the rest of the catalog. These bundles ship inline
# because they don't map 1:1 to a single slug — they're deployment-mode presets.


AWS_CLOUDLESS_SERVICES = [
    {
        "service_name": "bedrock-agentcore",
        "description": "AWS Bedrock AgentCore managed runtime",
        "criticality": "critical",
        "validation_command": "aws bedrock-agent list-agents --region {region}",
    },
    {
        "service_name": "ecr",
        "description": "Elastic Container Registry for agent images",
        "criticality": "critical",
        "validation_command": "aws ecr describe-repositories --region {region}",
    },
    {
        "service_name": "ecs-express-mode",
        "description": "ECS Express Mode for frontend deployment",
        "criticality": "optional",
        "validation_command": "aws ecs list-clusters --region {region}",
    },
]

AWS_ECS_EXPRESS_SERVICES = [
    {
        "service_name": "ecs-express-mode",
        "description": "ECS Express Mode for frontend deployment",
        "criticality": "critical",
        "validation_command": "aws ecs list-clusters --region {region}",
    }
]

GCP_CLOUDLESS_SERVICES = [
    {
        "service_name": "vertex-agent-engine",
        "description": "GCP Vertex AI Agent Engine managed runtime",
        "criticality": "critical",
        "validation_command": "gcloud ai agent-engines list --project {project} --location {region}",
    },
    {
        "service_name": "gcs",
        "description": "Google Cloud Storage for staging artifacts",
        "criticality": "critical",
        "validation_command": "gsutil ls",
    },
    {
        "service_name": "cloud-run",
        "description": "Cloud Run for frontend deployment",
        "criticality": "optional",
        "validation_command": "gcloud run services list --region {region}",
    },
]

GCP_CLOUDRUN_SERVICES = [
    {
        "service_name": "cloud-run",
        "description": "GCP Cloud Run for backend container deployment",
        "criticality": "critical",
        "validation_command": "gcloud run services list --region {region}",
    },
    {
        "service_name": "artifact-registry",
        "description": "Artifact Registry for container images",
        "criticality": "critical",
        "validation_command": "gcloud artifacts repositories list --location {region}",
    },
]


def load_catalog(framework_root: Path) -> dict:
    """Load the cloud service catalog from aah/_resources/."""
    catalog_path = framework_root / "_resources" / "access" / "cloud-service-catalog.yaml"
    if not catalog_path.exists():
        print(f"Error: cloud-service-catalog.yaml not found at {catalog_path}", file=sys.stderr)
        sys.exit(1)
    return read_yaml(catalog_path)


def _detect_provider_from_slugs(slug_registry: dict) -> str | None:
    """Read cloud-provider from the /aah-discuss slug registry (pre_resolved or decisions)."""
    for section in ("pre_resolved", "decisions"):
        for entry in slug_registry.get(section, []) or []:
            if entry.get("slug_id") == "cloud-provider":
                value = entry.get("value") if section == "pre_resolved" else entry.get("response")
                if isinstance(value, str):
                    return value.lower()
    return None


def detect_cloud_provider(registry: dict) -> str | None:
    """Detect the cloud provider from the /aah-discuss slug registry."""
    provider = _detect_provider_from_slugs(registry)
    if provider:
        return provider

    context = registry.get("context", {})
    infra = context.get("infrastructure_givens", {})
    if isinstance(infra, dict):
        provider = (
            infra.get("cloud_provider")
            or infra.get("primary_cloud")
            or infra.get("cloud")
        )
        if provider:
            return provider.lower()

    deployment = context.get("deployment_target", "")
    if deployment:
        dep_lower = deployment.lower()
        if "aws" in dep_lower:
            return "aws"
        if "azure" in dep_lower:
            return "azure"
        if "gcp" in dep_lower or "google" in dep_lower:
            return "gcp"

    return None


def _iter_slug_decisions(registry: dict):
    """Yield (slug_id, resolved_option) for every resolved slug across pre_resolved and decisions."""
    for section in ("pre_resolved", "decisions"):
        for entry in registry.get(section, []) or []:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug_id")
            if not slug:
                continue
            # pre_resolved uses `value`; decisions uses `response`.
            resolved = entry.get("value") if section == "pre_resolved" else entry.get("response")
            if not resolved:
                continue
            yield slug, resolved


def _detect_managed_deployment_mode(registry: dict) -> tuple[bool, bool, bool]:
    """Detect if a managed deployment runtime is chosen (cloudless, cloud-run, ecs-express)."""
    is_cloudless = False
    is_cloudrun = False
    is_ecs_express = False

    for slug, resolved in _iter_slug_decisions(registry):
        if slug == "deployment-runtime":
            resolved_lower = str(resolved).lower()
            is_cloudrun = resolved_lower == "cloud-run"
            is_ecs_express = resolved_lower == "ecs-express-mode"
            is_cloudless = resolved_lower == "cloudless-managed"
            break

    return is_cloudless, is_cloudrun, is_ecs_express


def _resolve_target_cloud(project_path: Path, registry: dict) -> str | None:
    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        target_cloud = (manifest.get("stack_choices", {}).get("cloud") or "").lower()
        if target_cloud in ("aws", "gcp"):
            return target_cloud
        ai_stack = (manifest.get("stack_choices", {}).get("ai") or "").lower()
        if "bedrock" in ai_stack or "aws" in ai_stack:
            return "aws"
        if "vertex" in ai_stack or "gcp" in ai_stack:
            return "gcp"

    provider = detect_cloud_provider(registry)
    if provider in ("aws", "gcp"):
        return provider
    return None


def extract_services_from_registry(project_path: Path) -> dict:
    """Compatibility extractor for managed deployment cloud validation."""
    registry_path = project_path / ".aah" / "discuss" / "decision-registry.yaml"
    if not registry_path.exists():
        return {"target_cloud": None, "services": [], "is_cloudless": False, "is_cloudrun": False, "is_ecs_express": False}

    registry = read_yaml(registry_path)
    is_cloudless, is_cloudrun, is_ecs_express = _detect_managed_deployment_mode(registry)

    if not is_cloudless and not is_cloudrun and not is_ecs_express:
        return {"target_cloud": None, "services": [], "is_cloudless": False, "is_cloudrun": False, "is_ecs_express": False}

    if is_cloudrun:
        return {
            "target_cloud": "gcp",
            "services": list(GCP_CLOUDRUN_SERVICES),
            "is_cloudless": False,
            "is_cloudrun": True,
            "is_ecs_express": False,
        }

    if is_ecs_express:
        return {
            "target_cloud": "aws",
            "services": list(AWS_ECS_EXPRESS_SERVICES),
            "is_cloudless": False,
            "is_cloudrun": False,
            "is_ecs_express": True,
        }

    target_cloud = _resolve_target_cloud(project_path, registry)
    if target_cloud == "aws":
        services = list(AWS_CLOUDLESS_SERVICES)
    elif target_cloud == "gcp":
        services = list(GCP_CLOUDLESS_SERVICES)
    else:
        services = []

    for slug, resolved in _iter_slug_decisions(registry):
        if slug != "frontend-hosting":
            continue
        frontend_option = str(resolved).lower()
        if target_cloud == "aws" and "ecs" in frontend_option:
            if not any(service["service_name"] == "ecs-express-mode" for service in services):
                services.append(AWS_CLOUDLESS_SERVICES[2])
        if target_cloud == "gcp" and "cloud-run" in frontend_option:
            if not any(service["service_name"] == "cloud-run" for service in services):
                services.append(GCP_CLOUDLESS_SERVICES[2])

    return {
        "target_cloud": target_cloud,
        "services": services,
        "is_cloudless": True,
        "is_cloudrun": False,
        "is_ecs_express": False,
    }


def collect_routing_inputs(
    registry: dict,
    catalog: dict,
    provider: str | None,
) -> dict:
    """
    Emit the raw inputs the /aah-access skill needs to route slugs to handlers.

    No classification is done here. The skill is responsible for reading
    `slug_decisions` and `handlers`, deciding for each slug which handler
    (if any) fits, and calling the gate back with a `--routings` payload.

    Returns:
      - slug_decisions: [{slug_id, response}] for every resolved slug in the
        registry (both pre_resolved[] and decisions[]).
      - handlers: [{handler_id, description, applies_when, provider, ...}]
        from the catalog. All fields preserved so the skill can build the
        seeded service row without a second catalog read.
      - provider: detected or specified cloud provider.
    """
    slug_decisions = [
        {"slug_id": slug, "response": resolved}
        for slug, resolved in _iter_slug_decisions(registry)
    ]
    handlers = [
        h for h in (catalog.get("handlers") or [])
        if isinstance(h, dict) and h.get("handler_id")
    ]
    return {
        "slug_decisions": slug_decisions,
        "handlers": handlers,
        "provider": provider,
    }


def build_service_from_handler(
    slug_id: str,
    response,
    handler_id: str,
    handlers: list[dict],
) -> dict | None:
    """
    Assemble a probeable service row from a routed (slug, response, handler).

    Called by the gate at seed time once the skill's routing decisions have
    been passed back via --routings. Returns None if handler_id isn't in the
    provided handler registry (defensive; the caller should validate first).
    """
    handler = next(
        (h for h in handlers if h.get("handler_id") == handler_id),
        None,
    )
    if handler is None:
        return None
    return {
        "source_slug": slug_id,
        "resolved_option": response,
        "handler_id": handler_id,
        "criticality": handler.get("criticality_default", "advisory"),
        "provider": handler.get("provider", "general"),
        "service_type": handler.get("handler_id", ""),  # legacy consumer key
        "display_name": handler.get("handler_id", ""),
        "description": handler.get("description", ""),
        "required_user_input": handler.get("required_user_input", []),
        "optional_user_input": handler.get("optional_user_input", []),
        "auth_options": handler.get("auth_options", []),
        "sdk_package": handler.get("sdk_package"),
        "routed_by": "skill-side-llm",
    }


def main() -> None:
    from aah.core.common.runguard import require_rapids_run
    require_rapids_run("registry_extractor")

    parser = argparse.ArgumentParser(
        description="Extract cloud service requirements from /aah-discuss decision registry"
    )
    parser.add_argument(
        "--project-path", type=Path, required=True,
        help="Path to the project directory containing .aah/"
    )
    parser.add_argument(
        "--provider", type=str, default=None,
        choices=["aws", "azure", "gcp"],
        help="Override cloud provider detection"
    )
    args = parser.parse_args()

    project_path = resolve_project_path(args.project_path)
    if project_path is None:
        print("Error: could not resolve project path", file=sys.stderr)
        sys.exit(1)

    aah_path = project_path / ".aah"
    registry_path = aah_path / "discuss" / "decision-registry.yaml"
    if not registry_path.exists():
        print(json.dumps({
            "services": [],
            "skipped_slugs": [],
            "no_match_slugs": [],
            "provider": None,
            "total_services": 0,
            "note": "No decision-registry.yaml found — no services to validate",
        }))
        sys.exit(0)

    registry = read_yaml(registry_path)

    framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root", file=sys.stderr)
        sys.exit(1)

    catalog = load_catalog(framework_root)
    provider = args.provider or detect_cloud_provider(registry)

    result = collect_routing_inputs(registry, catalog, provider)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
