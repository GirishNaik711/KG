"""Tests for env_injector.py — inter-service URL computation."""

from aah.core.deploy.env_injector import (
    compute_env_vars_for_layer,
    format_env_vars_for_cli,
    format_env_vars_for_ecs,
)


class TestComputeEnvVarsForLayer:
    def test_frontend_gets_backend_url(self):
        services = [
            {"name": "backend", "dependencies": []},
            {"name": "frontend", "dependencies": ["backend"]},
        ]
        deployed_urls = {"backend": "https://backend-xyz.run.app"}

        result = compute_env_vars_for_layer(["frontend"], services, deployed_urls)
        assert result["frontend"] == {"BACKEND_URL": "https://backend-xyz.run.app"}

    def test_service_with_no_deps_gets_empty_env(self):
        services = [{"name": "backend", "dependencies": []}]
        deployed_urls = {}

        result = compute_env_vars_for_layer(["backend"], services, deployed_urls)
        assert result["backend"] == {}

    def test_hyphenated_service_name_becomes_underscore(self):
        services = [
            {"name": "auth-service", "dependencies": []},
            {"name": "api-gateway", "dependencies": ["auth-service"]},
        ]
        deployed_urls = {"auth-service": "https://auth-service.run.app"}

        result = compute_env_vars_for_layer(["api-gateway"], services, deployed_urls)
        assert result["api-gateway"] == {"AUTH_SERVICE_URL": "https://auth-service.run.app"}

    def test_multiple_deps_get_multiple_vars(self):
        services = [
            {"name": "auth", "dependencies": []},
            {"name": "storage", "dependencies": []},
            {"name": "api", "dependencies": ["auth", "storage"]},
        ]
        deployed_urls = {
            "auth": "https://auth.run.app",
            "storage": "https://storage.run.app",
        }

        result = compute_env_vars_for_layer(["api"], services, deployed_urls)
        assert result["api"] == {
            "AUTH_URL": "https://auth.run.app",
            "STORAGE_URL": "https://storage.run.app",
        }

    def test_dep_not_yet_deployed_is_skipped(self):
        services = [
            {"name": "backend", "dependencies": []},
            {"name": "frontend", "dependencies": ["backend"]},
        ]
        deployed_urls = {}  # Backend not deployed yet

        result = compute_env_vars_for_layer(["frontend"], services, deployed_urls)
        assert result["frontend"] == {}  # No URL to inject


class TestFormatEnvVarsForCli:
    def test_single_var(self):
        result = format_env_vars_for_cli({"BACKEND_URL": "https://x.run.app"})
        assert result == "BACKEND_URL=https://x.run.app"

    def test_multiple_vars_comma_separated(self):
        result = format_env_vars_for_cli({
            "AUTH_URL": "https://auth.run.app",
            "DB_URL": "postgres://localhost",
        })
        assert "AUTH_URL=https://auth.run.app" in result
        assert "DB_URL=postgres://localhost" in result
        assert "," in result

    def test_empty_returns_empty_string(self):
        assert format_env_vars_for_cli({}) == ""


class TestFormatEnvVarsForEcs:
    def test_returns_ecs_format(self):
        result = format_env_vars_for_ecs({"BACKEND_URL": "https://x.ecs.aws"})
        assert result == [{"name": "BACKEND_URL", "value": "https://x.ecs.aws"}]

    def test_multiple_vars(self):
        result = format_env_vars_for_ecs({"A": "1", "B": "2"})
        assert len(result) == 2
        assert {"name": "A", "value": "1"} in result
        assert {"name": "B", "value": "2"} in result
