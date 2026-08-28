#!/usr/bin/env python3
"""Domain Brief loader with ancestor-merge inheritance.

The taxonomy is a tree of nodes keyed by slash-separated paths. Calling
`load_node(id)` returns the leaf's content merged with all ancestors'
content — union of list fields with leaf precedence for duplicates,
scalars from leaf only.
"""

import sys
from pathlib import Path
from typing import Iterable, Optional

from aah.core.common.io_utils import read_yaml


# Fields that are lists of dicts keyed by `name` or `term`.
_NAMED_LIST_FIELDS = {
    "processes": "name",
    "data_entities": "name",
    "glossary": "term",
    "regulations": "name",
    "common_patterns": "name",
    "reference_frameworks": "name",
}

# Fields that are simple lists of strings.
_STRING_LIST_FIELDS = (
    "typical_horizontals",
    "typical_integrations",
    "kpis",
    "anti_patterns",
    "sample_problem_statements",
)


def find_briefs_root() -> Optional[Path]:
    """Locate the domain-briefs/ directory by walking up from this file."""
    current = Path(__file__).resolve()
    for parent in [current, *current.parents]:
        candidate = parent / "domain-briefs"
        if candidate.is_dir() and (candidate / "_taxonomy.yaml").is_file():
            return candidate
    return None


def load_taxonomy() -> dict:
    """Load the raw _taxonomy.yaml content."""
    root = find_briefs_root()
    if root is None:
        return {"industries": []}
    return read_yaml(root / "_taxonomy.yaml")


def _walk_nodes(nodes: Iterable[dict]) -> Iterable[dict]:
    """Yield every node (with its children) by depth-first traversal."""
    for node in nodes:
        yield node
        yield from _walk_nodes(node.get("children") or [])


def is_stub(node_id: str) -> bool:
    """True if the node is a stub (own content empty; inherits from parent).

    Reads the `stub` flag from the taxonomy index (cheap — one file). Stub nodes
    are excluded from candidate enumeration/ranking but remain loadable via
    load_node (ancestor-merge). See BRIEF_SCHEMA.yaml `stub`.
    """
    taxonomy = load_taxonomy()
    for n in _walk_nodes(taxonomy.get("industries") or []):
        if n.get("id") == node_id:
            return bool(n.get("stub"))
    return False


def list_all_ids(include_stubs: bool = False) -> list[str]:
    """Return every node id in taxonomy order (stubs excluded by default)."""
    taxonomy = load_taxonomy()
    return [
        n["id"]
        for n in _walk_nodes(taxonomy.get("industries") or [])
        if include_stubs or not n.get("stub")
    ]


def list_leaf_ids(include_stubs: bool = False) -> list[str]:
    """Return only leaf node ids (no children; stubs excluded by default)."""
    taxonomy = load_taxonomy()
    leaves: list[str] = []

    def visit(node: dict) -> None:
        children = node.get("children") or []
        if not children:
            if include_stubs or not node.get("stub"):
                leaves.append(node["id"])
        else:
            for c in children:
                visit(c)

    for industry in taxonomy.get("industries") or []:
        visit(industry)
    return leaves


def get_ancestors(node_id: str) -> list[str]:
    """Return ancestor ids for a node, root-first. Excludes the node itself."""
    parts = node_id.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def _locate_node_file(root: Path, node_id: str) -> Optional[Path]:
    """Find the domain-brief.yaml for a given node id by looking up _taxonomy.yaml."""
    taxonomy = load_taxonomy()

    def visit(node: dict) -> Optional[str]:
        if node["id"] == node_id:
            return node.get("file")
        for child in node.get("children") or []:
            result = visit(child)
            if result:
                return result
        return None

    for industry in taxonomy.get("industries") or []:
        file_rel = visit(industry)
        if file_rel:
            return root / file_rel
    return None


def _merge(acc: dict, node: dict) -> dict:
    """Merge node into acc. List fields are unioned; scalars are replaced."""
    for field, key in _NAMED_LIST_FIELDS.items():
        incoming = node.get(field) or []
        if not incoming:
            continue
        existing = acc.setdefault(field, [])
        seen = {
            item.get(key): idx
            for idx, item in enumerate(existing)
            if isinstance(item, dict) and item.get(key)
        }
        for item in incoming:
            if not isinstance(item, dict):
                continue
            name = item.get(key)
            if name and name in seen:
                # leaf precedence: replace
                existing[seen[name]] = item
            else:
                existing.append(item)
                if name:
                    seen[name] = len(existing) - 1

    for field in _STRING_LIST_FIELDS:
        incoming = node.get(field) or []
        if not incoming:
            continue
        existing = acc.setdefault(field, [])
        seen = set(existing)
        for item in incoming:
            if item not in seen:
                existing.append(item)
                seen.add(item)

    # Scalars always taken from the last merged node (leaf wins)
    for field in ("id", "parent_id", "level", "name", "description"):
        if field in node:
            acc[field] = node[field]

    # Keywords: union of strong and moderate
    incoming_kw = node.get("keywords") or {}
    if incoming_kw:
        acc_kw = acc.setdefault("keywords", {"strong": [], "moderate": []})
        for bucket in ("strong", "moderate"):
            for kw in incoming_kw.get(bucket) or []:
                if kw not in acc_kw[bucket]:
                    acc_kw[bucket].append(kw)

    return acc


def load_node(node_id: str) -> Optional[dict]:
    """Load a node with merged ancestor content.

    Returns None if the node id is not in the taxonomy or its file is missing.
    """
    root = find_briefs_root()
    if root is None:
        return None

    ancestors = get_ancestors(node_id)
    ids_to_load = ancestors + [node_id]

    merged: dict = {}
    loaded_any = False
    for nid in ids_to_load:
        path = _locate_node_file(root, nid)
        if path is None or not path.is_file():
            # Skip if ancestor file is missing — shouldn't happen in validated taxonomies
            continue
        node = read_yaml(path)
        _merge(merged, node)
        loaded_any = True

    if not loaded_any:
        return None

    # After merge, id/name should be the leaf's, not the root's
    leaf_path = _locate_node_file(root, node_id)
    if leaf_path and leaf_path.is_file():
        leaf = read_yaml(leaf_path)
        for scalar in ("id", "parent_id", "level", "name", "description"):
            if scalar in leaf:
                merged[scalar] = leaf[scalar]

    return merged


def list_files(node_id: str) -> list[str]:
    """Return the list of domain-brief.yaml file paths that load_node() reads for a given node_id.

    Paths are relative to the harness root (e.g., "domain-briefs/financial-services/domain-brief.yaml").
    """
    root = find_briefs_root()
    if root is None:
        return []

    ancestors = get_ancestors(node_id)
    ids_to_load = ancestors + [node_id]

    files = []
    for nid in ids_to_load:
        path = _locate_node_file(root, nid)
        if path is not None and path.is_file():
            # Make path relative to the harness root (parent of domain-briefs/)
            harness_root = root.parent
            rel_path = str(path.relative_to(harness_root))
            files.append(rel_path)

    return files


def main() -> None:
    """CLI: `aah run core.domain_briefs.loader <command>`."""
    import argparse
    import json

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-leaves", help="List all leaf node ids")
    sub.add_parser("list-all", help="List all node ids")
    load_parser = sub.add_parser("load", help="Load a node with ancestor merge")
    load_parser.add_argument("--id", required=True, help="Node id")
    files_parser = sub.add_parser("list-files", help="List domain-brief files loaded for a node")
    files_parser.add_argument("--id", required=True, help="Node id")

    args = parser.parse_args()

    if args.command == "list-leaves":
        for nid in list_leaf_ids():
            print(nid)
    elif args.command == "list-all":
        for nid in list_all_ids():
            print(nid)
    elif args.command == "load":
        node = load_node(args.id)
        if node is None:
            print(f"node not found: {args.id}", file=sys.stderr)
            sys.exit(1)
        json.dump(node, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    elif args.command == "list-files":
        files = list_files(args.id)
        if not files:
            print(f"no files found for: {args.id}", file=sys.stderr)
            sys.exit(1)
        for f in files:
            print(f)


if __name__ == "__main__":
    main()
