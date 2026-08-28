#!/usr/bin/env python3
"""Render Domain Brief content as injectable markdown.

Two rendering modes:
  1. build_domain_context_summary(project_path) — compact ~1.5 KB summary
     injected at SessionStart during research and analysis phases.
  2. get_section(project_path, section) — full content for one section,
     used on-demand by agents via the get-section CLI command.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from aah.core.common.manifest import load_manifest
from aah.core.domain_briefs.loader import load_node


_SECTION_MAP = {
    "processes": "processes",
    "data-entities": "data_entities",
    "glossary": "glossary",
    "integrations": "typical_integrations",
    "regulations": "regulations",
    "patterns": "common_patterns",
    "kpis": "kpis",
    "frameworks": "reference_frameworks",
    "anti-patterns": "anti_patterns",
}


def _get_industry_domain_path(project_path: Path) -> Optional[str]:
    manifest = load_manifest(project_path / ".aah" / "manifest.yaml")
    if not isinstance(manifest, dict):
        return None
    return manifest.get("industry_domain_path")


def _is_research_or_analysis_phase(project_path: Path) -> bool:
    manifest = load_manifest(project_path / ".aah" / "manifest.yaml")
    if not isinstance(manifest, dict):
        return False
    return manifest.get("current_phase") in ("discuss", "architecture")


def build_domain_context_summary(project_path: Path) -> str:
    """Produce the `## Domain Intelligence` markdown section for SessionStart.

    Returns empty string if:
      - No industry_domain_path set in manifest
      - Current phase is not research or analysis
      - The node id does not resolve to loadable content
    """
    domain_path = _get_industry_domain_path(project_path)
    if not domain_path:
        return ""

    if not _is_research_or_analysis_phase(project_path):
        return ""

    node = load_node(domain_path)
    if node is None:
        return ""

    lines: list[str] = []
    lines.append("## Domain Intelligence")
    lines.append("")

    # Pretty breadcrumb of path
    breadcrumb = " › ".join(
        _title_case(seg) for seg in domain_path.split("/")
    )
    lines.append(f"**Industry domain:** {breadcrumb}")
    lines.append("")

    description = (node.get("description") or "").strip()
    if description:
        # Keep to 2 sentences for budget
        summary = description.split(". ")
        lines.append("**Description:** " + ". ".join(summary[:2]).rstrip(". ") + ".")
        lines.append("")

    processes = node.get("processes") or []
    if processes:
        names = ", ".join(p.get("name", "") for p in processes[:5] if p.get("name"))
        if names:
            lines.append(f"**Key processes:** {names}")
            lines.append("")

    glossary = node.get("glossary") or []
    if glossary:
        compact = "; ".join(
            f"{g.get('term', '')} ({_short(g.get('definition', ''), 80)})"
            for g in glossary[:5]
            if g.get("term")
        )
        if compact:
            lines.append(f"**Glossary:** {compact}")
            lines.append("")

    regs = node.get("regulations") or []
    if regs:
        compact = "; ".join(r.get("name", "") for r in regs[:5] if r.get("name"))
        if compact:
            lines.append(f"**Regulations:** {compact}")
            lines.append("")

    lines.append(
        "> Full detail: `aah run core.domain_briefs.context "
        "get-section --section <processes|data-entities|glossary|integrations|"
        "regulations|patterns|kpis|frameworks|anti-patterns>`"
    )

    return "\n".join(lines).rstrip() + "\n"


def get_section(project_path: Path, section: str) -> str:
    """Return a full section of the merged Domain Brief as markdown or YAML-ish text."""
    domain_path = _get_industry_domain_path(project_path)
    if not domain_path:
        return ""
    node = load_node(domain_path)
    if node is None:
        return ""

    field = _SECTION_MAP.get(section)
    if field is None:
        return ""

    value = node.get(field) or []
    if not value:
        return f"_(no {section} on this domain)_\n"

    lines = [f"## {_title(section)}", ""]
    if field == "glossary":
        for item in value:
            lines.append(f"- **{item.get('term', '')}**: {item.get('definition', '')}")
    elif field == "regulations":
        for item in value:
            lines.append(f"- **{item.get('name', '')}**")
            impact = (item.get("impact") or "").strip()
            if impact:
                lines.append(f"    {impact}")
    elif field == "processes":
        for proc in value:
            lines.append(f"- **{proc.get('name', '')}**")
            for step in proc.get("steps") or []:
                lines.append(f"    - {step}")
    elif field == "data_entities":
        for ent in value:
            lines.append(f"- **{ent.get('name', '')}**")
            for attr in ent.get("attributes") or []:
                lines.append(f"    - {attr}")
    elif field == "common_patterns":
        for pat in value:
            lines.append(f"- **{pat.get('name', '')}** — {pat.get('description', '').strip()}")
            when = (pat.get("when_to_apply") or "").strip()
            if when:
                lines.append(f"    When: {when}")
    elif field == "reference_frameworks":
        for fw in value:
            nm = fw.get("name", "")
            url = fw.get("url")
            if url:
                lines.append(f"- [{nm}]({url})")
            else:
                lines.append(f"- {nm}")
    else:  # string lists
        for item in value:
            lines.append(f"- {item}")

    return "\n".join(lines).rstrip() + "\n"


def _title_case(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.split("-"))


def _title(section: str) -> str:
    return " ".join(word.capitalize() for word in section.split("-"))


def _short(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    summary_parser = sub.add_parser("summary", help="Render domain context summary")
    summary_parser.add_argument("--project-path", type=Path, required=True)

    section_parser = sub.add_parser("get-section", help="Return one full section")
    section_parser.add_argument("--project-path", type=Path, required=True)
    section_parser.add_argument(
        "--section",
        required=True,
        choices=sorted(_SECTION_MAP.keys()),
    )

    args = parser.parse_args()

    if args.command == "summary":
        sys.stdout.write(build_domain_context_summary(args.project_path))
    elif args.command == "get-section":
        sys.stdout.write(get_section(args.project_path, args.section))


if __name__ == "__main__":
    main()
