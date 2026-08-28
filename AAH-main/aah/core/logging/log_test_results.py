#!/usr/bin/env python3
"""
PostToolUse hook: log test execution results to .aah/build/test-results/.

Triggered after Bash commands. Detects test execution output and logs
structured results. Exit 0 = logged (always succeeds).
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import append_jsonl


# Patterns indicating test execution
TEST_PATTERNS = [
    r"(\d+) passed",
    r"(\d+) failed",
    r"Tests?:\s+(\d+)",
    r"test[s]?\s+(?:passed|failed|error)",
    r"PASS\s",
    r"FAIL\s",
    r"pytest",
    r"jest",
    r"mocha",
    r"npm test",
]


def detect_test_output(stdout: str, stderr: str, command: str) -> dict | None:
    """
    Detect if command output contains test results.
    Returns parsed results dict or None.
    """
    combined = stdout + "\n" + stderr
    command_lower = command.lower()

    # Check if this looks like a test command
    is_test_command = any(kw in command_lower for kw in [
        "pytest", "test", "jest", "mocha", "vitest", "cargo test",
        "go test", "npm test", "yarn test",
    ])

    if not is_test_command:
        return None

    # Try to parse Jest/Mocha output first (more specific pattern)
    jest_match = re.search(r"Tests:\s+(?:(\d+) failed,\s+)?(\d+) passed", combined)
    if jest_match:
        failed = int(jest_match.group(1)) if jest_match.group(1) else 0
        passed = int(jest_match.group(2))
        return {
            "framework": "jest",
            "passed": passed,
            "failed": failed,
            "total": passed + failed,
            "success": failed == 0,
        }

    # Try to parse pytest output
    passed_match = re.search(r"(\d+) passed", combined)
    failed_match = re.search(r"(\d+) failed", combined)
    error_match = re.search(r"(\d+) error", combined)

    if passed_match or failed_match:
        passed = int(passed_match.group(1)) if passed_match else 0
        failed = int(failed_match.group(1)) if failed_match else 0
        errors = int(error_match.group(1)) if error_match else 0

        return {
            "framework": "pytest",
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "total": passed + failed + errors,
            "success": failed == 0 and errors == 0,
        }

    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_output", {})
    command = tool_input.get("command", "")
    stdout = tool_output.get("stdout", "")
    stderr = tool_output.get("stderr", "")

    if not command:
        sys.exit(0)

    test_results = detect_test_output(stdout, stderr, command)
    if test_results is None:
        sys.exit(0)

    # Determine project path
    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    # Log the results
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command[:500],
        "results": test_results,
        "evaluator": "--evaluator" in sys.argv,
    }

    log_path = project_path / ".aah" / "build" / "test-results" / "test-execution-log.jsonl"
    try:
        append_jsonl(log_entry, log_path)
    except Exception:
        pass  # Non-fatal

    sys.exit(0)


if __name__ == "__main__":
    main()
