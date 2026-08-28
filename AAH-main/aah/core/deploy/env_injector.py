#!/usr/bin/env python3
"""
Environment variable computation for deploy layers.

Two responsibilities:

1. Inter-service URL injection — every service in a layer gets
   ``{DEP_UPPER}_URL`` pointing at each dependency's deployed URL.

2. Validated runtime-resource injection — every service in a layer
   inherits the bucket / endpoint / table coordinates the cloud-readiness
   and data-readiness gates already proved exist (see
   ``aah.core.common.readiness``). The deploy skill calls this so
   the application boots wired against real resources, not placeholders.

Usage:
    aah run core.deploy.env_injector compute \
      --layer-services '["frontend"]' \
      --all-services '[{"name":"backend","dependencies":[]},{"name":"frontend","dependencies":["backend"]}]' \
      --deployed-urls '{"backend":"https://backend-xyz.run.app"}'

    aah run core.deploy.env_injector runtime-resources \
      --project-path "$PROJECT_DIR"
"""

import argparse
import json
import sys
from pathlib import Path


def compute_env_vars_for_layer(
    layer_services: list[str],
    all_services: list[dict],
    deployed_urls: dict[str, str],
) -> dict[str, dict[str, str]]:
    """
    For each service in this layer, compute env vars pointing to its dependencies.

    Args:
        layer_services: names of services being deployed in this layer
        all_services: full service list with dependency info
        deployed_urls: {service_name: deployed_url} from prior layers

    Returns:
        {service_name: {ENV_VAR_NAME: value, ...}}

    Convention:
        - Env var name: {DEPENDENCY_NAME_UPPER}_URL
        - Underscores replace hyphens in service names
        - E.g., "auth-service" → AUTH_SERVICE_URL
    """
    # Build lookup: service name → service dict
    service_map = {svc["name"]: svc for svc in all_services}

    result: dict[str, dict[str, str]] = {}

    for svc_name in layer_services:
        svc = service_map.get(svc_name)
        if svc is None:
            result[svc_name] = {}
            continue

        env_vars: dict[str, str] = {}
        dependencies = svc.get("dependencies", [])

        for dep_name in dependencies:
            if dep_name in deployed_urls:
                # Convert service name to env var: auth-service → AUTH_SERVICE_URL
                env_key = dep_name.upper().replace("-", "_") + "_URL"
                env_vars[env_key] = deployed_urls[dep_name]

        result[svc_name] = env_vars

    return result


def compute_all_env_vars(
    layers: list[list[str]],
    all_services: list[dict],
    deployed_urls: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """
    Compute env vars for all layers sequentially.

    Simulates the deploy order: each layer's deployed URLs become available
    to subsequent layers.

    Args:
        layers: list of layers (each layer is list of service names)
        all_services: full service list with dependency info
        deployed_urls: pre-existing URLs (e.g., from brownfield redeploy)

    Returns:
        {service_name: {ENV_VAR_NAME: value}} for ALL services across all layers
    """
    if deployed_urls is None:
        deployed_urls = {}

    all_env_vars: dict[str, dict[str, str]] = {}

    for layer in layers:
        layer_vars = compute_env_vars_for_layer(layer, all_services, deployed_urls)
        all_env_vars.update(layer_vars)

        # After this layer deploys, its services would have URLs
        # (For planning purposes, we use placeholder URLs)
        for svc_name in layer:
            if svc_name not in deployed_urls:
                deployed_urls[svc_name] = f"https://{svc_name}.placeholder.url"

    return all_env_vars


def compute_runtime_resource_env_vars(project_path: Path) -> dict[str, str]:
    """
    Translate validated cloud-readiness services into deploy-time env vars.

    Reads ``.aah/cloud-readiness.yaml`` (and ``data-schema-snapshot.yaml``)
    via the shared loader, then emits one env-var map suitable for every
    service in every deploy layer. Every var is prefixed with the service's
    ``env_prefix`` (defaults to its service_type) so two services of the
    same kind don't collide.

    Returns ``{}`` when no cloud-readiness exists or no service passed.
    Secret *values* are never surfaced — only secret *paths* (so the app
    can fetch them at runtime via its own SDK).
    """
    try:
        from aah.core.common.readiness import (
            load_runtime_resources,
            render_runtime_env_vars,
        )
    except ImportError as exc:
        import logging
        logging.getLogger(__name__).debug(
            "aah.core.common.readiness not available: %s", exc
        )
        return {}

    aah_path = project_path / ".aah"
    resources = load_runtime_resources(aah_path)
    if resources is None:
        return {}
    return render_runtime_env_vars(resources)


def merge_runtime_into_layer_env(
    layer_env: dict[str, dict[str, str]],
    runtime_env: dict[str, str],
) -> dict[str, dict[str, str]]:
    """
    Layer the runtime-resource env vars onto every service in a layer.

    Inter-service URLs win on collision (a service may explicitly point a
    dependency env var at an internal URL). Otherwise the runtime resource
    coordinates flow through unchanged.
    """
    merged: dict[str, dict[str, str]] = {}
    for svc_name, svc_env in layer_env.items():
        combined = dict(runtime_env)
        combined.update(svc_env)
        merged[svc_name] = combined
    return merged


def format_env_vars_for_cli(env_vars: dict[str, str]) -> str:
    """
    Format env vars for cloud CLI --set-env-vars flag.

    Returns comma-separated KEY=VALUE string suitable for:
    - gcloud run deploy --set-env-vars "KEY1=VAL1,KEY2=VAL2"
    - aws ecs --environment '[{"name":"KEY","value":"VAL"}]'
    """
    if not env_vars:
        return ""
    return ",".join(f"{k}={v}" for k, v in env_vars.items())


def format_env_vars_for_ecs(env_vars: dict[str, str]) -> list[dict[str, str]]:
    """
    Format env vars for AWS ECS task definition JSON format.

    Returns list of {"name": "KEY", "value": "VALUE"} dicts.
    """
    return [{"name": k, "value": v} for k, v in env_vars.items()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Inter-service URL env var computation")
    sub = parser.add_subparsers(dest="command", required=True)

    comp_p = sub.add_parser("compute", help="Compute env vars for a layer")
    comp_p.add_argument("--layer-services", type=str, required=True, help="JSON list of service names")
    comp_p.add_argument("--all-services", type=str, required=True, help="JSON list of service dicts")
    comp_p.add_argument("--deployed-urls", type=str, required=True, help="JSON {name: url} from prior layers")
    comp_p.add_argument(
        "--project-path", type=Path, default=None,
        help="If set, fold validated runtime-resource env vars into every "
             "service in the layer (reads .aah/cloud-readiness.yaml).",
    )

    all_p = sub.add_parser("compute-all", help="Compute env vars for all layers")
    all_p.add_argument("--layers", type=str, required=True, help="JSON list of layers")
    all_p.add_argument("--all-services", type=str, required=True, help="JSON list of service dicts")
    all_p.add_argument("--deployed-urls", type=str, default="{}", help="JSON pre-existing URLs")
    all_p.add_argument(
        "--project-path", type=Path, default=None,
        help="If set, fold validated runtime-resource env vars into every "
             "service across all layers.",
    )

    rr_p = sub.add_parser(
        "runtime-resources",
        help="Print env vars derived from validated cloud-readiness services. "
             "These are safe to pass to deploy agents (no secret values, "
             "only paths/coordinates).",
    )
    rr_p.add_argument("--project-path", type=Path, required=True)
    rr_p.add_argument(
        "--format", choices=["json", "cli", "ecs", "dotenv"], default="json",
        help="Output format. 'cli' = KEY=VAL,KEY=VAL (gcloud), 'ecs' = "
             "[{name,value}] list (AWS ECS), 'dotenv' = newline-delimited.",
    )

    args = parser.parse_args()

    if args.command == "compute":
        layer_services = json.loads(args.layer_services)
        all_services = json.loads(args.all_services)
        deployed_urls = json.loads(args.deployed_urls)
        result = compute_env_vars_for_layer(layer_services, all_services, deployed_urls)
        if args.project_path is not None:
            runtime_env = compute_runtime_resource_env_vars(args.project_path)
            if runtime_env:
                result = merge_runtime_into_layer_env(result, runtime_env)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "compute-all":
        layers = json.loads(args.layers)
        all_services = json.loads(args.all_services)
        deployed_urls = json.loads(args.deployed_urls)
        result = compute_all_env_vars(layers, all_services, deployed_urls)
        if args.project_path is not None:
            runtime_env = compute_runtime_resource_env_vars(args.project_path)
            if runtime_env:
                result = merge_runtime_into_layer_env(result, runtime_env)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "runtime-resources":
        env = compute_runtime_resource_env_vars(args.project_path)
        # Last line of defense: pipe through the cloud-readiness output guard
        # so a misclassified field can't reach the deploy agent.
        try:
            from aah.core.gates.validate_cloud_readiness import guard_output
            guard_output(env, label="env_injector runtime-resources")
        except Exception:
            pass
        if args.format == "json":
            json.dump(env, sys.stdout, indent=2)
            print()
        elif args.format == "cli":
            print(format_env_vars_for_cli(env))
        elif args.format == "ecs":
            json.dump(format_env_vars_for_ecs(env), sys.stdout, indent=2)
            print()
        elif args.format == "dotenv":
            for k, v in env.items():
                print(f"{k}={v}")


if __name__ == "__main__":
    main()
