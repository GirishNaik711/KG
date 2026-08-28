#!/usr/bin/env python3
"""Validate DDR YAML files against the DDR schema."""

import argparse
import json
import re
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml
from aah.core.ddr_ops.loader import LAYERS, resolve_resources_path, load_all_ddrs


# DDR validation schema rules
DDR_ID_PATTERN = re.compile(r"^DDR-(L[1-9]-\d{3}|SHARED-\d{3})$")
OPTION_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
VALID_VERDICTS = {"favours", "neutral", "trades-off"}
VALID_CATEGORIES = {"archetype", "shared"}

REQUIRED_FIELDS = ["schema_version", "id", "category", "layer", "layer_number",
                   "decision_question", "forces", "options", "anti_patterns"]


def validate_ddr(data: dict, file_path: Path | None = None) -> list[str]:
    """Validate a DDR dict against the v2.0 schema. Returns list of error strings."""
    errors = []

    # Required fields
    for field in REQUIRED_FIELDS:
        if field not in data or data[field] is None:
            errors.append(f"Missing required field: '{field}'")

    # Schema version
    schema_version = data.get("schema_version")
    if schema_version and schema_version != "2.0":
        errors.append(f"Invalid schema_version: '{schema_version}'. Must be '2.0'")

    # Category
    category = data.get("category")
    if category and category not in VALID_CATEGORIES:
        errors.append(f"Invalid category: '{category}'. Must be one of: {VALID_CATEGORIES}")

    # ID format
    ddr_id = data.get("id", "")
    if ddr_id and not DDR_ID_PATTERN.match(ddr_id):
        errors.append(f"Invalid DDR ID format: '{ddr_id}'. Expected DDR-L[1-9]-NNN or DDR-SHARED-NNN")

    # Layer number range (0 is valid for shared DDRs)
    layer_number = data.get("layer_number")
    if layer_number is not None:
        if not isinstance(layer_number, int) or layer_number < 0 or layer_number > 9:
            errors.append(f"Invalid layer_number: {layer_number}. Must be integer 0-9")

    # ID and layer_number consistency (skip for SHARED DDRs)
    if ddr_id and layer_number and "SHARED" not in ddr_id:
        id_layer = int(ddr_id.split("-")[1][1:]) if len(ddr_id.split("-")) >= 2 else None
        if id_layer and id_layer != layer_number:
            errors.append(f"ID layer ({id_layer}) doesn't match layer_number ({layer_number})")

    # Forces: at least 2, each with required sub-fields {id, tension, description}
    forces = data.get("forces", [])
    if isinstance(forces, list):
        if len(forces) < 2:
            errors.append(f"DDR must have at least 2 forces, found {len(forces)}")
        force_ids = set()
        for i, force in enumerate(forces):
            if not isinstance(force, dict):
                errors.append(f"Force {i} must be a dict with {{id, tension, description}}")
                continue
            if "id" not in force:
                errors.append(f"Force {i} missing 'id'")
            else:
                force_ids.add(force["id"])
            if "tension" not in force:
                errors.append(f"Force {i} missing 'tension'")
            if "description" not in force:
                errors.append(f"Force {i} missing 'description'")

    # Options: at least 2, each with force_resolution matrix
    options = data.get("options", [])
    if isinstance(options, list):
        if len(options) < 2:
            errors.append(f"DDR must have at least 2 options, found {len(options)}")
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                errors.append(f"Option {i} must be a dict")
                continue
            if "label" not in opt:
                errors.append(f"Option {i} missing 'label'")
            elif not OPTION_LABEL_PATTERN.match(opt["label"]):
                errors.append(f"Option {i} label '{opt['label']}' must be lowercase kebab-case")
            if "description" not in opt:
                errors.append(f"Option {i} missing 'description'")
            # Validate force_resolution matrix
            force_resolution = opt.get("force_resolution")
            if force_resolution is None:
                errors.append(f"Option {i} ('{opt.get('label', '?')}') missing 'force_resolution'")
            elif isinstance(force_resolution, dict):
                # Every force must be covered
                for fid in force_ids:
                    if fid not in force_resolution:
                        errors.append(
                            f"Option {i} ('{opt.get('label', '?')}') missing force_resolution "
                            f"for force '{fid}'"
                        )
                # Each resolution cell must have verdict and detail
                for fid, cell in force_resolution.items():
                    if not isinstance(cell, dict):
                        errors.append(
                            f"Option {i} force_resolution['{fid}'] must be a dict "
                            f"with {{verdict, detail}}"
                        )
                        continue
                    verdict = cell.get("verdict")
                    if verdict not in VALID_VERDICTS:
                        errors.append(
                            f"Option {i} force_resolution['{fid}'].verdict = '{verdict}' "
                            f"is invalid. Must be one of: {VALID_VERDICTS}"
                        )
                    if "detail" not in cell:
                        errors.append(
                            f"Option {i} force_resolution['{fid}'] missing 'detail'"
                        )

    # Anti-patterns: at least 1
    anti_patterns = data.get("anti_patterns", [])
    if isinstance(anti_patterns, list) and len(anti_patterns) < 1:
        errors.append("DDR must have at least 1 anti_pattern")

    # depends_on: must be a list of strings
    depends_on = data.get("depends_on")
    if depends_on is not None:
        if not isinstance(depends_on, list):
            errors.append("'depends_on' must be a list of DDR IDs")
        else:
            for dep in depends_on:
                if not isinstance(dep, str) or not DDR_ID_PATTERN.match(dep):
                    errors.append(f"depends_on entry '{dep}' is not a valid DDR ID")

    # skip_if: must be a list of dicts with {ddr_id, resolved_to, reason}
    skip_if = data.get("skip_if")
    if skip_if is not None:
        if not isinstance(skip_if, list):
            errors.append("'skip_if' must be a list")
        else:
            for j, cond in enumerate(skip_if):
                if not isinstance(cond, dict):
                    errors.append(f"skip_if[{j}] must be a dict with {{ddr_id, resolved_to, reason}}")
                    continue
                if "ddr_id" not in cond:
                    errors.append(f"skip_if[{j}] missing 'ddr_id'")
                if "resolved_to" not in cond:
                    errors.append(f"skip_if[{j}] missing 'resolved_to'")
                if "reason" not in cond:
                    errors.append(f"skip_if[{j}] missing 'reason'")

    # Seed tools_by_option keys must match option labels
    seed = data.get("seed")
    if seed and isinstance(seed, dict):
        tools_by_option = seed.get("tools_by_option", {})
        if tools_by_option and isinstance(options, list):
            option_labels = {opt.get("label") for opt in options if isinstance(opt, dict)}
            for key in tools_by_option:
                if key not in option_labels:
                    errors.append(f"seed.tools_by_option key '{key}' doesn't match any option label")

    # File location validation (if path provided, skip for shared DDRs with layer 0)
    if file_path and layer_number and layer_number > 0:
        expected_prefix = f"L{layer_number}-"
        parent_name = file_path.parent.name
        if not parent_name.startswith(expected_prefix):
            errors.append(
                f"DDR file is in folder '{parent_name}' but layer_number is {layer_number}. "
                f"Expected folder starting with '{expected_prefix}'"
            )

    # Filename validation
    if file_path and ddr_id:
        if not file_path.name.startswith(ddr_id):
            errors.append(f"Filename '{file_path.name}' must start with DDR ID '{ddr_id}'")

    return errors


def check_completeness(content: str, layer_number: int | None = None) -> dict:
    """Analyze content to determine which DDR fields can be populated.

    Returns a dict mapping each DDR field to a completeness assessment.
    """
    content_lower = content.lower()

    # Heuristic checks for field coverage
    assessments = {}

    # Decision question: look for question patterns or clear decision points
    has_question = any(q in content_lower for q in ["how should", "which", "what approach",
                                                     "decision", "choose", "select", "determine"])
    assessments["decision_question"] = {
        "covered": has_question,
        "confidence": "high" if has_question else "low",
        "note": "Clear decision point found" if has_question else "No clear decision question identified",
    }

    # Forces: look for tension/tradeoff language
    tension_words = ["vs", "versus", "tradeoff", "trade-off", "tension", "competing",
                     "balance", "on the other hand", "however", "but"]
    tension_count = sum(1 for w in tension_words if w in content_lower)
    assessments["forces"] = {
        "covered": tension_count >= 2,
        "confidence": "high" if tension_count >= 3 else "medium" if tension_count >= 1 else "low",
        "note": f"Found {tension_count} tension indicators",
    }

    # Options: look for alternative approaches
    option_words = ["option", "approach", "alternative", "method", "strategy",
                    "solution", "pattern", "could use", "one way", "another way"]
    option_count = sum(1 for w in option_words if w in content_lower)
    assessments["options"] = {
        "covered": option_count >= 2,
        "confidence": "high" if option_count >= 3 else "medium" if option_count >= 1 else "low",
        "note": f"Found {option_count} option indicators",
    }

    # Anti-patterns: look for negative guidance
    anti_words = ["avoid", "don't", "never", "anti-pattern", "mistake", "wrong",
                  "bad practice", "pitfall", "common error"]
    anti_count = sum(1 for w in anti_words if w in content_lower)
    assessments["anti_patterns"] = {
        "covered": anti_count >= 1,
        "confidence": "high" if anti_count >= 2 else "medium" if anti_count >= 1 else "low",
        "note": f"Found {anti_count} anti-pattern indicators",
    }

    # Mandate: look for hard requirements
    mandate_words = ["must", "required", "mandatory", "non-negotiable", "always",
                     "prerequisite", "before", "never skip"]
    mandate_count = sum(1 for w in mandate_words if w in content_lower)
    assessments["mandate"] = {
        "covered": mandate_count >= 1,
        "confidence": "medium" if mandate_count >= 2 else "low",
        "note": f"Found {mandate_count} mandate indicators (mandates are optional)",
    }

    # Seed tools: look for specific technology mentions
    tech_indicators = ["tool", "library", "framework", "sdk", "api", "service",
                       "package", "module", "platform"]
    tech_count = sum(1 for w in tech_indicators if w in content_lower)
    assessments["seed"] = {
        "covered": tech_count >= 2,
        "confidence": "medium" if tech_count >= 3 else "low",
        "note": f"Found {tech_count} technology references",
    }

    # Overall completeness score
    covered_count = sum(1 for a in assessments.values() if a["covered"])
    total = len(assessments)

    return {
        "completeness_score": round(covered_count / total, 2),
        "fields": assessments,
        "gaps": [field for field, info in assessments.items() if not info["covered"]],
        "ready_to_draft": covered_count >= 4,  # At minimum: question, forces, options, anti_patterns
    }


def validate_all(resources_path: Path, archetype: str | None = None) -> dict:
    """Validate all DDRs in the resources directory.

    Returns summary with counts and any errors per DDR.
    """
    all_ddrs = load_all_ddrs(resources_path, archetype)
    results = {"total": len(all_ddrs), "valid": 0, "invalid": 0, "errors": {}}

    for ddr in all_ddrs:
        ddr_path = Path(ddr["_path"])
        clean_data = {k: v for k, v in ddr.items() if not k.startswith("_")}
        errors = validate_ddr(clean_data, ddr_path)

        if errors:
            results["invalid"] += 1
            results["errors"][ddr.get("id", ddr_path.name)] = errors
        else:
            results["valid"] += 1

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR validator — validate DDR schema compliance")
    sub = parser.add_subparsers(dest="command", required=True)

    # validate single
    val_p = sub.add_parser("validate", help="Validate a single DDR file")
    val_p.add_argument("--path", type=Path, required=True, help="Path to DDR YAML file")

    # validate-all
    all_p = sub.add_parser("validate-all", help="Validate all DDRs")
    all_p.add_argument("--archetype", type=str, default=None, help="Filter by archetype")

    # check-completeness
    comp_p = sub.add_parser("check-completeness", help="Check if content is sufficient for a DDR")
    comp_p.add_argument("--content-file", type=Path, required=True, help="Path to content file")
    comp_p.add_argument("--layer", type=int, default=None, help="Target layer number")

    args = parser.parse_args()

    if args.command == "validate":
        if not args.path.exists():
            print(f"Error: file not found: {args.path}", file=sys.stderr)
            sys.exit(1)

        data = read_yaml(args.path)
        errors = validate_ddr(data, args.path)

        result = {"valid": len(errors) == 0, "errors": errors, "path": str(args.path)}
        json.dump(result, sys.stdout, indent=2)
        print()
        if errors:
            sys.exit(1)

    elif args.command == "validate-all":
        resources_path = resolve_resources_path()
        results = validate_all(resources_path, args.archetype)
        json.dump(results, sys.stdout, indent=2)
        print()
        if results["invalid"] > 0:
            sys.exit(1)

    elif args.command == "check-completeness":
        if not args.content_file.exists():
            print(f"Error: file not found: {args.content_file}", file=sys.stderr)
            sys.exit(1)

        content = args.content_file.read_text(encoding="utf-8")
        result = check_completeness(content, args.layer)
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
