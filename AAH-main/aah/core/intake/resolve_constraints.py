#!/usr/bin/env python3
"""
Resolve constraint catalog mandates against intake context.

Reads intake.json, detects compliance and infrastructure regimes from trigger_signals
in catalog files, evaluates engineering mandates from the archetype catalog, and writes
constraints-resolved.yaml to the project's research directory.

Called from rapids-research SKILL.md Step 3.5 before any research activities dispatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


# ── applies_when evaluators for engineering mandate catalog ──────────────────

APPLIES_WHEN: dict[str, object] = {
    "always": lambda text: True,
    "if_rag": lambda text: any(
        s in text for s in [
            "rag", "retrieval", "corpus", "knowledge base", "documents",
            "vector", "embeddings", "grounding",
        ]
    ),
    "if_multi_agent": lambda text: any(
        s in text for s in [
            "multi-agent", "multi agent", "multiple agents", "specialist agent",
            "agent team", "agent coordination",
        ]
    ),
    "if_long_running_or_hitl": lambda text: any(
        s in text for s in [
            "approval", "hitl", "human in the loop", "human-in-the-loop",
            "long-running", "workflow", "durable", "pause", "resume",
        ]
    ),
    "if_persistent_memory": lambda text: any(
        s in text for s in [
            "remember", "memory", "personalisation", "personalization",
            "cross-session", "user history", "long-term memory",
        ]
    ),
}


def flatten_intake_text(intake: dict) -> str:
    """Concatenate all intake text fields into a single lowercase string for signal detection."""
    parts: list[str] = []
    if intake.get("problem_statement"):
        parts.append(intake["problem_statement"])
    if intake.get("raw_context"):
        parts.append(intake["raw_context"])
    for round_ in intake.get("rounds", []):
        for qa in round_.get("questions", []):
            if qa.get("answer"):
                parts.append(qa["answer"])
            if qa.get("question"):
                parts.append(qa["question"])
    return " ".join(parts).lower()


def load_regime_catalog_files(catalog_dir: Path) -> list[dict]:
    """Load all YAML catalog files from compliance/ and infrastructure/ subdirectories."""
    catalogs: list[dict] = []
    if not catalog_dir.exists():
        return catalogs
    for subdir in ["compliance", "infrastructure"]:
        subpath = catalog_dir / subdir
        if not subpath.exists():
            continue
        for yaml_file in sorted(subpath.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "regime" in data and "mandates" in data:
                    catalogs.append(data)
            except Exception:
                pass
    return catalogs


def detect_triggered_regimes(text: str, catalogs: list[dict]) -> list[str]:
    """Return list of regime names whose trigger_signals appear in the intake text."""
    triggered: list[str] = []
    for catalog in catalogs:
        regime = catalog.get("regime", "")
        signals = catalog.get("trigger_signals", [])
        if any(signal.lower() in text for signal in signals):
            triggered.append(regime)
    return triggered


def collect_regime_mandates(catalogs: list[dict], triggered_regimes: list[str]) -> list[dict]:
    """Return mandate entries from all triggered regime catalogs."""
    mandates: list[dict] = []
    for catalog in catalogs:
        if catalog.get("regime") in triggered_regimes:
            for mandate in catalog.get("mandates", []):
                mandates.append({**mandate, "_source_regime": catalog["regime"]})
    return mandates


def load_archetype_catalog(catalog_path: Path, text: str) -> list[dict]:
    """
    Load engineering mandates from archetype catalog (e.g., ai-applications/constraints/catalog.yaml).
    Evaluates applies_when condition against intake text.
    """
    if not catalog_path.exists():
        return []
    try:
        data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    mandates: list[dict] = []
    for mandate in data.get("mandates", []):
        applies_when = mandate.get("applies_when", "always")
        evaluator = APPLIES_WHEN.get(applies_when, lambda _: True)
        if evaluator(text):
            mandates.append({**mandate, "_source_regime": "engineering"})
    return mandates


def flatten_eliminated_options(sources: list[dict], source_key: str, source_id_field: str) -> list[dict]:
    """Extract closes_options entries from any source list into a flat eliminated list.

    Args:
        sources: list of dicts, each having closes_options and an ID field
        source_key: key name in output (e.g., "by_mandate", "by_constraint", "by_nfr")
        source_id_field: field name to read the source ID from
    """
    eliminated: list[dict] = []
    for source in sources:
        for opt in source.get("closes_options", []):
            for option_label in opt.get("eliminates", []):
                entry = {
                    "ddr_id": opt["ddr_id"],
                    "option": option_label,
                    source_key: source[source_id_field],
                }
                if opt.get("reason"):
                    entry["reason"] = opt["reason"]
                eliminated.append(entry)
    return eliminated


def resolve_preference_eliminations(
    client_constraints: list[dict],
    nfr_section: list[dict],
    mandate_eliminations: list[dict],
    ddr_options: list[dict],
) -> None:
    """Call Claude to populate closes_options on constraints and NFRs in-place.

    Mutates client_constraints[].closes_options and nfr_section[].closes_options.
    Also populates nfr_section[].measurable_target.
    No-op if ANTHROPIC_API_KEY is missing or any error occurs.
    """
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return

    try:
        import anthropic
    except ImportError:
        return

    constraints_summary = [
        {"constraint_id": c["constraint_id"], "statement": c["statement"]}
        for c in client_constraints
    ]
    nfr_summary = [
        {"nfrd_id": n["nfrd_id"], "category": n["category"], "statement": n["statement"]}
        for n in nfr_section
    ]
    already_eliminated = [
        {"ddr_id": e["ddr_id"], "option": e["option"], "by_mandate": e.get("by_mandate", "")}
        for e in mandate_eliminations
    ]
    ddr_summary = [
        {
            "id": d.get("id", ""),
            "decision_question": d.get("decision_question", ""),
            "options": [o.get("label", o.get("id", "")) for o in d.get("options", [])],
        }
        for d in ddr_options
    ]

    prompt = f"""You are resolving user preferences and NFRs against architectural decision options.

CONSTRAINTS: {json.dumps(constraints_summary)}
NFRs: {json.dumps(nfr_summary)}
ALREADY ELIMINATED (do not repeat): {json.dumps(already_eliminated)}
DDR OPTIONS: {json.dumps(ddr_summary)}

Return JSON:
{{
  "constraint_closures": {{"OC-5": [{{"ddr_id": "...", "eliminates": [...], "reason": "..."}}], ...}},
  "nfr_targets": {{"NFRD-1": "measurable target string", ...}},
  "nfr_closures": {{"NFRD-2": [{{"ddr_id": "...", "eliminates": [...], "reason": "..."}}], ...}}
}}

Rules:
- Only eliminate when IMPOSSIBLE, not merely suboptimal
- Provider preference narrows seeds, does NOT close the DDR
- Constraints with no actionable preference → omit from output
- Return ONLY valid JSON, no markdown fences"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=2000,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text)
    except Exception:
        return

    for cid, closures in result.get("constraint_closures", {}).items():
        for c in client_constraints:
            if c["constraint_id"] == cid:
                c["closes_options"] = closures

    for nfrd_id, target in result.get("nfr_targets", {}).items():
        for nfr in nfr_section:
            if nfr["nfrd_id"] == nfrd_id:
                nfr["measurable_target"] = target

    for nfrd_id, closures in result.get("nfr_closures", {}).items():
        for nfr in nfr_section:
            if nfr["nfrd_id"] == nfrd_id:
                nfr["closes_options"] = closures


# ── Client constraint extraction from intake Q&A ─────────────────────────────

# Each pattern matches on question text (what was asked) and answer text (what
# was said). The question match provides high confidence that the answer is in
# the right category. Answer-only signals serve as fallback for raw_context.

CONSTRAINT_PATTERNS: list[dict] = [
    {
        "category": "budget",
        "question_signals": ["budget", "cost constraint", "spend"],
        "answer_signals": [
            "budget", "credit", "cap", "cost limit", "spend limit",
            "/month", "allocation",
        ],
    },
    {
        "category": "stack",
        "question_signals": [
            "technology stack", "tech stack", "planned or existing",
            "llm providers", "models are preferred",
        ],
        "answer_signals": [
            "stack is fixed", "pre-decided", "predecided", "already chosen",
            "must use", "no evaluation needed", "specific stack defined",
            "google cloud", "vertex ai", "gcp", "aws", "azure",
            "openai", "anthropic", "bedrock", "sagemaker",
        ],
    },
    {
        "category": "environment",
        "question_signals": ["environment", "deployment environment"],
        "answer_signals": [
            "single project", "no staging", "no production environment",
            "single environment", "one environment", "single gcp project",
            "single aws account",
        ],
    },
    {
        "category": "vendor",
        "question_signals": [],
        "answer_signals": [
            "only aws", "only gcp", "only azure", "only google cloud",
            "no other provider", "approved provider", "approved vendor",
            "gcp-native", "aws-native", "azure-native",
            "locked to", "committed to", "sole provider",
        ],
    },
    {
        "category": "scale",
        "question_signals": [
            "scale", "traffic", "concurrent users", "load",
            "expected volume", "users expected",
        ],
        "answer_signals": [
            "< 100", "<100", "low traffic", "small scale",
            "internal only", "limited users", "few users",
            "proof of concept", "poc", "pilot",
        ],
    },
    {
        "category": "scope",
        "question_signals": [
            "scope", "integration", "existing systems",
            "dependencies", "standalone",
        ],
        "answer_signals": [
            "standalone", "no integration", "greenfield",
            "no legacy", "net new", "from scratch",
            "independent", "self-contained", "no dependencies",
        ],
    },
]


# ── NFR extraction from intake text + mandates ────────────────────────────────

NFR_PATTERNS: list[dict] = [
    {
        "category": "performance",
        "signals": [
            "latency", "response time", "p95", "p99", "ms", "seconds",
            "throughput", "rps", "requests per second", "sub-second",
            "sub-3-second", "real-time", "low-latency",
        ],
    },
    {
        "category": "availability",
        "signals": [
            "uptime", "sla", "availability", "99.9%", "99.95%", "99.99%",
            "downtime", "always-on", "high availability", "ha",
        ],
    },
    {
        "category": "scalability",
        "signals": [
            "concurrent users", "scale", "horizontal scaling",
            "auto-scale", "autoscale", "elastic", "peak load",
            "growth", "traffic spike",
        ],
    },
    {
        "category": "security",
        "signals": [
            "encryption", "audit", "compliance", "access control",
            "authentication", "authorization", "zero-trust",
            "soc2", "hipaa", "gdpr", "pci",
        ],
    },
    {
        "category": "cost",
        "signals": [
            "budget", "cost", "spend", "ceiling", "cap",
            "/month", "per month", "monthly", "annual",
        ],
    },
    {
        "category": "ai_specific",
        "signals": [
            "hallucination", "accuracy", "recall", "precision",
            "golden set", "golden eval", "eval", "f1",
            "token", "drift", "grounding", "faithfulness",
        ],
    },
]


def extract_nfr_section(
    text: str,
    applied_mandates: list[dict],
    client_constraints: list[dict],
) -> list[dict]:
    """
    Extract non-functional requirements from intake text, applied mandates,
    and client constraints. Returns a list of NFRD entries.
    """
    import re

    nfrds: list[dict] = []
    seen_categories: set[str] = set()

    # First pass: scan intake text for NFR signals
    sentences = [s.strip() for s in re.split(r'[.\n]', text) if len(s.strip()) > 10]

    for pattern in NFR_PATTERNS:
        category = pattern["category"]
        for sentence in sentences:
            if any(sig in sentence for sig in pattern["signals"]):
                nfrds.append({
                    "nfrd_id": f"NFRD-{len(nfrds) + 1}",
                    "category": category,
                    "statement": sentence.strip()[:200],
                    "measurable_target": "",
                    "source": "intake",
                    "source_ref": "",
                })
                seen_categories.add(category)
                break

    # Second pass: derive from applied mandates (security mandates → security NFRs)
    for mandate in applied_mandates:
        statement = mandate.get("statement", "").lower()
        mandate_id = mandate.get("mandate_id", "")

        if "security" not in seen_categories and any(
            s in statement for s in ["encrypt", "audit", "access control", "auth"]
        ):
            nfrds.append({
                "nfrd_id": f"NFRD-{len(nfrds) + 1}",
                "category": "security",
                "statement": mandate.get("statement", ""),
                "measurable_target": "",
                "source": "mandate",
                "source_ref": mandate_id,
            })
            seen_categories.add("security")

    # Third pass: derive from client constraints (budget → cost NFR)
    for constraint in client_constraints:
        constraint_id = constraint.get("constraint_id", "")
        statement_lower = constraint.get("statement", "").lower()

        if "cost" not in seen_categories and any(
            s in statement_lower for s in ["budget", "cost", "spend", "cap", "/month"]
        ):
            nfrds.append({
                "nfrd_id": f"NFRD-{len(nfrds) + 1}",
                "category": "cost",
                "statement": constraint.get("statement", ""),
                "measurable_target": "",
                "source": "intake",
                "source_ref": constraint_id,
            })
            seen_categories.add("cost")

        if "performance" not in seen_categories and any(
            s in statement_lower for s in ["latency", "response", "ms", "second"]
        ):
            nfrds.append({
                "nfrd_id": f"NFRD-{len(nfrds) + 1}",
                "category": "performance",
                "statement": constraint.get("statement", ""),
                "measurable_target": "",
                "source": "intake",
                "source_ref": constraint_id,
            })
            seen_categories.add("performance")

    return nfrds


def extract_client_constraints(intake: dict) -> list[dict]:
    """
    Extract client-specific organizational constraints from intake Q&A rounds
    and raw_context using keyword heuristics. Returns a list of constraint dicts.
    """
    constraints: list[dict] = []
    seen_categories: set[str] = set()

    # First pass: structured Q&A rounds — require answer signals to confirm the
    # answer actually states a constraint (not just any answer to that question type)
    for round_ in intake.get("rounds", []):
        for qa in round_.get("questions", []):
            question = (qa.get("question") or "").lower()
            answer = qa.get("answer") or ""
            answer_lower = answer.lower()

            for pattern in CONSTRAINT_PATTERNS:
                if pattern["category"] in seen_categories:
                    continue
                a_match = any(s in answer_lower for s in pattern["answer_signals"])
                if not a_match:
                    continue
                q_match = any(s in question for s in pattern["question_signals"])
                if q_match or a_match:
                    constraints.append({
                        "constraint_id": f"OC-{len(constraints) + 1}",
                        "statement": answer.strip(),
                        "source": "intake — user-stated",
                        "closes_options": [],
                        "flexibility": "fixed",
                    })
                    seen_categories.add(pattern["category"])
                    break

    # Second pass: raw_context for categories not yet found
    raw_context = intake.get("raw_context") or ""
    if raw_context and len(seen_categories) < len(CONSTRAINT_PATTERNS):
        import re
        sentences = [s.strip() for s in re.split(r'[.\n]', raw_context) if len(s.strip()) > 15]
        for pattern in CONSTRAINT_PATTERNS:
            if pattern["category"] in seen_categories:
                continue
            for sentence in sentences:
                sentence_lower = sentence.lower()
                if any(s in sentence_lower for s in pattern["answer_signals"]):
                    constraints.append({
                        "constraint_id": f"OC-{len(constraints) + 1}",
                        "statement": sentence.strip(),
                        "source": "intake — user-stated (raw context)",
                        "closes_options": [],
                        "flexibility": "fixed",
                    })
                    seen_categories.add(pattern["category"])
                    break

    return constraints


def resolve(
    intake_path: Path,
    constraints_catalog_dir: Path,
    archetype_catalog_path: Path,
    framework_root: Path | None = None,
    project_types: list[str] | None = None,
) -> dict:
    """
    Main resolution function. Returns the resolved constraints structure.
    """
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    text = flatten_intake_text(intake)

    # Load regime catalogs (compliance + infrastructure)
    regime_catalogs = load_regime_catalog_files(constraints_catalog_dir)

    # Detect which regimes apply
    triggered_regimes = detect_triggered_regimes(text, regime_catalogs)

    # Collect mandates from triggered regimes
    regime_mandates = collect_regime_mandates(regime_catalogs, triggered_regimes)

    # Collect engineering mandates
    engineering_mandates = load_archetype_catalog(archetype_catalog_path, text)

    all_mandates = regime_mandates + engineering_mandates

    # Build applied mandates summary (omit internal _source_regime in output)
    applied_mandates = [
        {
            "mandate_id": m["mandate_id"],
            "statement": m.get("statement", "").strip(),
            "source_regime": m.get("_source_regime", ""),
        }
        for m in all_mandates
    ]

    client_constraints = extract_client_constraints(intake)
    nfr_section = extract_nfr_section(text, applied_mandates, client_constraints)

    # LLM-powered preference elimination: populate closes_options on constraints + NFRs
    if framework_root and project_types:
        try:
            from aah.core.ddr_ops.loader import load_all_ddrs, resolve_resources_path
            resources = resolve_resources_path(framework_root)
            ddr_options = []
            for pt in project_types:
                ddr_options.extend(load_all_ddrs(resources, archetype=pt))
            mandate_elims = flatten_eliminated_options(all_mandates, "by_mandate", "mandate_id")
            resolve_preference_eliminations(client_constraints, nfr_section, mandate_elims, ddr_options)
        except Exception:
            pass

    # Flatten ALL sources uniformly
    eliminated_options = (
        flatten_eliminated_options(all_mandates, "by_mandate", "mandate_id")
        + flatten_eliminated_options(client_constraints, "by_constraint", "constraint_id")
        + flatten_eliminated_options(nfr_section, "by_nfr", "nfrd_id")
    )

    return {
        "schema_version": "1.0",
        "triggered_regimes": triggered_regimes,
        "applied_mandates": applied_mandates,
        "eliminated_options": eliminated_options,
        "client_specific_constraints": client_constraints,
        "nfr_section": nfr_section,
    }



# CLI entry point removed — this module is used as a library by
# aah.core.registry.regimes. Use rapids-regimes CLI instead.
