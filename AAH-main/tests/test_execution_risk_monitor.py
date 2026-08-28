"""Tests for aah.core.guards.execution_risk_monitor."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch  # One-time exception: redirects state file path for test isolation, not mocking services

import pytest

from aah.core.guards.execution_risk_monitor import (
    analyze,
    detect_connection_refused,
    detect_doom_loop,
    detect_proxy_test,
    detect_rebuild_gap,
    detect_shallow_validation,
    detect_timeout_loop,
    load_state,
    normalize_command,
    normalize_error,
    save_state,
    REPEAT_THRESHOLD,
)


# --- Helpers ---

def fresh_state() -> dict:
    return {
        "error_counts": {},
        "timeout_counts": {},
        "command_history": [],
        "source_changed_since_build": False,
        "warnings_emitted": [],
        "total_commands": 0,
    }


def bash_input(command: str, exit_code: int = 0, stderr: str = "", stdout: str = "") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_output": {"exit_code": exit_code, "stderr": stderr, "stdout": stdout},
    }


def write_input(file_path: str) -> dict:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path},
        "tool_output": {},
    }


def edit_input(file_path: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "tool_output": {},
    }


# --- normalize_error tests ---

class TestNormalizeError:
    def test_strips_file_paths(self):
        sig = normalize_error("FileNotFoundError: /home/user/project/src/main.py not found")
        assert "/home/user" not in sig
        assert "<path>" in sig

    def test_strips_line_numbers(self):
        sig = normalize_error("SyntaxError at line 42")
        assert "42" not in sig
        assert "line N" in sig

    def test_strips_pids(self):
        sig = normalize_error("Process 12345 terminated")
        assert "12345" not in sig

    def test_strips_timestamps(self):
        sig = normalize_error("2026-05-01T10:30:00 ERROR: connection failed")
        assert "2026-05-01" not in sig
        assert "TIMESTAMP" in sig

    def test_empty_input(self):
        assert normalize_error("") == ""
        assert normalize_error("   \n\n  ") == ""

    def test_takes_first_line(self):
        sig = normalize_error("first error\nsecond error\nthird error")
        assert "first" in sig

    def test_truncates_long_errors(self):
        long_err = "E" * 500
        sig = normalize_error(long_err)
        assert len(sig) <= 200


class TestNormalizeCommand:
    def test_extracts_first_two_words(self):
        assert normalize_command("pip install flask gunicorn") == "pip install"

    def test_short_command(self):
        assert normalize_command("ls") == "ls"

    def test_empty_command(self):
        assert normalize_command("") == ""


# --- Doom loop tests ---

class TestDoomLoop:
    def test_no_warning_on_first_error(self):
        state = fresh_state()
        result = detect_doom_loop(state, "make build", 1, "Error: missing dependency")
        assert result is None

    def test_no_warning_on_success(self):
        state = fresh_state()
        result = detect_doom_loop(state, "make build", 0, "")
        assert result is None

    def test_warns_after_threshold_same_error(self):
        state = fresh_state()
        stderr = "ImportError: No module named 'flask'"
        for i in range(REPEAT_THRESHOLD - 1):
            result = detect_doom_loop(state, "python app.py", 1, stderr)
            assert result is None

        result = detect_doom_loop(state, "python app.py", 1, stderr)
        assert result is not None
        assert "doom loop" in result.lower()

    def test_different_errors_dont_trigger(self):
        state = fresh_state()
        detect_doom_loop(state, "cmd1", 1, "Error A")
        detect_doom_loop(state, "cmd2", 1, "Error B")
        result = detect_doom_loop(state, "cmd3", 1, "Error C")
        assert result is None

    def test_rate_limited(self):
        state = fresh_state()
        stderr = "SomeError: failed"
        # Trigger warning 3 times (MAX_WARNINGS_PER_TYPE)
        for _ in range(10):
            detect_doom_loop(state, "cmd", 1, stderr)
        # Count how many warnings were emitted
        doom_warnings = [w for w in state["warnings_emitted"] if w == "doom_loop"]
        assert len(doom_warnings) <= 3

    def test_no_warning_for_empty_stderr(self):
        state = fresh_state()
        result = detect_doom_loop(state, "cmd", 1, "")
        assert result is None


# --- Timeout loop tests ---

class TestTimeoutLoop:
    def test_no_warning_on_normal_command(self):
        state = fresh_state()
        result = detect_timeout_loop(state, "ls", 0, "")
        assert result is None

    def test_warns_after_repeated_timeouts(self):
        state = fresh_state()
        # First timeout
        result = detect_timeout_loop(state, "npm install", 124, "Command timed out")
        assert result is None
        # Second timeout — same command shape
        result = detect_timeout_loop(state, "npm install express", 124, "Command timed out")
        assert result is not None
        assert "timeout" in result.lower()

    def test_detects_timeout_in_stderr(self):
        state = fresh_state()
        detect_timeout_loop(state, "make build", 1, "timed out after 120s")
        result = detect_timeout_loop(state, "make build all", 1, "timed out after 120s")
        assert result is not None

    def test_different_commands_dont_trigger(self):
        state = fresh_state()
        detect_timeout_loop(state, "npm install", 124, "timed out")
        result = detect_timeout_loop(state, "cargo build", 124, "timed out")
        assert result is None


# --- Connection refused tests ---

class TestConnectionRefused:
    def test_no_warning_on_success(self):
        state = fresh_state()
        result = detect_connection_refused(state, "curl localhost:3000", 0, "")
        assert result is None

    def test_warns_after_repeated_connection_refused(self):
        state = fresh_state()
        stderr = "curl: (7) Failed to connect to localhost port 3000: Connection refused"
        detect_connection_refused(state, "curl localhost:3000", 7, stderr)
        result = detect_connection_refused(state, "curl localhost:3000", 7, stderr)
        assert result is not None
        assert "service running" in result.lower()

    def test_detects_econnrefused(self):
        state = fresh_state()
        stderr = "Error: connect ECONNREFUSED 127.0.0.1:5432"
        detect_connection_refused(state, "node test.js", 1, stderr)
        result = detect_connection_refused(state, "node test.js", 1, stderr)
        assert result is not None

    def test_single_failure_no_warning(self):
        state = fresh_state()
        stderr = "Connection refused"
        result = detect_connection_refused(state, "curl localhost", 7, stderr)
        assert result is None


# --- Shallow validation tests ---

class TestShallowValidation:
    def test_no_warning_early_in_session(self):
        state = fresh_state()
        state["total_commands"] = 2  # Too early
        result = detect_shallow_validation(
            state, "test -f output.json", 0, ""
        )
        assert result is None

    def test_warns_on_file_existence_check_late_in_session(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "test -f output.json", 0, ""
        )
        assert result is not None
        assert "shallow validation" in result.lower()

    def test_warns_on_import_only_check(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "python -c 'import mymodule'", 0, ""
        )
        assert result is not None
        assert "import-only" in result.lower()

    def test_warns_on_py_compile(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "python -m py_compile src/main.py", 0, ""
        )
        assert result is not None

    def test_warns_on_json_parse_only(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "python3 -c 'json.load(open(\"out.json\"))'", 0, ""
        )
        assert result is not None

    def test_no_warning_on_failure(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "test -f output.json", 1, ""
        )
        assert result is None

    def test_no_warning_on_normal_command(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "pytest tests/", 0, ""
        )
        assert result is None

    def test_bracket_syntax_detected(self):
        state = fresh_state()
        state["total_commands"] = 10
        result = detect_shallow_validation(
            state, "[ -f /app/output.json ]", 0, ""
        )
        assert result is not None


# --- Proxy test tests ---

class TestProxyTest:
    def test_tracks_adhoc_files(self):
        state = fresh_state()
        result = detect_proxy_test(
            state, "", 0, "Write",
            {"file_path": "/project/check_output.py"},
        )
        assert result is None
        assert "/project/check_output.py" in state.get("adhoc_test_files", [])

    def test_warns_when_running_adhoc_file(self):
        state = fresh_state()
        state["adhoc_test_files"] = ["/project/check_output.py"]
        result = detect_proxy_test(
            state, "python check_output.py", 0, "Bash",
            {"command": "python check_output.py"},
        )
        assert result is not None
        assert "self-written validation" in result.lower()

    def test_no_warning_without_prior_write(self):
        state = fresh_state()
        result = detect_proxy_test(
            state, "python check_output.py", 0, "Bash",
            {"command": "python check_output.py"},
        )
        assert result is None

    def test_tracks_verify_files(self):
        state = fresh_state()
        detect_proxy_test(
            state, "", 0, "Write",
            {"file_path": "/project/verify_results.py"},
        )
        assert len(state.get("adhoc_test_files", [])) == 1

    def test_tracks_validate_files(self):
        state = fresh_state()
        detect_proxy_test(
            state, "", 0, "Write",
            {"file_path": "/project/validate_api.py"},
        )
        assert len(state.get("adhoc_test_files", [])) == 1

    def test_ignores_normal_test_files(self):
        state = fresh_state()
        detect_proxy_test(
            state, "", 0, "Write",
            {"file_path": "/project/tests/test_api.py"},
        )
        # test_api.py doesn't match check_/verify_/validate_ prefix
        assert len(state.get("adhoc_test_files", [])) == 0


# --- Rebuild gap tests ---

class TestRebuildGap:
    def test_no_warning_without_source_change(self):
        state = fresh_state()
        result = detect_rebuild_gap(
            state, "go test ./...", 0, "Bash",
            {"command": "go test ./..."},
        )
        assert result is None

    def test_warns_when_testing_after_source_change(self):
        state = fresh_state()
        # Edit a TypeScript file
        detect_rebuild_gap(state, "", 0, "Write", {"file_path": "/project/src/app.ts"})
        assert state["source_changed_since_build"] is True

        # Run tests without building (npm test doesn't implicitly compile TS)
        result = detect_rebuild_gap(
            state, "npm test", 0, "Bash",
            {"command": "npm test"},
        )
        assert result is not None
        assert "rebuild" in result.lower()

    def test_build_resets_flag(self):
        state = fresh_state()
        # Edit source
        detect_rebuild_gap(state, "", 0, "Write", {"file_path": "/project/main.go"})
        assert state["source_changed_since_build"] is True

        # Build
        detect_rebuild_gap(state, "go build ./...", 0, "Bash", {"command": "go build ./..."})
        assert state["source_changed_since_build"] is False

    def test_no_warning_for_python_files(self):
        state = fresh_state()
        # Edit a Python file (interpreted, no build needed)
        detect_rebuild_gap(state, "", 0, "Write", {"file_path": "/project/main.py"})
        assert state["source_changed_since_build"] is False

    def test_tracks_typescript_files(self):
        state = fresh_state()
        detect_rebuild_gap(state, "", 0, "Write", {"file_path": "/project/src/app.ts"})
        assert state["source_changed_since_build"] is True

    def test_tracks_rust_files(self):
        state = fresh_state()
        detect_rebuild_gap(state, "", 0, "Edit", {"file_path": "/project/src/lib.rs"})
        assert state["source_changed_since_build"] is True

    def test_npm_build_resets(self):
        state = fresh_state()
        state["source_changed_since_build"] = True
        detect_rebuild_gap(
            state, "npm run build", 0, "Bash",
            {"command": "npm run build"},
        )
        assert state["source_changed_since_build"] is False

    def test_cargo_build_resets(self):
        state = fresh_state()
        state["source_changed_since_build"] = True
        detect_rebuild_gap(
            state, "cargo build", 0, "Bash",
            {"command": "cargo build"},
        )
        assert state["source_changed_since_build"] is False


# --- Integration: analyze() function ---

class TestAnalyze:
    """Test the full analyze() pipeline with isolated state file."""

    @pytest.fixture(autouse=True)
    def isolated_state(self, tmp_path):
        state_file = tmp_path / "rapids_exec_risk_test.json"
        with patch("aah.core.guards.execution_risk_monitor._state_path", return_value=state_file):
            yield state_file

    def test_no_warnings_on_success(self):
        warnings = analyze(bash_input("ls -la", exit_code=0))
        assert warnings == []

    def test_doom_loop_integration(self):
        stderr = "ModuleNotFoundError: No module named 'flask'"
        for _ in range(REPEAT_THRESHOLD - 1):
            warnings = analyze(bash_input("python app.py", exit_code=1, stderr=stderr))
            assert warnings == []

        warnings = analyze(bash_input("python app.py", exit_code=1, stderr=stderr))
        assert len(warnings) == 1
        assert "doom loop" in warnings[0].lower()

    def test_connection_refused_integration(self):
        stderr = "curl: (7) Failed to connect: Connection refused"
        analyze(bash_input("curl localhost:3000", exit_code=7, stderr=stderr))
        warnings = analyze(bash_input("curl localhost:3000", exit_code=7, stderr=stderr))
        assert len(warnings) == 1
        assert "service running" in warnings[0].lower()

    def test_state_persists_across_calls(self):
        stderr = "Error: something broke"
        analyze(bash_input("cmd", exit_code=1, stderr=stderr))
        analyze(bash_input("cmd", exit_code=1, stderr=stderr))
        warnings = analyze(bash_input("cmd", exit_code=1, stderr=stderr))
        assert len(warnings) >= 1

    def test_command_counter_increments(self, isolated_state):
        for _ in range(5):
            analyze(bash_input("echo hello"))
        state = json.loads(isolated_state.read_text())
        assert state["total_commands"] == 5

    def test_command_history_maintained(self, isolated_state):
        analyze(bash_input("echo one"))
        analyze(bash_input("echo two"))
        state = json.loads(isolated_state.read_text())
        assert len(state["command_history"]) == 2

    def test_command_history_capped_at_20(self, isolated_state):
        for i in range(25):
            analyze(bash_input(f"echo {i}"))
        state = json.loads(isolated_state.read_text())
        assert len(state["command_history"]) == 20

    def test_write_tracking_for_proxy_test(self):
        # Write an ad-hoc check script
        analyze(write_input("/project/check_output.py"))
        # Run it
        warnings = analyze(bash_input("python check_output.py"))
        assert len(warnings) == 1
        assert "self-written" in warnings[0].lower()

    def test_rebuild_gap_integration(self):
        # Edit a .ts file
        analyze(edit_input("/project/src/app.ts"))
        # Run tests without building (npm test doesn't implicitly compile TS)
        warnings = analyze(bash_input("npm test"))
        assert any("rebuild" in w.lower() for w in warnings)

    def test_non_bash_tools_dont_increment_counter(self, isolated_state):
        analyze(write_input("/project/foo.py"))
        state = json.loads(isolated_state.read_text())
        assert state["total_commands"] == 0

    def test_always_exits_zero(self):
        """The monitor is advisory — it should never block."""
        proc = subprocess.run(
            [sys.executable, "-m", "aah.core.guards.execution_risk_monitor"],
            input=json.dumps(bash_input("cat /etc/passwd", exit_code=1, stderr="permission denied")),
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
        )
        assert proc.returncode == 0

    def test_handles_invalid_json(self):
        proc = subprocess.run(
            [sys.executable, "-m", "aah.core.guards.execution_risk_monitor"],
            input="not json",
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
        )
        assert proc.returncode == 0

    def test_handles_empty_input(self):
        proc = subprocess.run(
            [sys.executable, "-m", "aah.core.guards.execution_risk_monitor"],
            input="",
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
        )
        assert proc.returncode == 0
