"""Tests for serverless_registry.py — target lookup, route classification."""

from aah.core.deploy.serverless_registry import (
    get_target,
    is_serverless_route,
    list_targets,
    TARGETS,
)


class TestGetTarget:
    def test_cloud_run_returns_gcp_config(self):
        target = get_target("cloud-run")
        assert target is not None
        assert target["cloud"] == "gcp"
        assert target["agent"] == "cloudrun-deploy-engineer"
        assert target["credential_check"] == "gcp"
        assert target["dockerfile_required"] is False
        assert target["build_remote"] is True

    def test_ecs_express_returns_aws_config(self):
        target = get_target("ecs-express")
        assert target is not None
        assert target["cloud"] == "aws"
        assert target["agent"] == "ecs-deploy-engineer"
        assert target["credential_check"] == "aws"
        assert target["dockerfile_required"] is True
        assert target["build_remote"] is True

    def test_unknown_route_returns_none(self):
        assert get_target("unknown-target") is None
        assert get_target("") is None
        assert get_target("standard") is None

    def test_log_fetch_cmd_has_service_placeholder(self):
        for route, config in TARGETS.items():
            assert "{service}" in config["log_fetch_cmd"], (
                f"Target '{route}' log_fetch_cmd missing {{service}} placeholder"
            )


class TestIsServerlessRoute:
    def test_cloud_run_is_serverless(self):
        assert is_serverless_route("cloud-run") is True

    def test_ecs_express_is_serverless(self):
        assert is_serverless_route("ecs-express") is True

    def test_standard_is_not_serverless(self):
        assert is_serverless_route("standard") is False

    def test_cloudless_is_not_serverless(self):
        assert is_serverless_route("cloudless") is False

    def test_empty_string_is_not_serverless(self):
        assert is_serverless_route("") is False


class TestListTargets:
    def test_returns_all_registered_targets(self):
        targets = list_targets()
        assert "cloud-run" in targets
        assert "ecs-express" in targets
        assert len(targets) == 2
