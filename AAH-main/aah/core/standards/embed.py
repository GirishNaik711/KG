#!/usr/bin/env python3
"""
Inject applicable standards into feature .md files.

Embeds only rule IDs grouped by priority — NOT full rule text.  The full rule
database lives in resolved-standards.yaml and is loaded once as shared session
context by load_impl_context.py.

CLI:
  aah run core.standards embed --project-path .
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml
from aah.core.common.feature_utils import append_section


def _group_rules_by_priority(rules: list[dict]) -> dict:
    """Group all resolved rule IDs by priority level."""
    grouped: dict[str, list[str]] = {"critical": [], "high": [], "medium": [], "low": []}
    for rule in rules:
        priority = rule.get("priority", "low")
        rid = rule.get("id", "")
        if rid and priority in grouped:
            grouped[priority].append(rid)
    return {
        "total_rules": len(rules),
        "critical": grouped["critical"],
        "high": grouped["high"],
        "medium": grouped["medium"],
        "low": grouped["low"],
    }


def _build_standards_lines(applicable: dict) -> list[str]:
    """Build markdown bullet lines for the Applicable Standards section."""
    lines = [f"- Total rules: {applicable['total_rules']}"]
    for priority in ("critical", "high", "medium", "low"):
        ids = applicable.get(priority, [])
        if ids:
            lines.append(f"- {priority.capitalize()}:")
            for rid in ids:
                lines.append(f"  - {rid}")
    return lines


def embed_standards(project_path: Path) -> dict:
    """Inject applicable_standards into each feature .md file."""
    aah_path = project_path / ".aah"
    resolved_path = aah_path / "plan" / "resolved-standards.yaml"

    if not resolved_path.exists():
        return {"error": "resolved-standards.yaml not found. Run: aah run core.standards resolve"}

    resolved = read_yaml(resolved_path)
    rules = resolved.get("rules", [])

    features_dir = aah_path / "plan" / "features"
    if not features_dir.is_dir():
        return {"error": f"Features directory not found: {features_dir}"}

    applicable = _group_rules_by_priority(rules)
    section_lines = _build_standards_lines(applicable)

    results = {"features_updated": 0, "features_skipped": 0, "details": []}

    for feature_file in sorted(features_dir.glob("*.md")):
        content = feature_file.read_text(encoding="utf-8")
        if not content.strip():
            results["features_skipped"] += 1
            continue

        append_section(feature_file, "Applicable Standards", section_lines)

        results["features_updated"] += 1
        results["details"].append({
            "feature_id": feature_file.stem,
            "standards_count": applicable["total_rules"],
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed standards into feature files")
    sub = parser.add_subparsers(dest="command", required=True)

    embed_p = sub.add_parser("embed", help="Inject applicable_standards into features")
    embed_p.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    result = embed_standards(project_path)
    json.dump(result, sys.stdout, indent=2)
    print()

    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Embedded standards into {result['features_updated']} feature(s)", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
