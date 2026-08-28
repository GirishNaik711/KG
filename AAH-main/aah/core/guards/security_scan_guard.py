#!/usr/bin/env python3
"""
PreToolUse:Bash guard: block deployment commands without passing security scan.

Two-stage design for performance:
  1. Fast regex match against deployment command patterns — exit 0 immediately
     if no match (zero file I/O for non-deployment commands).
  2. On match: read scan-results-latest.json and check policy_result.passed.

Exit 0 = allow, Exit 2 = block (stderr fed back to agent).
"""

import json
import re
import sys


DEPLOY_PATTERNS = [
    r"docker\s+push\b",
    r"docker\s+buildx\s+push\b",
    r"kubectl\s+apply\b",
    r"terraform\s+apply\b",
    r"helm\s+install\b",
    r"helm\s+upgrade\b",
    r"cdk\s+deploy\b",
    r"aws\s+ecs\s+update-service\b",
    r"gcloud\s+(?:run|app)\s+deploy\b",
]


def check_command(command: str) -> str | None:
    """Check if a deployment command should be blocked due to failing security scan.

    Returns a reason string if blocked, None if allowed.
    Stage 1: regex match — returns None immediately for non-deployment commands.
    Stage 2: file I/O — only runs when a deployment command is detected.
    """
    command_lower = command.lower()

    # Skip inline scripts — deploy keywords inside string literals are not
    # actual deployment commands. Handles: python3 -c "...", PYTHONPATH=... python3 -c "..."
    if re.search(r"\bpython3?\s+-[ce]\s+", command_lower):
        return None
    if re.search(r"\b(?:ruby|node)\s+-[ce]\s+", command_lower):
        return None

    # Skip grep/cat/echo/read commands that reference deploy keywords in content
    if re.match(r"^\s*(?:\w+=\S+\s+)*(?:grep|cat|head|tail|echo|printf|read|less|more)\s+", command_lower):
        return None

    matched_pattern = None
    for pattern in DEPLOY_PATTERNS:
        if re.search(pattern, command_lower):
            matched_pattern = pattern
            break

    if matched_pattern is None:
        return None

    from pathlib import Path
    from aah.core.common.config import resolve_project_path

    project_path = resolve_project_path()
    if project_path is None:
        return None

    from aah.core.security.state import SecurityStateMigrationError, ensure_security_state

    try:
        security_dir = ensure_security_state(project_path, notify=True).security_dir
    except SecurityStateMigrationError as exc:
        return f"Blocked: security state migration failed: {exc}"

    results_file = security_dir / "scan-results-latest.json"
    if not results_file.exists():
        return (
            "Blocked: deployment command detected but no security scan results found. "
            "Run /aah-security-scan before deploying."
        )

    try:
        with open(results_file, encoding='utf-8') as f:
            results = json.load(f)
    except (json.JSONDecodeError, OSError):
        return (
            "Blocked: deployment command detected but security scan results are unreadable. "
            "Re-run /aah-security-scan."
        )

    policy_result = results.get("policy_result", {})
    if not policy_result.get("passed", False):
        reason = policy_result.get("reason", "unknown")
        blocking = policy_result.get("blocking_findings", 0)
        return (
            f"Blocked: deployment command detected but security scan failed "
            f"({reason}, {blocking} blocking findings). "
            f"Resolve findings or run /aah-security-scan before deploying."
        )

    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    reason = check_command(command)
    if reason:
        print(reason, file=sys.stderr)
        print(
            "\nAAH policy: deployment commands require a passing security scan. "
            "See SECURITY-SCANNING-PLAN.md for details.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
