#!/usr/bin/env python3
"""Load and query the RAPIDS activity library.

The activity library lives in the framework repo under activity-library/.
This module provides:
  - Registry loading and activity YAML parsing
  - Filtering by project type, phase, and dependencies
  - Activity DAG construction for execution ordering
  - Schema validation for contributed activities

Usage:
    aah run core.activities.loader list --project-type ai-infra-platforms
    aah run core.activities.loader validate
    aah run core.activities.loader dag --project-type ai-infra-platforms
"""

import argparse
import json
import sys
from pathlib import Path

import networkx as nx

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_activity_library_path(framework_root: Path | None = None) -> Path:
    """Resolve the activity-library/ directory in the framework repo."""
    if framework_root is None:
        framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root for activity library", file=sys.stderr)
        sys.exit(1)
    lib_path = framework_root / "activity-library"
    if not lib_path.is_dir():
        print(f"Error: activity library not found at {lib_path}", file=sys.stderr)
        sys.exit(1)
    return lib_path


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------

def load_registry(lib_path: Path | None = None) -> dict:
    """Load the master registry (_registry.yaml)."""
    if lib_path is None:
        lib_path = resolve_activity_library_path()
    registry_path = lib_path / "_registry.yaml"
    if not registry_path.exists():
        print(f"Error: registry not found at {registry_path}", file=sys.stderr)
        sys.exit(1)
    return read_yaml(registry_path)


# ---------------------------------------------------------------------------
# Activity loading
# ---------------------------------------------------------------------------

def load_activity(lib_path: Path, relative_path: str) -> dict:
    """Load a single activity YAML file."""
    full_path = lib_path / relative_path
    if not full_path.exists():
        print(f"Warning: activity file not found: {full_path}", file=sys.stderr)
        return {}
    return read_yaml(full_path)


def load_all_activities(lib_path: Path | None = None) -> list[dict]:
    """Load all activity definitions from the registry."""
    if lib_path is None:
        lib_path = resolve_activity_library_path()
    registry = load_registry(lib_path)
    activities = []
    for activity_id, meta in registry.get("activities", {}).items():
        file_path = meta.get("file", "")
        activity = load_activity(lib_path, file_path)
        if activity:
            activity["_registry_meta"] = meta
            activities.append(activity)
    return activities


def load_activities_for_project_type(
    project_types: list[str],
    lib_path: Path | None = None,
) -> list[dict]:
    """Load activities relevant to the given project type(s).

    Includes:
      - Activities whose project_types overlap with the given types
      - Shared activities (project_types contains "all")
    """
    if lib_path is None:
        lib_path = resolve_activity_library_path()
    all_activities = load_all_activities(lib_path)

    matched = []
    seen_ids = set()
    for activity in all_activities:
        act_types = activity.get("project_types", [])
        act_id = activity.get("id", "")

        # Include if shared ("all") or if any overlap with requested types
        is_shared = "all" in act_types
        has_overlap = bool(set(act_types) & set(project_types))

        if (is_shared or has_overlap) and act_id not in seen_ids:
            matched.append(activity)
            seen_ids.add(act_id)

    return matched


# ---------------------------------------------------------------------------
# Activity filtering
# ---------------------------------------------------------------------------

def filter_by_phase(activities: list[dict], phase: str) -> list[dict]:
    """Filter activities to a specific phase (research or analysis)."""
    return [a for a in activities if a.get("phase") == phase]


def get_activity_by_id(activities: list[dict], activity_id: str) -> dict | None:
    """Find a specific activity by ID."""
    for a in activities:
        if a.get("id") == activity_id:
            return a
    return None


# ---------------------------------------------------------------------------
# Activity DAG
# ---------------------------------------------------------------------------

def build_activity_dag(activities: list[dict]) -> nx.DiGraph:
    """Build a dependency DAG from a list of activities.

    Nodes are activity IDs. Edges go from dependency -> dependent.
    """
    G = nx.DiGraph()
    id_set = {a["id"] for a in activities if "id" in a}

    for activity in activities:
        aid = activity.get("id")
        if not aid:
            continue
        G.add_node(aid, name=activity.get("name", ""), phase=activity.get("phase", ""))
        for dep in activity.get("dependencies", []):
            if dep in id_set:
                G.add_edge(dep, aid)

    return G


def compute_activity_waves(activities: list[dict]) -> list[list[str]]:
    """Compute execution waves (topological generations) for activities."""
    G = build_activity_dag(activities)
    if not nx.is_directed_acyclic_graph(G):
        cycles = list(nx.simple_cycles(G))
        print(f"Error: activity dependency graph has cycles: {cycles}", file=sys.stderr)
        sys.exit(1)

    waves = []
    for generation in nx.topological_generations(G):
        waves.append(sorted(generation))
    return waves


def validate_activity_dag(activities: list[dict]) -> list[str]:
    """Validate the activity dependency graph. Returns list of error strings."""
    errors = []
    id_set = {a["id"] for a in activities if "id" in a}

    for activity in activities:
        aid = activity.get("id", "UNKNOWN")
        for dep in activity.get("dependencies", []):
            if dep not in id_set:
                errors.append(f"{aid}: dependency '{dep}' not found in loaded activities")

    G = build_activity_dag(activities)
    if not nx.is_directed_acyclic_graph(G):
        for cycle in nx.simple_cycles(G):
            errors.append(f"Cycle detected: {' -> '.join(cycle)}")

    return errors


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ["id", "name", "phase", "project_types", "description",
                   "rationale", "dependencies", "depth_levels",
                   "relevance_context"]

VALID_PHASES = {"research", "analysis", "brownfield", "deploy", "sustain"}
VALID_ACTIONS = {"web_search", "web_fetch", "codebase_analysis", "analyze",
                 "write_artifact", "validate"}
VALID_EFFORTS = {"minimal", "moderate", "significant"}


def validate_activity_schema(activity: dict) -> list[str]:
    """Validate a single activity against the schema. Returns error list."""
    errors = []
    aid = activity.get("id", "UNKNOWN")

    # Required fields
    for field in REQUIRED_FIELDS:
        if field not in activity:
            errors.append(f"{aid}: missing required field '{field}'")

    # Phase
    if activity.get("phase") not in VALID_PHASES:
        errors.append(f"{aid}: invalid phase '{activity.get('phase')}'")

    # Depth levels
    depth = activity.get("depth_levels", {})
    has_decisions_addressed = bool(activity.get("decisions_addressed"))
    for level in ("light", "standard", "deep"):
        if level not in depth:
            errors.append(f"{aid}: missing depth level '{level}'")
        elif "tasks" not in depth[level] and not has_decisions_addressed:
            errors.append(f"{aid}: depth level '{level}' missing tasks list")

    # Tasks (optional when decisions_addressed is present)
    tasks = activity.get("tasks", [])
    task_ids = set()
    if tasks:
        for task in tasks:
            tid = task.get("id")
            if not tid:
                errors.append(f"{aid}: task missing 'id'")
                continue
            if tid in task_ids:
                errors.append(f"{aid}: duplicate task id '{tid}'")
            task_ids.add(tid)
            if task.get("ddr_ref"):
                pass
            elif task.get("action") not in VALID_ACTIONS:
                errors.append(f"{aid}.{tid}: invalid action '{task.get('action')}'")
            if task.get("action") == "write_artifact" and not task.get("template"):
                errors.append(f"{aid}.{tid}: write_artifact task missing 'template'")

        # Depth task references
        for level_name, level_def in depth.items():
            for ref_tid in level_def.get("tasks", []):
                if ref_tid not in task_ids:
                    errors.append(f"{aid}: depth '{level_name}' references unknown task '{ref_tid}'")

        # Task internal dependencies
        for task in tasks:
            for dep_tid in task.get("depends_on", []):
                if dep_tid not in task_ids:
                    errors.append(f"{aid}.{task.get('id')}: depends_on unknown task '{dep_tid}'")
    elif not has_decisions_addressed:
        errors.append(f"{aid}: must have 'tasks' or 'decisions_addressed'")

    # Artifacts — required for non-DDR activities, optional for DDR-backed
    has_ddr_tasks = any(t.get("ddr_ref") for t in tasks)
    artifacts = activity.get("artifacts", [])
    if not artifacts and not has_ddr_tasks and not has_decisions_addressed:
        errors.append(f"{aid}: must have at least one artifact")
    for art in artifacts:
        if not art.get("template"):
            errors.append(f"{aid}: artifact missing 'template'")
        if not art.get("sections"):
            errors.append(f"{aid}: artifact missing 'sections'")

    # Estimated effort
    effort = activity.get("estimated_effort")
    if effort and effort not in VALID_EFFORTS:
        errors.append(f"{aid}: invalid estimated_effort '{effort}'")

    return errors


def validate_all_activities(lib_path: Path | None = None) -> dict:
    """Validate all activities in the library. Returns summary dict."""
    activities = load_all_activities(lib_path)
    all_errors = {}
    for activity in activities:
        errors = validate_activity_schema(activity)
        if errors:
            all_errors[activity.get("id", "UNKNOWN")] = errors

    dag_errors = validate_activity_dag(activities)

    return {
        "total_activities": len(activities),
        "valid": len(activities) - len(all_errors),
        "invalid": len(all_errors),
        "schema_errors": all_errors,
        "dag_errors": dag_errors,
    }


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def activity_to_summary(activity: dict) -> dict:
    """Produce a compact summary of an activity for LLM consumption."""
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "phase": activity.get("phase"),
        "project_types": activity.get("project_types", []),
        "description": activity.get("description", "").strip(),
        "rationale": activity.get("rationale", "").strip(),
        "dependencies": activity.get("dependencies", []),
        "estimated_effort": activity.get("estimated_effort", "moderate"),
        "relevance_context": activity.get("relevance_context", "").strip(),
        "depth_levels": {
            level: info.get("description", "").strip()
            for level, info in activity.get("depth_levels", {}).items()
        },
        "artifact_count": len(activity.get("artifacts", [])),
        "task_count": len(activity.get("tasks", [])),
        "tags": activity.get("tags", []),
    }


def activities_to_context(activities: list[dict]) -> list[dict]:
    """Convert a list of activities to LLM-friendly context summaries."""
    return [activity_to_summary(a) for a in activities]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS Activity Library Loader")
    sub = parser.add_subparsers(dest="command", required=True)

    # list — list activities for a project type
    list_p = sub.add_parser("list", help="List activities for a project type")
    list_p.add_argument("--project-type", type=str, nargs="+", required=True,
                        help="One or more project types")
    list_p.add_argument("--phase", type=str, choices=["research", "analysis"],
                        help="Filter to a specific phase")
    list_p.add_argument("--format", type=str, choices=["summary", "full", "ids"],
                        default="summary", help="Output format")

    # validate — validate all activities
    sub.add_parser("validate", help="Validate all activities against schema")

    # dag — show activity DAG
    dag_p = sub.add_parser("dag", help="Show activity DAG for a project type")
    dag_p.add_argument("--project-type", type=str, nargs="+", required=True)

    # context — produce LLM-ready context for recommendation
    ctx_p = sub.add_parser("context", help="Produce LLM-ready activity context")
    ctx_p.add_argument("--project-type", type=str, nargs="+", required=True)

    args = parser.parse_args()

    if args.command == "list":
        activities = load_activities_for_project_type(args.project_type)
        if args.phase:
            activities = filter_by_phase(activities, args.phase)

        if args.format == "ids":
            result = [a.get("id") for a in activities]
        elif args.format == "full":
            result = activities
        else:
            result = activities_to_context(activities)

        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "validate":
        result = validate_all_activities()
        json.dump(result, sys.stdout, indent=2)
        print()
        if result["invalid"] > 0 or result["dag_errors"]:
            sys.exit(1)

    elif args.command == "dag":
        activities = load_activities_for_project_type(args.project_type)
        waves = compute_activity_waves(activities)
        dag_errors = validate_activity_dag(activities)

        result = {
            "project_types": args.project_type,
            "total_activities": len(activities),
            "waves": [
                {
                    "wave": i + 1,
                    "activities": [
                        {"id": aid, "name": (get_activity_by_id(activities, aid) or {}).get("name", "")}
                        for aid in wave
                    ],
                }
                for i, wave in enumerate(waves)
            ],
            "dag_errors": dag_errors,
        }
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "context":
        activities = load_activities_for_project_type(args.project_type)
        result = {
            "project_types": args.project_type,
            "activities": activities_to_context(activities),
            "waves": compute_activity_waves(activities),
        }
        json.dump(result, sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()
