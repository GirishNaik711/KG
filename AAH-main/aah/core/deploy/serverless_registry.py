#!/usr/bin/env python3
"""
Serverless deploy target registry.

Single extension point for adding new cloud targets. Adding a new serverless
platform = adding one dict entry to TARGETS.

Usage:
    aah run core.deploy.serverless_registry get-target --route cloud-run
    aah run core.deploy.serverless_registry is-serverless --route ecs-express
"""

import argparse
import json
import sys


TARGETS: dict[str, dict] = {
    "cloud-run": {
        "cloud": "gcp",
        "agent": "cloudrun-deploy-engineer",
        "credential_check": "gcp",
        "dockerfile_required": False,  # buildpacks fallback via --source
        "build_remote": True,  # Cloud Build handles it
        "log_fetch_cmd": (
            "gcloud logging read "
            "'resource.type=cloud_run_revision AND resource.labels.service_name={service}' "
            "--limit=50 --format=json"
        ),
    },
    "ecs-express": {
        "cloud": "aws",
        "agent": "ecs-deploy-engineer",
        "credential_check": "aws",
        "dockerfile_required": True,
        "build_remote": True,  # CodeBuild (no Docker required locally)
        "log_fetch_cmd": (
            "MSYS_NO_PATHCONV=1 aws logs get-log-events "
            "--log-group-name /ecs/aah-{service} "
            "--log-stream-name latest --limit 50"
        ),
    },
}


def get_target(route: str) -> dict | None:
    """
    Look up target configuration by route name.

    Returns the target dict or None if route not found.
    """
    return TARGETS.get(route)


def is_serverless_route(route: str) -> bool:
    """Check if a route is a serverless (AAH-managed) deployment target."""
    return route in TARGETS


def list_targets() -> list[str]:
    """Return all registered serverless target names."""
    return list(TARGETS.keys())


def main() -> None:
    parser = argparse.ArgumentParser(description="Serverless deploy target registry")
    sub = parser.add_subparsers(dest="command", required=True)

    get_p = sub.add_parser("get-target", help="Get target configuration")
    get_p.add_argument("--route", type=str, required=True)

    is_p = sub.add_parser("is-serverless", help="Check if route is serverless")
    is_p.add_argument("--route", type=str, required=True)

    sub.add_parser("list-targets", help="List all registered targets")

    args = parser.parse_args()

    if args.command == "get-target":
        target = get_target(args.route)
        if target is None:
            print(f"Unknown route: {args.route}", file=sys.stderr)
            sys.exit(1)
        json.dump(target, sys.stdout, indent=2)
        print()

    elif args.command == "is-serverless":
        result = is_serverless_route(args.route)
        json.dump({"route": args.route, "is_serverless": result}, sys.stdout, indent=2)
        print()

    elif args.command == "list-targets":
        json.dump({"targets": list_targets()}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
