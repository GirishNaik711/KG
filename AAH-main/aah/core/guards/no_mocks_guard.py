#!/usr/bin/env python3
"""
PreToolUse hook guard: warn on mock framework installation (relaxed mode).

Reads hook input JSON from stdin (Claude Code convention).
Detects attempts to install mock frameworks via pip/npm/etc.
Exit 0 = allow (with warning). Mocks are permitted as fallback when
functional testing is not feasible.
"""

import json
import re
import sys
from typing import Any


# Patterns that indicate mock framework installation or usage
BLOCKED_PACKAGES = [
    # Python
    "pytest-mock", "unittest.mock", "mock", "responses", "httpretty",
    "vcrpy", "freezegun", "fakeredis", "moto", "localstack",
    # JavaScript/TypeScript
    "sinon", "nock", "testdouble", "jest-mock", "proxyquire",
    "msw", "miragejs", "json-server",
    # General patterns
    "mock", "stub", "fake",
]

# Regex patterns for detecting mock installation commands
INSTALL_PATTERNS = [
    r"pip\s+install\s+.*(?:pytest-mock|mock|responses|httpretty|vcrpy|freezegun|fakeredis|moto)",
    r"npm\s+install\s+.*(?:sinon|nock|testdouble|msw|miragejs|jest-mock|proxyquire)",
    r"yarn\s+add\s+.*(?:sinon|nock|testdouble|msw|miragejs|jest-mock|proxyquire)",
    r"pnpm\s+(?:add|install)\s+.*(?:sinon|nock|testdouble|msw|miragejs|jest-mock|proxyquire)",
    r"uv\s+(?:pip\s+install|add)\s+.*(?:pytest-mock|mock|responses|httpretty|vcrpy|freezegun|fakeredis|moto)",
]

# Patterns for mock usage in code (for Bash commands running tests with mocks)
MOCK_USAGE_PATTERNS = [
    r"from\s+unittest\.mock\s+import",
    r"from\s+unittest\s+import\s+mock",
    r"import\s+unittest\.mock",
    r"@patch\(",
    r"@mock\.",
    r"jest\.mock\(",
    r"jest\.spyOn\(",
    r"sinon\.stub\(",
    r"sinon\.mock\(",
    r"vi\.mock\(",
]


def check_command(command: str) -> str | None:
    """
    Check if a bash command contains mock framework installation or usage.
    Returns a reason string if blocked, None if allowed.
    """
    command_lower = command.lower()

    # Check installation patterns
    for pattern in INSTALL_PATTERNS:
        if re.search(pattern, command_lower):
            match = re.search(pattern, command_lower)
            return f"Blocked: mock framework installation detected: {match.group()}"

    # Check for mock usage patterns in inline code execution
    for pattern in MOCK_USAGE_PATTERNS:
        if re.search(pattern, command):
            return f"Blocked: mock framework usage detected in command"

    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        # If no input or invalid JSON, allow (non-hook invocation)
        sys.exit(0)

    # Extract the command from tool_input
    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    reason = check_command(command)
    if reason:
        print(
            f"AAH notice: {reason}. Mocks are permitted as fallback when functional "
            "testing is not feasible. Prefer functional tests where possible.",
            file=sys.stderr,
        )
        # Allow but warn — no longer blocking (exit 0)

    sys.exit(0)


if __name__ == "__main__":
    main()
