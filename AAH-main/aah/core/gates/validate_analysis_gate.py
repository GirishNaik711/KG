#!/usr/bin/env python3
"""
Stop hook: validate analysis phase completion before transitioning.

Checks decision registry resolution (v2 path) or activity plan completion
(legacy path). Exit 0 = pass, Exit 2 = fail.
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml


def validate_registry_resolution(rapids_path: Path) -> list[str]:
    """Validate all decisions in the registry are resolved or skipped."""
    issues = []
    registry_path = rapids_path.parent / ".aah" / "decision-registry.yaml"
    registry = read_yaml(registry_path)

    decisions = registry.get("decisions", [])
    unresolved = []
    for d in decisions:
        status = d.get("status", "open")
        if status not in ("resolved", "skipped"):
            unresolved.append(f"{d.get('ddr_id', '?')} ({status})")

    if unresolved:
        issues.append(
            f"{len(unresolved)} decisions not resolved/skipped: "
            + ", ".join(unresolved[:10])
            + ("..." if len(unresolved) > 10 else "")
        )

    return issues


def validate_registry_adrs(rapids_path: Path) -> list[str]:
    """Validate ADR exists for each resolved decision."""
    issues = []
    registry_path = rapids_path.parent / ".aah" / "decision-registry.yaml"
    registry = read_yaml(registry_path)
    decisions_dir = rapids_path / "analysis" / "decisions"

    for d in registry.get("decisions", []):
        if d.get("status") != "resolved":
            continue
        ddr_id = d.get("ddr_id", "")
        adr_path_str = d.get("adr_path")

        if adr_path_str:
            adr_path = rapids_path / adr_path_str
            if not adr_path.exists():
                issues.append(f"{ddr_id}: adr_path '{adr_path_str}' does not exist")
        else:
            adr_filename = ddr_id.replace("DDR-", "ADR-", 1) + ".md"
            adr_path = decisions_dir / adr_filename
            if not adr_path.exists():
                issues.append(
                    f"{ddr_id}: resolved but no ADR found at "
                    f"analysis/decisions/{adr_filename}"
                )

    return issues


def validate_adr_quality(rapids_path: Path) -> list[str]:
    """Validate ADR content quality for DDR-based ADRs."""
    issues = []
    decisions_dir = rapids_path / "analysis" / "decisions"

    if not decisions_dir.exists():
        return []

    for adr_path in sorted(decisions_dir.glob("ADR-*.md")):
        try:
            content = adr_path.read_text(encoding="utf-8")
        except Exception:
            continue

        if "## Forces in Tension" not in content:
            issues.append(
                f"{adr_path.name}: missing '## Forces in Tension' section"
            )

        if "## Rejected Options" not in content:
            issues.append(
                f"{adr_path.name}: missing '## Rejected Options' section"
            )
            continue

        lines = content.splitlines()
        in_rejected = False
        rejected_text_len = 0

        for line in lines:
            if line.strip() == "## Rejected Options":
                in_rejected = True
                continue
            if in_rejected and line.startswith("## "):
                break
            if in_rejected:
                stripped = line.strip()
                if (stripped.startswith("|")
                        and "---|" not in stripped
                        and "Option" not in stripped
                        and stripped != "|"):
                    rejected_text_len += len(stripped)

        if in_rejected and rejected_text_len < 40:
            issues.append(
                f"{adr_path.name}: Rejected Options section too thin — "
                "each rejected option needs a substantive reason (>10 words)"
            )

        if "## Rationale" in content or "## Decision" in content:
            rationale_start = content.find("## Rationale")
            if rationale_start == -1:
                rationale_start = content.find("## Decision")
            if rationale_start >= 0:
                forces_section = ""
                forces_start = content.find("## Forces in Tension")
                if forces_start >= 0:
                    forces_end = content.find("\n## ", forces_start + 1)
                    if forces_end > 0:
                        forces_section = content[forces_start:forces_end]

                force_names = []
                for line in forces_section.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- **") or stripped.startswith("| **"):
                        name_part = stripped.split("**")[1] if "**" in stripped else ""
                        if name_part:
                            force_names.append(name_part.lower())

                if force_names:
                    rationale_end = content.find("\n## ", rationale_start + 1)
                    rationale_text = content[rationale_start:rationale_end if rationale_end > 0 else len(content)].lower()
                    if not any(fn in rationale_text for fn in force_names):
                        issues.append(
                            f"{adr_path.name}: Rationale does not reference any force name — "
                            "rationale should explain which force drove the decision"
                        )

    return issues


def validate_cross_cutting_artifacts(rapids_path: Path, synthesis_mode: str = "standard") -> list[str]:
    """Validate cross-cutting analysis artifacts exist (mode-aware)."""
    issues = []
    analysis_dir = rapids_path / "analysis"

    # Always required in all modes
    always_required = [
        ("adr-digest.yaml", "ADR digest"),
        ("nfr-analysis.md", "NFR analysis register"),
        ("security-review.md", "Security review document"),
        ("solution-architecture.md", "Solution architecture document"),
    ]

    # solution-integration.md only required in standard/full
    if synthesis_mode in ("standard", "full"):
        always_required.append(("solution-integration.md", "Solution integration map"))

    # Review findings only required in standard/full
    if synthesis_mode in ("standard", "full"):
        always_required.append(("architecture-review-findings.md", "Architecture review findings"))

    for filename, label in always_required:
        if not (analysis_dir / filename).exists():
            issues.append(f"Missing {label}: analysis/{filename}")

    # Risk acknowledgment required in ALL modes
    risk_ack = analysis_dir / "risk-acknowledgment.yaml"
    if not risk_ack.exists():
        issues.append(
            "Missing risk acknowledgment: analysis/risk-acknowledgment.yaml "
            "(user must acknowledge risks before phase closes)"
        )

    # ADR digest freshness check
    digest_path = analysis_dir / "adr-digest.yaml"
    if digest_path.exists():
        digest_mtime = digest_path.stat().st_mtime
        decisions_dir = rapids_path / "analysis" / "decisions"
        if decisions_dir.exists():
            stale_adrs = [
                f.name for f in decisions_dir.glob("ADR-*.md")
                if f.stat().st_mtime > digest_mtime
            ]
            if stale_adrs:
                issues.append(
                    f"ADR digest is stale — {len(stale_adrs)} ADR(s) modified after digest generation: "
                    + ", ".join(stale_adrs[:5])
                    + ". Re-run build_adr_digest."
                )

    return issues


def validate_cloud_readiness(rapids_path: Path) -> list[str]:
    """
    Enforce that Step 6.5 (Cloud Readiness Validation) was executed.
    The user must have either run the gate (passed/skipped) or explicitly
    chosen local-only deployment. A missing cloud-readiness.yaml means
    Step 6.5 was bypassed entirely, which is not allowed.

    Checks two locations:
    - .aah/cloud-readiness.yaml (written by interactive gate)
    - .aah/analysis/cloud-readiness.yaml (written by managed-deploy validator)
    """
    issues = []

    # Check both possible locations — interactive gate writes to root,
    # managed-deploy validator (cloudless/cloud-run/ecs-express) writes to analysis/
    cloud_readiness_path = rapids_path / "cloud-readiness.yaml"
    if not cloud_readiness_path.exists():
        cloud_readiness_path = rapids_path / "analysis" / "cloud-readiness.yaml"

    if not cloud_readiness_path.exists():
        issues.append(
            "Missing .aah/cloud-readiness.yaml — Step 6.5 (Cloud Readiness "
            "Validation) was not executed. Run: aah run "
            "aah.core.cloud.registry_extractor --project-path <path> "
            "and then validate_cloud_readiness, OR explicitly skip with "
            "--deployment-method local-only"
        )
        return issues

    state = read_yaml(cloud_readiness_path)
    gate_status = state.get("gate_status", "")
    overall_status = state.get("overall_status", "")
    deployment_method = state.get("deployment_method", "")

    # Full interactive gate schema uses "gate_status"
    if gate_status == "passed":
        return []
    if deployment_method == "local-only":
        return []
    if gate_status == "skipped":
        return []

    # Managed-deploy validator schema uses "overall_status"
    if overall_status == "passed":
        return []
    if overall_status == "skipped":
        return []
    if overall_status == "failed":
        # Check for critical failures in validated_services
        validated_services = state.get("validated_services", [])
        critical_failures = [
            s for s in validated_services
            if s.get("status") in ("unavailable", "fail")
            and s.get("criticality") == "critical"
        ]
        if critical_failures:
            names = [s.get("service_name", s.get("display_name", "?")) for s in critical_failures[:5]]
            issues.append(
                f"Cloud readiness failed — {len(critical_failures)} critical "
                f"service(s) unavailable: {', '.join(names)}. "
                "Fix cloud credentials/permissions and re-run validation."
            )
        return issues

    # Full interactive gate pending state
    if gate_status == "pending":
        services = state.get("services", [])
        overrides = state.get("overrides", [])
        overridden_ids = {o.get("service_id") for o in overrides if o.get("service_id")}
        overridden_types = {o.get("service_type") for o in overrides if o.get("service_type")}

        def is_overridden(svc: dict) -> bool:
            return (
                svc.get("id") in overridden_ids
                or svc.get("service_type") in overridden_types
                or svc.get("type") in overridden_types
            )

        failed = [s for s in services if s.get("status") == "fail" and not is_overridden(s)]
        not_tested = [s for s in services if s.get("status") == "not-tested" and not is_overridden(s)]
        critical_open = [
            s for s in (failed + not_tested)
            if s.get("criticality") == "critical"
        ]
        if critical_open:
            issues.append(
                f"Cloud readiness pending — {len(critical_open)} critical "
                f"service(s) unresolved (not tested or failed): "
                + ", ".join(s.get("name", s.get("display_name", "?")) for s in critical_open[:5])
                + ". Either re-run the gate to test them, or add an override "
                "in cloud-readiness.yaml under 'overrides' to acknowledge."
            )

    return issues


def validate_data_readiness(rapids_path: Path) -> list[str]:
    """
    Validate that Step 6.6 (Data Readiness Validation) was executed.
    Warning-level if file missing (backwards compat), blocking if
    pending with critical failures.
    """
    issues = []
    data_readiness_path = rapids_path / "data-readiness.yaml"

    if not data_readiness_path.exists():
        print(
            "  Warning: .aah/data-readiness.yaml not found -- "
            "Step 6.6 (Data Readiness) was not executed",
            file=sys.stderr,
        )
        return []

    state = read_yaml(data_readiness_path)
    gate_status = state.get("gate_status", "")

    if gate_status in ("passed", "skipped"):
        return []

    if gate_status == "pending":
        checks = state.get("checks", [])
        overrides = state.get("overrides", [])
        overridden_types = {o.get("service_type") for o in overrides}

        failed = [
            c for c in checks
            if c.get("status") == "fail"
            and c.get("service_type") not in overridden_types
        ]
        if failed:
            names = [c.get("display_name", c.get("service_type", "?")) for c in failed[:5]]
            issues.append(
                f"Data readiness pending -- {len(failed)} service(s) have unresolved "
                f"failures: {', '.join(names)}. Re-run validation or add overrides."
            )

    return issues


def validate_embedded_diagrams(rapids_path: Path, synthesis_mode: str = "standard") -> list[str]:
    """Check for embedded Mermaid diagrams in analysis artifacts (warning-level, mode-aware)."""
    warnings = []
    analysis_dir = rapids_path / "analysis"

    # Integration diagrams only checked in standard/full
    if synthesis_mode in ("standard", "full"):
        integration_md = analysis_dir / "solution-integration.md"
        if integration_md.exists():
            content = integration_md.read_text(encoding="utf-8")
            if content.count("```mermaid") < 1:
                warnings.append(
                    "solution-integration.md has no embedded Mermaid diagram "
                    "(expected integration flow diagram)"
                )

    # Architecture diagrams — mode-aware minimum
    min_arch_diagrams = 1 if synthesis_mode == "light" else 2
    architecture_md = analysis_dir / "solution-architecture.md"
    if architecture_md.exists():
        content = architecture_md.read_text(encoding="utf-8")
        count = content.count("```mermaid")
        if count < min_arch_diagrams:
            warnings.append(
                f"solution-architecture.md has {count} embedded Mermaid diagram(s) "
                f"(expected at least {min_arch_diagrams})"
            )

    return warnings


def validate_analysis_gate(project_path: Path) -> tuple[bool, list[str]]:
    """Validate analysis phase completion."""
    issues = []
    rapids_path = project_path / ".aah"

    manifest_path = rapids_path / "manifest.yaml"
    if not manifest_path.exists():
        return True, []

    manifest = read_yaml(manifest_path)
    if manifest.get("current_phase") != "analysis":
        return True, []

    # Read synthesis_mode from manifest (backward-compatible default: standard)
    synthesis_mode = manifest.get("synthesis_mode", "standard")

    registry_path = project_path / ".aah" / "decision-registry.yaml"
    if registry_path.exists():
        issues += validate_registry_resolution(rapids_path)
        issues += validate_registry_adrs(rapids_path)
        issues += validate_adr_quality(rapids_path)
        issues += validate_cross_cutting_artifacts(rapids_path, synthesis_mode)
        issues += validate_cloud_readiness(rapids_path)
        issues += validate_data_readiness(rapids_path)

        # Embedded Mermaid diagrams — warning only, does not block gate
        diagram_warnings = validate_embedded_diagrams(rapids_path, synthesis_mode)
        for w in diagram_warnings:
            print(f"  Warning: {w}", file=sys.stderr)
    else:
        issues += _validate_legacy_analysis(rapids_path)

    if issues:
        return False, issues

    return True, []


def _validate_legacy_analysis(rapids_path: Path) -> list[str]:
    """Validate legacy activity-plan.yaml based analysis."""
    issues = []
    activity_plan_path = rapids_path / "activity-plan.yaml"
    if not activity_plan_path.exists():
        issues.append(
            "Neither decision-registry.yaml nor activity-plan.yaml found — "
            "no analysis plan defined"
        )
        return issues

    activity_plan = read_yaml(activity_plan_path)
    activities = activity_plan.get("activities", [])
    analysis_activities = [a for a in activities if a.get("phase") == "analysis"]

    if not analysis_activities:
        return []

    analysis_dir = rapids_path / "analysis"
    for activity in analysis_activities:
        activity_id = activity.get("activity_id", activity.get("id", "unknown"))
        status = activity.get("status", "pending")

        if status != "completed":
            issues.append(
                f"Activity '{activity_id}': status is '{status}' (expected 'completed')"
            )

        for artifact in activity.get("artifacts", []):
            artifact_name = artifact if isinstance(artifact, str) else artifact.get("file", artifact.get("template", ""))
            if artifact_name:
                artifact_path = analysis_dir / artifact_name
                if not artifact_path.exists():
                    candidates = [
                        analysis_dir / f"{artifact_name}.md",
                        analysis_dir / artifact_name.replace(".md", ""),
                    ]
                    if not any(c.exists() for c in candidates):
                        issues.append(f"Activity '{activity_id}': missing artifact '{artifact_name}'")

    if analysis_dir.is_dir():
        analysis_files = [
            f for f in analysis_dir.iterdir()
            if f.is_file() and f.suffix in (".md", ".yaml", ".json")
        ]
        if not analysis_files:
            issues.append("No analysis artifacts found in .aah/analysis/")
    else:
        issues.append("Missing .aah/analysis/ directory")

    decisions_dir = rapids_path / "analysis" / "decisions"
    has_adr_activity = any(
        "architecture" in (a.get("activity_id", "") or "").lower()
        or "decision" in (a.get("activity_id", "") or "").lower()
        for a in analysis_activities
    )
    if has_adr_activity:
        if decisions_dir.is_dir():
            adrs = list(decisions_dir.glob("*.md"))
            if not adrs:
                issues.append("No Architecture Decision Records (ADRs) found in analysis/decisions/")
        else:
            issues.append("Missing analysis/decisions/ directory")

    return issues


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

    passed, issues = validate_analysis_gate(project_path)

    if not passed:
        print("Analysis phase gate FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
