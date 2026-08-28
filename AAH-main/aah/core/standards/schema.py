#!/usr/bin/env python3
"""Validate standard artifact YAML files against the expected schema."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml
from aah.core.common.validators import validate_dict_schema


STANDARD_SCHEMA = {
    "standard_id": {"required": False, "type": str},
    "name": {"required": True, "type": str},
    "version": {"required": False, "type": str},
    "source_type": {
        "required": False,
        "type": str,
        "allowed_values": ["company", "compliance", "architecture", "industry-default"],
    },
    "applies_to": {"required": False, "type": dict},
    "rules": {"required": False, "type": list},
    "categories": {"required": False, "type": list},
    "disabled_rules": {"required": False, "type": list},
}

RULE_SCHEMA = {
    "id": {"required": True, "type": str},
    "category": {"required": True, "type": str},
    "priority": {
        "required": True,
        "type": str,
        "allowed_values": ["critical", "high", "medium", "low"],
    },
    "description": {"required": True, "type": str},
    "remediation": {"required": False, "type": str},
}


def validate_standard_data(data: dict, context: str = "") -> list[str]:
    """Validate an already-parsed standards dict. Returns list of errors (empty = valid).

    Accepts two formats:
      - Flat: top-level ``rules`` list
      - Categorized: top-level ``categories`` list with nested ``rules``
    At least one of ``rules`` or ``categories`` must be present.
    """
    errors = validate_dict_schema(data, STANDARD_SCHEMA, context=context)

    has_rules = isinstance(data.get("rules"), list) and len(data.get("rules", [])) > 0
    has_categories = isinstance(data.get("categories"), list) and len(data.get("categories", [])) > 0

    if not has_rules and not has_categories:
        errors.append(f"{context}: must contain 'rules' or 'categories' with at least one entry")

    # Validate flat rules
    for i, rule in enumerate(data.get("rules", [])):
        if not isinstance(rule, dict):
            errors.append(f"Rule {i}: expected dict, got {type(rule).__name__}")
            continue
        rule_errors = validate_dict_schema(rule, RULE_SCHEMA, context=f"rule[{i}]")
        errors.extend(rule_errors)

    # Validate rules nested inside categories
    for ci, cat in enumerate(data.get("categories", [])):
        if not isinstance(cat, dict):
            errors.append(f"Category {ci}: expected dict, got {type(cat).__name__}")
            continue
        for ri, rule in enumerate(cat.get("rules", [])):
            if not isinstance(rule, dict):
                errors.append(f"categories[{ci}].rules[{ri}]: expected dict, got {type(rule).__name__}")
                continue
            # Rules inside categories inherit category/priority — only id+description required
            if "id" not in rule:
                errors.append(f"categories[{ci}].rules[{ri}]: missing required field 'id'")
            if "description" not in rule:
                errors.append(f"categories[{ci}].rules[{ri}]: missing required field 'description'")

    return errors


def validate_standard_file(path: Path) -> list[str]:
    """Validate a standards YAML file. Convenience wrapper around ``validate_standard_data``."""
    try:
        data = read_yaml(path)
    except Exception as e:
        return [f"Failed to read {path}: {e}"]

    if not data:
        return [f"Empty or invalid YAML: {path}"]

    return validate_standard_data(data, context=str(path.name))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a standards YAML file")
    parser.add_argument("--file", type=Path, required=True, help="Path to standards YAML")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    errors = validate_standard_file(args.file)
    result = {"file": str(args.file), "valid": len(errors) == 0, "errors": errors}
    json.dump(result, sys.stdout, indent=2)
    print()

    if errors:
        print(f"Validation FAILED: {len(errors)} error(s)", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("Validation PASSED", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
