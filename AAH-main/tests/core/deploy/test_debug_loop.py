"""Tests for debug_loop.py — deployment failure diagnosis."""

from aah.core.deploy.debug_loop import diagnose


class TestDiagnose:
    def test_detects_module_not_found(self):
        logs = "ModuleNotFoundError: No module named 'google.adk'"
        results = diagnose(logs)
        assert len(results) >= 1
        assert results[0]["error_type"] == "module_not_found"
        assert results[0]["extracted_value"] == "google.adk"
        assert results[0]["confidence"] == "high"

    def test_detects_port_mismatch(self):
        logs = "Connection refused on port :3000"
        results = diagnose(logs)
        assert results[0]["error_type"] == "port_mismatch"
        assert results[0]["extracted_value"] == "3000"

    def test_detects_missing_env_var(self):
        logs = "KeyError: 'DATABASE_URL'"
        results = diagnose(logs)
        assert results[0]["error_type"] == "missing_env_var"
        assert results[0]["extracted_value"] == "DATABASE_URL"

    def test_detects_oom(self):
        logs = "Container was OOMKilled due to memory limit exceeded"
        results = diagnose(logs)
        assert results[0]["error_type"] == "oom_killed"

    def test_detects_permission_denied(self):
        logs = "PermissionDenied: 403 IAM permission check failed"
        results = diagnose(logs)
        assert results[0]["error_type"] == "permission_denied"

    def test_unknown_error_returns_low_confidence(self):
        logs = "Something completely unexpected happened xyz123"
        results = diagnose(logs)
        assert results[0]["error_type"] == "unknown"
        assert results[0]["confidence"] == "low"

    def test_empty_logs_returns_fetch_failed(self):
        results = diagnose("")
        assert results[0]["error_type"] == "log_fetch_failed"

    def test_error_prefix_returns_fetch_failed(self):
        results = diagnose("ERROR: gcloud CLI not found")
        assert results[0]["error_type"] == "log_fetch_failed"

    def test_multiple_errors_sorted_by_confidence(self):
        logs = (
            "ModuleNotFoundError: No module named 'flask'\n"
            "PermissionDenied: cannot access resource\n"
        )
        results = diagnose(logs)
        # High confidence should come first
        assert results[0]["confidence"] == "high"

    def test_import_error_detected(self):
        logs = "ImportError: attempted relative import beyond top-level package"
        results = diagnose(logs)
        assert results[0]["error_type"] == "import_error"
