#!/usr/bin/env python3
"""DAST scanning orchestrator — OWASP ZAP + Nuclei.

Entry point: aah run core.security.run_dast_scan run [options]
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.io_utils import ensure_dir, read_yaml, write_json, write_text
from aah.core.security import parsers_phase2, policy, suppressions, tool_check
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state

TOOL_TIMEOUT = 600
ZAP_TIMEOUT = 1800


def _dast_target_allowed(url: str, dast_config: dict) -> tuple[bool, str]:
    """Return (allowed, reason) — DAST may only hit declared local/test endpoints.

    Fail-closed target safety: a DAST scan is an active, potentially
    intrusive probe, so it is restricted to loopback (localhost / 127.0.0.1 /
    ::1) OR an endpoint the policy explicitly declared under
    ``dast.allowed_targets``. Anything else is refused so a scan can never fire
    against an arbitrary remote host. In default CI (no network) nothing is
    declared and no localhost service is up, so DAST simply records a
    configuration error rather than reaching the network.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False, f"unparseable target URL: {url!r}"
    host = (parsed.hostname or "").lower()
    loopback = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if host in loopback:
        return True, ""
    declared = dast_config.get("allowed_targets") or []
    if isinstance(declared, str):
        declared = [declared]
    declared_set = {str(d).strip() for d in declared if str(d).strip()}
    if url in declared_set or host in declared_set:
        return True, ""
    return False, (
        f"DAST target {url!r} is not loopback and not in dast.allowed_targets; "
        "refusing to scan a non-local/undeclared endpoint"
    )


def _run_tool(cmd: list[str], timeout: int = TOOL_TIMEOUT) -> tuple[int, str, str]:
    """Run a tool, returning (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Tool timed out after {timeout}s"
    except OSError as e:
        return -1, "", str(e)


def _sanitize_stderr(stderr: str, project_path: Path) -> str:
    """Strip project path from stderr to avoid leaking filesystem paths in reports."""
    if not stderr:
        return ""
    abs_str = str(project_path.resolve())
    raw_str = str(project_path)
    result = stderr.replace(abs_str, "<project>")
    if raw_str != abs_str:
        result = result.replace(raw_str, "<project>")
    return result


def _check_docker_available() -> bool:
    """Check if Docker is available for running ZAP."""
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_dast(
    project_path: Path,
    mode: str = "baseline",
    target_url: str = "",
) -> dict:
    """Run DAST scans via ZAP (Docker) and Nuclei."""
    aah_path = ensure_security_state(project_path, notify=True).security_dir.parent
    raw_dir = ensure_dir(aah_path / "security" / "raw")
    reports_dir = ensure_dir(aah_path / "security" / "reports")

    scan_policy = policy.load_policy(aah_path)
    dast_config = scan_policy.get("dast", {})

    url = target_url or dast_config.get("target_url", "")
    if not url:
        return {
            "tool": "dast",
            "findings": [],
            "errors": [{
                "tool": "dast",
                "error_type": "configuration",
                "message": (
                    "No target URL configured. Set dast.target_url in "
                    ".aah/security/policy.yaml or pass --target URL."
                ),
                "stderr_tail": "",
            }],
        }

    if not url.startswith(("http://", "https://")):
        return {
            "tool": "dast",
            "findings": [],
            "errors": [{
                "tool": "dast",
                "error_type": "configuration",
                "message": (
                    f"Invalid target URL: {url!r}. "
                    "URL must start with http:// or https://."
                ),
                "stderr_tail": "",
            }],
        }

    # Target safety: only declared local/test endpoints (loopback or an explicit
    # dast.allowed_targets entry). No network in default CI (nothing declared).
    allowed, deny_reason = _dast_target_allowed(url, dast_config)
    if not allowed:
        return {
            "tool": "dast",
            "findings": [],
            "errors": [{
                "tool": "dast",
                "error_type": "target_not_allowed",
                "message": deny_reason,
                "stderr_tail": "",
            }],
            "target_url": url,
        }

    tools = tool_check.check_tools()
    all_findings: list[dict] = []
    all_errors: list[dict] = []

    # OWASP ZAP via Docker
    docker_ok = _check_docker_available()
    if docker_ok:
        zap_mode = mode or dast_config.get("zap_scan_mode", "baseline")
        zap_script = {
            "baseline": "zap-baseline.py",
            "api": "zap-api-scan.py",
            "full": "zap-full-scan.py",
        }.get(zap_mode, "zap-baseline.py")

        zap_output = raw_dir / "zap-results.json"
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{raw_dir}:/zap/wrk",
            "ghcr.io/zaproxy/zaproxy:stable",
            zap_script,
            "-t", url,
            "-J", "zap-results.json",
        ]

        if zap_mode == "api":
            cmd.extend(["-f", "openapi"])

        returncode, stdout, stderr = _run_tool(cmd, timeout=ZAP_TIMEOUT)
        if zap_output.exists():
            all_findings.extend(parsers_phase2.parse_zap_json(zap_output))
        elif returncode != 0:
            all_errors.append({
                "tool": "zap",
                "error_type": "crash",
                "message": f"ZAP {zap_mode} scan failed (exit {returncode})",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })
    else:
        all_errors.append({
            "tool": "zap",
            "error_type": "not_installed",
            "message": "Docker not available. ZAP requires Docker to run.",
            "stderr_tail": "",
        })

    # Nuclei template scan
    if tools.get("nuclei", {}).get("available", False):
        nuclei_output = raw_dir / "nuclei-results.json"
        nuclei_tags = dast_config.get("nuclei_tags", ["cve", "misconfig", "exposure"])
        tags_str = ",".join(nuclei_tags)

        # Bounded request rate: cap Nuclei's outbound rate so an active scan
        # against a local/test endpoint can never become a flood. Policy may
        # tune ``dast.rate_limit`` but never above a hard ceiling.
        try:
            rate_limit = int(dast_config.get("rate_limit", 50))
        except (TypeError, ValueError):
            rate_limit = 50
        rate_limit = max(1, min(rate_limit, 150))

        cmd = [
            "nuclei",
            "-u", url,
            "-tags", tags_str,
            "-severity", "critical,high,medium",
            "-rate-limit", str(rate_limit),
            "-jsonl",
            "-output", str(nuclei_output),
            "-silent",
        ]

        custom_templates = aah_path / "security" / "nuclei-templates"
        if custom_templates.is_dir() and any(custom_templates.iterdir()):
            cmd.extend(["-t", str(custom_templates)])

        returncode, stdout, stderr = _run_tool(cmd)
        if nuclei_output.exists():
            all_findings.extend(parsers_phase2.parse_nuclei_json(nuclei_output))
        elif returncode != 0:
            all_errors.append({
                "tool": "nuclei",
                "error_type": "crash",
                "message": f"Nuclei scan failed (exit {returncode})",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })
    else:
        all_errors.append({
            "tool": "nuclei",
            "error_type": "not_installed",
            "message": "Nuclei not found. Install from https://github.com/projectdiscovery/nuclei",
            "stderr_tail": "",
        })

    # Generate report
    _generate_dast_report(all_findings, all_errors, url, mode, reports_dir / "dast-report.md")

    return {
        "tool": "dast",
        "findings": all_findings,
        "errors": all_errors,
        "target_url": url,
    }


def _generate_dast_report(
    findings: list[dict],
    errors: list[dict],
    target_url: str,
    mode: str,
    report_path: Path,
) -> None:
    """Generate DAST report markdown."""
    zap_findings = [f for f in findings if f["tool"] == "zap"]
    nuclei_findings = [f for f in findings if f["tool"] == "nuclei"]

    lines = [
        "# DAST Scan Report",
        "",
        f"**Target:** {target_url}",
        f"**Scan Mode:** {mode}",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Combined Summary",
        "",
        f"| Source | Findings |",
        f"|--------|----------|",
        f"| OWASP ZAP | {len(zap_findings)} |",
        f"| Nuclei | {len(nuclei_findings)} |",
        f"| **Total** | **{len(findings)}** |",
        "",
        "## Severity Breakdown",
        "",
        f"| Severity | Count |",
        f"|----------|-------|",
    ]

    for sev in ("critical", "high", "medium", "low"):
        count = sum(1 for f in findings if f["severity"] == sev)
        lines.append(f"| {sev.title()} | {count} |")

    if zap_findings:
        lines.extend(["", "## ZAP Findings", ""])
        lines.append("| ID | Severity | CWE | URL | Description |")
        lines.append("|-----|----------|-----|-----|-------------|")
        for f in zap_findings[:25]:
            msg = f["message"][:80].replace("|", "\\|")
            lines.append(f"| {f['id']} | {f['severity']} | {f['cwe']} | {f['file'][:60]} | {msg} |")

    if nuclei_findings:
        lines.extend(["", "## Nuclei Findings", ""])
        lines.append("| ID | Template | Severity | Matched URL | Description |")
        lines.append("|-----|----------|----------|-------------|-------------|")
        for f in nuclei_findings[:25]:
            msg = f["message"][:80].replace("|", "\\|")
            lines.append(f"| {f['id']} | {f['rule_id']} | {f['severity']} | {f['file'][:60]} | {msg} |")

    if errors:
        lines.extend(["", "## Errors", ""])
        for err in errors:
            lines.append(f"- **{err['tool']}** ({err['error_type']}): {err['message']}")

    lines.append("")
    write_text("\n".join(lines), report_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH DAST Scanner (ZAP + Nuclei)")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run DAST scans")
    run_parser.add_argument(
        "--mode",
        choices=["baseline", "api", "full"],
        default="baseline",
        help="ZAP scan mode (default: baseline)",
    )
    run_parser.add_argument("--target", help="Target URL to scan")
    run_parser.add_argument("--project-path", help="Explicit project path")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    explicit = Path(args.project_path) if args.project_path else None
    project_path = resolve_project_path(explicit)
    if project_path is None:
        print("Error: could not resolve project path", file=sys.stderr)
        sys.exit(1)

    try:
        ensure_security_state(project_path, notify=True)
    except SecurityStateMigrationError as exc:
        print(f"Error: security state migration failed: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"Running DAST scan ({args.mode} mode)...")
    print(f"Project: {project_path}")

    result = run_dast(project_path, args.mode, args.target or "")

    findings = result["findings"]
    errors = result["errors"]
    print(f"\nDAST: {len(findings)} findings, {len(errors)} errors")

    if errors:
        for err in errors:
            print(f"  {err['tool']}: {err['message']}", file=sys.stderr)


if __name__ == "__main__":
    main()
