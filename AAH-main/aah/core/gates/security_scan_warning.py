#!/usr/bin/env python3
"""
TeammateIdle hook: advisory security scan status check.

Runs alongside validate_regression_pass at wave boundaries. Prints warnings
to stderr but NEVER blocks (always exits 0). Proactively runs a quick
SAST+secrets scan if no fresh results exist.

Exit 0 = always (advisory only).
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json
from aah.core.security.policy import check_staleness
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state


def check_security_status(project_path: Path) -> list[str]:
    """Check security scan status and return advisory warnings."""
    warnings = []
    try:
        security_dir = ensure_security_state(project_path, notify=True).security_dir
    except SecurityStateMigrationError as exc:
        return [f"Security state migration failed: {exc}"]
    results_file = security_dir / "scan-results-latest.json"

    if not results_file.exists():
        warnings.append(
            "No security scan results found. "
            "Consider running /aah-security-scan before the next wave."
        )
        _run_quick_scan(project_path, warnings)
        return warnings

    try:
        results = read_json(results_file)
    except Exception:
        warnings.append("Could not read security scan results.")
        return warnings

    scan_ts = results.get("scan_timestamp", "")
    max_age = results.get("policy_result", {}).get("max_age_hours", 24)
    if check_staleness(scan_ts, max_age):
        warnings.append(
            f"Security scan results are stale (>{max_age}h old). "
            "Consider re-running /aah-security-scan."
        )
        _run_quick_scan(project_path, warnings)
        return warnings

    policy_result = results.get("policy_result", {})
    if not policy_result.get("passed", True):
        reason = policy_result.get("reason", "unknown")
        blocking = policy_result.get("blocking_findings", 0)
        warnings.append(
            f"Security scan has unresolved issues: {reason} "
            f"({blocking} blocking findings). "
            "Run /aah-security-scan to review."
        )

        findings = results.get("findings", [])
        blocking_findings = [
            f for f in findings
            if not f.get("suppressed") and f.get("severity") in ("critical", "high")
        ]
        for f in blocking_findings[:5]:
            warnings.append(
                f"  [{f['severity'].upper()}] {f.get('file', '?')}:{f.get('line', 0)} "
                f"— {f.get('message', '')[:80]}"
            )

    return warnings


def _run_quick_scan(project_path: Path, warnings: list[str]) -> None:
    """Run a lightweight SAST+secrets+deps scan if tools are available."""
    try:
        from aah.core.security.tool_check import check_tools

        tools = check_tools()
        has_semgrep = tools.get("semgrep", {}).get("available", False)
        has_gitleaks = tools.get("gitleaks", {}).get("available", False)
        has_trivy = tools.get("trivy", {}).get("available", False)

        if not has_semgrep and not has_gitleaks and not has_trivy:
            return

        scan_types = []
        if has_semgrep:
            scan_types.append("sast")
        if has_gitleaks:
            scan_types.append("secrets")
        if has_trivy:
            scan_types.append("deps")

        from aah.core.security.run_security_scan import run_scan

        result = run_scan(project_path, scan_types)
        s = result.get("summary", {})

        critical = s.get("critical", 0)
        high = s.get("high", 0)
        if critical or high:
            warnings.append(
                f"Quick scan found {critical} critical + {high} high severity findings."
            )

            findings = result.get("findings", [])
            dep_blockers = [
                f for f in findings
                if f.get("tool") == "trivy" and not f.get("suppressed")
                and f.get("severity") in ("critical", "high")
            ]
            if dep_blockers:
                warnings.append(
                    f"  {len(dep_blockers)} vulnerable dependency CVE(s) — "
                    "run /aah-security-fix to upgrade."
                )
    except Exception as exc:
        print(f"Security scan warning: quick scan failed ({exc})", file=sys.stderr)


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

    warnings = check_security_status(project_path)

    if warnings:
        print("Security advisory:", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)
        print(file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
