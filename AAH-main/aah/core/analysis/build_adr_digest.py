#!/usr/bin/env python3
"""
Build a structured ADR digest from individual ADR markdown files.

Reads all ADR-*.md files in the analysis/decisions/ directory, parses
key sections (Decision Question, Decision Outcome, Forces, Consequences,
Review Criteria), and merges with registry metadata to produce a single
adr-digest.yaml that synthesis agents can read in one call instead of
globbing 40+ individual files.

Usage:
    aah run core.analysis.build_adr_digest --aah-dir .aah
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _extract_section(content: str, heading: str) -> str:
    """Extract text under a ## heading, up to the next ## or end of file."""
    pattern = rf"##\s*{re.escape(heading)}\s*\n+(.*?)(?:\n##\s|\Z)"
    match = re.search(pattern, content, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_decision_context(content: str) -> dict:
    """Extract structured decision context from an ADR."""
    section = _extract_section(content, "Decision Context")
    if not section:
        return {"dominant_forces": [], "compliance_regimes": [], "prior_decisions": [], "narrative": ""}

    result: dict = {
        "dominant_forces": [],
        "compliance_regimes": [],
        "prior_decisions": [],
        "narrative": "",
    }

    narrative_lines = []
    for line in section.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue

        # Parse structured bullet points
        forces_match = re.match(r"-\s*\*\*Dominant Forces:\*\*\s*(.*)", stripped)
        if forces_match:
            raw = forces_match.group(1).strip()
            if raw and raw.lower() != "none":
                result["dominant_forces"] = [f.strip() for f in raw.split(",") if f.strip()]
            continue

        compliance_match = re.match(r"-\s*\*\*Compliance Regimes?:\*\*\s*(.*)", stripped)
        if compliance_match:
            raw = compliance_match.group(1).strip()
            if raw and raw.lower() != "none":
                result["compliance_regimes"] = [r.strip() for r in raw.split(",") if r.strip()]
            continue

        prior_match = re.match(r"-\s*\*\*Prior Decisions?:\*\*\s*(.*)", stripped)
        if prior_match:
            raw = prior_match.group(1).strip()
            if raw and raw.lower() != "none":
                result["prior_decisions"] = re.findall(r"DDR-[A-Z0-9]+-\d+", raw)
            continue

        narrative_lines.append(stripped)

    narrative = " ".join(narrative_lines)
    if len(narrative) > 400:
        narrative = narrative[:397] + "..."
    result["narrative"] = narrative

    return result


def _extract_decision_question(content: str) -> str:
    """Extract the decision question from an ADR."""
    section = _extract_section(content, "Decision Question")
    if section:
        # Strip HTML comments
        section = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL).strip()
        return section.split("\n")[0].strip() if section else ""

    # Fallback: parse from title (# ADR-XXX: Question)
    title_match = re.match(r"#\s*(?:ADR-\S+:?\s*)?(.*)", content)
    return title_match.group(1).strip() if title_match else ""


def _extract_chosen_option(content: str) -> str:
    """Extract the chosen option from Decision Outcome section."""
    section = _extract_section(content, "Decision Outcome")
    if not section:
        section = _extract_section(content, "Decision")
    if not section:
        return ""

    # Look for **bold** chosen option
    bold_match = re.search(r"\*\*(?:Chosen option:\s*)?(.+?)\*\*", section)
    if bold_match:
        return bold_match.group(1).strip()

    # Fallback: first non-empty line
    for line in section.split("\n"):
        line = line.strip()
        if line and not line.startswith("<!--"):
            return line[:120]
    return ""


def _extract_rationale(content: str) -> str:
    """Extract rationale summary from Decision Outcome section."""
    section = _extract_section(content, "Decision Outcome")
    if not section:
        return ""

    lines = section.split("\n")
    rationale_lines = []
    past_chosen = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("**Rationale"):
            # Handle inline format: **Rationale:** text on same line
            inline_match = re.match(r"\*\*Rationale[:\*]*\*?\*?\s*:?\s*(.*)", stripped)
            if inline_match and inline_match.group(1).strip():
                rationale_lines.append(inline_match.group(1).strip())
            past_chosen = True
            continue
        if past_chosen and stripped and not stripped.startswith("<!--"):
            rationale_lines.append(stripped)

    rationale = " ".join(rationale_lines)
    # Truncate to ~600 chars for digest
    if len(rationale) > 600:
        rationale = rationale[:597] + "..."
    return rationale


def _extract_consequences(content: str) -> dict:
    """Extract structured consequences from Consequences section."""
    section = _extract_section(content, "Consequences")
    if not section:
        return {"enables": [], "constrains": [], "harder": []}

    result: dict[str, list[str]] = {"enables": [], "constrains": [], "harder": []}
    current_key = None

    for line in section.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue

        lower = stripped.lower()

        # Handle inline format: **Enables:** item1, item2, item3
        inline_match = re.match(
            r"\*\*(?:what this )?(enables|constrains|harder)[:\*]*\*?\*?\s*:?\s*(.*)",
            stripped,
            re.IGNORECASE,
        )
        if inline_match:
            current_key = inline_match.group(1).lower()
            inline_value = inline_match.group(2).strip()
            if inline_value:
                # Split comma-separated inline values
                for item in inline_value.split(","):
                    item = item.strip()
                    if item:
                        result[current_key].append(item)
            continue

        # Handle heading-style: **What this enables:** or just "Enables"
        if "enables" in lower or "what this enables" in lower:
            current_key = "enables"
            continue
        elif "constrains" in lower or "what this constrains" in lower:
            current_key = "constrains"
            continue
        elif "harder" in lower or "what becomes harder" in lower:
            current_key = "harder"
            continue

        if current_key and stripped.startswith(("-", "*")):
            result[current_key].append(stripped.lstrip("-* ").strip())
        elif current_key and stripped:
            result[current_key].append(stripped)

    return result


def _extract_forces(content: str) -> list[str]:
    """Extract force summaries from Forces in Tension section."""
    section = _extract_section(content, "Forces in Tension")
    if not section:
        return []

    forces = []
    # Parse table rows: | Force ID | Tension | Weight | Rationale |
    for match in re.finditer(
        r"\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(HIGH|MEDIUM|LOW)\s*\|",
        section,
        re.IGNORECASE,
    ):
        force_id = match.group(1).strip()
        if force_id and not force_id.startswith("-") and force_id != "Force ID":
            weight = match.group(3).strip().upper()
            forces.append(f"{force_id}: {weight}")

    return forces


def _extract_dependencies(content: str) -> list[str]:
    """Extract DDR dependency references."""
    section = _extract_section(content, "Dependencies")
    if not section:
        return []

    return re.findall(r"DDR-[A-Z0-9]+-\d+", section)


def _extract_technology_choices(content: str) -> list[str]:
    """Extract technology choices mentioned in the ADR."""
    technologies: list[str] = []

    # Look in Decision Outcome for bold tech names
    outcome = _extract_section(content, "Decision Outcome")
    if outcome:
        # Find technology-like mentions (capitalized words with versions, known patterns)
        tech_matches = re.findall(
            r"\b(?:PostgreSQL|MySQL|MongoDB|Redis|Kafka|RabbitMQ|Docker|"
            r"Kubernetes|AWS|GCP|Azure|LangGraph|LangChain|LangSmith|FastAPI|"
            r"Next\.js|React|Vue|Prisma|SQLAlchemy|Supabase|Firebase|Firestore|"
            r"Pinecone|Weaviate|Qdrant|OpenAI|Anthropic|Terraform|GitHub Actions|"
            r"Datadog|Grafana|Prometheus|ECS|Fargate|Lambda|S3|Cloud Run|"
            r"Python|Node\.js|Go|Rust|Java|TypeScript|"
            r"Slack|Teams|BigQuery|Pub/Sub|Cloud Storage|Secret Manager|"
            r"Vertex AI|Bedrock|SageMaker|Cloud Build|ArgoCD|Helm)\b"
            r"(?:\s+\d+[\.\d]*)?",
            content,
        )
        technologies = list(dict.fromkeys(tech_matches))  # dedupe, preserve order

    return technologies[:10]  # cap at 10


def _extract_review_trigger(content: str) -> str:
    """Extract review criteria / trigger conditions."""
    section = _extract_section(content, "Review Criteria")
    if not section:
        return ""

    # Strip comments, take first meaningful line
    lines = [
        line.strip()
        for line in section.split("\n")
        if line.strip() and not line.strip().startswith("<!--")
    ]
    return lines[0][:200] if lines else ""


def build_digest(rapids_dir: Path) -> dict:
    """Build the ADR digest from analysis decisions and registry."""
    decisions_dir = rapids_dir / "analysis" / "decisions"
    registry_path = rapids_dir / "decision-registry.yaml"

    if not decisions_dir.is_dir():
        print(f"Error: decisions directory not found: {decisions_dir}", file=sys.stderr)
        sys.exit(1)

    # Load registry for metadata
    registry: dict = {}
    if registry_path.exists():
        with open(registry_path, encoding="utf-8") as f:
            registry = yaml.safe_load(f) or {}

    # Build lookup from registry decisions
    registry_decisions: dict[str, dict] = {}
    for d in registry.get("decisions", []):
        ddr_id = d.get("ddr_id", "")
        registry_decisions[ddr_id] = d

    # Parse all ADR files
    digest_entries: list[dict] = []
    for adr_file in sorted(decisions_dir.glob("ADR-*.md")):
        content = adr_file.read_text(encoding="utf-8")
        adr_id = adr_file.stem  # e.g., ADR-SHARED-002

        # Derive DDR ID from ADR filename (ADR-SHARED-002 → DDR-SHARED-002)
        ddr_id = adr_id.replace("ADR-", "DDR-", 1)

        # Get registry metadata for this decision
        reg_entry = registry_decisions.get(ddr_id, {})

        consequences = _extract_consequences(content)
        context = _extract_decision_context(content)

        entry = {
            "ddr_id": ddr_id,
            "adr_file": adr_file.name,
            "layer": re.match(r"DDR-(L\d+|SHARED)", ddr_id, re.IGNORECASE).group(1).lower() if re.match(r"DDR-(L\d+|SHARED)", ddr_id, re.IGNORECASE) else "",
            "decision_question": _extract_decision_question(content),
            "decision_context": context,
            "chosen_option": (
                reg_entry.get("resolved")
                or _extract_chosen_option(content)
            ),
            "rationale_summary": _extract_rationale(content),
            "key_consequences": (
                consequences.get("enables", [])
                + consequences.get("constrains", [])
            ),
            "constraints_created": consequences.get("constrains", []),
            "forces_in_tension": _extract_forces(content),
            "external_dependencies": _extract_dependencies(content),
            "technology_choices": _extract_technology_choices(content),
            "review_trigger": _extract_review_trigger(content),
        }

        digest_entries.append(entry)

    project_name = registry.get("project", {}).get("name", "unknown")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_name": project_name,
        "adr_count": len(digest_entries),
        "decisions": digest_entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build ADR digest from analysis decisions"
    )
    parser.add_argument(
        "--aah-dir",
        "--rapids-dir",
        dest="aah_dir",
        type=str,
        required=True,
        help="Path to .aah directory (--rapids-dir accepted as a deprecated alias)",
    )
    args = parser.parse_args()

    rapids_dir = Path(args.aah_dir)
    if not rapids_dir.is_dir():
        print(f"Error: .aah directory not found: {rapids_dir}", file=sys.stderr)
        sys.exit(1)

    digest = build_digest(rapids_dir)

    output_path = rapids_dir / "analysis" / "adr-digest.yaml"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Auto-generated ADR digest — do not edit manually\n")
        f.write(f"# Source: {rapids_dir}/analysis/decisions/ADR-*.md\n")
        yaml.dump(digest, f, default_flow_style=False, sort_keys=False, width=120)

    print(
        f"ADR digest written: {output_path} ({digest['adr_count']} decisions)",
        file=sys.stderr,
    )

    import json
    json.dump(
        {"built": True, "path": str(output_path), "count": digest["adr_count"]},
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
