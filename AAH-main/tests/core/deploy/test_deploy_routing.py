#!/usr/bin/env python3
"""Unit tests for deploy/deploy_routing.py."""

import pytest
from pathlib import Path

from aah.core.common.io_utils import write_yaml
from aah.core.deploy.deploy_routing import (
    is_langgraph_project,
    is_cloudless_deployment,
    get_deployment_route,
    get_target_cloud,
)


@pytest.fixture
def project(tmp_path):
    """Create minimal RAPIDS project structure."""
    rapids = tmp_path / ".rapids"
    rapids.mkdir()
    return tmp_path


class TestIsLanggraphProject:
    def test_detects_from_registry(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-L2-001", "resolved_option": "langgraph", "status": "resolved"}
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert is_langgraph_project(project) is True

    def test_detects_from_manifest(self, project):
        write_yaml({
            "project_name": "test",
            "stack_choices": {"ai": "aws-bedrock-langgraph", "cloud": "aws"},
        }, project / ".rapids" / "manifest.yaml")

        assert is_langgraph_project(project) is True

    def test_detects_from_source_imports(self, project):
        src = project / "src"
        src.mkdir()
        (src / "agent.py").write_text("from langgraph.graph import StateGraph\n")

        assert is_langgraph_project(project) is True

    def test_returns_false_for_langchain(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-L2-001", "resolved_option": "langchain", "status": "resolved"}
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert is_langgraph_project(project) is False

    def test_returns_false_empty_project(self, project):
        assert is_langgraph_project(project) is False

    def test_handles_null_ai_stack(self, project):
        write_yaml({
            "project_name": "test",
            "stack_choices": {"ai": None, "cloud": "aws"},
        }, project / ".rapids" / "manifest.yaml")

        assert is_langgraph_project(project) is False


class TestIsCloudlessDeployment:
    def test_detects_cloudless_managed(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-SHARED-017", "resolved_option": "cloudless-managed", "status": "resolved"}
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert is_cloudless_deployment(project) is True

    def test_returns_false_for_container_based(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-SHARED-017", "resolved_option": "container-based", "status": "resolved"}
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert is_cloudless_deployment(project) is False

    def test_returns_false_no_registry(self, project):
        assert is_cloudless_deployment(project) is False


class TestGetDeploymentRoute:
    def test_cloudless_route(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-L2-001", "resolved_option": "langgraph", "status": "resolved"},
                {"ddr_id": "DDR-SHARED-017", "resolved_option": "cloudless-managed", "status": "resolved"},
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert get_deployment_route(project) == "cloudless"

    def test_standard_route_non_langgraph(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-L2-001", "resolved_option": "langchain", "status": "resolved"},
                {"ddr_id": "DDR-SHARED-017", "resolved_option": "container-based", "status": "resolved"},
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert get_deployment_route(project) == "standard"

    def test_standard_route_langgraph_without_cloudless(self, project):
        write_yaml({
            "decisions": [
                {"ddr_id": "DDR-L2-001", "resolved_option": "langgraph", "status": "resolved"},
                {"ddr_id": "DDR-SHARED-017", "resolved_option": "container-based", "status": "resolved"},
            ]
        }, project / ".rapids" / "decision-registry.yaml")

        assert get_deployment_route(project) == "standard"


class TestGetTargetCloud:
    def test_aws_from_manifest(self, project):
        write_yaml({
            "project_name": "test",
            "stack_choices": {"cloud": "aws"},
        }, project / ".rapids" / "manifest.yaml")

        assert get_target_cloud(project) == "aws"

    def test_gcp_from_manifest(self, project):
        write_yaml({
            "project_name": "test",
            "stack_choices": {"cloud": "gcp"},
        }, project / ".rapids" / "manifest.yaml")

        assert get_target_cloud(project) == "gcp"

    def test_none_when_not_set(self, project):
        write_yaml({
            "project_name": "test",
            "stack_choices": {},
        }, project / ".rapids" / "manifest.yaml")

        assert get_target_cloud(project) is None
