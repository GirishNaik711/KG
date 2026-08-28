#!/usr/bin/env python3
"""Runtime checkpoint determination engine.

Computes checkpoint configuration at runtime during the Plan phase based on
project signals: manifest, DAG structure, waves, domain context, and standards.

Determines:
  1. System checkpoint level: wave_only | wave_plus_feature
  2. User review checkpoint placement (2-3 checkpoints)
  3. Feature-level check assignments (when upgraded)

Usage:
    aah run core.plan.determine_checkpoints generate --project-path .
    aah run core.plan.determine_checkpoints show --project-path .
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from aah.core.common.dag import dag_from_json, get_dependents
from aah.core.common.feature_utils import (
    load_features_from_dir,
    parse_feature_frontmatter,
)
from aah.core.common.io_utils import read_json, read_yaml, write_yaml_atomic
from aah.core.common.validators import FEATURE_SCHEMA, validate_dict_schema

# ---------------------------------------------------------------------------
# Deterministic fan-out coverage ratchet
# ---------------------------------------------------------------------------

# A feature with more than this many transitive dependents is a "hub" whose
# regressions cascade downstream — it earns a raised coverage floor.
FANOUT_THRESHOLD = 3
# Raised coverage floor for flagged features. Global default is 60.0.
FANOUT_COVERAGE_FLOOR = 80.0
# file_scope substrings that mark a feature as touching a schema/interface/API
# surface (case-insensitive). Matched against each declared file_scope path.
INTERFACE_PATH_MARKERS = ("schema", "interface", "api", "contract", "proto", "openapi")
VERIFICATION_PROFILE_RULE_VERSION = "1"

# Version of the checkpoint-cadence rules encoded in a generated
# checkpoint-config.yaml. Stamped at the TOP LEVEL of ``checkpoint_configuration``
# — deliberately NOT inside ``system_checkpoints`` or ``user_review_checkpoints``,
# the only two blocks ``runtime_criteria_identity`` hashes — so the marker itself
# is hash-neutral and adding it never invalidates runtime evidence.
#
# ``checkpoint_config_migration`` reads it on build resume: a config older than
# this (or with no marker at all, i.e. pre-5.1.1) still lists a user review after
# EVERY wave and is regenerated once with the odd-plus-last cadence. Bump this
# whenever the cadence rules change.
CHECKPOINT_CONFIG_VERSION = "5.1.1"

_RISK_FIELDS: tuple[tuple[str, str], ...] = (
    ("security_scope", "security_scope"),
    ("auth_scope", "auth_scope"),
    ("authentication_scope", "auth_scope"),
    ("payment_scope", "payment_scope"),
    ("credential_scope", "credential_scope"),
    ("critical_nfr", "critical_nfr"),
    ("regulated_data", "regulated_data"),
    ("external_integration", "external_integration"),
    ("shared_interface", "shared_interface"),
    ("shared_schema", "shared_schema"),
    ("shared_contract", "shared_contract"),
    ("concurrency", "concurrency"),
    ("migration", "migration"),
    ("integrity", "integrity"),
)
_RISK_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security_scope", ("security", "encryption")),
    ("auth_scope", ("auth", "login", "oauth", "jwt")),
    ("payment_scope", ("payment", "billing", "card")),
    ("credential_scope", ("credential", "secret", "token")),
    ("regulated_data", ("regulated", "pii", "phi", "pci", "hipaa")),
    ("external_integration", ("external integration", "third-party", "webhook")),
    ("concurrency", ("concurrent", "concurrency", "race condition")),
    ("migration", ("migration", "backfill")),
    ("integrity", ("integrity", "transactional")),
)


def _feature_map(dag: nx.DiGraph, feature_data: dict | list | None) -> dict[str, dict]:
    """Return a deterministic id->contract mapping, falling back to DAG attributes."""
    if isinstance(feature_data, list):
        supplied = {str(item.get("id")): item for item in feature_data if isinstance(item, dict)}
    elif isinstance(feature_data, dict):
        supplied = feature_data
    else:
        supplied = {}
    result = {}
    for fid in sorted(dag.nodes, key=str):
        contract = supplied.get(fid) or supplied.get(str(fid))
        if feature_data is not None and not isinstance(contract, dict):
            raise ValueError(f"{fid}: missing valid current Markdown feature contract")
        if feature_data is not None:
            schema_errors = validate_dict_schema(contract, FEATURE_SCHEMA, context=str(fid))
            if schema_errors:
                raise ValueError("; ".join(schema_errors))
        result[str(fid)] = dict(contract or dag.nodes[fid])
    return result


def _is_critical_nfr(item: dict) -> bool:
    priority = str(item.get("priority") or item.get("severity") or "").strip().lower()
    return bool(item.get("critical")) or priority in {"critical", "hard", "high", "severe"}


def compute_verification_profiles(
    dag: nx.DiGraph,
    feature_data: dict | list | None,
    nfrs: list[dict],
    adrs: list[dict],
    integration: dict | list | None,
) -> dict[str, dict]:
    """Compute the side-effect-free, versioned verification routing profiles.

    Structured boolean risk metadata can be supplied either at the feature root
    or under ``risk_metadata``/``verification_risk``.  False values never
    suppress conservative signals from the contract, graph or referenced
    analysis.  Contradictory aliases fail closed to ``deep``.
    """
    profiles: dict[str, dict] = {}
    nfr_by_id = {str(n.get("id")): n for n in nfrs if isinstance(n, dict) and n.get("id")}
    adr_by_id = {str(a.get("id")): a for a in adrs if isinstance(a, dict) and a.get("id")}
    integrations = integration if isinstance(integration, list) else (
        (integration or {}).get("integrations", []) if isinstance(integration, dict) else []
    )
    integration_by_id = {
        str(item.get("id")): item for item in integrations
        if isinstance(item, dict) and item.get("id")
    }

    for fid, feature in _feature_map(dag, feature_data).items():
        sources = [feature]
        for container in ("risk_metadata", "verification_risk"):
            value = feature.get(container)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{fid}: {container} must be a mapping")
            if isinstance(value, dict):
                sources.append(value)

        values: dict[str, set[bool]] = {}
        for field, reason in _RISK_FIELDS:
            for source in sources:
                if field not in source:
                    continue
                value = source[field]
                if type(value) is not bool:
                    raise ValueError(f"{fid}: risk field {field} must be boolean")
                if isinstance(value, bool):
                    values.setdefault(reason, set()).add(value)

        reasons = {f"explicit_{reason}" for reason, seen in values.items() if True in seen}
        if any(len(seen) > 1 for seen in values.values()):
            reasons.add("metadata_conflict")

        fanout = len(get_dependents(dag, fid))
        if fanout > FANOUT_THRESHOLD:
            reasons.add("high_fanout")
        file_scope = feature.get("file_scope") or dag.nodes[fid].get("file_scope") or []
        if any(marker in str(path).lower() for path in file_scope for marker in INTERFACE_PATH_MARKERS):
            reasons.add("interface_surface")

        referenced = []
        referenced.extend(nfr_by_id.get(str(ref), {}) for ref in feature.get("nfr_refs", []) or [])
        referenced.extend(adr_by_id.get(str(ref), {}) for ref in feature.get("adr_refs", []) or [])
        referenced.extend(integration_by_id.get(str(ref), {}) for ref in feature.get("integration_refs", []) or [])
        if any(_is_critical_nfr(item) for item in referenced if isinstance(item, dict)):
            reasons.add("referenced_critical_nfr")
        if (feature.get("integration_refs") or []) or any(
            str(item.get("type", "")).lower() in {"external", "third_party"}
            for item in referenced if isinstance(item, dict)
        ):
            reasons.add("referenced_external_integration")

        referenced_text = " ".join(
            str(value)
            for item in referenced if isinstance(item, dict)
            for key, value in item.items()
            if key not in {"id", "file"}
        ).lower()
        if any(keyword in referenced_text for _, keywords in _RISK_KEYWORDS for keyword in keywords):
            reasons.add("referenced_analysis_risk")

        text = " ".join(
            str(feature.get(key, "")) for key in ("id", "description", "name", "tags")
        ).lower()
        for reason, keywords in _RISK_KEYWORDS:
            if any(keyword in text for keyword in keywords):
                reasons.add(f"keyword_{reason}")

        override_value = feature.get("verification_profile_override") or feature.get("verification_profile")
        override = None
        if override_value == "deep":
            reasons.add("inline_raise_override")
            override = "deep"
        elif override_value == "standard" and reasons:
            reasons.add("inline_lowering_rejected")

        profiles[fid] = {
            "level": "deep" if reasons else "standard",
            "rule_version": VERIFICATION_PROFILE_RULE_VERSION,
            "reasons": sorted(reasons),
            "override": override,
        }
    return profiles


def _coverage_floors_from_profiles(profiles: dict[str, dict]) -> dict[str, float]:
    ratchet_reasons = {"high_fanout", "interface_surface"}
    return {
        fid: FANOUT_COVERAGE_FLOOR
        for fid, profile in sorted(profiles.items())
        if ratchet_reasons.intersection(profile.get("reasons", []))
    }


# ---------------------------------------------------------------------------
# Step 1: Compute Project Complexity Score
# ---------------------------------------------------------------------------


def compute_checkpoint_complexity(
    manifest: dict,
    dag: nx.DiGraph,
    waves: list[list[str]],
    adrs: list[dict],
    nfrs: list[dict],
) -> int:
    """Compute a numeric complexity score from project signals.

    Higher score = more validation needed. Score components:
    - Wave count: +10 per wave
    - Feature count: +10 (>8) or +20 (>15)
    - DAG depth: +8 (>3) or +15 (>5)
    - ADR count: +8 (>2) or +15 (>5)
    - High-risk domain: +20
    - Critical NFRs: +5 each
    - Complexity tier bonus: trivial=0, moderate=10, significant=25, complex=40
    """
    score = 0

    # Wave count factor
    total_waves = len(waves)
    score += total_waves * 10

    # Feature count factor
    total_features = dag.number_of_nodes()
    if total_features > 15:
        score += 20
    elif total_features > 8:
        score += 10

    # DAG depth factor (deep dependency chains = riskier)
    try:
        max_depth = nx.dag_longest_path_length(dag) if dag.number_of_nodes() > 0 else 0
    except nx.NetworkXError:
        max_depth = 0
    if max_depth > 5:
        score += 15
    elif max_depth > 3:
        score += 8

    # Architecture complexity (number of ADRs)
    if len(adrs) > 5:
        score += 15
    elif len(adrs) > 2:
        score += 8

    # Security-sensitive domain
    domain = manifest.get("industry_domain_path", "") or ""
    high_risk_domains = ["financial", "healthcare", "government", "payments", "hipaa", "pci"]
    if any(d in domain.lower() for d in high_risk_domains):
        score += 20

    # NFR stringency
    critical_nfrs = [n for n in nfrs if _is_critical_nfr(n)]
    score += len(critical_nfrs) * 5

    # Complexity tier from manifest
    tier = manifest.get("complexity_tier") or "moderate"
    tier_bonus = {"trivial": 0, "moderate": 10, "significant": 25, "complex": 40}
    score += tier_bonus.get(tier, 10)

    return score


# ---------------------------------------------------------------------------
# Step 2: Determine System Checkpoint Level
# ---------------------------------------------------------------------------


def determine_system_checkpoint_level(
    complexity_score: int,
    waves: list[list[str]],
    dag: nx.DiGraph,
    nfrs: list[dict],
    manifest: dict,
) -> tuple[str, list[str]]:
    """Determine wave_only or wave_plus_feature.

    Requires 2+ risk signals to upgrade to wave_plus_feature.
    Returns (level, upgrade_reasons).
    """
    upgrade_reasons: list[str] = []

    # Condition 1: High complexity score
    if complexity_score > 60:
        upgrade_reasons.append(f"complexity_score={complexity_score} > 60")

    # Condition 2: Complexity tier is significant or complex
    tier = manifest.get("complexity_tier") or "moderate"
    if tier in ("significant", "complex"):
        upgrade_reasons.append(f"complexity_tier={tier}")

    # Condition 3: Critical NFRs >= 3
    critical_nfrs = [n for n in nfrs if _is_critical_nfr(n)]
    if len(critical_nfrs) >= 3:
        upgrade_reasons.append(f"critical_nfrs={len(critical_nfrs)} >= 3")

    # Condition 4: High-risk domain
    domain = manifest.get("industry_domain_path", "") or ""
    high_risk_indicators = ["financial", "healthcare", "government", "payments", "hipaa", "pci"]
    if any(indicator in domain.lower() for indicator in high_risk_indicators):
        upgrade_reasons.append(f"high_risk_domain={domain}")

    # Condition 4: Deep DAG
    try:
        max_depth = nx.dag_longest_path_length(dag) if dag.number_of_nodes() > 0 else 0
    except nx.NetworkXError:
        max_depth = 0
    if max_depth > 5:
        upgrade_reasons.append(f"dag_depth={max_depth} > 5")

    # Condition 5: Large wave size (parallel work = integration risk)
    max_wave_size = max(len(w) for w in waves) if waves else 0
    if max_wave_size > 4:
        upgrade_reasons.append(f"max_wave_size={max_wave_size} > 4")

    # Condition 6: Many features (>12 = significant integration surface)
    total_features = dag.number_of_nodes()
    if total_features > 12:
        upgrade_reasons.append(f"total_features={total_features} > 12")

    # Decision: upgrade if 2+ conditions met
    level = "wave_plus_feature" if len(upgrade_reasons) >= 2 else "wave_only"

    return level, upgrade_reasons


# ---------------------------------------------------------------------------
# Step 3: Build Feature-Level Checks (when upgraded)
# ---------------------------------------------------------------------------


def build_feature_level_checks(
    dag: nx.DiGraph,
    standards_loaded: bool,
) -> dict[str, list[str]]:
    """Define per-feature validation checks for wave_plus_feature mode.

    Every feature gets the quality gate. Standards compliance is added when
    standards are loaded. There is no ``spec_validation`` entry: no producer
    runs that check any more, and naming one in the config would be a lie.
    """
    feature_checks: dict[str, list[str]] = {}

    for node in dag.nodes:
        attrs = dag.nodes[node]
        checks = []

        # Every feature gets quality gate
        checks.append("quality_gate")

        # Standards compliance if standards loaded
        if standards_loaded:
            checks.append("standards_compliance")

        # Security scan for security-tagged features
        tags = attrs.get("tags", [])
        name = (attrs.get("name", "") or attrs.get("description", "")).lower()
        security_tags = ["auth", "security", "payment", "encryption"]
        security_keywords = ["auth", "login", "payment", "encrypt", "credential", "token"]
        if any(t in tags for t in security_tags) or any(k in name for k in security_keywords):
            checks.append("security_scan")

        feature_checks[node] = checks

    return feature_checks


# ---------------------------------------------------------------------------
# Step 4: Place User Review Checkpoints
# ---------------------------------------------------------------------------


def place_user_review_checkpoints(
    waves: list[list[str]],
    complexity_score: int,
    manifest: dict,
) -> list[int]:
    """Determine where to place user review checkpoints.

    Returns 0-indexed wave numbers AFTER which a user review occurs: every 2 waves
    (the odd positions, since waves are 0-indexed) plus the last wave. An empty
    wave doesn't count; the last wave always does.

    Same rule as ``orchestrator.is_checkpoint_wave``, which gates the paired system
    checkpoint — the two must not drift. But that one re-derives from the live
    waves.json while this output is baked into ``checkpoint-config.yaml``, so
    **waves.json and checkpoint-config.yaml must always be written together**; any
    path changing ``len(waves)`` must call ``regenerate_checkpoint_config`` too.
    """
    if not waves:
        return []
    return sorted(
        {i for i in range(len(waves)) if i % 2 == 1 and waves[i]} | {len(waves) - 1}
    )


# ---------------------------------------------------------------------------
# Step 5: Generate Checkpoint Config
# ---------------------------------------------------------------------------


def _build_user_checkpoint_entries(
    checkpoint_waves: list[int],
    waves: list[list[str]],
    dag: nx.DiGraph,
    manifest: dict,
) -> list[dict]:
    """Build structured user review checkpoint entries."""
    entries = []
    for i, wave_idx in enumerate(checkpoint_waves):
        checkpoint_id = f"UCR-{i + 1:03d}"
        # Gather features completed up to and including this wave
        covered_features = []
        for w_idx in range(wave_idx + 1):
            if w_idx < len(waves):
                covered_features.extend(waves[w_idx])

        entries.append({
            "id": checkpoint_id,
            "after_wave": wave_idx,
            "covered_features": covered_features,
            "demo_checklist": [
                "System starts successfully",
                "All covered features are functional",
                "User can execute primary workflows",
            ],
            "feedback_questions": [
                "Are the implemented features meeting expectations?",
                "Is any functionality missing or incorrect?",
                "Are there performance or usability concerns?",
            ],
            "success_criteria": [
                "All automated system checks pass",
                "User demo completed without critical issues",
                "No blocking feedback items remain unaddressed",
            ],
        })
    return entries


def generate_checkpoint_config(
    manifest: dict,
    dag: nx.DiGraph,
    waves: list[list[str]],
    adrs: list[dict],
    nfrs: list[dict],
    standards_paths: list[Path] | None = None,
    feature_data: dict | list | None = None,
    integration: dict | list | None = None,
) -> dict:
    """Master function: generates the checkpoint configuration.

    Called during Plan phase after DAG and waves are computed.

    """
    complexity_score = compute_checkpoint_complexity(manifest, dag, waves, adrs, nfrs)
    sys_level, upgrade_reasons = determine_system_checkpoint_level(
        complexity_score, waves, dag, nfrs, manifest
    )
    user_checkpoint_waves = place_user_review_checkpoints(waves, complexity_score, manifest)
    standards_loaded = bool(standards_paths)

    # Build feature-level checks only if upgraded
    feature_checks: dict[str, list[str]] = {}
    if sys_level == "wave_plus_feature":
        feature_checks = build_feature_level_checks(dag, standards_loaded)

    verification_profiles = compute_verification_profiles(
        dag, feature_data, nfrs, adrs, integration
    )
    coverage_floors = _coverage_floors_from_profiles(verification_profiles)
    if coverage_floors:
        reasons = []
        for fid in sorted(coverage_floors):
            fanout = len(get_dependents(dag, fid))
            file_scope = dag.nodes[fid].get("file_scope") or []
            interface = any(
                marker in str(path).lower()
                for path in file_scope
                for marker in INTERFACE_PATH_MARKERS
            )
            why = []
            if fanout > FANOUT_THRESHOLD:
                why.append(f"fan-out={fanout}")
            if interface:
                why.append("interface")
            reasons.append(f"{fid} ({', '.join(why)})")
        print(
            f"⚙ checkpoint: coverage ratchet → {FANOUT_COVERAGE_FLOOR}% on "
            f"{len(coverage_floors)} feature(s): {'; '.join(reasons)}",
            file=sys.stderr,
        )

    # ``per_wave_checks`` is DECLARATIVE — nothing reads this list to decide what
    # runs. The orchestrator gates firing (``is_checkpoint_wave``), which is why
    # no ``cadence`` field is added here: keeping the schema still limits churn,
    # and every extra field in ``system_checkpoints`` invalidates in-flight
    # runtime evidence via ``runtime_criteria_identity``.
    #
    # ``regression_suite`` is deliberately absent: regression is no longer part
    # of the system checkpoint (it runs once, on the last wave, as its own step),
    # and the config must not advertise a per-wave check the loop never runs.
    system_checkpoints = {
        "per_wave_checks": {
            "enabled": True,
            "checks": [
                {"id": "startup_validation", "name": "System Startup Check", "automated": True},
                {"id": "smoke_tests", "name": "Integration Smoke Tests", "automated": True},
                {"id": "code_quality", "name": "Code Quality & Standards Check", "automated": True},
            ],
        },
        "per_feature_checks": {
            "enabled": sys_level == "wave_plus_feature",
            "checks_by_feature": feature_checks,
        },
    }

    config = {
        "checkpoint_configuration": {
            "project_name": manifest.get("project_name", "Unknown"),
            # Hash-neutral cadence marker — see CHECKPOINT_CONFIG_VERSION.
            "checkpoint_config_version": CHECKPOINT_CONFIG_VERSION,
            "total_waves": len(waves),
            "complexity_score": complexity_score,
            "system_checkpoint_level": sys_level,
            "upgrade_reasons": upgrade_reasons,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_profiles": verification_profiles,

            "system_checkpoints": system_checkpoints,

            "user_review_checkpoints": _build_user_checkpoint_entries(
                user_checkpoint_waves, waves, dag, manifest
            ),

            "standards": {
                "loaded": standards_loaded,
                "sources": [str(path) for path in (standards_paths or [])],
                "per_feature_coverage_floors": coverage_floors,
            },

            "user_notification": {
                "always_notify": ["critical_violations", "security_issues", "system_checkpoint_failures"],
                "summary_only": ["medium_violations", "quality_warnings"],
                "never_notify": ["low_priority_linting", "documentation_warnings"],
            },
        }
    }

    return config


# ---------------------------------------------------------------------------
# Project signal loading helpers
# ---------------------------------------------------------------------------


def _load_adrs(project_path: Path) -> list[dict]:
    """Load ADR files from .aah/analysis/decisions/."""
    adrs_dir = project_path / ".aah" / "analysis" / "decisions"
    from aah.core.plan.synthesize_analysis_context import extract_adrs

    adrs = extract_adrs(adrs_dir)
    if adrs_dir.is_dir():
        for f in sorted(adrs_dir.glob("*.yaml")):
            try:
                adrs.append(read_yaml(f))
            except Exception:
                continue
    return adrs


def _load_nfrs(project_path: Path) -> list[dict]:
    """Load NFRs from .aah analysis or Markdown feature contracts."""
    nfrs_file = project_path / ".aah" / "analysis" / "nfrs.yaml"
    if nfrs_file.exists():
        data = read_yaml(nfrs_file)
        return data.get("nfrs", []) if isinstance(data, dict) else []

    # Fallback: extract NFRs from Markdown feature frontmatter.
    features_dir = project_path / ".aah" / "plan" / "features"
    nfrs = []
    if features_dir.is_dir():
        for f in sorted(features_dir.glob("*.md")):
            data = parse_feature_frontmatter(f) or {}
            feature_nfrs = data.get("nfrs", []) or data.get("non_functional_requirements", [])
            if isinstance(feature_nfrs, list):
                nfrs.extend(feature_nfrs)
    return nfrs


def _find_standards(project_path: Path) -> list[Path]:
    """Find project and AAH knowledge standards files."""
    standards_dirs = [
        project_path / "knowledge" / "standards",
        project_path / ".aah" / "knowledge" / "standards",
    ]
    paths = []
    for directory in standards_dirs:
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.yaml")))
            paths.extend(sorted(directory.glob("*.yml")))
    return paths


def _load_checkpoint_inputs(project_path: Path) -> dict:
    """Load the canonical inputs shared by CLI and rework regeneration."""
    project_path = Path(project_path).resolve()
    aah_path = project_path / ".aah"

    manifest_path = aah_path / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError("manifest.yaml not found")
    manifest = read_yaml(manifest_path)

    dag_path = aah_path / "plan" / "dag.json"
    if not dag_path.exists():
        raise FileNotFoundError("dag.json not found; run build_dag first")
    dag = dag_from_json(read_json(dag_path))

    waves_path = aah_path / "plan" / "waves.json"
    if not waves_path.exists():
        raise FileNotFoundError("waves.json not found; run compute_waves first")
    from aah.core.plan.compute_waves import flatten_waves

    waves = flatten_waves(read_json(waves_path))
    synthesis_path = aah_path / "plan" / "analysis-synthesis.json"
    synthesis = read_json(synthesis_path) if synthesis_path.exists() else {}
    adrs = (
        synthesis["adr_inventory"]
        if "adr_inventory" in synthesis
        else _load_adrs(project_path)
    )
    nfrs = (
        synthesis["nfr_inventory"]
        if "nfr_inventory" in synthesis
        else _load_nfrs(project_path)
    )
    feature_data = {
        feature["id"]: feature
        for feature in load_features_from_dir(aah_path / "plan" / "features")
    }
    return {
        "manifest": manifest,
        "dag": dag,
        "waves": waves,
        "adrs": adrs,
        "nfrs": nfrs,
        "standards_paths": _find_standards(project_path),
        "feature_data": feature_data,
        "integration": synthesis.get("integration_inventory", []),
    }


def regenerate_checkpoint_config(project_path: Path) -> dict:
    """Compute and atomically replace checkpoint configuration on success."""
    project_path = Path(project_path).resolve()
    inputs = _load_checkpoint_inputs(project_path)
    config = generate_checkpoint_config(**inputs)
    write_yaml_atomic(
        config,
        project_path / ".aah" / "plan" / "checkpoint-config.yaml",
    )
    return config


# ---------------------------------------------------------------------------
# One-time cadence migration (5.1.1)
# ---------------------------------------------------------------------------


def _ucr_index(entry: dict) -> int:
    """Numeric suffix of a ``UCR-NNN`` id, or 0 when it cannot be parsed."""
    match = re.search(r"(\d+)$", str(entry.get("id") or ""))
    return int(match.group(1)) if match else 0


def checkpoint_config_needs_migration(project_path: Path) -> bool:
    """Whether this project's checkpoint-config predates the current cadence.

    True when a ``checkpoint-config.yaml`` exists and its
    ``checkpoint_config_version`` is absent or different from
    ``CHECKPOINT_CONFIG_VERSION``. An absent marker means the config was
    generated before 5.1.1, i.e. by the old ``place_user_review_checkpoints``
    that listed a user review after EVERY wave.

    Unreadable or malformed configs are False: a migration that cannot read what
    it is replacing must not run.
    """
    config_path = Path(project_path) / ".aah" / "plan" / "checkpoint-config.yaml"
    if not config_path.exists():
        return False
    try:
        config = read_yaml(config_path) or {}
    except Exception:
        return False
    if not isinstance(config, dict):
        return False
    cfg = config.get("checkpoint_configuration", config)
    if not isinstance(cfg, dict):
        return False
    return cfg.get("checkpoint_config_version") != CHECKPOINT_CONFIG_VERSION


def migrate_checkpoint_config(project_path: Path, current_wave: int) -> dict | None:
    """Rewrite an older ``checkpoint-config.yaml`` with the 5.1.1 cadence.

    Reuses ``generate_checkpoint_config`` (the same entry point
    ``create_rework_entry`` already regenerates through), so the new cadence
    comes from the single plan-time rule rather than a second implementation.

    Returns a summary dict describing the change, or ``None`` when nothing was
    done (already current, no config, or the inputs cannot be loaded).

    Two invariants:

    * **Idempotent.** Regenerating with the same waves and cadence yields the
      same config, and the refreshed version marker makes a re-run a no-op.
    * **Reached checkpoints are never re-placed.** Only user-review entries for
      waves STRICTLY AFTER ``current_wave`` are replaced. An entry for the
      in-progress or an already-passed wave is preserved verbatim — including
      its id — so a recorded approval is never invalidated by a checkpoint
      appearing, moving, or vanishing underneath it. New future entries are
      numbered above every preserved id so ids stay unique.

    Callers MUST surface ``forced_runtime_rerun`` when it is true: editing
    ``user_review_checkpoints`` changes what ``runtime_criteria_identity``
    hashes, so in-flight runtime evidence is invalidated with
    ``runtime_criteria_drift`` and the system checkpoint re-runs once. That is
    correct behaviour with an alarming presentation — unannounced it reads as a
    mystery re-dispatch.
    """
    project_path = Path(project_path).resolve()
    config_path = project_path / ".aah" / "plan" / "checkpoint-config.yaml"
    if not checkpoint_config_needs_migration(project_path):
        return None

    try:
        old_config = read_yaml(config_path) or {}
        old_cfg = old_config.get("checkpoint_configuration", old_config)
        old_ucrs = [
            entry for entry in (old_cfg.get("user_review_checkpoints") or [])
            if isinstance(entry, dict)
        ]
        inputs = _load_checkpoint_inputs(project_path)
        new_config = generate_checkpoint_config(**inputs)
    except Exception as exc:  # noqa: BLE001 — a migration must never break resume
        print(
            f"warning: checkpoint-config migration skipped ({exc})",
            file=sys.stderr,
        )
        return None

    new_cfg = new_config["checkpoint_configuration"]
    new_ucrs = [
        entry for entry in new_cfg.get("user_review_checkpoints", [])
        if isinstance(entry, dict)
    ]

    preserved = [e for e in old_ucrs if int(e.get("after_wave", -1)) <= current_wave]
    future = [e for e in new_ucrs if int(e.get("after_wave", -1)) > current_wave]

    next_index = max([_ucr_index(e) for e in preserved] or [0]) + 1
    for entry in sorted(future, key=lambda e: int(e.get("after_wave", -1))):
        entry["id"] = f"UCR-{next_index:03d}"
        next_index += 1

    merged = sorted(
        preserved + future, key=lambda e: int(e.get("after_wave", -1))
    )
    new_cfg["user_review_checkpoints"] = merged

    old_waves = sorted(int(e.get("after_wave", -1)) for e in old_ucrs)
    new_waves = sorted(int(e.get("after_wave", -1)) for e in merged)

    write_yaml_atomic(new_config, config_path)

    return {
        "migrated": True,
        "from_version": old_cfg.get("checkpoint_config_version"),
        "to_version": CHECKPOINT_CONFIG_VERSION,
        "current_wave": current_wave,
        "preserved_checkpoint_waves": [
            int(e.get("after_wave", -1)) for e in preserved
        ],
        "user_review_waves_before": old_waves,
        "user_review_waves_after": new_waves,
        # True whenever the hashed blocks actually moved. Regression left
        # per_wave_checks too, so system_checkpoints changes on every pre-5.1.1
        # config even when the UCR set happens to be identical.
        "forced_runtime_rerun": (
            old_waves != new_waves
            or old_cfg.get("system_checkpoints") != new_cfg.get("system_checkpoints")
        ),
        "notice": (
            "Checkpoint cadence migrated to 5.1.1: the system checkpoint and the "
            "user review now fire on odd 0-indexed waves plus the last wave "
            f"(user reviews: {old_waves} → {new_waves}), and regression left the "
            "system checkpoint for a single last-wave run. Checkpoints for wave "
            f"{current_wave} and earlier were preserved untouched. This edits the "
            "blocks runtime_criteria_identity hashes, so any in-flight runtime "
            "evidence is now runtime_criteria_drift and the system checkpoint "
            "re-runs ONCE. That is expected, not a fault."
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Checkpoint Determination Engine")
    sub = parser.add_subparsers(dest="command", required=True)

    gen_p = sub.add_parser("generate", help="Generate checkpoint-config.yaml")
    gen_p.add_argument("--project-path", type=Path, default=None)

    show_p = sub.add_parser("show", help="Show current checkpoint config")
    show_p.add_argument("--project-path", type=Path, default=None)

    score_p = sub.add_parser("score", help="Show complexity score breakdown")
    score_p.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    # Resolve project path
    from aah.core.common.config import require_project_path
    project_path = require_project_path(
        getattr(args, "project_path", None)
    )
    aah_path = project_path / ".aah"

    if args.command == "show":
        config_path = aah_path / "plan" / "checkpoint-config.yaml"
        if not config_path.exists():
            print("Error: checkpoint-config.yaml not found. Run 'generate' first.", file=sys.stderr)
            sys.exit(1)
        data = read_yaml(config_path)
        json.dump(data, sys.stdout, indent=2)
        print()
        sys.exit(0)

    if args.command == "score":
        try:
            inputs = _load_checkpoint_inputs(project_path)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        manifest = inputs["manifest"]
        dag = inputs["dag"]
        waves = inputs["waves"]
        adrs = inputs["adrs"]
        nfrs = inputs["nfrs"]
        score = compute_checkpoint_complexity(manifest, dag, waves, adrs, nfrs)
        level, reasons = determine_system_checkpoint_level(
            score, waves, dag, nfrs, manifest
        )
        result = {
            "complexity_score": score,
            "system_checkpoint_level": level,
            "upgrade_reasons": reasons,
            "signals": {
                "total_waves": len(waves),
                "total_features": dag.number_of_nodes(),
                "dag_depth": nx.dag_longest_path_length(dag) if dag.number_of_nodes() > 0 else 0,
                "adr_count": len(adrs),
                "critical_nfr_count": len([n for n in nfrs if _is_critical_nfr(n)]),
                "complexity_tier": manifest.get("complexity_tier"),
                "industry_domain": manifest.get("industry_domain_path"),
                "max_wave_size": max(len(w) for w in waves) if waves else 0,
                "standards_loaded": bool(inputs["standards_paths"]),
            },
        }
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0)

    output_path = aah_path / "plan" / "checkpoint-config.yaml"
    try:
        config = regenerate_checkpoint_config(project_path)
    except Exception as exc:
        label = type(exc).__name__
        print(
            f"Error: verification profile generation failed ({label}: {exc})",
            file=sys.stderr,
        )
        sys.exit(1)

    # Output to stdout
    json.dump(config, sys.stdout, indent=2)
    print()
    print(f"Checkpoint config written to {output_path}", file=sys.stderr)
    print(f"  Level: {config['checkpoint_configuration']['system_checkpoint_level']}", file=sys.stderr)
    print(f"  Score: {config['checkpoint_configuration']['complexity_score']}", file=sys.stderr)
    print(f"  User checkpoints: {len(config['checkpoint_configuration']['user_review_checkpoints'])}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
