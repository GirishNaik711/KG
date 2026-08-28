#!/usr/bin/env python3
"""
Integration test: Validates the full cloudless routing pipeline.

Tests the chain: DDRs -> detection -> template selection -> gate validation
WITHOUT needing actual cloud credentials or cloudless installed.

Run: python -m pytest tests/integration/test_cloudless_routing_e2e.py -v
"""

import pytest
from pathlib import Path

from aah.core.common.io_utils import write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.deploy.deploy_routing import (
    is_langgraph_project,
    is_cloudless_deployment,
    get_deployment_route,
)
from aah.core.cloud.registry_extractor import extract_services_from_registry
from aah.core.gates.validate_analysis_gate import validate_analysis_gate
from aah.core.registry.registry import init_registry, save_registry, update_decision


def _write_registry(
    path: Path,
    resolutions: dict[str, tuple[str, str | None]],
) -> None:
    """Build a complete registry through official state-machine writers."""
    registry = init_registry("cloudless-routing-test", "ai-applications")
    for decision in registry["decisions"]:
        update_decision(registry, decision["ddr_id"], status="skipped")
    for ddr_id, (option, adr_path) in resolutions.items():
        update_decision(
            registry,
            ddr_id,
            status="resolved",
            resolved=option,
            confirmed=True,
            adr_path=adr_path,
        )
    save_registry(registry, path)


def _write_project_state(
    project: Path,
    *,
    project_name: str,
    current_phase: str,
    ai: str,
    cloud: str,
    resolutions: dict[str, tuple[str, str | None]],
) -> None:
    manifest = get_default_manifest(project_name)
    manifest["current_phase"] = current_phase
    manifest["stack_choices"] = {"ai": ai, "cloud": cloud}
    # .aah is authoritative. The .rapids copies are official-writer legacy
    # decoys retained only for deploy-routing consumers outside this scope.
    save_manifest(manifest, project / ".aah" / "manifest.yaml")
    save_manifest(manifest, project / ".rapids" / "manifest.yaml")
    _write_registry(project / ".aah" / "decision-registry.yaml", resolutions)
    _write_registry(project / ".rapids" / "decision-registry.yaml", resolutions)


@pytest.fixture
def cloudless_project(tmp_path):
    """
    Create a fully-configured RAPIDS project that simulates
    a LangGraph + cloudless-managed selection.
    """
    rapids = tmp_path / ".rapids"
    rapids.mkdir()
    (rapids / "analysis").mkdir()
    (rapids / "analysis" / "decisions").mkdir()

    _write_project_state(
        tmp_path,
        project_name="test-agent",
        current_phase="analysis",
        ai="aws-bedrock-langgraph",
        cloud="aws",
        resolutions={
            "DDR-L2-001": ("langgraph", "analysis/decisions/ADR-L2-001.md"),
            "DDR-SHARED-017": (
                "cloudless-managed",
                "analysis/decisions/ADR-L9-001.md",
            ),
        },
    )

    # Minimal ADRs to pass gate
    (rapids / "analysis" / "decisions" / "ADR-L2-001.md").write_text(
        "# ADR-L2-001: LangGraph Framework\n\n"
        "## Forces in Tension\n- **Control vs. abstraction**: LangGraph provides explicit graph control.\n\n"
        "## Selected Option\nlanggraph\n\n"
        "## Rationale\nGraph-based control fits complex workflows. Control vs. abstraction favours langgraph.\n\n"
        "## Rejected Options\n| Option | Reason |\n|---|---|\n| langchain | Too much hidden abstraction for stateful workflows |\n"
    )
    (rapids / "analysis" / "decisions" / "ADR-L9-001.md").write_text(
        "# ADR-L9-001: Backend Compute\n\n"
        "## Forces in Tension\n- **Cost vs. availability**: Managed runtime handles both.\n\n"
        "## Selected Option\ncloudless-managed\n\n"
        "## Rationale\nZero ops, auto-scaling. Cost vs. availability favours cloudless-managed.\n\n"
        "## Rejected Options\n| Option | Reason |\n|---|---|\n| container-based | Requires infrastructure management we want to avoid |\n"
    )

    # Cross-cutting artifacts (required by gate)
    (rapids / "analysis" / "solution-integration.md").write_text(
        "# Solution Integration\n```mermaid\nflowchart LR\n  A-->B\n```\n"
    )
    (rapids / "analysis" / "nfr-analysis.md").write_text("# NFR Analysis\n")
    (rapids / "analysis" / "security-review.md").write_text("# Security Review\n")
    (rapids / "analysis" / "solution-architecture.md").write_text(
        "# Solution Architecture\n```mermaid\nflowchart TD\n  A-->B\n```\n```mermaid\nC4Context\n  System(x)\n```\n"
    )
    (rapids / "analysis" / "adr-digest.yaml").write_text("decisions: []\n")
    write_yaml(
        {"acknowledged": True, "acknowledged_by": "fixture-reviewer"},
        rapids / "analysis" / "risk-acknowledgment.yaml",
    )

    return tmp_path


@pytest.fixture
def standard_project(tmp_path):
    """A project that does NOT use cloudless."""
    rapids = tmp_path / ".rapids"
    rapids.mkdir()
    (rapids / "analysis").mkdir()

    _write_project_state(
        tmp_path,
        project_name="test-standard",
        current_phase="deploy",
        ai="langchain",
        cloud="gcp",
        resolutions={
            "DDR-L2-001": ("langchain", None),
            "DDR-SHARED-017": ("self-managed", None),
        },
    )

    return tmp_path


class TestCloudlessRoutingE2E:
    """End-to-end test of the full routing decision chain."""

    def test_full_chain_cloudless_project(self, cloudless_project):
        """DDRs -> detection -> extraction -> route = cloudless."""
        # Step 1: Detection
        assert is_langgraph_project(cloudless_project) is True
        assert is_cloudless_deployment(cloudless_project) is True
        assert get_deployment_route(cloudless_project) == "cloudless"

        # Step 2: Service extraction
        result = extract_services_from_registry(cloudless_project)
        assert result["target_cloud"] == "aws"
        service_names = [s["service_name"] for s in result["services"]]
        assert "bedrock-agentcore" in service_names
        assert "ecr" in service_names

        # Step 3: Critical services flagged
        for svc in result["services"]:
            if svc["service_name"] == "bedrock-agentcore":
                assert svc["criticality"] == "critical"

    def test_full_chain_standard_project(self, standard_project):
        """Non-cloudless project routes to standard."""
        assert is_langgraph_project(standard_project) is False
        assert is_cloudless_deployment(standard_project) is False
        assert get_deployment_route(standard_project) == "standard"

        result = extract_services_from_registry(standard_project)
        assert result["services"] == []

    def test_analysis_gate_blocks_without_cloud_readiness(self, cloudless_project):
        """Analysis gate blocks if cloudless selected but no cloud-readiness.yaml."""
        passed, issues = validate_analysis_gate(cloudless_project)
        assert passed is False
        assert any("cloud-readiness.yaml" in issue for issue in issues)

    def test_analysis_gate_passes_with_cloud_readiness(self, cloudless_project):
        """Analysis gate passes when cloud-readiness.yaml shows all services available."""
        write_yaml({
            "overall_status": "passed",
            "critical_failures": 0,
            "validated_services": [
                {"service_name": "bedrock-agentcore", "status": "available", "criticality": "critical"},
                {"service_name": "ecr", "status": "available", "criticality": "critical"},
            ],
        }, cloudless_project / ".rapids" / "cloud-readiness.yaml")

        passed, issues = validate_analysis_gate(cloudless_project)
        assert passed is True, issues
        assert issues == []

    def test_analysis_gate_blocks_on_critical_failure(self, cloudless_project):
        """Analysis gate blocks when critical service is unavailable."""
        write_yaml({
            "overall_status": "failed",
            "critical_failures": 1,
            "validated_services": [
                {"service_name": "bedrock-agentcore", "status": "unavailable",
                 "criticality": "critical", "error": "No credentials"},
                {"service_name": "ecr", "status": "available", "criticality": "critical"},
            ],
        }, cloudless_project / ".rapids" / "cloud-readiness.yaml")

        passed, issues = validate_analysis_gate(cloudless_project)
        assert passed is False
        assert any("critical" in issue.lower() or "unavailable" in issue.lower() for issue in issues)


class TestGCPCloudlessRouting:
    """Same chain but for GCP path."""

    @pytest.fixture
    def gcp_project(self, tmp_path):
        rapids = tmp_path / ".rapids"
        rapids.mkdir()
        (rapids / "analysis").mkdir()
        (rapids / "analysis" / "decisions").mkdir()

        _write_project_state(
            tmp_path,
            project_name="test-gcp-agent",
            current_phase="analysis",
            ai="vertex-langgraph",
            cloud="gcp",
            resolutions={
                "DDR-L2-001": ("langgraph", "analysis/decisions/ADR-L2-001.md"),
                "DDR-SHARED-017": (
                    "cloudless-managed",
                    "analysis/decisions/ADR-L9-001.md",
                ),
            },
        )

        (rapids / "analysis" / "decisions" / "ADR-L2-001.md").write_text(
            "# ADR\n\n## Forces in Tension\n- **Control vs abstraction**: x\n\n## Selected Option\nX\n\n## Rationale\nControl vs abstraction favours this.\n\n## Rejected Options\n| O | R |\n|---|---|\n| langchain | Not suitable for graph workflows |\n"
        )
        (rapids / "analysis" / "decisions" / "ADR-L9-001.md").write_text(
            "# ADR\n\n## Forces in Tension\n- **Cost vs availability**: x\n\n## Selected Option\nX\n\n## Rationale\nCost vs availability favours managed.\n\n## Rejected Options\n| O | R |\n|---|---|\n| serverless | Timeout constraints too restrictive for workflow |\n"
        )
        (rapids / "analysis" / "solution-integration.md").write_text("# SI\n```mermaid\nA-->B\n```\n")
        (rapids / "analysis" / "nfr-analysis.md").write_text("# NFR\n")
        (rapids / "analysis" / "security-review.md").write_text("# Sec\n")
        (rapids / "analysis" / "solution-architecture.md").write_text("# SA\n```mermaid\nA\n```\n```mermaid\nB\n```\n")
        (rapids / "analysis" / "adr-digest.yaml").write_text("decisions: []\n")
        write_yaml(
            {"acknowledged": True, "acknowledged_by": "fixture-reviewer"},
            rapids / "analysis" / "risk-acknowledgment.yaml",
        )

        return tmp_path

    def test_gcp_extracts_correct_services(self, gcp_project):
        result = extract_services_from_registry(gcp_project)
        assert result["target_cloud"] == "gcp"
        service_names = [s["service_name"] for s in result["services"]]
        assert "vertex-agent-engine" in service_names
        assert "gcs" in service_names

    def test_gcp_routes_to_cloudless(self, gcp_project):
        assert get_deployment_route(gcp_project) == "cloudless"
