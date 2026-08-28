"""Tests for service_discovery.py — convention-based multi-service scanning."""

from pathlib import Path

from aah.core.deploy.service_discovery import (
    discover_services,
    detect_language,
    detect_framework,
    detect_port,
    detect_entry_point,
)


class TestDiscoverServicesSingleBackend:
    """Pattern 1: src/ + root pyproject.toml = single backend."""

    def test_detects_python_backend(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-app"\ndependencies = ["fastapi"]')
        (tmp_path / ".rapids").mkdir()

        services = discover_services(tmp_path)
        assert len(services) == 1
        assert services[0]["name"] == "backend"
        assert services[0]["language"] == "python"
        assert services[0]["framework"] == "fastapi"
        assert services[0]["is_frontend"] is False

    def test_detects_backend_and_frontend(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("app = None")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-app"')
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text('{"dependencies":{"react":"18"}}')
        (tmp_path / ".rapids").mkdir()

        services = discover_services(tmp_path)
        assert len(services) == 2

        backend = next(s for s in services if s["name"] == "backend")
        frontend = next(s for s in services if s["name"] == "frontend")

        assert backend["is_frontend"] is False
        assert frontend["is_frontend"] is True
        assert frontend["dependencies"] == ["backend"]

    def test_no_src_no_services(self, tmp_path):
        (tmp_path / ".rapids").mkdir()
        services = discover_services(tmp_path)
        assert services == []


class TestDiscoverServicesMultiService:
    """Additional services detected from root-level folders."""

    def test_detects_additional_service_folder(self, tmp_path):
        # Main backend
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"')
        (tmp_path / ".rapids").mkdir()

        # Additional service
        (tmp_path / "gateway").mkdir()
        (tmp_path / "gateway" / "pyproject.toml").write_text('[project]\nname = "gateway"')
        (tmp_path / "gateway" / "main.py").write_text("from flask import Flask")

        services = discover_services(tmp_path)
        names = [s["name"] for s in services]
        assert "backend" in names
        assert "gateway" in names

    def test_skips_non_service_folders(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"')
        (tmp_path / ".rapids").mkdir()

        # These should NOT be detected as services
        (tmp_path / "tests").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "scripts").mkdir()
        (tmp_path / ".git").mkdir()

        services = discover_services(tmp_path)
        names = [s["name"] for s in services]
        assert "tests" not in names
        assert "docs" not in names
        assert "scripts" not in names
        assert ".git" not in names

    def test_frontend_detected_by_vite_config(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"')
        (tmp_path / ".rapids").mkdir()

        # A folder named "app-ui" with vite config
        (tmp_path / "app-ui").mkdir()
        (tmp_path / "app-ui" / "package.json").write_text('{"dependencies":{"vite":"5"}}')
        (tmp_path / "app-ui" / "vite.config.ts").write_text("export default {}")

        services = discover_services(tmp_path)
        ui_svc = next((s for s in services if s["name"] == "app-ui"), None)
        assert ui_svc is not None
        assert ui_svc["is_frontend"] is True


class TestDetectLanguage:
    def test_python_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert detect_language(tmp_path) == "python"

    def test_python_from_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi")
        assert detect_language(tmp_path) == "python"

    def test_node_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert detect_language(tmp_path) == "node"

    def test_go_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/app")
        assert detect_language(tmp_path) == "go"

    def test_unknown_when_empty(self, tmp_path):
        assert detect_language(tmp_path) == "unknown"


class TestDetectFramework:
    def test_fastapi_from_pyproject_deps(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('dependencies = ["fastapi>=0.100"]')
        assert detect_framework(tmp_path, "python") == "fastapi"

    def test_adk_from_deps(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('dependencies = ["google-adk>=1.0"]')
        assert detect_framework(tmp_path, "python") == "adk"

    def test_langgraph_from_deps(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('dependencies = ["langgraph>=0.2"]')
        assert detect_framework(tmp_path, "python") == "langgraph"

    def test_nextjs_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text('{"dependencies":{"next":"14.0.0"}}')
        assert detect_framework(tmp_path, "node") == "nextjs"

    def test_vite_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text('{"dependencies":{"vite":"5.0.0"}}')
        assert detect_framework(tmp_path, "node") == "vite"

    def test_express_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text('{"dependencies":{"express":"4.18"}}')
        assert detect_framework(tmp_path, "node") == "express"


class TestDetectPort:
    def test_backend_python_defaults_8080(self, tmp_path):
        assert detect_port(tmp_path, "python", "fastapi", is_frontend=False) == 8080

    def test_frontend_nextjs_defaults_3000(self, tmp_path):
        assert detect_port(tmp_path, "node", "nextjs", is_frontend=True) == 3000

    def test_frontend_vite_defaults_5173(self, tmp_path):
        assert detect_port(tmp_path, "node", "vite", is_frontend=True) == 5173

    def test_port_from_dockerfile_expose(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 9090\nCMD [\"python\"]")
        assert detect_port(tmp_path, "python", "bare", is_frontend=False) == 9090


class TestDetectEntryPoint:
    def test_finds_src_main_py(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("")
        assert detect_entry_point(tmp_path, "python", "fastapi") == "src/main.py"

    def test_finds_app_py_at_root(self, tmp_path):
        (tmp_path / "app.py").write_text("")
        assert detect_entry_point(tmp_path, "python", "flask") == "app.py"

    def test_finds_go_cmd_server(self, tmp_path):
        (tmp_path / "cmd" / "server").mkdir(parents=True)
        (tmp_path / "cmd" / "server" / "main.go").write_text("")
        assert detect_entry_point(tmp_path, "go", "bare") == "cmd/server/main.go"
