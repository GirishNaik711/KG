#!/usr/bin/env python3
"""CRUD operations for Domain Briefs (for the /aah-domain-briefs skill).

This module provides programmatic primitives; the skill wraps them in
interactive AskUserQuestion flows. Each operation preserves taxonomy
integrity — add-node also updates _taxonomy.yaml, remove-node checks
for children first.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.domain_briefs.loader import (
    find_briefs_root,
    load_taxonomy,
)


def _taxonomy_path() -> Optional[Path]:
    root = find_briefs_root()
    if root is None:
        return None
    return root / "_taxonomy.yaml"


def add_node(
    node_id: str,
    name: str,
    description: str,
    *,
    keywords_strong: Optional[list[str]] = None,
    keywords_moderate: Optional[list[str]] = None,
    typical_horizontals: Optional[list[str]] = None,
    sample_problem_statements: Optional[list[str]] = None,
    processes: Optional[list[dict]] = None,
    data_entities: Optional[list[dict]] = None,
    glossary: Optional[list[dict]] = None,
    typical_integrations: Optional[list[str]] = None,
    regulations: Optional[list[dict]] = None,
    kpis: Optional[list[str]] = None,
    common_patterns: Optional[list[dict]] = None,
    anti_patterns: Optional[list[str]] = None,
    reference_frameworks: Optional[list[dict]] = None,
) -> dict:
    """Create a new node.yaml and register it in _taxonomy.yaml."""
    root = find_briefs_root()
    if root is None:
        raise RuntimeError("domain-briefs/ directory not found")

    parts = node_id.split("/")
    level = len(parts)
    parent_id = "/".join(parts[:-1]) if level > 1 else None

    # Write the brief file
    brief_dir = root / node_id
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "domain-brief.yaml"
    if brief_path.exists():
        raise FileExistsError(f"{brief_path} already exists")

    doc: dict = {
        "id": node_id,
        "parent_id": parent_id,
        "level": level,
        "name": name,
        "description": description,
    }
    if typical_horizontals:
        doc["typical_horizontals"] = typical_horizontals
    if keywords_strong or keywords_moderate:
        doc["keywords"] = {
            "strong": keywords_strong or [],
            "moderate": keywords_moderate or [],
        }
    if sample_problem_statements:
        doc["sample_problem_statements"] = sample_problem_statements
    if reference_frameworks:
        doc["reference_frameworks"] = reference_frameworks
    if processes:
        doc["processes"] = processes
    if data_entities:
        doc["data_entities"] = data_entities
    if glossary:
        doc["glossary"] = glossary
    if typical_integrations:
        doc["typical_integrations"] = typical_integrations
    if regulations:
        doc["regulations"] = regulations
    if kpis:
        doc["kpis"] = kpis
    if common_patterns:
        doc["common_patterns"] = common_patterns
    if anti_patterns:
        doc["anti_patterns"] = anti_patterns

    write_yaml(doc, brief_path)

    # Update _taxonomy.yaml
    _insert_into_taxonomy(node_id)

    return {"id": node_id, "path": str(brief_path.relative_to(root)), "level": level}


def _insert_into_taxonomy(node_id: str) -> None:
    """Insert node_id into _taxonomy.yaml under its parent."""
    taxonomy_path = _taxonomy_path()
    if taxonomy_path is None:
        return

    taxonomy = load_taxonomy()
    parts = node_id.split("/")

    def find_or_create(nodes: list, path_idx: int) -> dict:
        partial = "/".join(parts[: path_idx + 1])
        for node in nodes:
            if node["id"] == partial:
                if path_idx == len(parts) - 1:
                    return node
                return find_or_create(node.setdefault("children", []), path_idx + 1)
        new_node = {
            "id": partial,
            "file": f"{partial}/domain-brief.yaml",
        }
        nodes.append(new_node)
        if path_idx == len(parts) - 1:
            return new_node
        return find_or_create(new_node.setdefault("children", []), path_idx + 1)

    industries = taxonomy.setdefault("industries", [])
    find_or_create(industries, 0)

    write_yaml(taxonomy, taxonomy_path)


def remove_node(node_id: str, *, force: bool = False) -> dict:
    """Remove a node and its brief file. Fails if the node has children unless force=True."""
    root = find_briefs_root()
    if root is None:
        raise RuntimeError("domain-briefs/ directory not found")

    taxonomy = load_taxonomy()

    def prune(nodes: list) -> tuple[list, Optional[dict]]:
        remaining: list = []
        removed: Optional[dict] = None
        for node in nodes:
            if node["id"] == node_id:
                children = node.get("children") or []
                if children and not force:
                    raise ValueError(
                        f"{node_id} has {len(children)} child node(s); "
                        "pass force=True to cascade delete"
                    )
                removed = node
                continue
            new_children, child_removed = prune(node.get("children") or [])
            node["children"] = new_children
            if child_removed is not None:
                removed = child_removed
            remaining.append(node)
        return remaining, removed

    industries, removed = prune(taxonomy.get("industries") or [])
    if removed is None:
        raise ValueError(f"node not found: {node_id}")

    taxonomy["industries"] = industries
    tx_path = _taxonomy_path()
    if tx_path:
        write_yaml(taxonomy, tx_path)

    # Remove the brief file (and directory if empty)
    brief_path = root / node_id / "domain-brief.yaml"
    if brief_path.is_file():
        brief_path.unlink()
    brief_dir = root / node_id
    if brief_dir.is_dir():
        try:
            brief_dir.rmdir()  # succeeds only if empty
        except OSError:
            pass

    return {"removed": node_id}


def reindex() -> dict:
    """Rebuild _taxonomy.yaml from filesystem. Useful after manual edits."""
    root = find_briefs_root()
    if root is None:
        raise RuntimeError("domain-briefs/ directory not found")

    found: list[str] = []
    for brief in root.rglob("domain-brief.yaml"):
        doc = read_yaml(brief)
        if "id" in doc:
            found.append(doc["id"])

    # Build nested structure
    tree: dict = {"industries": []}
    index: dict[str, dict] = {}

    def ensure(nid: str) -> dict:
        if nid in index:
            return index[nid]
        node = {"id": nid, "file": f"{nid}/domain-brief.yaml"}
        index[nid] = node
        parts = nid.split("/")
        if len(parts) == 1:
            tree["industries"].append(node)
        else:
            parent = ensure("/".join(parts[:-1]))
            parent.setdefault("children", []).append(node)
        return node

    for nid in sorted(found, key=lambda s: (s.count("/"), s)):
        ensure(nid)

    tree["taxonomy_version"] = "1.0"
    from datetime import date
    tree["last_updated"] = date.today().isoformat()
    # Reorder so taxonomy_version/last_updated come first
    ordered = {
        "taxonomy_version": tree["taxonomy_version"],
        "last_updated": tree["last_updated"],
        "industries": tree["industries"],
    }

    tx_path = _taxonomy_path()
    if tx_path:
        write_yaml(ordered, tx_path)

    return {"indexed": len(found)}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add a new node (expects JSON on stdin)")
    add_parser.add_argument("--id", required=True)
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--description", required=True)

    rm_parser = sub.add_parser("remove", help="Remove a node")
    rm_parser.add_argument("--id", required=True)
    rm_parser.add_argument("--force", action="store_true")

    sub.add_parser("reindex", help="Rebuild _taxonomy.yaml from filesystem")

    args = parser.parse_args()

    if args.command == "add":
        # Read extended fields from stdin JSON if provided (non-interactive use)
        extras: dict = {}
        if not sys.stdin.isatty():
            try:
                extras = json.loads(sys.stdin.read() or "{}")
            except json.JSONDecodeError:
                extras = {}
        result = add_node(
            node_id=args.id,
            name=args.name,
            description=args.description,
            keywords_strong=extras.get("keywords_strong"),
            keywords_moderate=extras.get("keywords_moderate"),
            typical_horizontals=extras.get("typical_horizontals"),
            sample_problem_statements=extras.get("sample_problem_statements"),
            processes=extras.get("processes"),
            data_entities=extras.get("data_entities"),
            glossary=extras.get("glossary"),
            typical_integrations=extras.get("typical_integrations"),
            regulations=extras.get("regulations"),
            kpis=extras.get("kpis"),
            common_patterns=extras.get("common_patterns"),
            anti_patterns=extras.get("anti_patterns"),
            reference_frameworks=extras.get("reference_frameworks"),
        )
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.command == "remove":
        result = remove_node(args.id, force=args.force)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.command == "reindex":
        result = reindex()
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
