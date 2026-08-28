#!/usr/bin/env python3
"""
Synthesize analysis-phase artifacts into a structured planning context.

Primary sources:
  - ADRs from analysis/decisions/ADR-*.md
  - solution-architecture.md (component catalog, NFR specification, agent topology)
  - solution-integration.md (integration seam inventories, failure modes)
  - decision-registry.yaml (project context, resolved decisions)
  - analysis/diagrams/*.mmd (Mermaid architecture diagrams)

Fallback sources:
  - constraints-resolved.yaml (NFRs, applied mandates — if present)
  - engagement-decision-log.json (legacy decision log — if registry absent)

Produces analysis-synthesis.json consumed by spec generation, feature
decomposition, and coverage validation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


def extract_adrs(decisions_dir: Path) -> list[dict]:
    """Parse ADR markdown files for key decision data."""
    adrs: list[dict] = []
    if not decisions_dir.is_dir():
        return adrs

    for adr_file in sorted(decisions_dir.glob("ADR-*.md")):
        adr_id = adr_file.stem
        content = adr_file.read_text(encoding="utf-8")

        decision_question = ""
        chosen_option = ""
        constrains_downstream: list[str] = []
        fitness_functions: list[str] = []

        # Try new-style "Decision Question" then fall back to title or Context
        dq_match = re.search(r"##\s*Decision Question\s*\n+(.+?)(?:\n\n|\n##)", content, re.DOTALL)
        if dq_match:
            decision_question = dq_match.group(1).strip()
        else:
            # Fall back to first heading (# ADR-001: Question)
            title_match = re.match(r"#\s*(?:ADR-\S+:?\s*)?(.*)", content)
            if title_match:
                decision_question = title_match.group(1).strip()

        # Try new-style "Decision Outcome" then "Decision" section
        outcome_match = re.search(
            r"##\s*Decision(?:\s+Outcome)?\s*\n+(.*?)(?:\n##|\Z)", content, re.DOTALL
        )
        if outcome_match:
            outcome_text = outcome_match.group(1).strip()
            option_match = re.search(r"\*\*(.+?)\*\*", outcome_text)
            if option_match:
                chosen_option = option_match.group(1)
            else:
                chosen_option = outcome_text.split("\n")[0][:120]

        consequences_match = re.search(
            r"##\s*Consequences\s*\n+(.*?)(?:\n##|\Z)", content, re.DOTALL
        )
        if consequences_match:
            cons_text = consequences_match.group(1)
            for line in cons_text.split("\n"):
                line_lower = line.lower()
                if "constrain" in line_lower or "downstream" in line_lower:
                    ddr_refs = re.findall(r"DDR-[A-Z0-9-]+", line)
                    constrains_downstream.extend(ddr_refs)
                if "fitness" in line_lower or "validate" in line_lower:
                    fitness_functions.append(line.strip().lstrip("- "))

        adrs.append({
            "id": adr_id,
            "decision_question": decision_question,
            "chosen_option": chosen_option,
            "constrains_downstream": constrains_downstream,
            "fitness_functions": [f for f in fitness_functions if f],
            "file": str(adr_file.name),
        })

    return adrs


def extract_nfrs(architecture_path: Path, constraints_path: Path) -> list[dict]:
    """
    Extract NFRs from solution-architecture.md Section 9 (primary)
    and constraints-resolved.yaml nfr_section (fallback).
    """
    nfrds: list[dict] = []
    seen_ids: set[str] = set()

    # Primary: parse from solution-architecture.md Section 9
    if architecture_path.exists():
        content = architecture_path.read_text(encoding="utf-8")
        nfr_section_match = re.search(
            r"##\s*9\.\s*NFR Specification\s*\n+(.*?)(?:\n##\s*\d|\Z)", content, re.DOTALL
        )
        if nfr_section_match:
            table_text = nfr_section_match.group(1)
            rows = re.findall(
                r"\|\s*(NFRD-\d+)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]*)\|",
                table_text,
            )
            for row in rows:
                nfrd_id = row[0].strip()
                if nfrd_id in seen_ids:
                    continue
                seen_ids.add(nfrd_id)
                nfrds.append({
                    "id": nfrd_id,
                    "category": row[1].strip(),
                    "requirement": row[2].strip(),
                    "target": row[3].strip(),
                    "priority": "Hard" if "hard" in row[4].strip().lower() else "Soft",
                    "source": row[5].strip(),
                    "components": [c.strip() for c in row[6].split(",") if c.strip()],
                })

    # Fallback: constraints-resolved.yaml nfr_section
    if constraints_path.exists():
        try:
            resolved = yaml.safe_load(constraints_path.read_text(encoding="utf-8"))
            for nfr in resolved.get("nfr_section", []):
                nfrd_id = nfr.get("nfrd_id", "")
                if nfrd_id and nfrd_id not in seen_ids:
                    seen_ids.add(nfrd_id)
                    nfrds.append({
                        "id": nfrd_id,
                        "category": nfr.get("category", ""),
                        "requirement": nfr.get("statement", ""),
                        "target": nfr.get("measurable_target", ""),
                        "priority": "Hard",
                        "source": nfr.get("source", "constraints-resolved"),
                        "components": [],
                    })
        except Exception:
            pass

    return nfrds


def extract_integrations(integration_path: Path) -> list[dict]:
    """Parse solution-integration.md for external and internal integration seams."""
    integrations: list[dict] = []
    if not integration_path.exists():
        return integrations

    content = integration_path.read_text(encoding="utf-8")

    # Match EXT-XX and INT-XX rows from integration inventory tables
    ext_rows = re.findall(
        r"\|\s*(EXT-\d+)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]*)\|\s*([^|]*)\|",
        content,
    )
    for row in ext_rows:
        integrations.append({
            "id": row[0].strip(),
            "system": row[1].strip(),
            "direction": row[2].strip(),
            "protocol": row[3].strip(),
            "auth": row[4].strip(),
            "type": "external",
        })

    int_rows = re.findall(
        r"\|\s*(INT-\d+)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]*)\|\s*([^|]*)\|",
        content,
    )
    for row in int_rows:
        integrations.append({
            "id": row[0].strip(),
            "system": row[1].strip(),
            "direction": row[2].strip(),
            "protocol": row[3].strip(),
            "auth": row[4].strip(),
            "type": "internal",
        })

    # Extract failure modes from per-seam detail sections
    for integration in integrations:
        seam_id = integration["id"]
        failure_match = re.search(
            rf"{seam_id}.*?Failure Mode[:\s]+(.*?)(?:\n\n|\n-|\n\||\Z)",
            content, re.DOTALL | re.IGNORECASE,
        )
        if failure_match:
            integration["failure_modes"] = failure_match.group(1).strip()[:200]
        else:
            integration["failure_modes"] = ""

    return integrations


def extract_components(architecture_path: Path) -> list[dict]:
    """Parse Component Catalog from solution-architecture.md Section 4."""
    components: list[dict] = []
    if not architecture_path.exists():
        return components

    content = architecture_path.read_text(encoding="utf-8")
    catalog_match = re.search(
        r"##\s*4\.\s*Component Catalog\s*\n+(.*?)(?:\n##\s*\d|\Z)", content, re.DOTALL
    )
    if not catalog_match:
        return components

    table_text = catalog_match.group(1)
    rows = re.findall(
        r"\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]*)\|",
        table_text,
    )
    for row in rows:
        name = row[0].strip()
        if name.lower() in ("component", "---", ""):
            continue
        if name.startswith("-"):
            continue
        components.append({
            "name": name,
            "responsibility": row[1].strip(),
            "technology": row[2].strip(),
            "adr_ref": row[3].strip(),
        })

    return components


def extract_architecture_constraints(architecture_path: Path) -> list[str]:
    """Extract 'the architecture MUST...' constraints from Section 9 Design Constraints."""
    constraints: list[str] = []
    if not architecture_path.exists():
        return constraints

    content = architecture_path.read_text(encoding="utf-8")
    dc_match = re.search(
        r"###\s*Design Constraints Derived from NFRs\s*\n+(.*?)(?:\n##\s*\d|\n###|\Z)",
        content, re.DOTALL,
    )
    if dc_match:
        for line in dc_match.group(1).split("\n"):
            line = line.strip().lstrip("- ")
            if line and len(line) > 10:
                constraints.append(line)

    return constraints


def extract_agent_topology(architecture_path: Path) -> dict:
    """Extract agent topology from solution-architecture.md Section 14."""
    topology: dict = {"agents": [], "orchestration_pattern": "", "has_topology": False}
    if not architecture_path.exists():
        return topology

    content = architecture_path.read_text(encoding="utf-8")
    section_match = re.search(
        r"##\s*14\.\s*Agent Topology.*?\n(.*?)(?:\n##\s*\d|\Z)", content, re.DOTALL
    )
    if not section_match:
        return topology

    topology["has_topology"] = True
    section_text = section_match.group(1)

    # Extract agent roster rows
    agent_rows = re.findall(
        r"\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]*)\|",
        section_text,
    )
    for row in agent_rows:
        name = row[0].strip()
        if name.lower() in ("agent", "---", ""):
            continue
        if name.startswith("-"):
            continue
        topology["agents"].append({
            "name": name,
            "role": row[1].strip(),
            "model": row[2].strip(),
            "tools": row[3].strip(),
            "autonomy": row[4].strip(),
        })

    # Extract orchestration pattern
    orch_match = re.search(
        r"###\s*14\.2\s*Orchestration Pattern\s*\n+(.*?)(?:\n###|\n##|\Z)",
        section_text, re.DOTALL,
    )
    if orch_match:
        topology["orchestration_pattern"] = orch_match.group(1).strip()[:300]

    return topology


def extract_decision_log(log_path: Path) -> list[dict]:
    """Read engagement-decision-log.json entries."""
    if not log_path.exists():
        return []
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def extract_registry_decisions(rapids_dir: Path) -> list[dict]:
    """Read decision-registry.yaml as alternative to engagement-decision-log.json."""
    registry_path = rapids_dir / "decision-registry.yaml"
    if not registry_path.exists():
        return []
    try:
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        decisions = registry.get("decisions", [])
        return [
            {
                "ddr_id": d.get("ddr_id", ""),
                "status": d.get("status", ""),
                "resolved_option": d.get("resolved_option", d.get("resolved", "")),
                "confirmed": d.get("confirmed", False),
            }
            for d in decisions
            if d.get("status") in ("resolved", "skipped")
        ]
    except Exception:
        return []


def extract_review_conditions(findings_path: Path) -> list[str]:
    """Extract conditions from architecture-review-findings.md."""
    conditions: list[str] = []
    if not findings_path.exists():
        return conditions

    content = findings_path.read_text(encoding="utf-8")
    # Look for conditions in the board sign-off or approved-with-conditions section
    cond_match = re.search(
        r"(?:conditions|approved.with.conditions)[:\s]+(.*?)(?:\n##|\Z)",
        content, re.DOTALL | re.IGNORECASE,
    )
    if cond_match:
        for line in cond_match.group(1).split("\n"):
            line = line.strip().lstrip("- ")
            if line and len(line) > 5:
                conditions.append(line)

    return conditions


def synthesize(rapids_dir: Path) -> dict:
    """Orchestrate all extractors and produce the synthesis output."""
    analysis_dir = rapids_dir / "analysis"
    research_dir = rapids_dir / "research"

    coverage_summary: dict[str, str] = {}
    result: dict = {"coverage_summary": coverage_summary}

    # ADRs
    decisions_dir = analysis_dir / "decisions"
    if decisions_dir.is_dir() and any(decisions_dir.glob("ADR-*.md")):
        result["adr_inventory"] = extract_adrs(decisions_dir)
        coverage_summary["adrs"] = "present"
    else:
        result["adr_inventory"] = []
        coverage_summary["adrs"] = "absent"

    # Solution Architecture (contains NFR Specification and Component Catalog)
    architecture_path = analysis_dir / "solution-architecture.md"
    coverage_summary["solution_architecture"] = "present" if architecture_path.exists() else "absent"

    # NFRs (from solution-architecture.md Section 9 + constraints-resolved.yaml fallback)
    constraints_path = research_dir / "constraints-resolved.yaml"
    nfrs = extract_nfrs(architecture_path, constraints_path)
    result["nfr_inventory"] = nfrs
    coverage_summary["nfrs"] = "present" if nfrs else "absent"

    # Integrations (from solution-integration.md)
    integration_path = analysis_dir / "solution-integration.md"
    if integration_path.exists():
        result["integration_inventory"] = extract_integrations(integration_path)
        coverage_summary["integrations"] = "present"
    else:
        result["integration_inventory"] = []
        coverage_summary["integrations"] = "absent"

    # Components
    result["component_catalog"] = extract_components(architecture_path)
    coverage_summary["components"] = "present" if result["component_catalog"] else "absent"

    # Architecture constraints (design MUST statements)
    result["architecture_constraints"] = extract_architecture_constraints(architecture_path)

    # Agent topology
    agent_topology = extract_agent_topology(architecture_path)
    result["agent_topology"] = agent_topology
    coverage_summary["agent_topology"] = "present" if agent_topology["has_topology"] else "absent"

    # Diagrams (embedded in parent .md files, no longer standalone .mmd)
    embedded_diagrams = []
    for md_file in [analysis_dir / "solution-architecture.md",
                    analysis_dir / "solution-integration.md"]:
        if md_file.exists():
            content = md_file.read_text(encoding="utf-8")
            count = content.count("```mermaid")
            if count > 0:
                embedded_diagrams.append({"file": md_file.name, "count": count})
    result["diagrams"] = embedded_diagrams
    coverage_summary["diagrams"] = "present" if embedded_diagrams else "absent"

    # Decision log (try engagement-decision-log.json, fall back to decision-registry.yaml)
    log_path = analysis_dir / "engagement-decision-log.json"
    if log_path.exists():
        result["decision_log"] = extract_decision_log(log_path)
        coverage_summary["decision_log"] = "present"
    else:
        registry_decisions = extract_registry_decisions(rapids_dir)
        result["decision_log"] = registry_decisions
        coverage_summary["decision_log"] = "present (from registry)" if registry_decisions else "absent"

    # Review conditions
    findings_path = analysis_dir / "architecture-review-findings.md"
    result["review_conditions"] = extract_review_conditions(findings_path)
    coverage_summary["review_findings"] = "present" if findings_path.exists() else "absent"

    # Constraints-resolved (for applied mandates)
    if constraints_path.exists():
        try:
            resolved = yaml.safe_load(constraints_path.read_text(encoding="utf-8"))
            result["applied_mandates"] = resolved.get("applied_mandates", [])
        except Exception:
            result["applied_mandates"] = []
        coverage_summary["constraints_resolved"] = "present"
    else:
        result["applied_mandates"] = []
        coverage_summary["constraints_resolved"] = "absent"

    # Validated runtime resources (cloud-readiness + data-schema-snapshot).
    # Spec generation and feature decomposition use these to wire features
    # against the *real* bucket names, endpoints, and table names instead
    # of inventing placeholders.
    try:
        from aah.core.common.readiness import load_runtime_resources
        runtime_resources = load_runtime_resources(rapids_dir)
    except Exception:
        runtime_resources = None
    if runtime_resources:
        result["runtime_resources"] = runtime_resources
        coverage_summary["runtime_resources"] = "present"
    else:
        result["runtime_resources"] = None
        coverage_summary["runtime_resources"] = "absent"

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthesize analysis artifacts into planning context."
    )
    parser.add_argument(
        "--aah-dir", "--rapids-dir", dest="aah_dir", type=Path, required=True,
        help="Path to the .aah directory (--rapids-dir accepted as a deprecated alias)",
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="Output path for analysis-synthesis.json",
    )
    args = parser.parse_args()

    result = synthesize(args.aah_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Summary to stdout
    cs = result["coverage_summary"]
    print("Analysis synthesis complete:")
    print(f"  ADRs: {len(result['adr_inventory'])} ({cs['adrs']})")
    print(f"  NFRs: {len(result['nfr_inventory'])} ({cs['nfrs']})")
    print(f"  Integrations: {len(result['integration_inventory'])} ({cs['integrations']})")
    print(f"  Components: {len(result['component_catalog'])} ({cs['components']})")
    print(f"  Architecture constraints: {len(result['architecture_constraints'])}")
    print(f"  Agent topology: {len(result['agent_topology']['agents'])} agents ({cs['agent_topology']})")
    print(f"  Diagrams: {len(result['diagrams'])} ({cs['diagrams']})")
    print(f"  Review conditions: {len(result['review_conditions'])}")
    print(f"  Applied mandates: {len(result['applied_mandates'])}")
    rr = result.get("runtime_resources")
    if rr:
        n_svc = len(rr.get("services", []))
        n_dbs = len((rr.get("data") or {}).get("databases", []))
        n_storage = len((rr.get("data") or {}).get("storage", []))
        n_apis = len((rr.get("data") or {}).get("apis", []))
        print(f"  Runtime resources: {n_svc} services, {n_dbs} databases, "
              f"{n_storage} storage targets, {n_apis} api integrations "
              f"({cs['runtime_resources']})")
    else:
        print(f"  Runtime resources: {cs['runtime_resources']}")

    sys.exit(0)


if __name__ == "__main__":
    main()
