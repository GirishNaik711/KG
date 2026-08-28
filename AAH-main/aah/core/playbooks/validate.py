#!/usr/bin/env python3
"""Validate Build Playbooks.

Checks:
  - _registry.yaml references real files
  - Each playbook file loads as YAML and has required fields
  - Each playbook's technology_domain matches its registry key
  - `published` flag is explicitly set (boolean)
  - No orphan playbook files not indexed in _registry.yaml

Usage:
  aah run core.playbooks.validate
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml
from aah.core.playbooks.loader import (
    find_playbooks_root,
    is_v2,
    load_registry,
    resolve_doc_ref,
    resolve_skill_ref,
)


# v1 (legacy flat) required fields.
REQUIRED_FIELDS = ("technology_domain", "name", "description", "published", "patterns")

# v2 additionally requires a schema_version and a single cross-cutting guidance line.
V2_REQUIRED_FIELDS = (
    "schema_version", "technology_domain", "name", "description",
    "published", "guidance", "patterns",
)

# Reference kinds and the only phase marker a reference may carry.
_REF_KINDS = ("skill", "doc", "url")
_REF_PHASES = ("discuss",)

# Blocks that belonged to earlier drafts / v1 and must NOT appear in a v2 body —
# their substance now lives in references (permitted/forbidden) or _registry.yaml
# (keywords). Presence signals an un-migrated or malformed v2 playbook.
_V2_FORBIDDEN_BLOCKS = ("keywords", "phase_usage", "discuss", "deltas", "approved_services")


def _validate_references(refs, where: str, errors: list[str]) -> None:
    """Validate a `references[]` list (top-level or per-pattern).

    Each entry must be a dict with `kind ∈ {skill,doc,url}` + non-empty `ref`,
    optional `phase` (only `discuss`). `kind: doc` must resolve to a real file;
    `kind: skill` to a real SKILL.md; `kind: url` is not filesystem-checked.
    `where` is a human label for error messages (e.g. the file / pattern name).
    """
    if refs is None:
        return
    if not isinstance(refs, list):
        errors.append(f"{where}: `references` must be a list")
        return
    for i, ref in enumerate(refs):
        tag = f"{where}: references[{i}]"
        if not isinstance(ref, dict):
            errors.append(f"{tag}: must be a mapping with `kind` + `ref`")
            continue
        kind = ref.get("kind")
        target = ref.get("ref")
        if kind not in _REF_KINDS:
            errors.append(f"{tag}: `kind` must be one of {_REF_KINDS}, got {kind!r}")
        if not target or not str(target).strip():
            errors.append(f"{tag}: `ref` is required and must be non-empty")
        phase = ref.get("phase")
        if phase is not None and phase not in _REF_PHASES:
            errors.append(f"{tag}: `phase` must be one of {_REF_PHASES}, got {phase!r}")
        # Filesystem resolution (only when kind + ref are well-formed).
        if kind == "doc" and target and resolve_doc_ref(str(target)) is None:
            errors.append(f"{tag}: doc ref does not resolve to a file — {target}")
        elif kind == "skill" and target and resolve_skill_ref(str(target)) is None:
            errors.append(f"{tag}: skill ref does not resolve to a SKILL.md — {target}")


def _validate_v2(playbook: dict, file_rel: str, errors: list[str]) -> None:
    """v2-specific structural checks (fields, patterns, references, forbidden blocks)."""
    for field in V2_REQUIRED_FIELDS:
        if field not in playbook:
            errors.append(f"{file_rel}: missing required field `{field}`")

    for block in _V2_FORBIDDEN_BLOCKS:
        if block in playbook:
            errors.append(
                f"{file_rel}: `{block}` is not allowed in a v2 body "
                "(keywords live in _registry.yaml; substance lives in references)"
            )

    # Top-level references
    _validate_references(playbook.get("references"), file_rel, errors)

    # Patterns: each needs name + description; per-pattern references validated too.
    patterns = playbook.get("patterns") or []
    for i, pat in enumerate(patterns):
        if not isinstance(pat, dict):
            errors.append(f"{file_rel}: patterns[{i}] must be a mapping")
            continue
        pname = pat.get("name")
        if not pname:
            errors.append(f"{file_rel}: patterns[{i}] missing `name`")
        if not pat.get("description"):
            errors.append(f"{file_rel}: patterns[{i}] ({pname or '?'}) missing `description`")
        _validate_references(
            pat.get("references"), f"{file_rel}: pattern `{pname or i}`", errors
        )


def validate() -> dict:
    """Validate the Build Playbook library. Returns a report dict."""
    root = find_playbooks_root()
    if root is None:
        return {
            "ok": False,
            "errors": ["build-playbooks/ directory not found"],
            "warnings": [],
            "published": [],
            "unpublished": [],
        }

    registry = load_registry()
    errors: list[str] = []
    warnings: list[str] = []
    published: list[str] = []
    unpublished: list[str] = []

    indexed_files: set[Path] = set()
    for key, entry in registry.get("playbooks", {}).items():
        file_rel = entry.get("file")
        if not file_rel:
            errors.append(f"registry[{key}]: missing `file`")
            continue
        playbook_path = root / file_rel
        indexed_files.add(playbook_path.resolve())

        if not playbook_path.is_file():
            errors.append(f"registry[{key}]: file not found at {file_rel}")
            continue

        try:
            playbook = read_yaml(playbook_path)
        except Exception as exc:
            errors.append(f"registry[{key}]: YAML parse error — {exc}")
            continue

        # Version-aware field checks. v2 has its own required set + reference and
        # forbidden-block rules; v1 keeps the flat REQUIRED_FIELDS contract.
        if is_v2(playbook):
            _validate_v2(playbook, file_rel, errors)
        else:
            for field in REQUIRED_FIELDS:
                if field not in playbook:
                    errors.append(f"{file_rel}: missing required field `{field}`")

        declared = playbook.get("technology_domain")
        if declared and declared != key:
            errors.append(
                f"{file_rel}: technology_domain `{declared}` does not match "
                f"registry key `{key}`"
            )

        published_flag = playbook.get("published")
        if not isinstance(published_flag, bool):
            errors.append(f"{file_rel}: `published` must be boolean")
        elif published_flag:
            published.append(key)
        else:
            unpublished.append(key)

        patterns = playbook.get("patterns") or []
        if not patterns:
            warnings.append(f"{file_rel}: `patterns` is empty")

    # Orphan detection
    for playbook_file in root.rglob("build-playbook.yaml"):
        if playbook_file.resolve() not in indexed_files:
            warnings.append(
                f"orphan: {playbook_file.relative_to(root)} exists on disk but "
                "is not indexed in _registry.yaml"
            )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "published": sorted(published),
        "unpublished": sorted(unpublished),
    }


def main() -> None:
    report = validate()
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
