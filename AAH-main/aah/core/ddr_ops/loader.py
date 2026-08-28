#!/usr/bin/env python3
"""Load, list, search, and read DDRs from the activity library."""

import argparse
import json
import re
import sys
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml


# Architecture layers mapping
LAYERS = {
    1: {"name": "Interaction Design", "slug": "interaction-design"},
    2: {"name": "Framework & Tooling", "slug": "framework-tooling"},
    3: {"name": "LLM Selection", "slug": "llm-selection"},
    4: {"name": "Retrieval & Grounding", "slug": "retrieval-grounding"},
    5: {"name": "Agent Coordination", "slug": "agent-coordination"},
    6: {"name": "Memory & Context", "slug": "memory-context"},
    7: {"name": "Safety & Evaluation", "slug": "safety-evaluation"},
    8: {"name": "Tool Integration / MCP", "slug": "tool-mcp"},
    9: {"name": "Compute Infrastructure", "slug": "cloud-compute"},
}


def resolve_resources_path(framework_root: Path | None = None) -> Path:
    """Resolve the _resources directory path."""
    if framework_root is None:
        framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root", file=sys.stderr)
        sys.exit(1)
    return Path(framework_root) / "build-playbooks"


def discover_archetypes(resources_path: Path) -> list[str]:
    """Discover all archetypes that have decisions directories."""
    archetypes = []
    if not resources_path.exists():
        return archetypes
    for entry in sorted(resources_path.iterdir()):
        if entry.is_dir() and (entry / "decisions").is_dir():
            archetypes.append(entry.name)
    return archetypes


def load_all_ddrs(resources_path: Path, archetype: str | None = None) -> list[dict]:
    """Load all DDR YAML files, optionally filtered by archetype.

    Returns a list of dicts, each augmented with:
      - _archetype: the archetype folder name
      - _path: absolute path to the YAML file
    """
    ddrs = []
    archetypes = [archetype] if archetype else discover_archetypes(resources_path)

    for arch in archetypes:
        decisions_dir = resources_path / arch / "decisions"
        if not decisions_dir.exists():
            continue
        for layer_dir in sorted(decisions_dir.iterdir()):
            if not layer_dir.is_dir():
                continue
            for yaml_file in sorted(layer_dir.glob("DDR-*.yaml")):
                try:
                    data = read_yaml(yaml_file)
                    data["_archetype"] = arch
                    data["_path"] = str(yaml_file)
                    ddrs.append(data)
                except Exception as e:
                    print(f"Warning: failed to parse {yaml_file}: {e}", file=sys.stderr)
    return ddrs


def load_ddr_by_id(resources_path: Path, ddr_id: str, archetype: str | None = None) -> dict | None:
    """Load a specific DDR by its ID (e.g., DDR-L3-002)."""
    all_ddrs = load_all_ddrs(resources_path, archetype)
    for ddr in all_ddrs:
        if ddr.get("id") == ddr_id:
            return ddr
    return None


def search_ddrs(resources_path: Path, query: str, archetype: str | None = None,
                layer: int | None = None) -> list[dict]:
    """Search DDRs by keyword against decision_question, forces, and options."""
    all_ddrs = load_all_ddrs(resources_path, archetype)
    query_lower = query.lower()
    results = []

    for ddr in all_ddrs:
        if layer is not None and ddr.get("layer_number") != layer:
            continue

        # Search across key text fields
        forces_text = ""
        raw_forces = ddr.get("forces", [])
        if isinstance(raw_forces, list):
            for f in raw_forces:
                if isinstance(f, dict):
                    forces_text += f" {f.get('id', '')} {f.get('tension', '')} {f.get('description', '')}"
                elif isinstance(f, str):
                    forces_text += f" {f}"

        searchable = " ".join([
            ddr.get("decision_question", ""),
            forces_text,
            " ".join(opt.get("description", "") for opt in ddr.get("options", [])),
            " ".join(opt.get("label", "") for opt in ddr.get("options", [])),
            " ".join(ddr.get("anti_patterns", [])),
            ddr.get("mandate", "") or "",
        ]).lower()

        if query_lower in searchable:
            results.append(ddr)

    return results


def get_next_sequence(resources_path: Path, archetype: str, layer_number: int) -> int:
    """Get the next available DDR sequence number for a given archetype and layer."""
    all_ddrs = load_all_ddrs(resources_path, archetype)
    max_seq = 0
    pattern = re.compile(rf"^DDR-L{layer_number}-(\d{{3}})$")

    for ddr in all_ddrs:
        match = pattern.match(ddr.get("id", ""))
        if match:
            seq = int(match.group(1))
            if seq > max_seq:
                max_seq = seq

    return max_seq + 1


def find_ddrs_for_activity(resources_path: Path, activity_id: str) -> list[dict]:
    """Find DDRs referenced by a specific activity's decisions_addressed field."""
    framework_root = resources_path.parent.parent
    activity_lib = framework_root / "activity-library"

    # Search all activity YAML files for the given activity ID
    ddr_ids = []
    for phase_dir in ["research", "analysis"]:
        phase_path = activity_lib / phase_dir
        if not phase_path.exists():
            continue
        for yaml_file in phase_path.rglob("*.yaml"):
            try:
                data = read_yaml(yaml_file)
                if data.get("id") == activity_id:
                    ddr_ids = data.get("decisions_addressed", [])
                    break
            except Exception:
                continue
        if ddr_ids:
            break

    # Load each referenced DDR
    results = []
    for ddr_id in ddr_ids:
        ddr = load_ddr_by_id(resources_path, ddr_id)
        if ddr:
            results.append(ddr)
    return results


def format_summary(ddrs: list[dict]) -> str:
    """Format DDRs as a summary table."""
    if not ddrs:
        return "No DDRs found."

    lines = [
        f"{'ID':<12} {'Layer':<25} {'Archetype':<20} {'Decision Question'}",
        f"{'─'*12} {'─'*25} {'─'*20} {'─'*50}",
    ]
    for ddr in ddrs:
        question = ddr.get("decision_question", "").strip()[:60]
        lines.append(
            f"{ddr.get('id', 'N/A'):<12} "
            f"{ddr.get('layer', 'N/A'):<25} "
            f"{ddr.get('_archetype', 'N/A'):<20} "
            f"{question}"
        )
    return "\n".join(lines)


def format_detail(ddr: dict) -> str:
    """Format a single DDR in full detail."""
    import yaml as yaml_mod
    # Remove internal fields for display
    display = {k: v for k, v in ddr.items() if not k.startswith("_")}
    return yaml_mod.dump(display, default_flow_style=False, sort_keys=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR loader — list, search, and read DDRs")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    list_p = sub.add_parser("list", help="List DDRs")
    list_p.add_argument("--archetype", type=str, default=None, help="Filter by archetype")
    list_p.add_argument("--layer", type=int, default=None, help="Filter by layer number (1-9)")
    list_p.add_argument("--activity", type=str, default=None, help="Filter by activity ID")
    list_p.add_argument("--format", choices=["summary", "json", "ids"], default="summary")

    # search
    search_p = sub.add_parser("search", help="Search DDRs by keyword")
    search_p.add_argument("--query", type=str, required=True, help="Search query")
    search_p.add_argument("--archetype", type=str, default=None)
    search_p.add_argument("--layer", type=int, default=None)

    # read
    read_p = sub.add_parser("read", help="Read a specific DDR")
    read_p.add_argument("--id", type=str, required=True, help="DDR ID (e.g., DDR-L3-002)")
    read_p.add_argument("--archetype", type=str, default=None)
    read_p.add_argument("--format", choices=["yaml", "json"], default="yaml")

    # next-id
    next_p = sub.add_parser("next-id", help="Get next available DDR sequence number")
    next_p.add_argument("--archetype", type=str, required=True)
    next_p.add_argument("--layer", type=int, required=True)

    # list-archetypes
    sub.add_parser("list-archetypes", help="List all available archetypes with decisions")

    args = parser.parse_args()
    resources_path = resolve_resources_path()

    if args.command == "list":
        if args.activity:
            ddrs = find_ddrs_for_activity(resources_path, args.activity)
        else:
            ddrs = load_all_ddrs(resources_path, args.archetype)
            if args.layer:
                ddrs = [d for d in ddrs if d.get("layer_number") == args.layer]

        if args.format == "summary":
            print(format_summary(ddrs))
        elif args.format == "json":
            clean = [{k: v for k, v in d.items() if not k.startswith("_")} for d in ddrs]
            json.dump(clean, sys.stdout, indent=2)
            print()
        elif args.format == "ids":
            for ddr in ddrs:
                print(ddr.get("id", ""))

    elif args.command == "search":
        results = search_ddrs(resources_path, args.query, args.archetype, args.layer)
        print(format_summary(results))

    elif args.command == "read":
        ddr = load_ddr_by_id(resources_path, args.id, args.archetype)
        if ddr is None:
            print(f"Error: DDR '{args.id}' not found", file=sys.stderr)
            sys.exit(1)
        if args.format == "yaml":
            print(format_detail(ddr))
        else:
            clean = {k: v for k, v in ddr.items() if not k.startswith("_")}
            json.dump(clean, sys.stdout, indent=2)
            print()

    elif args.command == "next-id":
        seq = get_next_sequence(resources_path, args.archetype, args.layer)
        result = {
            "archetype": args.archetype,
            "layer": args.layer,
            "next_sequence": seq,
            "next_id": f"DDR-L{args.layer}-{seq:03d}",
        }
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "list-archetypes":
        archetypes = discover_archetypes(resources_path)
        json.dump({"archetypes": archetypes}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
