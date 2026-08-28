#!/usr/bin/env python3
"""
Service dependency graph and deploy ordering.

Builds a DAG from service dependencies and computes topological deploy layers.
Frontend services are always placed in the last layer.

Uses simple topological sort (same pattern as aah/core/common/dag.py).
No networkx dependency.

Usage:
    aah run core.deploy.service_graph order --project-path <path>
    aah run core.deploy.service_graph validate --project-path <path>
"""

import argparse
import json
import sys
from pathlib import Path


def build_service_graph(services: list[dict]) -> list[list[str]]:
    """
    Build DAG from service dependencies and compute deploy layers.

    Frontend services are always placed in the last layer regardless of
    explicit dependencies.

    Args:
        services: list of service dicts with 'name', 'dependencies', 'is_frontend'

    Returns:
        List of layers. Each layer is a list of service names that can deploy
        in parallel. Layer 0 deploys first, last layer deploys last.
    """
    # Separate frontend and backend services
    backends = [s for s in services if not s.get("is_frontend", False)]
    frontends = [s for s in services if s.get("is_frontend", False)]

    # Build adjacency for backends only (frontends go last regardless)
    backend_names = {s["name"] for s in backends}
    deps: dict[str, list[str]] = {}

    for svc in backends:
        svc_deps = [
            d for d in svc.get("dependencies", [])
            if d in backend_names  # Only count deps within backend services
        ]
        deps[svc["name"]] = svc_deps

    # Topological sort backends into layers
    backend_layers = _topological_layers(deps)

    # Add frontend as final layer (depends on all backends)
    if frontends:
        frontend_layer = [s["name"] for s in frontends]
        backend_layers.append(frontend_layer)

    return backend_layers


def _topological_layers(deps: dict[str, list[str]]) -> list[list[str]]:
    """
    Compute topological generations (layers) from a dependency dict.

    Each layer contains nodes whose dependencies are ALL in prior layers.
    Nodes with no dependencies go in layer 0.

    Pattern adapted from aah/core/common/dag.py::compute_execution_waves().
    """
    if not deps:
        return []

    resolved: set[str] = set()
    remaining = set(deps.keys())
    layers: list[list[str]] = []

    while remaining:
        # Find all nodes whose deps are fully resolved
        layer = []
        for name in sorted(remaining):
            node_deps = deps.get(name, [])
            if all(d in resolved for d in node_deps):
                layer.append(name)

        if not layer:
            # Circular dependency — force remaining into one layer with warning
            layer = sorted(remaining)
            print(
                f"Warning: circular dependency detected among: {layer}",
                file=sys.stderr,
            )

        for name in layer:
            remaining.discard(name)
            resolved.add(name)

        layers.append(layer)

    return layers


def validate_service_graph(services: list[dict]) -> list[str]:
    """
    Validate service dependency graph for issues.

    Checks:
    1. Circular dependencies
    2. References to non-existent services
    3. Frontend depending on another frontend

    Returns list of error messages. Empty = valid.
    """
    errors = []
    all_names = {s["name"] for s in services}
    frontend_names = {s["name"] for s in services if s.get("is_frontend", False)}

    for svc in services:
        name = svc["name"]
        dependencies = svc.get("dependencies", [])

        # Check for references to non-existent services
        for dep in dependencies:
            if dep not in all_names:
                errors.append(
                    f"Service '{name}' depends on '{dep}' which does not exist"
                )

        # Check for frontend depending on frontend
        if name in frontend_names:
            for dep in dependencies:
                if dep in frontend_names:
                    errors.append(
                        f"Frontend '{name}' depends on frontend '{dep}' — "
                        "frontends should only depend on backend services"
                    )

    # Check for circular dependencies (among backends)
    backends = [s for s in services if not s.get("is_frontend", False)]
    backend_names = {s["name"] for s in backends}
    deps = {
        s["name"]: [d for d in s.get("dependencies", []) if d in backend_names]
        for s in backends
    }

    if _has_cycle(deps):
        errors.append(
            "Circular dependency detected among backend services: "
            + ", ".join(sorted(deps.keys()))
        )

    return errors


def _has_cycle(deps: dict[str, list[str]]) -> bool:
    """Detect cycles using DFS coloring (white/gray/black)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in deps}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for neighbor in deps.get(node, []):
            if neighbor not in color:
                continue
            if color[neighbor] == GRAY:
                return True  # Back edge = cycle
            if color[neighbor] == WHITE and dfs(neighbor):
                return True
        color[node] = BLACK
        return False

    for node in deps:
        if color[node] == WHITE:
            if dfs(node):
                return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Service dependency graph and ordering")
    sub = parser.add_subparsers(dest="command", required=True)

    order_p = sub.add_parser("order", help="Compute deploy layers from discovered services")
    order_p.add_argument("--project-path", type=Path, required=True)
    order_p.add_argument("--services-json", type=str, default=None,
                         help="JSON services list (if not provided, runs discovery)")

    val_p = sub.add_parser("validate", help="Validate service graph")
    val_p.add_argument("--project-path", type=Path, required=True)
    val_p.add_argument("--services-json", type=str, default=None)

    args = parser.parse_args()

    # Get services (from JSON arg or by running discovery)
    if args.services_json:
        services = json.loads(args.services_json)
    else:
        from aah.core.deploy.service_discovery import discover_services
        services = discover_services(args.project_path)

    if args.command == "order":
        layers = build_service_graph(services)
        result = {
            "layers": layers,
            "layer_count": len(layers),
            "total_services": len(services),
            "deploy_order": [svc for layer in layers for svc in layer],
        }
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "validate":
        errors = validate_service_graph(services)
        result = {
            "valid": len(errors) == 0,
            "error_count": len(errors),
            "errors": errors,
        }
        json.dump(result, sys.stdout, indent=2)
        print()
        if errors:
            sys.exit(1)


if __name__ == "__main__":
    main()
