"""Tests for Python version mismatch detection in bootstrap."""

from unittest.mock import patch

from aah.core.install.bootstrap import _diagnose_python_version


class TestDiagnosePythonVersion:
    def test_detects_version_mismatch(self):
        stderr = (
            "error: No solution found when resolving dependencies:\n"
            "  Because the requested Python version (Python 3.10.17) does not satisfy "
            "Python>=3.11 and you require Python>=3.11, we can conclude that your "
            "requirements are unsatisfiable."
        )
        result = _diagnose_python_version(stderr)
        assert result is not None
        assert "3.10.17" in result
        assert "UV_PYTHON=3.11" in result
        assert "uv python install 3.11" in result
        assert "uv python list" in result

    def test_detects_no_python_found(self):
        stderr = "error: No Python installations found with the required version >=3.11 and <4.0"
        result = _diagnose_python_version(stderr)
        assert result is not None
        assert "uv python install 3.11" in result

    def test_returns_none_for_unrelated_error(self):
        stderr = "error: No such file or directory (os error 2)"
        result = _diagnose_python_version(stderr)
        assert result is None

    def test_returns_none_for_empty_stderr(self):
        result = _diagnose_python_version("")
        assert result is None

    @patch("aah.core.install.bootstrap._base_repo_url", return_value="https://github.com/org/repo")
    def test_includes_repo_url_in_hint(self, _mock):
        stderr = "Python 3.10.5 does not satisfy Python>=3.11"
        result = _diagnose_python_version(stderr)
        assert result is not None
        assert "git+https://github.com/org/repo" in result

    def test_detects_version_without_patch(self):
        stderr = "Python 3.10 does not satisfy Python>=3.11"
        result = _diagnose_python_version(stderr)
        assert result is not None
        assert "3.10" in result
