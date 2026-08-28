"""Tests for aah.core.deploy.access_tier — Tiered Access Gate."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from aah.core.deploy.access_tier import (
    TIERS,
    TIER_LABELS,
    _compute_tiers_to_try,
    build_failure_report,
    cleanup_tier_resources,
    deploy_with_cascade,
    detect_deployer_ip,
    get_tier_deploy_config,
    health_check_for_tier,
    load_tier_prediction,
    record_tier_result,
)


# ---------------------------------------------------------------------------
# Tier configuration tests
# ---------------------------------------------------------------------------


class TestTierConstants:
    def test_tiers_are_ordered(self):
        assert TIERS == ["1A", "1B", "1C"]

    def test_tier_labels_complete(self):
        assert set(TIER_LABELS.keys()) == {"1A", "1B", "1C"}
        assert TIER_LABELS["1A"] == "Full Public"
        assert TIER_LABELS["1B"] == "Deployer-Scoped"
        assert TIER_LABELS["1C"] == "Local Proxy"


class TestGetTierDeployConfig:
    """Test tier config generation for both clouds."""

    def test_gcp_1a_config(self):
        config = get_tier_deploy_config("gcp", "1A")
        assert config["tier"] == "1A"
        assert config["deploy_flags"]["ingress"] == "all"
        assert config["deploy_flags"]["allow_unauthenticated"] is True
        assert config["health_check_method"] == "bare_curl"
        assert config["access_type"] == "public_url"

    def test_gcp_1b_config(self):
        config = get_tier_deploy_config("gcp", "1B", deployer_ip="10.0.0.1")
        assert config["tier"] == "1B"
        assert config["deploy_flags"]["ingress"] == "all"
        assert config["deploy_flags"]["allow_unauthenticated"] is False
        assert config["health_check_method"] == "auth_curl"
        assert config["access_type"] == "authenticated_url"
        assert config["deployer_ip"] == "10.0.0.1"

    def test_gcp_1c_config(self):
        config = get_tier_deploy_config("gcp", "1C")
        assert config["tier"] == "1C"
        assert config["deploy_flags"]["ingress"] == "internal"
        assert config["deploy_flags"]["allow_unauthenticated"] is False
        assert config["health_check_method"] == "proxy"
        assert config["access_type"] == "local_proxy"

    def test_aws_1a_config(self):
        config = get_tier_deploy_config("aws", "1A")
        assert config["tier"] == "1A"
        assert config["deploy_flags"]["alb_scheme"] == "internet-facing"
        assert config["deploy_flags"]["sg_ingress_cidr"] == "0.0.0.0/0"
        assert config["health_check_method"] == "bare_curl"

    def test_aws_1b_config(self):
        config = get_tier_deploy_config("aws", "1B", deployer_ip="203.0.113.5")
        assert config["tier"] == "1B"
        assert config["deploy_flags"]["alb_scheme"] == "internet-facing"
        assert config["deploy_flags"]["sg_ingress_cidr"] == "203.0.113.5/32"
        assert config["health_check_method"] == "bare_curl"

    def test_aws_1c_config(self):
        config = get_tier_deploy_config("aws", "1C")
        assert config["tier"] == "1C"
        assert config["deploy_flags"]["alb_scheme"] == "internal"
        assert config["deploy_flags"]["sg_ingress_cidr"] is None
        assert config["deploy_flags"]["enable_ecs_exec"] is True
        assert config["health_check_method"] == "ecs_exec"

    def test_unknown_cloud(self):
        config = get_tier_deploy_config("azure", "1A")
        assert "error" in config

    def test_unknown_tier(self):
        config = get_tier_deploy_config("gcp", "2A")
        assert "error" in config


# ---------------------------------------------------------------------------
# Tier selection / computation tests
# ---------------------------------------------------------------------------


class TestComputeTiersToTry:
    """Test tier selection logic with predictions."""

    def test_no_prediction_backend_all_tiers(self):
        tiers = _compute_tiers_to_try(None, is_frontend=False)
        assert tiers == ["1A", "1B", "1C"]

    def test_no_prediction_frontend_excludes_1c(self):
        tiers = _compute_tiers_to_try(None, is_frontend=True)
        assert tiers == ["1A", "1B"]

    def test_prediction_blocks_1a(self):
        prediction = {
            "tier_1a_possible": False,
            "tier_1b_possible": True,
            "tier_1c_possible": True,
        }
        tiers = _compute_tiers_to_try(prediction, is_frontend=False)
        assert tiers == ["1B", "1C"]

    def test_prediction_blocks_1a_and_1b(self):
        prediction = {
            "tier_1a_possible": False,
            "tier_1b_possible": False,
            "tier_1c_possible": True,
        }
        tiers = _compute_tiers_to_try(prediction, is_frontend=False)
        assert tiers == ["1C"]

    def test_prediction_all_blocked_backend_fallback_1c(self):
        prediction = {
            "tier_1a_possible": False,
            "tier_1b_possible": False,
            "tier_1c_possible": False,
        }
        tiers = _compute_tiers_to_try(prediction, is_frontend=False)
        assert tiers == ["1C"]

    def test_prediction_all_blocked_frontend_fallback_1b(self):
        prediction = {
            "tier_1a_possible": False,
            "tier_1b_possible": False,
            "tier_1c_possible": False,
        }
        tiers = _compute_tiers_to_try(prediction, is_frontend=True)
        assert tiers == ["1B"]

    def test_frontend_with_1a_blocked(self):
        prediction = {
            "tier_1a_possible": False,
            "tier_1b_possible": True,
            "tier_1c_possible": True,
        }
        tiers = _compute_tiers_to_try(prediction, is_frontend=True)
        assert tiers == ["1B"]  # 1C excluded for frontend

    def test_missing_prediction_keys_default_to_possible(self):
        """If prediction dict exists but doesn't have all keys, defaults to True."""
        prediction = {"tier_1a_possible": False}  # 1B and 1C not specified
        tiers = _compute_tiers_to_try(prediction, is_frontend=False)
        assert tiers == ["1B", "1C"]


# ---------------------------------------------------------------------------
# Cascade state machine tests
# ---------------------------------------------------------------------------


class TestCascadeFlow:
    """Test the deploy_with_cascade + record_tier_result state machine."""

    def test_cascade_initializes_with_correct_tiers(self):
        cascade = deploy_with_cascade(
            service_name="backend",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            tier_prediction=None,
            deployer_ip="1.2.3.4",
        )
        assert cascade["tiers_to_try"] == ["1A", "1B", "1C"]
        assert cascade["service_name"] == "backend"
        assert cascade["cloud"] == "gcp"
        assert cascade["deployer_ip"] == "1.2.3.4"
        assert cascade["success"] is None  # Not yet determined

    def test_cascade_respects_prediction(self):
        prediction = {"tier_1a_possible": False, "tier_1b_possible": True, "tier_1c_possible": True}
        cascade = deploy_with_cascade(
            service_name="backend",
            cloud="aws",
            route="ecs-express",
            context={"account_id": "123", "region": "us-east-1"},
            tier_prediction=prediction,
            deployer_ip="1.2.3.4",
        )
        assert cascade["tiers_to_try"] == ["1B", "1C"]

    def test_record_success_locks_tier(self):
        cascade = deploy_with_cascade(
            service_name="backend",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            deployer_ip="1.2.3.4",
        )
        cascade = record_tier_result(cascade, "1A", success=True, url="https://backend.run.app")
        assert cascade["success"] is True
        assert cascade["tier_used"] == "1A"
        assert cascade["tier_label"] == "Full Public"
        assert cascade["url"] == "https://backend.run.app"
        assert cascade["next_tier"] is None
        assert "access_instructions" in cascade

    def test_record_failure_advances_to_next_tier(self):
        cascade = deploy_with_cascade(
            service_name="backend",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            deployer_ip="1.2.3.4",
        )
        cascade = record_tier_result(cascade, "1A", success=False, error="org policy blocks")
        assert cascade["success"] is None  # Not yet determined
        assert cascade["next_tier"] == "1B"

    def test_record_all_failures_generates_report(self):
        prediction = {"tier_1a_possible": False, "tier_1b_possible": True, "tier_1c_possible": True}
        cascade = deploy_with_cascade(
            service_name="backend",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1", "identity_email": "dev@corp.com"},
            tier_prediction=prediction,
            deployer_ip="1.2.3.4",
        )
        # Simulate 1B and 1C both fail
        cascade = record_tier_result(cascade, "1B", success=False, error="ingress blocked")
        assert cascade["next_tier"] == "1C"
        cascade = record_tier_result(cascade, "1C", success=False, error="proxy permission denied")
        assert cascade["success"] is False
        assert cascade["next_tier"] is None
        assert cascade["failure_report"] is not None
        assert "DEPLOY GATE FAILED" in cascade["failure_report"]

    def test_frontend_cascade_excludes_1c(self):
        cascade = deploy_with_cascade(
            service_name="frontend",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            is_frontend=True,
            deployer_ip="1.2.3.4",
        )
        assert "1C" not in cascade["tiers_to_try"]


# ---------------------------------------------------------------------------
# Failure report tests
# ---------------------------------------------------------------------------


class TestFailureReport:
    def test_gcp_failure_report_structure(self):
        report = build_failure_report(
            service_name="backend",
            cloud="gcp",
            context={"project_id": "corp-project", "region": "us-central1", "identity_email": "user@corp.com"},
            tier_results={
                "1A": {"attempted": True, "success": False, "error": "org policy blocks allUsers"},
                "1B": {"attempted": True, "success": False, "error": "ingress=all blocked"},
                "1C": {"attempted": True, "success": False, "error": "missing roles"},
            },
        )
        assert "DEPLOY GATE FAILED" in report
        assert "GCP" in report
        assert "corp-project" in report
        assert "user@corp.com" in report
        assert "Tier 1A (Full Public) -- FAILED" in report
        assert "Tier 1B (Deployer-Scoped) -- FAILED" in report
        assert "Tier 1C (Local Proxy) -- FAILED" in report
        assert "REQUIRED ACTION" in report
        assert "roles/run.invoker" in report
        assert "roles/run.services.connect" in report

    def test_aws_failure_report_structure(self):
        report = build_failure_report(
            service_name="api",
            cloud="aws",
            context={"account_id": "123456789012", "region": "us-east-1"},
            tier_results={
                "1A": {"attempted": True, "success": False, "error": "SCP blocks 0.0.0.0/0"},
                "1B": {"attempted": False},
                "1C": {"attempted": True, "success": False, "error": "ECS Exec denied"},
            },
        )
        assert "AWS" in report
        assert "123456789012" in report
        assert "Tier 1B (Deployer-Scoped) -- SKIPPED" in report
        assert "Enable ECS Exec" in report
        assert "ssmmessages" in report

    def test_skipped_tier_in_report(self):
        report = build_failure_report(
            service_name="backend",
            cloud="gcp",
            context={"project_id": "p", "region": "r", "identity_email": "e"},
            tier_results={
                "1A": {"attempted": False},  # Skipped by prediction
                "1B": {"attempted": True, "success": False, "error": "failed"},
                "1C": {"attempted": True, "success": False, "error": "failed"},
            },
        )
        assert "SKIPPED (predicted impossible)" in report


# ---------------------------------------------------------------------------
# Access instructions tests
# ---------------------------------------------------------------------------


class TestAccessInstructions:
    def test_tier_1a_instructions(self):
        cascade = deploy_with_cascade(
            service_name="api",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            deployer_ip="1.2.3.4",
        )
        cascade = record_tier_result(cascade, "1A", success=True, url="https://api.run.app")
        instructions = cascade["access_instructions"]
        assert "Tier 1A" in instructions
        assert "Full Public" in instructions
        assert "https://api.run.app" in instructions
        assert "No authentication required" in instructions

    def test_tier_1b_gcp_instructions(self):
        cascade = deploy_with_cascade(
            service_name="api",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            deployer_ip="1.2.3.4",
        )
        cascade = record_tier_result(cascade, "1B", success=True, url="https://api.run.app")
        instructions = cascade["access_instructions"]
        assert "Tier 1B" in instructions
        assert "identity token" in instructions.lower() or "identity-token" in instructions.lower()
        assert "roles/run.invoker" in instructions

    def test_tier_1b_aws_instructions(self):
        cascade = deploy_with_cascade(
            service_name="api",
            cloud="aws",
            route="ecs-express",
            context={"account_id": "123", "region": "us-east-1"},
            deployer_ip="10.0.0.5",
        )
        cascade = record_tier_result(cascade, "1B", success=True, url="https://api.ecs.us-east-1.on.aws")
        instructions = cascade["access_instructions"]
        assert "Tier 1B" in instructions
        assert "10.0.0.5" in instructions
        assert "security group" in instructions.lower() or "security-group" in instructions.lower()

    def test_tier_1c_gcp_instructions(self):
        cascade = deploy_with_cascade(
            service_name="api",
            cloud="gcp",
            route="cloud-run",
            context={"project_id": "proj", "region": "us-central1"},
            deployer_ip="1.2.3.4",
        )
        cascade = record_tier_result(cascade, "1C", success=True, url=None)
        instructions = cascade["access_instructions"]
        assert "Tier 1C" in instructions
        assert "Local Proxy" in instructions
        assert "gcloud run services proxy" in instructions

    def test_tier_1c_aws_instructions(self):
        cascade = deploy_with_cascade(
            service_name="api",
            cloud="aws",
            route="ecs-express",
            context={"account_id": "123", "region": "us-east-1"},
            deployer_ip="1.2.3.4",
        )
        cascade = record_tier_result(cascade, "1C", success=True, url=None)
        instructions = cascade["access_instructions"]
        assert "Tier 1C" in instructions
        assert "ecs execute-command" in instructions.lower() or "execute-command" in instructions


# ---------------------------------------------------------------------------
# Deployer IP detection tests
# ---------------------------------------------------------------------------


class TestDeployerIP:
    @patch("aah.core.deploy.access_tier.subprocess.run")
    def test_detect_ip_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="203.0.113.50\n")
        ip = detect_deployer_ip()
        assert ip == "203.0.113.50"

    @patch("aah.core.deploy.access_tier.subprocess.run")
    def test_detect_ip_invalid_response(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not-an-ip\n")
        ip = detect_deployer_ip()
        # First call fails validation, second (ipify) also fails
        assert ip is None

    @patch("aah.core.deploy.access_tier.subprocess.run")
    def test_detect_ip_command_fails(self, mock_run):
        mock_run.side_effect = Exception("network error")
        ip = detect_deployer_ip()
        assert ip is None


# ---------------------------------------------------------------------------
# Tier prediction loading tests
# ---------------------------------------------------------------------------


class TestLoadTierPrediction:
    def test_loads_from_architecture_dir(self, tmp_path):
        arch_dir = tmp_path / ".aah" / "architecture"
        arch_dir.mkdir(parents=True)

        from aah.core.common.io_utils import write_yaml
        write_yaml({
            "overall_status": "passed",
            "tier_prediction": {
                "tier_1a_possible": False,
                "tier_1b_possible": True,
                "tier_1c_possible": True,
                "recommended_starting_tier": "1B",
                "blockers": {"1A": "org policy blocks"},
            },
        }, arch_dir / "cloud-readiness.yaml")

        prediction = load_tier_prediction(tmp_path)
        assert prediction is not None
        assert prediction["tier_1a_possible"] is False
        assert prediction["recommended_starting_tier"] == "1B"

    def test_returns_none_when_no_file(self, tmp_path):
        prediction = load_tier_prediction(tmp_path)
        assert prediction is None

    def test_returns_none_when_no_prediction_field(self, tmp_path):
        arch_dir = tmp_path / ".aah" / "architecture"
        arch_dir.mkdir(parents=True)

        from aah.core.common.io_utils import write_yaml
        write_yaml({"overall_status": "passed"}, arch_dir / "cloud-readiness.yaml")

        prediction = load_tier_prediction(tmp_path)
        assert prediction is None
