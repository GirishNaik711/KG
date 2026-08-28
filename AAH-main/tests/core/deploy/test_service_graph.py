"""Tests for service_graph.py — dependency ordering and validation."""

from aah.core.deploy.service_graph import (
    build_service_graph,
    validate_service_graph,
)


class TestBuildServiceGraph:
    def test_single_backend_single_layer(self):
        services = [{"name": "backend", "is_frontend": False, "dependencies": []}]
        layers = build_service_graph(services)
        assert layers == [["backend"]]

    def test_backend_plus_frontend_two_layers(self):
        services = [
            {"name": "backend", "is_frontend": False, "dependencies": []},
            {"name": "frontend", "is_frontend": True, "dependencies": ["backend"]},
        ]
        layers = build_service_graph(services)
        assert layers == [["backend"], ["frontend"]]

    def test_frontend_always_last_layer(self):
        services = [
            {"name": "frontend", "is_frontend": True, "dependencies": []},
            {"name": "backend", "is_frontend": False, "dependencies": []},
        ]
        layers = build_service_graph(services)
        # Backend in layer 0, frontend in layer 1 (even with no explicit dep)
        assert layers[-1] == ["frontend"]
        assert "backend" in layers[0]

    def test_parallel_backends_same_layer(self):
        services = [
            {"name": "auth", "is_frontend": False, "dependencies": []},
            {"name": "worker", "is_frontend": False, "dependencies": []},
            {"name": "frontend", "is_frontend": True, "dependencies": ["auth"]},
        ]
        layers = build_service_graph(services)
        assert layers[0] == ["auth", "worker"]  # Parallel (sorted)
        assert layers[-1] == ["frontend"]

    def test_sequential_backends(self):
        services = [
            {"name": "db-proxy", "is_frontend": False, "dependencies": []},
            {"name": "api", "is_frontend": False, "dependencies": ["db-proxy"]},
            {"name": "frontend", "is_frontend": True, "dependencies": ["api"]},
        ]
        layers = build_service_graph(services)
        assert layers == [["db-proxy"], ["api"], ["frontend"]]

    def test_empty_services(self):
        layers = build_service_graph([])
        assert layers == []

    def test_multiple_frontends_in_last_layer(self):
        services = [
            {"name": "backend", "is_frontend": False, "dependencies": []},
            {"name": "admin-ui", "is_frontend": True, "dependencies": []},
            {"name": "web-app", "is_frontend": True, "dependencies": []},
        ]
        layers = build_service_graph(services)
        assert layers[-1] == ["admin-ui", "web-app"]  # Both frontends last


class TestValidateServiceGraph:
    def test_valid_graph_no_errors(self):
        services = [
            {"name": "backend", "is_frontend": False, "dependencies": []},
            {"name": "frontend", "is_frontend": True, "dependencies": ["backend"]},
        ]
        errors = validate_service_graph(services)
        assert errors == []

    def test_detects_nonexistent_dependency(self):
        services = [
            {"name": "backend", "is_frontend": False, "dependencies": ["ghost-service"]},
        ]
        errors = validate_service_graph(services)
        assert len(errors) == 1
        assert "ghost-service" in errors[0]
        assert "does not exist" in errors[0]

    def test_detects_circular_dependency(self):
        services = [
            {"name": "a", "is_frontend": False, "dependencies": ["b"]},
            {"name": "b", "is_frontend": False, "dependencies": ["a"]},
        ]
        errors = validate_service_graph(services)
        assert any("Circular" in e or "circular" in e.lower() for e in errors)

    def test_detects_frontend_depending_on_frontend(self):
        services = [
            {"name": "web", "is_frontend": True, "dependencies": ["admin"]},
            {"name": "admin", "is_frontend": True, "dependencies": []},
        ]
        errors = validate_service_graph(services)
        assert any("frontend" in e.lower() and "depends on frontend" in e.lower() for e in errors)
