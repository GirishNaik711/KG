#!/usr/bin/env python3
"""
Stop hook: validate security scan results before deploy phase.

Advisory gate — reads current phase from manifest and exits 0 immediately
if the project is not in the implement phase. Primary enforcement is
the /aah-deploy skill (Step 1.5).

Exit 0 = pass, Exit 2 = fail.
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json
from aah.core.security.policy import check_staleness
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state


def validate_security_scan(project_path: Path) -> tuple[bool, list[str]]:
    """Check that security scan results exist, are fresh, and pass policy."""
    issues = []
    try:
        security_dir = ensure_security_state(project_path, notify=True).security_dir
    except SecurityStateMigrationError as exc:
        return False, [f"Security state migration failed: {exc}"]

    results_file = security_dir / "scan-results-latest.json"
    if not results_file.exists():
        issues.append(
            "No security scan results found. "
            "Run /aah-security-scan first."
        )
        return False, issues

    try:
        results = read_json(results_file)
    except Exception as e:
        issues.append(f"Error reading scan results: {e}")
        return False, issues

    scan_ts = results.get("scan_timestamp", "")
    max_age = results.get("policy_result", {}).get("max_age_hours", 24)
    if check_staleness(scan_ts, max_age):
        issues.append(
            f"Scan results are stale (max {max_age}h). Re-run /aah-security-scan."
        )
        return False, issues

    policy_result = results.get("policy_result", {})
    if not policy_result.get("passed", False):
        reason = policy_result.get("reason", "unknown")
        blocking = policy_result.get("blocking_findings", 0)
        issues.append(
            f"Security gate failed: {reason} ({blocking} blocking findings)"
        )

        findings = results.get("findings", [])
        blocking_findings = [
            f for f in findings
            if not f.get("suppressed") and f.get("severity") in ("critical", "high")
        ]
        for f in blocking_findings[:10]:
            issues.append(
                f"  - [{f['severity'].upper()}] {f['file']}:{f.get('line', 0)} "
                f"({f['rule_id']}): {f['message'][:100]}"
            )

        return False, issues

    return True, []


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path

    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    # Determine current phase — only hard-block during deploy transition
    phase = ""
    try:
        from aah.core.common.manifest import load_manifest, find_manifest
        manifest_path = find_manifest(project_path)
        if manifest_path:
            manifest = load_manifest(manifest_path)
            phase = manifest.get("current_phase", "")
    except Exception:
        sys.exit(0)

    if phase not in ("implement", "deploy"):
        sys.exit(0)

    from aah.core.guards.delegation_guard import exit_if_delegated
    exit_if_delegated()

    passed, issues = validate_security_scan(project_path)

    if not passed:
        # During implement phase: advisory only (exit 0)
        if phase == "implement":
            print("Security scan advisory:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}", file=sys.stderr)
            print(
                "\nRun /aah-security-scan before moving to deploy phase.",
                file=sys.stderr,
            )
            sys.exit(0)

        # During deploy phase: advisory (exit 0) — actual deploy commands
        # are blocked by security_scan_guard.py (PreToolUse:Bash)
        print("Security scan gate WARNING:", file=sys.stderr)
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        print(
            "\nRun /aah-security-scan to scan and resolve findings "
            "before deploying. Deploy commands will be blocked until "
            "the scan passes.",
            file=sys.stderr,
        )
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
