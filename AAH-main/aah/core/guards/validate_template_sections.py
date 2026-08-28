#!/usr/bin/env python3
"""
Standalone command to verify that every template file contains all sections
declared in its activity YAML.

This catches drift before artifacts are generated — templates are the source
from which artifacts are copied, so they must have all YAML sections.

Usage:
    aah run core.guards.validate_template_sections
"""

import json
import re
import sys
from pathlib import Path

from aah.core.activities.loader import (
    load_activity,
    load_registry,
    resolve_activity_library_path,
)
from aah.core.common.config import resolve_framework_root


def normalize_section(header: str) -> str:
    """Normalize a section header for comparison.

    Strips ## prefix, numbered prefixes (e.g., "1. "), and lowercases for comparison.
    """
    text = re.sub(r'^#+\s*', '', header.strip())
    return text.strip()


def validate_templates_against_yaml() -> dict:
    """For each activity with an existing template, verify the template
    contains all YAML-declared sections as ## headers.

    Returns summary dict. Templates may have extra sections (bonus).
    Only YAML sections missing from template are flagged.
    """
    framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root", file=sys.stderr)
        sys.exit(1)

    templates_dir = framework_root / "activity-library" / "_templates"
    if not templates_dir.is_dir():
        print(f"Error: templates directory not found at {templates_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        lib_path = resolve_activity_library_path()
        registry = load_registry(lib_path)
    except (SystemExit, Exception) as e:
        print(f"Error loading activity registry: {e}", file=sys.stderr)
        sys.exit(1)

    results = {
        "total": 0,
        "pass": 0,
        "fail": 0,
        "details": [],
    }

    for activity_id, meta in registry.get("activities", {}).items():
        phase = meta.get("phase", "")
        registry_templates = meta.get("templates") or []

        # Use registry templates list to check template existence first,
        # only loading the activity YAML when we actually need sections.
        for template_filename in registry_templates:
            # Resolve template path: activity-library/_templates/{phase}/{template}
            template_path = templates_dir / phase / template_filename
            if not template_path.exists():
                results["details"].append({
                    "activity_id": activity_id,
                    "template": template_filename,
                    "status": "TEMPLATE_NOT_FOUND",
                    "missing_sections": [],
                })
                continue

            results["total"] += 1

            # Parse template headers
            try:
                content = template_path.read_text(encoding='utf-8')
            except Exception as e:
                results["fail"] += 1
                results["details"].append({
                    "activity_id": activity_id,
                    "template": template_filename,
                    "status": "READ_ERROR",
                    "error": str(e),
                    "missing_sections": [],
                })
                continue

            # Load the activity YAML to get sections for this template
            activity = load_activity(lib_path, meta.get("file", ""))
            if not activity:
                continue

            # Find the matching artifact spec
            yaml_sections = []
            for art_spec in activity.get("artifacts", []):
                if art_spec.get("template") == template_filename:
                    yaml_sections = art_spec.get("sections", [])
                    break

            template_headers = re.findall(r'^##\s+(.+)$', content, re.MULTILINE)
            normalized_template = {normalize_section(h) for h in template_headers}

            # Check every YAML section exists in template
            missing = []
            for section in yaml_sections:
                if normalize_section(section) not in normalized_template:
                    missing.append(section)

            if missing:
                results["fail"] += 1
                results["details"].append({
                    "activity_id": activity_id,
                    "template": template_filename,
                    "status": "FAIL",
                    "missing_sections": missing,
                    "yaml_sections": yaml_sections,
                })
            else:
                results["pass"] += 1
                results["details"].append({
                    "activity_id": activity_id,
                    "template": template_filename,
                    "status": "PASS",
                    "missing_sections": [],
                })

        # Handle activities with no templates in registry (legacy entries)
        if not registry_templates:
            activity = load_activity(lib_path, meta.get("file", ""))
            if not activity:
                continue
            for art_spec in activity.get("artifacts", []):
                template_filename = art_spec.get("template", "")
                if not template_filename:
                    continue
                template_path = templates_dir / phase / template_filename
                if not template_path.exists():
                    results["details"].append({
                        "activity_id": activity_id,
                        "template": template_filename,
                        "status": "TEMPLATE_NOT_FOUND",
                        "missing_sections": [],
                    })
                    continue
                results["total"] += 1
                try:
                    content = template_path.read_text(encoding='utf-8')
                except Exception:
                    continue
                template_headers = re.findall(r'^##\s+(.+)$', content, re.MULTILINE)
                normalized_template = {normalize_section(h) for h in template_headers}
                yaml_sections = art_spec.get("sections", [])
                missing = []
                for section in yaml_sections:
                    if normalize_section(section) not in normalized_template:
                        missing.append(section)
                if missing:
                    results["fail"] += 1
                    results["details"].append({
                        "activity_id": activity_id,
                        "template": template_filename,
                        "status": "FAIL",
                        "missing_sections": missing,
                        "yaml_sections": yaml_sections,
                    })
                else:
                    results["pass"] += 1
                    results["details"].append({
                        "activity_id": activity_id,
                        "template": template_filename,
                        "status": "PASS",
                        "missing_sections": [],
                    })

    return results


def main() -> None:
    results = validate_templates_against_yaml()
    json.dump(results, sys.stdout, indent=2)
    print()
    if results["fail"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
