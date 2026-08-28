#!/usr/bin/env python3
"""Validate the Domain Brief library.

Checks:
  - _taxonomy.yaml references files that exist
  - Every domain-brief.yaml on disk is indexed in _taxonomy.yaml
  - Each brief declares id, parent_id, level, name, description
  - Each brief's id matches its taxonomy position
  - parent_id matches the actual parent in the taxonomy tree
  - level matches path depth
  - No circular references (inherent if ids are slash-paths)
  - Leaf keyword uniqueness (warn if two leaves share a strong keyword)
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml
from aah.core.domain_briefs.loader import find_briefs_root, load_taxonomy


REQUIRED_FIELDS = ("id", "parent_id", "level", "name", "description")


def validate() -> dict:
    root = find_briefs_root()
    if root is None:
        return {
            "ok": False,
            "errors": ["domain-briefs/ directory not found"],
            "warnings": [],
            "node_count": 0,
            "leaf_count": 0,
        }

    taxonomy = load_taxonomy()
    errors: list[str] = []
    warnings: list[str] = []
    indexed: dict[str, Path] = {}
    parents: dict[str, str | None] = {}
    leaves: set[str] = set()

    def visit(node: dict, parent_id: str | None) -> None:
        nid = node.get("id")
        file_rel = node.get("file")
        if not nid:
            errors.append("taxonomy node missing `id`")
            return
        if not file_rel:
            errors.append(f"taxonomy[{nid}]: missing `file`")
            return
        indexed[nid] = root / file_rel
        parents[nid] = parent_id
        children = node.get("children") or []
        if not children:
            leaves.add(nid)
        for child in children:
            visit(child, nid)

    for industry in taxonomy.get("industries") or []:
        visit(industry, None)

    # Check every file exists + schema
    brief_docs: dict[str, dict] = {}
    for nid, path in indexed.items():
        if not path.is_file():
            errors.append(f"taxonomy[{nid}]: file missing at {path}")
            continue
        try:
            doc = read_yaml(path)
        except Exception as exc:
            errors.append(f"taxonomy[{nid}]: YAML parse error — {exc}")
            continue
        brief_docs[nid] = doc
        for field in REQUIRED_FIELDS:
            if field not in doc:
                errors.append(f"{path.name} ({nid}): missing required field `{field}`")
        if doc.get("id") != nid:
            errors.append(
                f"{path.name}: declared id `{doc.get('id')}` does not match taxonomy position `{nid}`"
            )
        expected_parent = parents.get(nid)
        actual_parent = doc.get("parent_id")
        if expected_parent != actual_parent:
            errors.append(
                f"{nid}: parent_id in brief is `{actual_parent}` but taxonomy parent is `{expected_parent}`"
            )
        expected_level = nid.count("/") + 1
        actual_level = doc.get("level")
        if actual_level != expected_level:
            errors.append(
                f"{nid}: level={actual_level} does not match path depth ({expected_level})"
            )

    # Check orphans: any .yaml under domain-briefs/ not indexed
    allowed = {p.resolve() for p in indexed.values()}
    allowed.add((root / "_taxonomy.yaml").resolve())
    allowed.add((root / "BRIEF_SCHEMA.yaml").resolve())
    for f in root.rglob("*.yaml"):
        if f.resolve() not in allowed:
            warnings.append(f"orphan: {f.relative_to(root)} not indexed in _taxonomy.yaml")

    # Keyword uniqueness across leaves
    kw_owners: dict[str, list[str]] = {}
    for nid, doc in brief_docs.items():
        if nid not in leaves:
            continue
        for kw in (doc.get("keywords") or {}).get("strong") or []:
            kw_owners.setdefault(kw.lower(), []).append(nid)
    for kw, owners in kw_owners.items():
        if len(owners) > 1:
            warnings.append(
                f"strong keyword '{kw}' appears on multiple leaves: {', '.join(owners)}"
            )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "node_count": len(indexed),
        "leaf_count": len(leaves),
    }


def main() -> None:
    report = validate()
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
