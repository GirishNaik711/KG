#!/usr/bin/env python3
"""
Load standards from all sources, merge by priority, write resolved-standards.yaml.

Sources (highest priority first):
  1. Company standards:  knowledge/ root or knowledge/standards/ (any *.yaml / *.yml)
  2. Domain compliance:  knowledge/compliance/ + domain brief regulations
  3. Architecture rules: knowledge/architecture/
  4. Industry defaults:  aah/_resources/_standards/industry-defaults.yaml

The knowledge/ folder is scanned recursively.  Files may use either the flat
``rules`` format or the nested ``categories`` format — both are supported.
Files that do not look like standards (no rules/categories/standard_id) are
silently skipped.

CLI:
  aah run core.standards resolve --project-path .
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.common.manifest import load_manifest, find_manifest
from aah.core.standards.schema import validate_standard_data


# Priority: lower number = higher priority (wins on conflict)
SOURCE_PRIORITY = {"company": 1, "compliance": 2, "architecture": 2, "domain-brief": 2, "industry-default": 3}

# Map subfolder names under knowledge/ to source types.
# Files in unrecognised subfolders or at the knowledge/ root default to "company".
_FOLDER_SOURCE_MAP = {
    "standards": "company",
    "compliance": "compliance",
    "architecture": "architecture",
}

# Fail fast if folder map references source types not in the priority table.
_unmapped = set(_FOLDER_SOURCE_MAP.values()) - set(SOURCE_PRIORITY)
assert not _unmapped, f"_FOLDER_SOURCE_MAP values {_unmapped} missing from SOURCE_PRIORITY"
del _unmapped

# Top-level keys that indicate a YAML file is a standards artifact.
# Files without any of these are silently skipped (not standards files).
_STANDARDS_MARKERS = {"standard_id", "rules", "categories"}

DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "_resources" / "_standards" / "industry-defaults.yaml"
# Top-level metadata keys in industry-defaults.yaml (not stack sections)
_DEFAULTS_META_KEYS = {"standard_id", "name", "version", "source_type", "applies_to"}


def _infer_source_type(file_path: Path, knowledge_dir: Path, data: dict) -> str:
    """Infer the source classification for a standards file.

    Resolution order:
      1. Explicit ``source_type`` field in the YAML
      2. Subfolder name (standards/ → company, compliance/ → compliance, etc.)
      3. Default to "company"
    """
    # Honour explicit source_type in the file
    explicit = data.get("source_type")
    if explicit and explicit in SOURCE_PRIORITY:
        return explicit

    # Classify by immediate subfolder under knowledge/
    relative = file_path.relative_to(knowledge_dir)
    parts = relative.parts
    if len(parts) > 1:
        return _FOLDER_SOURCE_MAP.get(parts[0], "company")

    # Root-level file — default to company
    return "company"


def _flatten_categories(data: dict) -> list[dict]:
    """Extract a flat rule list from the categories-based format.

    Each category supplies a default ``category`` and ``priority`` that are
    inherited by its child rules unless the rule overrides them.
    """
    rules: list[dict] = []
    for cat in data.get("categories", []):
        if not isinstance(cat, dict):
            continue
        cat_name = cat.get("category", "unknown")
        cat_priority = cat.get("priority", "medium")
        for rule in cat.get("rules", []):
            if not isinstance(rule, dict):
                continue
            flat = dict(rule)
            flat.setdefault("category", cat_name)
            flat.setdefault("priority", cat_priority)
            rules.append(flat)
    return rules


def _extract_rules(data: dict) -> list[dict]:
    """Extract rules from flat ``rules``, nested ``categories``, or both.

    When a file contains both ``rules`` and ``categories``, all are merged.
    """
    combined: list[dict] = []

    flat_rules = data.get("rules")
    if isinstance(flat_rules, list):
        combined.extend(dict(r) for r in flat_rules if isinstance(r, dict))

    if isinstance(data.get("categories"), list):
        combined.extend(_flatten_categories(data))

    return combined


def _load_knowledge_standards(knowledge_dir: Path) -> tuple[list[dict], list[dict], set[str]]:
    """Recursively scan knowledge/ for *.yaml / *.yml files, classify, and extract rules.

    Works regardless of whether the user places files directly in knowledge/
    or organises them into standards/, compliance/, architecture/ subfolders.

    Files that do not contain any standards markers (``standard_id``, ``rules``,
    or ``categories``) are silently skipped — they are assumed to be non-standards
    documents (API examples, data dictionaries, etc.).
    """
    if not knowledge_dir.is_dir():
        return [], [], set()

    all_rules: list[dict] = []
    all_sources: list[dict] = []
    disabled_ids: set[str] = set()

    # Collect both .yaml and .yml files
    yaml_files = sorted(
        set(knowledge_dir.rglob("*.yaml")) | set(knowledge_dir.rglob("*.yml"))
    )

    for f in yaml_files:
        if not f.is_file():
            continue

        # Read once — used for both detection, validation, and rule extraction
        try:
            data = read_yaml(f)
        except Exception:
            print(f"Warning: could not read {f.name}, skipping", file=sys.stderr)
            continue

        if not data or not isinstance(data, dict):
            continue

        # Skip files that don't look like standards artifacts
        if not (_STANDARDS_MARKERS & data.keys()):
            continue

        errors = validate_standard_data(data, context=f.name)
        if errors:
            print(f"Warning: skipping {f.name} ({len(errors)} validation errors)", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            continue

        source_type = _infer_source_type(f, knowledge_dir, data)
        file_rules = _extract_rules(data)
        if not file_rules:
            continue

        for rule in file_rules:
            rule["source"] = source_type

        all_rules.extend(file_rules)

        # Collect disabled rules from any source
        disabled = data.get("disabled_rules", [])
        disabled_ids.update(disabled)

        all_sources.append({
            "type": source_type,
            "file": str(f.relative_to(knowledge_dir.parent)),
            "standard_id": data.get("standard_id", ""),
            "name": data.get("name", f.stem),
            "rules_loaded": len(file_rules),
            "disabled_rules": disabled if disabled else [],
        })

    return all_rules, all_sources, disabled_ids


def _extract_unstructured_inline(knowledge_dir: Path) -> tuple[list[dict], list[dict]]:
    """Auto-extract rules from unstructured text files (.md, .txt, .rst) inline.

    No caching — heuristic extraction is instant. Skips structured YAML files
    (already handled by _load_knowledge_standards) and non-standards files.
    """
    from aah.core.standards.extract import auto_extract_text_file

    text_extensions = {".md", ".txt", ".rst"}
    all_rules: list[dict] = []
    all_sources: list[dict] = []

    for f in sorted(knowledge_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() not in text_extensions:
            continue

        result = auto_extract_text_file(f)
        if not result or not result.get("rules"):
            continue

        rules = result["rules"]
        for rule in rules:
            rule["source"] = "company"

        all_rules.extend(rules)
        all_sources.append({
            "type": "company",
            "file": str(f.relative_to(knowledge_dir.parent)),
            "name": result.get("name", f.stem),
            "rules_loaded": len(rules),
            "extraction": "heuristic",
        })

    return all_rules, all_sources


def _load_domain_brief_regulations(domain_path: str | None) -> tuple[list[dict], list[dict]]:
    """Extract regulations from the project's domain brief and convert to rules."""
    if not domain_path:
        return [], []

    try:
        from aah.core.domain_briefs.loader import load_node
        node = load_node(domain_path)
    except Exception:
        return [], []

    if not node:
        return [], []

    regulations = node.get("regulations", [])
    if not regulations:
        return [], []

    rules = []
    for i, reg in enumerate(regulations):
        if isinstance(reg, str):
            rule_id = f"DOMAIN-REG-{i+1:03d}"
            description = reg
        elif isinstance(reg, dict):
            rule_id = reg.get("id", f"DOMAIN-REG-{i+1:03d}")
            description = reg.get("description", reg.get("name", str(reg)))
        else:
            continue
        rules.append({
            "id": rule_id,
            "category": "regulatory",
            "priority": "critical",
            "description": description,
            "source": "domain-brief",
        })

    sources = [{
        "type": "domain-brief",
        "path": domain_path,
        "regulations_extracted": len(rules),
    }]
    return rules, sources


def _collect_stack_tokens(stack: dict) -> set[str]:
    """Extract a flat set of lowercase tokens from stack_choices.

    Handles both structured keys (``language: python``, ``framework: fastapi``)
    and compound values (``primary: "Python/LangGraph"``).  Splits on ``/``,
    ``,``, and whitespace so every component is matchable.
    """
    tokens: set[str] = set()
    for value in stack.values():
        if isinstance(value, str):
            for part in value.replace("/", ",").replace(" ", ",").split(","):
                part = part.strip().lower()
                if part:
                    tokens.add(part)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    tokens.add(item.strip().lower())
    return tokens


def _section_applies_to_tokens(applies: dict) -> set[str]:
    """Collect all lowercase values from a section's ``applies_to`` dict."""
    tokens: set[str] = set()
    for vals in applies.values():
        if isinstance(vals, list):
            for v in vals:
                if isinstance(v, str) and v.strip():
                    tokens.add(v.strip().lower())
    return tokens


def _load_industry_defaults(stack: dict) -> tuple[list[dict], list[dict]]:
    """Load matching sections from industry-defaults.yaml based on project stack.

    Matching is data-driven: all stack values are tokenised and compared against
    each section's ``applies_to`` lists.  Works with both structured keys
    (``language``, ``framework``) and compound values (``primary``).
    """
    if not DEFAULTS_PATH.exists():
        return [], []

    defaults = read_yaml(DEFAULTS_PATH)
    matched_rules = []

    stack_tokens = _collect_stack_tokens(stack)
    if not stack_tokens:
        return [], []

    for section_key, section in defaults.items():
        if section_key in _DEFAULTS_META_KEYS:
            continue
        if not isinstance(section, dict):
            continue

        applies = section.get("applies_to", {})
        section_tokens = _section_applies_to_tokens(applies)

        if stack_tokens & section_tokens:
            for rule in section.get("rules", []):
                rule["source"] = "industry-default"
                matched_rules.append(rule)

    sources = []
    if matched_rules:
        sources.append({
            "type": "industry-default",
            "file": str(DEFAULTS_PATH.name),
            "rules_loaded": len(matched_rules),
        })
    return matched_rules, sources


def _merge_rules(all_rules: list[dict], disabled_ids: set[str]) -> list[dict]:
    """Merge rules by ID — higher-priority source wins. Remove disabled rules."""
    by_id: dict[str, dict] = {}
    for rule in all_rules:
        rid = rule["id"]
        if rid in disabled_ids:
            continue
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = rule
        else:
            # Lower priority number = higher priority = wins
            existing_pri = SOURCE_PRIORITY.get(existing.get("source", ""), 99)
            new_pri = SOURCE_PRIORITY.get(rule.get("source", ""), 99)
            if new_pri < existing_pri:
                by_id[rid] = rule

    # Sort: critical first, then high, medium, low
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(by_id.values(), key=lambda r: priority_order.get(r.get("priority", "low"), 4))


def resolve_standards(project_path: Path) -> dict:
    """Load all standard sources, merge, and return the resolved document."""
    manifest_path = find_manifest(project_path)
    manifest = load_manifest(manifest_path)

    stack = manifest.get("stack_choices", {})
    domain_path = manifest.get("industry_domain_path")

    # Find knowledge directory in the project
    from aah.core.knowledge.parser import find_knowledge_dir
    knowledge_dir = find_knowledge_dir(project_path)

    all_rules = []
    all_sources = []
    disabled_ids: set[str] = set()

    # Load from project knowledge folder (structured YAML + unstructured text files)
    if knowledge_dir:
        rules, sources, disabled = _load_knowledge_standards(knowledge_dir)
        all_rules.extend(rules)
        all_sources.extend(sources)
        disabled_ids.update(disabled)

        # Auto-extract rules from unstructured text files (.md, .txt, .rst) inline
        rules, sources = _extract_unstructured_inline(knowledge_dir)
        all_rules.extend(rules)
        all_sources.extend(sources)

    rules, sources = _load_domain_brief_regulations(domain_path)
    all_rules.extend(rules)
    all_sources.extend(sources)

    rules, sources = _load_industry_defaults(stack)
    all_rules.extend(rules)
    all_sources.extend(sources)

    # Merge
    resolved_rules = _merge_rules(all_rules, disabled_ids)

    # Build summary
    summary = {"total": len(resolved_rules), "critical": 0, "high": 0, "medium": 0, "low": 0}
    for r in resolved_rules:
        p = r.get("priority", "low")
        if p in summary:
            summary[p] += 1
    summary["disabled"] = sorted(disabled_ids)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": manifest.get("project_name", "unknown"),
        "stack": {
            "language": stack.get("language", ""),
            "framework": stack.get("framework", ""),
            "domain": domain_path or "",
        },
        "sources": all_sources,
        "rules": resolved_rules,
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve enterprise standards")
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_p = sub.add_parser("resolve", help="Load, merge, and write resolved-standards.yaml")
    resolve_p.add_argument("--project-path", type=Path, default=None)

    status_p = sub.add_parser("status", help="Show resolved standards summary")
    status_p.add_argument("--project-path", type=Path, default=None)

    validate_p = sub.add_parser("validate", help="Validate a standards YAML file")
    validate_p.add_argument("--file", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "validate":
        from aah.core.standards.schema import main as validate_main
        sys.argv = ["validate", "--file", str(args.file)]
        validate_main()
        return

    from aah.core.common.config import require_project_path
    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "resolve":
        resolved = resolve_standards(project_path)
        out_path = project_path / ".aah" / "plan" / "resolved-standards.yaml"
        write_yaml(resolved, out_path)

        json.dump(resolved["summary"], sys.stdout, indent=2)
        print()
        print(f"Resolved {resolved['summary']['total']} standards from {len(resolved['sources'])} source(s)", file=sys.stderr)
        print(f"Written to: {out_path}", file=sys.stderr)

    elif args.command == "status":
        resolved_path = project_path / ".aah" / "plan" / "resolved-standards.yaml"
        if not resolved_path.exists():
            print("No resolved-standards.yaml found. Run: aah run core.standards resolve", file=sys.stderr)
            sys.exit(1)
        data = read_yaml(resolved_path)
        json.dump(data.get("summary", {}), sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()
