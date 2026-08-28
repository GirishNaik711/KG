#!/usr/bin/env python3
"""
Validate Recommendation Brief structure and completeness.

Parses the RB document and verifies required sub-sections exist per DDR.
Exit 0 = pass, Exit 2 = fail.
"""

import json
import re
import sys
from pathlib import Path

import yaml


REQUIRED_SECTIONS = ["Eliminated", "Forces That Matter", "Recommendation"]
VALIDATED_SECTIONS = ["Seed Validation"]
FULL_RESEARCH_SECTIONS = ["Landscape"]


def parse_rb_sections(content: str) -> dict[str, list[str]]:
    """Parse the RB into a dict of DDR ID → list of sub-section headings."""
    ddr_sections: dict[str, list[str]] = {}
    current_ddr: str | None = None

    for line in content.splitlines():
        ddr_match = re.match(r"^## (DDR-\S+)", line)
        if ddr_match:
            current_ddr = ddr_match.group(1)
            ddr_sections[current_ddr] = []
            continue

        if current_ddr and line.startswith("### "):
            section_name = line[4:].strip()
            ddr_sections[current_ddr].append(section_name)

    return ddr_sections


def validate_research_artifacts(project_path: Path) -> tuple[bool, list[str]]:
    """Validate the Recommendation Brief document structure."""
    issues: list[str] = []
    rb_path = project_path / "research" / "recommendation-brief.md"

    if not rb_path.exists():
        return True, []  # No RB yet — not our concern (gate handles existence)

    try:
        content = rb_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, [f"Cannot read recommendation-brief.md: {e}"]

    ddr_sections = parse_rb_sections(content)

    if not ddr_sections:
        issues.append("Recommendation Brief has no DDR sections (## DDR-... headings)")
        return False, issues

    # Detect mode from content
    for ddr_id, subsections in ddr_sections.items():
        for required in REQUIRED_SECTIONS:
            if required not in subsections:
                issues.append(f"{ddr_id}: missing required section '### {required}'")

        # Check mode-specific sections
        mode_match = re.search(
            rf"## {re.escape(ddr_id)}.*?\*\*Mode:\*\*\s*(\S+)",
            content, re.DOTALL
        )
        if mode_match:
            mode = mode_match.group(1).strip()
            if mode == "validated":
                for sec in VALIDATED_SECTIONS:
                    if sec not in subsections:
                        issues.append(f"{ddr_id}: mode is 'validated' but missing '### {sec}'")
            elif mode == "full-research":
                for sec in FULL_RESEARCH_SECTIONS:
                    if sec not in subsections:
                        issues.append(f"{ddr_id}: mode is 'full-research' but missing '### {sec}'")

    if issues:
        return False, issues
    return True, []


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    rapids_path = project_path / ".rapids"
    passed, issues = validate_research_artifacts(rapids_path)

    if not passed:
        print("Research artifacts validation FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
