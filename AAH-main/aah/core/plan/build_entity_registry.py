#!/usr/bin/env python3
"""Generate `.aah/entity-registry.json` from feature YAMLs.

Issue #247, Phase 3 L5-D. Companion to ``mock_data_consistency_guard.py``
which BLOCKS fixture writes that introduce un-registered entity names.

Sources:
  * Walk ``.aah/plan/features/*.yaml``. Each feature YAML may
    contain an optional top-level ``entities:`` block:

    .. code-block:: yaml

        id: F012
        entities:
          - name: plant
            type: postgres_table
          - name: tenant
            type: postgres_table

    Names are aggregated, lowercased, and deduplicated.

  * **Brownfield supplement**: walk existing fixtures
    (``**/*mock*.json``, ``**/*fixtures*.json``) and collect
    candidate entity-name strings (under ``*_id`` / ``*_name`` keys
    or capitalized identifier-shaped values). Adds them to the
    registry so the guard treats them as already-canonical.

The registry is **idempotent**: re-running rebuilds from sources.
The user may add entities by hand; the next build will overwrite,
so prefer adding to the source feature YAMLs.

Usage:
    aah run core.plan.build_entity_registry build [--project-path .]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json, read_yaml, write_json


_IDENT = re.compile(r"^[A-Za-z][\w\-]{1,40}$")
_NAMEISH = re.compile(r"(_id|_name|_type|Type|Name|Id)$")


def _walk_for_entities(node, out: list[str], parent_key: str = "") -> None:
    """Walk a parsed-JSON node collecting candidate entity names —
    same heuristic as mock_data_consistency_guard's walker."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                s = v.strip()
                if s and len(s) <= 60 and (
                    (parent_key and _NAMEISH.search(parent_key)) or _IDENT.match(s)
                ):
                    out.append(s)
            else:
                _walk_for_entities(v, out, parent_key=k)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                if _IDENT.match(item.strip()):
                    out.append(item.strip())
            else:
                _walk_for_entities(item, out, parent_key=parent_key)


def _entities_from_feature_yamls(rapids_path: Path) -> set[str]:
    """Aggregate `entities:` block names across all feature YAMLs."""
    features_dir = rapids_path / "plan" / "features"
    if not features_dir.is_dir():
        return set()
    out: set[str] = set()
    for yaml_path in sorted(features_dir.glob("*.yaml")):
        try:
            data = read_yaml(yaml_path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        block = data.get("entities", []) or []
        if not isinstance(block, list):
            continue
        for entry in block:
            if isinstance(entry, str):
                out.add(entry.lower().strip())
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("id")
                if isinstance(name, str):
                    out.add(name.lower().strip())
    return {e for e in out if e}


def _entities_from_fixtures(project_path: Path) -> set[str]:
    """Brownfield: harvest candidate names from existing fixtures."""
    out: set[str] = set()
    candidates: list[Path] = []
    # Walk a bounded set of paths; full repo walks are expensive.
    for pattern in ("**/*mock*.json", "**/*fixtures*.json", "**/test-data/**/*.json"):
        candidates.extend(project_path.glob(pattern))
    # Cap the walk to avoid pathological repos.
    for path in candidates[:100]:
        # Skip files inside .aah — those are framework artifacts.
        if "/.aah/" in str(path).replace("\\", "/"):
            continue
        try:
            data = read_json(path)
        except Exception:
            continue
        names: list[str] = []
        _walk_for_entities(data, names)
        for n in names:
            out.add(n.lower().strip())
    return {e for e in out if e}


def build_entity_registry(project_path: Path) -> dict:
    """Build the registry dict from project sources.

    The output shape is `{"entities": [sorted, lowercased, unique]}`
    so the guard's `_load_registry` can consume it directly.
    """
    rapids_path = project_path / ".aah"
    entities = set()
    entities.update(_entities_from_feature_yamls(rapids_path))

    # Brownfield supplement.
    manifest_path = rapids_path / "manifest.yaml"
    project_type = "brownfield"
    if manifest_path.is_file():
        try:
            mf = read_yaml(manifest_path) or {}
            project_type = (mf.get("project_type") or "brownfield").lower()
        except Exception:
            pass
    if project_type == "brownfield":
        entities.update(_entities_from_fixtures(project_path))

    return {"entities": sorted(entities)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate entity registry")
    sub = parser.add_subparsers(dest="command", required=True)
    build_p = sub.add_parser("build", help="Generate .aah/entity-registry.json")
    build_p.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)
    rapids_path = project_path / ".aah"
    rapids_path.mkdir(parents=True, exist_ok=True)

    if args.command == "build":
        registry = build_entity_registry(project_path)
        out_path = rapids_path / "entity-registry.json"
        write_json(registry, out_path)
        print(f"Wrote {out_path} with {len(registry['entities'])} entities.",
              file=sys.stderr)
        json.dump(registry, sys.stdout, indent=2)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
