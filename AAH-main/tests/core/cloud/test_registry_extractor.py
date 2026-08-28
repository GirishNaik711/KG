#!/usr/bin/env python3
"""Unit tests for cloud/registry_extractor.py."""

import pytest
from pathlib import Path

from aah.core.cloud.registry_extractor import extract_services_from_registry
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.registry.registry import init_registry, save_registry, update_decision


@pytest.fixture
def project(tmp_path):
    aah_dir = tmp_path / ".aah"
    aah_dir.mkdir()
    return tmp_path


def write_authoritative_state(
    project: Path,
    decisions: dict[str, str],
    *,
    ai: str = "",
    cloud: str = "",
) -> None:
    registry = init_registry("extractor-test", "ai-applications")
    for decision in registry["decisions"]:
        update_decision(registry, decision["ddr_id"], status="skipped")
    for ddr_id, option in decisions.items():
        update_decision(
            registry,
            ddr_id,
            status="resolved",
            resolved=option,
            confirmed=True,
        )
    save_registry(registry, project / ".aah" / "decision-registry.yaml")

    manifest = get_default_manifest("extractor-test")
    manifest["stack_choices"] = {"ai": ai, "cloud": cloud}
    save_manifest(manifest, project / ".aah" / "manifest.yaml")


class TestExtractServicesFromRegistry:
    def test_aws_cloudless_extracts_services(self, project):
        write_authoritative_state(
            project,
            {"DDR-L2-001": "langgraph", "DDR-SHARED-017": "cloudless-managed"},
            ai="aws-bedrock-langgraph",
            cloud="aws",
        )

        result = extract_services_from_registry(project)
        assert result["target_cloud"] == "aws"
        assert result["is_cloudless"] is True
        service_names = [s["service_name"] for s in result["services"]]
        assert "bedrock-agentcore" in service_names
        assert "ecr" in service_names

    def test_gcp_cloudless_extracts_services(self, project):
        write_authoritative_state(
            project,
            {"DDR-L2-001": "langgraph", "DDR-SHARED-017": "cloudless-managed"},
            ai="vertex-langgraph",
            cloud="gcp",
        )

        result = extract_services_from_registry(project)
        assert result["target_cloud"] == "gcp"
        assert result["is_cloudless"] is True
        service_names = [s["service_name"] for s in result["services"]]
        assert "vertex-agent-engine" in service_names
        assert "gcs" in service_names

    def test_non_cloudless_returns_empty(self, project):
        write_authoritative_state(
            project,
            {"DDR-L2-001": "langchain", "DDR-SHARED-017": "self-managed"},
        )

        result = extract_services_from_registry(project)
        assert result["is_cloudless"] is False
        assert result["services"] == []

    def test_no_registry_returns_empty(self, project):
        result = extract_services_from_registry(project)
        assert result["is_cloudless"] is False
        assert result["services"] == []

    def test_critical_services_flagged(self, project):
        write_authoritative_state(
            project,
            {"DDR-SHARED-017": "cloudless-managed"},
            cloud="aws",
        )

        result = extract_services_from_registry(project)
        for svc in result["services"]:
            if svc["service_name"] == "bedrock-agentcore":
                assert svc["criticality"] == "critical"
