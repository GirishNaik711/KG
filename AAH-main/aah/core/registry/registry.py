#!/usr/bin/env python3
"""Decision registry — single YAML state machine for all project decisions.

The decision registry tracks:
- Project metadata (name, archetype, DDR sets)
- Context (facts, integrations, compliance, NFRs, constraints)
- Decisions (status, resolution, eliminated options)
- Progress (computed on every write)
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.registry.ddr_loader import (
    load_all_ddrs,
    resolve_resources_path,
    compute_resolution_order,
    get_valid_option_labels,
)
from aah.core.registry.regimes import get_eliminations, detect_regimes_from_context


REGISTRY_FILENAME = "decision-registry.yaml"
VALID_STATUSES = {"open", "partial", "recommended", "resolved", "skipped"}


def _compute_progress(decisions: list[dict]) -> dict:
    total = len(decisions)
    counts = {s: 0 for s in VALID_STATUSES}
    for d in decisions:
        status = d.get("status", "open")
        if status in counts:
            counts[status] += 1
    return {
        "total": total,
        "resolved": counts["resolved"],
        "recommended": counts["recommended"],
        "partial": counts["partial"],
        "open": counts["open"],
        "skipped": counts["skipped"],
    }


def init_registry(
    project_name: str,
    archetype: str,
    domain: str | None = None,
    ddr_sets: list[str] | None = None,
) -> dict:
    """Create a new decision registry with all applicable DDRs."""
    if ddr_sets is None:
        ddr_sets = [archetype, "shared"]

    resources_path = resolve_resources_path()
    all_ddrs = load_all_ddrs(resources_path, ddr_sets)

    decisions = []
    for ddr in all_ddrs:
        decisions.append({
            "ddr_id": ddr["id"],
            "status": "open",
            "resolved": None,
            "pending": None,
            "leaning_option": None,
            "confirmed": False,
            "eliminated_options": [],
            "adr_path": None,
        })

    registry = {
        "schema_version": "2.0",
        "project": {
            "name": project_name,
            "archetype": archetype,
            "domain": domain,
            "ddr_sets": ddr_sets,
        },
        "context": {
            "deployment_target": None,
            "delivery_intent": None,
            "compliance_regimes": [],
            "integrations": [],
            "infrastructure_givens": {},
            "nfrs": [],
            "organizational_constraints": [],
            "facts": [],
        },
        "decisions": decisions,
        "progress": _compute_progress(decisions),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return registry


def init_brownfield_registry(
    project_name: str,
    archetype: str,
    domain: str | None = None,
    intent_file: str | None = None,
    decisions_file: str | None = None,
    ddr_sets: list[str] | None = None,
) -> dict:
    """Create a decision registry for a brownfield project.

    Reads:
      - intent_file: .aah/research/project-intent.yaml (structured user intent)
      - decisions_file: .aah/research/codebase-decisions.json with:
          {"pre_resolved": [...], "open": [...], "skipped": [...]}

    DDRs enter the registry in one of three states:
      - pre_resolved: status=resolved, from codebase intelligence
      - open: status=open, needs resolution aligned with user intent
      - skipped: status=skipped, not relevant to improvement goal
    """
    if ddr_sets is None:
        ddr_sets = [archetype, "shared"]

    resources_path = resolve_resources_path()
    all_ddrs = load_all_ddrs(resources_path, ddr_sets)

    # Load intent file if provided
    user_intent = None
    if intent_file:
        intent_path = Path(intent_file)
        if intent_path.exists():
            user_intent = read_yaml(intent_path)

    # Load codebase decisions categorization
    categorization = {"pre_resolved": [], "open": [], "skipped": []}
    if decisions_file:
        decisions_path = Path(decisions_file)
        if decisions_path.exists():
            with open(decisions_path, encoding='utf-8') as f:
                categorization = json.load(f)

    # Build lookup maps from categorization
    pre_resolved_map = {
        e["ddr_id"]: e for e in categorization.get("pre_resolved", [])
    }
    open_set = {e["ddr_id"] for e in categorization.get("open", [])}
    skipped_map = {e["ddr_id"]: e for e in categorization.get("skipped", [])}

    decisions = []
    for ddr in all_ddrs:
        ddr_id = ddr["id"]
        if ddr_id in pre_resolved_map:
            entry = pre_resolved_map[ddr_id]
            decisions.append({
                "ddr_id": ddr_id,
                "status": "resolved",
                "resolved": entry.get("resolved"),
                "pending": None,
                "leaning_option": None,
                "confirmed": True,
                "eliminated_options": [],
                "adr_path": None,
                "source": "codebase-intel",
                "source_file": entry.get("source", ""),
            })
        elif ddr_id in skipped_map:
            entry = skipped_map[ddr_id]
            decisions.append({
                "ddr_id": ddr_id,
                "status": "skipped",
                "resolved": f"Skipped: {entry.get('reason', 'not in scope')}",
                "pending": None,
                "leaning_option": None,
                "confirmed": False,
                "eliminated_options": [],
                "adr_path": None,
            })
        else:
            # Default to open (includes explicit open entries and uncategorized)
            decisions.append({
                "ddr_id": ddr_id,
                "status": "open",
                "resolved": None,
                "pending": None,
                "leaning_option": None,
                "confirmed": False,
                "eliminated_options": [],
                "adr_path": None,
            })

    context = {
        "deployment_target": None,
        "delivery_intent": None,
        "compliance_regimes": [],
        "integrations": [],
        "infrastructure_givens": {},
        "nfrs": [],
        "organizational_constraints": [],
        "facts": [],
    }
    if user_intent:
        context["user_intent"] = user_intent

    registry = {
        "schema_version": "2.0",
        "project": {
            "name": project_name,
            "archetype": archetype,
            "domain": domain,
            "ddr_sets": ddr_sets,
        },
        "context": context,
        "decisions": decisions,
        "progress": _compute_progress(decisions),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return registry


def load_registry(registry_path: Path) -> dict:
    if not registry_path.exists():
        print(f"Error: registry not found at {registry_path}", file=sys.stderr)
        sys.exit(1)
    return read_yaml(registry_path)


def save_registry(registry: dict, registry_path: Path) -> None:
    registry["progress"] = _compute_progress(registry.get("decisions", []))
    registry["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_yaml(registry, registry_path)


def update_context(registry: dict, field: str, value) -> dict:
    """Update a field in the context block."""
    ctx = registry.setdefault("context", {})
    if field in ("facts", "integrations", "nfrs", "organizational_constraints", "compliance_regimes"):
        existing = ctx.get(field, [])
        if isinstance(value, list):
            existing.extend(value)
        else:
            existing.append(value)
        ctx[field] = existing
    elif field == "infrastructure_givens":
        givens = ctx.get("infrastructure_givens", {})
        if isinstance(value, dict):
            givens.update(value)
        ctx["infrastructure_givens"] = givens
    else:
        ctx[field] = value
    return registry


def update_decision(
    registry: dict,
    ddr_id: str,
    status: str | None = None,
    resolved: str | None = None,
    pending: str | None = None,
    leaning_option: str | None = None,
    confirmed: bool | None = None,
    adr_path: str | None = None,
) -> dict:
    """Update a decision entry in the registry."""
    for d in registry.get("decisions", []):
        if d["ddr_id"] == ddr_id:
            if status is not None:
                if status not in VALID_STATUSES:
                    print(f"Error: invalid status '{status}'", file=sys.stderr)
                    sys.exit(1)
                d["status"] = status
            if leaning_option is not None:
                _validate_option(registry, ddr_id, leaning_option, "leaning_option")
                d["leaning_option"] = leaning_option
            if resolved is not None:
                # Validate resolved value when decision is being resolved
                effective_status = status if status is not None else d.get("status")
                if effective_status == "resolved":
                    _validate_option(registry, ddr_id, resolved, "resolved")
                d["resolved"] = resolved
            if pending is not None:
                d["pending"] = pending
            if confirmed is not None:
                d["confirmed"] = confirmed
            if adr_path is not None:
                d["adr_path"] = adr_path
            return registry
    print(f"Error: DDR '{ddr_id}' not found in registry", file=sys.stderr)
    sys.exit(1)


def _validate_option(registry: dict, ddr_id: str, value: str, field_name: str) -> None:
    """Validate that a value is a valid option label for the given DDR."""
    resources_path = resolve_resources_path()
    ddr_sets = registry.get("project", {}).get("ddr_sets")
    valid_options = get_valid_option_labels(resources_path, ddr_id, ddr_sets)
    if valid_options and value not in valid_options:
        print(
            f"Error: '{value}' is not a valid {field_name} for {ddr_id}.\n"
            f"Valid options: {valid_options}",
            file=sys.stderr,
        )
        sys.exit(1)


def apply_eliminations(registry: dict) -> dict:
    """Apply compliance regime and engineering mandate eliminations."""
    resources_path = resolve_resources_path()
    context = registry.get("context", {})
    archetype = registry.get("project", {}).get("archetype", "ai-applications")

    regimes = detect_regimes_from_context(context, resources_path)
    context["compliance_regimes"] = regimes

    eliminations = get_eliminations(context, resources_path, archetype)

    elim_by_ddr: dict[str, list[dict]] = {}
    for e in eliminations:
        ddr_id = e["ddr_id"]
        elim_by_ddr.setdefault(ddr_id, []).append({
            "option": e["option"],
            "reason": e.get("reason", ""),
            "source": e.get("source", "mandate"),
        })

    for d in registry.get("decisions", []):
        ddr_id = d["ddr_id"]
        if ddr_id in elim_by_ddr:
            existing_options = {e["option"] for e in d.get("eliminated_options", [])}
            for new_elim in elim_by_ddr[ddr_id]:
                if new_elim["option"] not in existing_options:
                    d.setdefault("eliminated_options", []).append(new_elim)

    return registry


def apply_skip_conditions(registry: dict) -> dict:
    """Re-evaluate skip_if conditions based on resolved decisions."""
    resources_path = resolve_resources_path()
    ddr_sets = registry.get("project", {}).get("ddr_sets", ["ai-applications", "shared"])
    all_ddrs = load_all_ddrs(resources_path, ddr_sets)

    resolved_map: dict[str, str] = {}
    for d in registry.get("decisions", []):
        if d.get("status") == "resolved" and d.get("resolved"):
            resolved_map[d["ddr_id"]] = d["resolved"]

    ddr_skip_conditions: dict[str, list[dict]] = {}
    for ddr in all_ddrs:
        skip_if = ddr.get("skip_if", [])
        if skip_if:
            ddr_skip_conditions[ddr["id"]] = skip_if

    for d in registry.get("decisions", []):
        ddr_id = d["ddr_id"]
        if d.get("status") in ("resolved", "skipped"):
            continue
        conditions = ddr_skip_conditions.get(ddr_id, [])
        for cond in conditions:
            dep_id = cond.get("ddr_id")
            target_option = cond.get("resolved_to")
            if dep_id in resolved_map and resolved_map[dep_id] == target_option:
                d["status"] = "skipped"
                d["resolved"] = f"Skipped: {cond.get('reason', 'dependency condition met')}"
                break

    return registry


def next_unresolved(registry: dict) -> dict | None:
    """Return the next unresolved decision respecting dependency order."""
    resources_path = resolve_resources_path()
    ddr_sets = registry.get("project", {}).get("ddr_sets", ["ai-applications", "shared"])
    all_ddrs = load_all_ddrs(resources_path, ddr_sets)

    waves = compute_resolution_order(all_ddrs)

    resolved_ids = set()
    for d in registry.get("decisions", []):
        if d.get("status") in ("resolved", "skipped"):
            resolved_ids.add(d["ddr_id"])

    decision_map = {d["ddr_id"]: d for d in registry.get("decisions", [])}

    for wave in waves:
        for ddr_id in wave:
            if ddr_id not in resolved_ids:
                decision = decision_map.get(ddr_id)
                if decision and decision.get("status") not in ("resolved", "skipped"):
                    ddr = load_all_ddrs(resources_path, ddr_sets)
                    ddr_data = None
                    for dd in ddr:
                        if dd.get("id") == ddr_id:
                            ddr_data = dd
                            break
                    return {
                        "ddr_id": ddr_id,
                        "decision": decision,
                        "ddr": {k: v for k, v in ddr_data.items() if not k.startswith("_")} if ddr_data else None,
                    }

    return None


def get_status(registry: dict) -> dict:
    """Return registry status summary."""
    progress = _compute_progress(registry.get("decisions", []))
    context = registry.get("context", {})
    return {
        "project": registry.get("project", {}),
        "progress": progress,
        "context_populated": {
            "facts": len(context.get("facts", [])),
            "integrations": len(context.get("integrations", [])),
            "compliance_regimes": len(context.get("compliance_regimes", [])),
            "nfrs": len(context.get("nfrs", [])),
            "constraints": len(context.get("organizational_constraints", [])),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Decision registry — state machine for project decisions")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize a new decision registry")
    init_p.add_argument("--project-name", type=str, required=True)
    init_p.add_argument("--archetype", type=str, required=True)
    init_p.add_argument("--domain", type=str, default=None)
    init_p.add_argument("--ddr-sets", type=str, nargs="+", default=None)
    init_p.add_argument("--output", type=str, required=True, help="Output path for registry YAML")

    init_bf_p = sub.add_parser("init-brownfield", help="Initialize registry for brownfield project")
    init_bf_p.add_argument("--project-name", type=str, required=True)
    init_bf_p.add_argument("--archetype", type=str, required=True)
    init_bf_p.add_argument("--domain", type=str, default=None)
    init_bf_p.add_argument("--ddr-sets", type=str, nargs="+", default=None)
    init_bf_p.add_argument("--intent-file", type=str, default=None, help="Path to project-intent.yaml")
    init_bf_p.add_argument("--decisions-file", type=str, default=None, help="Path to codebase-decisions.json")
    init_bf_p.add_argument("--output", type=str, required=True, help="Output path for registry YAML")

    status_p = sub.add_parser("status", help="Show registry status")
    status_p.add_argument("--registry", type=str, required=True)
    status_p.add_argument("--verbose", action="store_true", help="Include per-decision details")

    update_ctx_p = sub.add_parser("update-context", help="Add context to registry")
    update_ctx_p.add_argument("--registry", type=str, required=True)
    update_ctx_p.add_argument("--field", type=str, required=True)
    update_ctx_p.add_argument("--value", type=str, required=True, help="JSON value")

    update_dec_p = sub.add_parser("update-decision", help="Update a decision")
    update_dec_p.add_argument("--registry", type=str, required=True)
    update_dec_p.add_argument("--ddr-id", type=str, required=True)
    update_dec_p.add_argument("--status", type=str, default=None)
    update_dec_p.add_argument("--resolved", type=str, default=None)
    update_dec_p.add_argument("--pending", type=str, default=None)
    update_dec_p.add_argument("--leaning-option", type=str, default=None)
    update_dec_p.add_argument("--confirmed", type=str, default=None)
    update_dec_p.add_argument("--adr-path", type=str, default=None)

    next_p = sub.add_parser("next-unresolved", help="Get next unresolved decision")
    next_p.add_argument("--registry", type=str, required=True)

    elim_p = sub.add_parser("apply-eliminations", help="Apply regime/mandate eliminations")
    elim_p.add_argument("--registry", type=str, required=True)

    skip_p = sub.add_parser("apply-skip-conditions", help="Evaluate skip_if on resolved decisions")
    skip_p.add_argument("--registry", type=str, required=True)

    args = parser.parse_args()

    if args.command == "init":
        registry = init_registry(
            args.project_name,
            args.archetype,
            args.domain,
            args.ddr_sets,
        )
        output_path = Path(args.output)
        save_registry(registry, output_path)
        result = get_status(registry)
        result["registry_path"] = str(output_path)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "init-brownfield":
        registry = init_brownfield_registry(
            args.project_name,
            args.archetype,
            args.domain,
            args.intent_file,
            args.decisions_file,
            args.ddr_sets,
        )
        output_path = Path(args.output)
        save_registry(registry, output_path)
        result = get_status(registry)
        result["registry_path"] = str(output_path)
        result["brownfield"] = True
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "status":
        registry = load_registry(Path(args.registry))
        result = get_status(registry)
        if args.verbose:
            decisions = registry.get("decisions", [])
            result["decisions"] = [
                {
                    "ddr_id": d.get("ddr_id"),
                    "status": d.get("status", "open"),
                    "resolved": d.get("resolved"),
                    "layer": d.get("layer"),
                }
                for d in decisions
            ]
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "update-context":
        registry_path = Path(args.registry)
        registry = load_registry(registry_path)
        value = json.loads(args.value)
        update_context(registry, args.field, value)
        save_registry(registry, registry_path)
        json.dump({"updated": True, "field": args.field}, sys.stdout, indent=2)
        print()

    elif args.command == "update-decision":
        registry_path = Path(args.registry)
        registry = load_registry(registry_path)
        confirmed = None
        if args.confirmed is not None:
            confirmed = args.confirmed.lower() in ("true", "1", "yes")
        update_decision(
            registry,
            args.ddr_id,
            status=args.status,
            resolved=args.resolved,
            pending=args.pending,
            leaning_option=args.leaning_option,
            confirmed=confirmed,
            adr_path=args.adr_path,
        )
        save_registry(registry, registry_path)
        json.dump({"updated": True, "ddr_id": args.ddr_id}, sys.stdout, indent=2)
        print()

    elif args.command == "next-unresolved":
        registry = load_registry(Path(args.registry))
        result = next_unresolved(registry)
        if result is None:
            json.dump({"status": "all_resolved"}, sys.stdout, indent=2)
        else:
            json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "apply-eliminations":
        registry_path = Path(args.registry)
        registry = load_registry(registry_path)
        apply_eliminations(registry)
        save_registry(registry, registry_path)
        progress = _compute_progress(registry.get("decisions", []))
        total_eliminations = sum(
            len(d.get("eliminated_options", []))
            for d in registry.get("decisions", [])
        )
        json.dump(
            {"applied": True, "total_eliminations": total_eliminations, "progress": progress},
            sys.stdout,
            indent=2,
        )
        print()

    elif args.command == "apply-skip-conditions":
        registry_path = Path(args.registry)
        registry = load_registry(registry_path)
        apply_skip_conditions(registry)
        save_registry(registry, registry_path)
        skipped = [
            d["ddr_id"] for d in registry.get("decisions", [])
            if d.get("status") == "skipped"
        ]
        json.dump({"applied": True, "skipped_decisions": skipped}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
