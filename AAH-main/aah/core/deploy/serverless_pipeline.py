#!/usr/bin/env python3
"""
Main serverless deploy pipeline state machine.

Orchestrates the full deployment lifecycle: brownfield check → discover →
sequence → authenticate → local preview → security scan → deploy per layer →
healthcheck → connectivity → debug → outputs.

State persisted in .aah/deploy/pipeline-state.json for idempotency and resume.

Follows aah/core/implement/orchestrator.py pattern (deterministic,
returns one action at a time).

Usage:
    aah run core.deploy.serverless_pipeline next-action --project-path <path> --route cloud-run
    aah run core.deploy.serverless_pipeline advance --project-path <path> --result '{...}'
    aah run core.deploy.serverless_pipeline get-state --project-path <path>
    aah run core.deploy.serverless_pipeline reset --project-path <path>
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_json, write_json


# ---------------------------------------------------------------------------
# Pipeline stages (fixed order)
# ---------------------------------------------------------------------------

STAGES = [
    "brownfield_check",
    "discover",
    "sequence",
    "authenticate",
    "local_preview",
    "security_scan",
    "tier_resolve",
    "deploy_layer",
    "healthcheck_layer",
    "connectivity",
    "outputs",
    "complete",
]

STATE_FILE = "pipeline-state.json"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _state_path(project_path: Path) -> Path:
    return project_path / ".aah" / "deploy" / STATE_FILE


def get_state(project_path: Path) -> dict:
    """Read current pipeline state. Returns default initial state if not found."""
    path = _state_path(project_path)
    if path.exists():
        return read_json(path)
    return _initial_state()


def _initial_state() -> dict:
    return {
        "current_stage": "brownfield_check",
        "current_layer_index": 0,
        "route": None,
        "services": [],
        "layers": [],
        "deployed_urls": {},
        "stage_results": {},
        "started_at": None,
        "completed_at": None,
        "status": "pending",  # pending | in_progress | completed | failed
    }


def _save_state(project_path: Path, state: dict) -> None:
    path = _state_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(state, path)


def reset_state(project_path: Path) -> None:
    """Reset pipeline to initial state."""
    _save_state(project_path, _initial_state())


# ---------------------------------------------------------------------------
# Next action computation
# ---------------------------------------------------------------------------

def next_action(project_path: Path, route: str) -> dict:
    """
    Read pipeline state and return exactly one action dict.

    The deploy skill calls this repeatedly. Each call returns what to do next.
    After the skill performs the action, it calls advance() with the result.

    Returns:
        {
            "stage": str,
            "action": str,  # what to do
            "context": dict,  # data needed to perform the action
            "is_gate": bool,  # if True, blocks until passes
            "skippable": bool,  # if True, user can skip this gate
        }
    """
    state = get_state(project_path)

    # Initialize on first call
    if state["status"] == "pending":
        state["route"] = route
        state["status"] = "in_progress"
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(project_path, state)

    current = state["current_stage"]

    if current == "brownfield_check":
        return {
            "stage": "brownfield_check",
            "action": "detect_prior_deployment",
            "context": {"project_path": str(project_path)},
            "is_gate": False,
            "skippable": False,
        }

    elif current == "discover":
        return {
            "stage": "discover",
            "action": "discover_services",
            "context": {"project_path": str(project_path)},
            "is_gate": False,
            "skippable": False,
        }

    elif current == "sequence":
        return {
            "stage": "sequence",
            "action": "compute_deploy_layers",
            "context": {"services": state.get("services", [])},
            "is_gate": False,
            "skippable": False,
        }

    elif current == "authenticate":
        from aah.core.deploy.serverless_registry import get_target
        target = get_target(route)
        cloud = target["cloud"] if target else "gcp"
        return {
            "stage": "authenticate",
            "action": "cloud_auth_check",
            "context": {"cloud": cloud, "route": route},
            "is_gate": True,
            "skippable": False,
        }

    elif current == "local_preview":
        return {
            "stage": "local_preview",
            "action": "preview_services",
            "context": {
                "project_path": str(project_path),
                "services": state.get("services", []),
                "layers": state.get("layers", []),
            },
            "is_gate": True,
            "skippable": True,
        }

    elif current == "security_scan":
        return {
            "stage": "security_scan",
            "action": "run_security_scan",
            "context": {"project_path": str(project_path)},
            "is_gate": True,
            "skippable": False,
        }

    elif current == "tier_resolve":
        # Resolve tier configuration before deploying
        from aah.core.deploy.access_tier import load_tier_prediction, detect_deployer_ip
        from aah.core.deploy.serverless_registry import get_target

        target = get_target(route)
        cloud = target["cloud"] if target else "gcp"

        # Load prediction from analysis phase (if available)
        tier_prediction = load_tier_prediction(project_path)

        # If no prediction exists (analysis used full gate, not managed-deploy validator),
        # compute it now. This ensures deploy always has a prediction regardless of
        # which analysis path ran.
        if tier_prediction is None:
            from aah.core.cloud.validate_cloud_readiness import _check_tier_prediction
            tier_prediction = _check_tier_prediction(cloud, project_path)

        deployer_ip = detect_deployer_ip()

        return {
            "stage": "tier_resolve",
            "action": "resolve_access_tier",
            "context": {
                "cloud": cloud,
                "route": route,
                "tier_prediction": tier_prediction,
                "deployer_ip": deployer_ip,
                "services": state.get("services", []),
            },
            "is_gate": False,
            "skippable": False,
        }

    elif current == "deploy_layer":
        layer_index = state.get("current_layer_index", 0)
        layers = state.get("layers", [])

        if layer_index >= len(layers):
            # All layers deployed, move to connectivity
            state["current_stage"] = "connectivity"
            _save_state(project_path, state)
            return next_action(project_path, route)

        current_layer = layers[layer_index]
        services = state.get("services", [])
        deployed_urls = state.get("deployed_urls", {})

        # Compute env vars for this layer
        from aah.core.deploy.env_injector import compute_env_vars_for_layer
        env_vars = compute_env_vars_for_layer(current_layer, services, deployed_urls)

        # Include tier context from tier_resolve stage
        tier_state = state.get("tier", {})

        # Include cloud context (account, profile, region, IAM roles)
        cloud_context = state.get("cloud_context", {})

        # Classify env vars: frontend services need them as build args, not runtime
        build_args = {}
        for svc in services:
            if svc["name"] in current_layer and svc.get("type") == "frontend":
                svc_env = env_vars.get(svc["name"], {})
                # Convert BACKEND_URL → VITE_API_URL for Vite frontends
                ba = {}
                for k, v in svc_env.items():
                    if "BACKEND" in k and "URL" in k:
                        ba["VITE_API_URL"] = v
                    ba[k] = v
                build_args[svc["name"]] = ba

        return {
            "stage": "deploy_layer",
            "action": "deploy_services",
            "context": {
                "layer_index": layer_index,
                "layer_services": current_layer,
                "env_vars": env_vars,
                "build_args": build_args,
                "route": route,
                "services": [s for s in services if s["name"] in current_layer],
                "tier": tier_state.get("current_tier"),
                "tier_config": tier_state.get("tier_config"),
                "deployer_ip": tier_state.get("deployer_ip"),
                "tiers_to_try": tier_state.get("tiers_to_try"),
                "cloud_context": cloud_context,
            },
            "is_gate": False,
            "skippable": False,
        }

    elif current == "healthcheck_layer":
        layer_index = state.get("current_layer_index", 0)
        layers = state.get("layers", [])
        deployed_urls = state.get("deployed_urls", {})

        current_layer = layers[layer_index] if layer_index < len(layers) else []
        layer_urls = {name: deployed_urls[name] for name in current_layer if name in deployed_urls}

        return {
            "stage": "healthcheck_layer",
            "action": "healthcheck_services",
            "context": {
                "layer_index": layer_index,
                "service_urls": layer_urls,
            },
            "is_gate": True,
            "skippable": False,
        }

    elif current == "connectivity":
        return {
            "stage": "connectivity",
            "action": "verify_connectivity",
            "context": {"service_urls": state.get("deployed_urls", {})},
            "is_gate": True,
            "skippable": False,
        }

    elif current == "outputs":
        tier_state = state.get("tier", {})
        return {
            "stage": "outputs",
            "action": "write_outputs",
            "context": {
                "project_path": str(project_path),
                "route": route,
                "services": state.get("services", []),
                "deployed_urls": state.get("deployed_urls", {}),
                "tier_used": tier_state.get("current_tier"),
                "tier_label": tier_state.get("tier_label"),
                "access_instructions": state.get("access_instructions", {}),
            },
            "is_gate": False,
            "skippable": False,
        }

    elif current == "complete":
        return {
            "stage": "complete",
            "action": "done",
            "context": {"deployed_urls": state.get("deployed_urls", {})},
            "is_gate": False,
            "skippable": False,
        }

    return {"stage": "error", "action": "unknown_stage", "context": {"stage": current}}


# ---------------------------------------------------------------------------
# State advancement
# ---------------------------------------------------------------------------

def advance(project_path: Path, result: dict) -> None:
    """
    Record stage result and advance to next stage.

    Args:
        result: {
            "stage": str,  # which stage completed
            "success": bool,
            "data": dict,  # stage-specific output
            "skipped": bool (optional),
        }
    """
    state = get_state(project_path)
    stage = result["stage"]
    success = result.get("success", True)
    data = result.get("data", {})
    skipped = result.get("skipped", False)

    # Record result
    state["stage_results"][stage] = {
        "success": success,
        "skipped": skipped,
        "data": data,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    if not success and not skipped:
        state["status"] = "failed"
        _save_state(project_path, state)
        return

    # Stage-specific state updates
    if stage == "brownfield_check":
        # Store brownfield info for later reference
        state["brownfield"] = data
        state["current_stage"] = "discover"

    elif stage == "discover":
        state["services"] = data.get("services", [])
        state["current_stage"] = "sequence"

    elif stage == "sequence":
        state["layers"] = data.get("layers", [])
        state["current_stage"] = "authenticate"

    elif stage == "authenticate":
        state["cloud_context"] = data
        state["current_stage"] = "local_preview"

    elif stage == "local_preview":
        state["current_stage"] = "security_scan"

    elif stage == "security_scan":
        state["current_stage"] = "tier_resolve"

    elif stage == "tier_resolve":
        # Store tier resolution state
        state["tier"] = {
            "current_tier": data.get("current_tier"),
            "tier_config": data.get("tier_config"),
            "deployer_ip": data.get("deployer_ip"),
            "tiers_to_try": data.get("tiers_to_try", ["1A", "1B", "1C"]),
            "tier_prediction": data.get("tier_prediction"),
        }
        state["current_stage"] = "deploy_layer"
        state["current_layer_index"] = 0

    elif stage == "deploy_layer":
        # Update deployed URLs from this layer
        layer_urls = data.get("deployed_urls", {})
        state["deployed_urls"].update(layer_urls)
        # Track which tier was used (set by first successful deploy)
        if data.get("tier_used") and "tier" in state:
            state["tier"]["current_tier"] = data["tier_used"]
            state["tier"]["tier_label"] = data.get("tier_label")
        # Store access instructions if provided
        if data.get("access_instructions"):
            if "access_instructions" not in state:
                state["access_instructions"] = {}
            state["access_instructions"].update(data.get("access_instructions", {}))
        state["current_stage"] = "healthcheck_layer"

    elif stage == "healthcheck_layer":
        # Move to next layer or connectivity
        layer_index = state.get("current_layer_index", 0)
        layers = state.get("layers", [])

        if layer_index + 1 < len(layers):
            state["current_layer_index"] = layer_index + 1
            state["current_stage"] = "deploy_layer"
        else:
            state["current_stage"] = "connectivity"

    elif stage == "connectivity":
        state["current_stage"] = "outputs"

    elif stage == "outputs":
        state["current_stage"] = "complete"
        state["status"] = "completed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()

    _save_state(project_path, state)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Serverless deploy pipeline state machine")
    sub = parser.add_subparsers(dest="command", required=True)

    next_p = sub.add_parser("next-action", help="Get next pipeline action")
    next_p.add_argument("--project-path", type=Path, required=True)
    next_p.add_argument("--route", type=str, required=True)

    adv_p = sub.add_parser("advance", help="Record result and advance state")
    adv_p.add_argument("--project-path", type=Path, required=True)
    adv_p.add_argument("--result", type=str, required=True, help="JSON result dict")

    state_p = sub.add_parser("get-state", help="Get current pipeline state")
    state_p.add_argument("--project-path", type=Path, required=True)

    reset_p = sub.add_parser("reset", help="Reset pipeline to initial state")
    reset_p.add_argument("--project-path", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "next-action":
        action = next_action(args.project_path, args.route)
        json.dump(action, sys.stdout, indent=2)
        print()

    elif args.command == "advance":
        result = json.loads(args.result)
        advance(args.project_path, result)
        # Print updated state summary
        state = get_state(args.project_path)
        json.dump({
            "current_stage": state["current_stage"],
            "status": state["status"],
            "deployed_urls": state.get("deployed_urls", {}),
        }, sys.stdout, indent=2)
        print()

    elif args.command == "get-state":
        state = get_state(args.project_path)
        json.dump(state, sys.stdout, indent=2)
        print()

    elif args.command == "reset":
        reset_state(args.project_path)
        print(json.dumps({"reset": True}))


if __name__ == "__main__":
    main()
