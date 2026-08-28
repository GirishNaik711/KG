#!/usr/bin/env python3
"""Load and validate the AAH ports framework catalog.

The catalog lives in the framework repo at
``aah/_resources/_references/_ports/_registry.yaml``. It is the read-only DEFINITION
of every non-core activity that can plug into the spine (design §7.1). This
module mirrors ``core.activities.loader`` conventions:

  - ``resolve_framework_root`` for path resolution (package-relative)
  - ``read_yaml`` for parsing
  - a ``validate`` entrypoint that returns a structured error report

Usage:
    aah run core.ports.catalog validate
    aah run core.ports.catalog list
    aah run core.ports.catalog show --id ACT-UX
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml


VALID_POSITIONS = {"before", "within", "after"}
VALID_TYPES = {"skill", "agent", "workflow", "plugin"}
VALID_TRIGGER_TYPES = {"default", "discuss"}
VALID_IMPL_STATUS = {"available", "stub"}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def catalog_path(framework_root: Path | None = None) -> Path:
    """Resolve aah/_resources/_references/_ports/_registry.yaml in the framework repo."""
    if framework_root is None:
        framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root for ports catalog", file=sys.stderr)
        sys.exit(1)
    # resolve_framework_root() returns the aah/ package dir.
    return framework_root / "_resources" / "_references" / "_ports" / "_registry.yaml"


def load_catalog(path: Path | None = None) -> dict:
    """Load the ports catalog. Callers should treat the result as read-only."""
    p = path or catalog_path()
    if not p.exists():
        print(f"Error: ports catalog not found at {p}", file=sys.stderr)
        sys.exit(1)
    return read_yaml(p)


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def activities(catalog: dict) -> list[dict]:
    return list(catalog.get("activities", []) or [])


def spine_order(catalog: dict) -> list[str]:
    return list(catalog.get("spine_order", []) or [])


def node_anchors(catalog: dict) -> dict:
    """Raw node_anchors mapping. Each node maps to a list whose entries are
    either a plain anchor-name string OR a {name, description} dict (the
    description says WHERE in the node's flow the anchor sits — used by the
    within-port plan so a core skill can self-localize without inline markers)."""
    return dict(catalog.get("node_anchors", {}) or {})


def anchor_names(catalog: dict, node: str) -> list[str]:
    """Anchor names a node exposes (handles both string and dict entry forms)."""
    out: list[str] = []
    for entry in (node_anchors(catalog).get(node) or []):
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict) and entry.get("name"):
            out.append(entry["name"])
    return out


def anchor_description(node_anchors_map: dict, node: str, anchor: str) -> str | None:
    """Location description for (node, anchor), or None. Accepts a raw
    node_anchors mapping (from the catalog OR a project registry snapshot)."""
    for entry in (node_anchors_map.get(node) or []):
        if isinstance(entry, dict) and entry.get("name") == anchor:
            return entry.get("description")
    return None


def triggers(activity: dict) -> list[dict]:
    """Normalize triggered_by to a list of trigger dicts.

    triggered_by may be a single {type, slug_id, option} map OR a list of such
    maps (an activity that fires from more than one slug answer). Returns [] when
    absent."""
    tb = activity.get("triggered_by")
    if tb is None:
        return []
    if isinstance(tb, list):
        return [t for t in tb if isinstance(t, dict)]
    return [tb]


def default_activities(catalog: dict) -> list[dict]:
    """Activities with any triggered_by.type == default (materialized at init)."""
    return [a for a in activities(catalog)
            if any(t.get("type") == "default" for t in triggers(a))]


def discuss_activities(catalog: dict) -> list[dict]:
    """Activities with any triggered_by.type == discuss (merged at compile)."""
    return [a for a in activities(catalog)
            if any(t.get("type") == "discuss" for t in triggers(a))]


def get_by_id(catalog: dict, activity_id: str) -> dict | None:
    for a in activities(catalog):
        if a.get("id") == activity_id:
            return a
    return None


def is_stub(activity: dict) -> bool:
    return activity.get("impl_status", "available") == "stub"


# ---------------------------------------------------------------------------
# Validation (design template checklist)
# ---------------------------------------------------------------------------

def validate_catalog(catalog: dict) -> list[str]:
    """Validate the catalog against the schema. Returns a list of error strings."""
    errors: list[str] = []

    spine = spine_order(catalog)
    acts = activities(catalog)

    if not spine:
        errors.append("catalog missing spine_order")

    seen_ids: set[str] = set()
    id_to_coord: dict[str, tuple] = {}

    for a in acts:
        aid = a.get("id", "UNKNOWN")

        # id uniqueness
        if aid in seen_ids:
            errors.append(f"{aid}: duplicate id")
        seen_ids.add(aid)

        # type
        if a.get("type") not in VALID_TYPES:
            errors.append(f"{aid}: invalid type '{a.get('type')}' (want one of {sorted(VALID_TYPES)})")

        # impl_status
        impl = a.get("impl_status", "available")
        if impl not in VALID_IMPL_STATUS:
            errors.append(f"{aid}: invalid impl_status '{impl}'")

        # target
        target = a.get("target")
        if target not in spine:
            errors.append(f"{aid}: target '{target}' not in spine_order")

        # position
        position = a.get("position")
        if position not in VALID_POSITIONS:
            errors.append(f"{aid}: invalid position '{position}'")

        # anchor rules
        anchor = a.get("anchor")
        if position == "within":
            if not anchor:
                errors.append(f"{aid}: position=within requires a non-null anchor")
            elif anchor not in anchor_names(catalog, target):
                errors.append(f"{aid}: anchor '{anchor}' not in node_anchors[{target}]")
        elif anchor:
            errors.append(f"{aid}: anchor '{anchor}' set but position != within")

        # triggered_by (a single trigger map or a list of them)
        trigs = triggers(a)
        if not trigs:
            errors.append(f"{aid}: missing triggered_by")
        for tb in trigs:
            ttype = tb.get("type")
            if ttype not in VALID_TRIGGER_TYPES:
                errors.append(f"{aid}: invalid triggered_by.type '{ttype}'")
            if ttype == "discuss":
                if not tb.get("slug_id") or tb.get("option") in (None, ""):
                    errors.append(f"{aid}: discuss trigger needs both slug_id and option")

        id_to_coord[aid] = (target, position)

    # depends_on: exists + same coordinate
    for a in acts:
        aid = a.get("id", "UNKNOWN")
        coord = id_to_coord.get(aid)
        for dep in a.get("depends_on", []) or []:
            if dep not in seen_ids:
                errors.append(f"{aid}: depends_on '{dep}' does not exist")
            elif id_to_coord.get(dep) != coord:
                errors.append(
                    f"{aid}: depends_on '{dep}' is at a different (target, position) "
                    f"coordinate — cross-coordinate deps are not allowed in v1"
                )

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AAH ports catalog loader/validator")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="Validate the catalog against the schema")
    sub.add_parser("list", help="List activity ids + coordinates")

    p_show = sub.add_parser("show", help="Dump one activity as JSON")
    p_show.add_argument("--id", required=True)

    args = parser.parse_args()
    catalog = load_catalog()

    if args.command == "validate":
        errors = validate_catalog(catalog)
        result = {
            "total_activities": len(activities(catalog)),
            "defaults": len(default_activities(catalog)),
            "discuss_triggered": len(discuss_activities(catalog)),
            "errors": errors,
            "valid": not errors,
        }
        print(json.dumps(result, indent=2))
        sys.exit(0 if not errors else 1)

    elif args.command == "list":
        rows = [
            {
                "id": a.get("id"),
                "ref": a.get("ref"),
                "type": a.get("type"),
                "impl_status": a.get("impl_status", "available"),
                "target": a.get("target"),
                "position": a.get("position"),
                "anchor": a.get("anchor"),
                "trigger": ",".join(sorted({t.get("type") for t in triggers(a) if t.get("type")})) or None,
            }
            for a in activities(catalog)
        ]
        print(json.dumps(rows, indent=2))

    elif args.command == "show":
        a = get_by_id(catalog, args.id)
        if a is None:
            print(f"Error: no activity with id '{args.id}'", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(a, indent=2))


if __name__ == "__main__":
    main()
