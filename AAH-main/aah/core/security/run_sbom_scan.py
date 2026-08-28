#!/usr/bin/env python3
"""SBOM generation and vulnerability scanning orchestrator.

Entry point: aah run core.security.run_sbom_scan run [options]
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.io_utils import ensure_dir, read_yaml, write_json, write_text
from aah.core.security import parsers_phase2, policy, tool_check
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state

TOOL_TIMEOUT = 600


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


def run_sbom(
    project_path: Path,
    sbom_format: str = "cyclonedx",
) -> dict:
    """Generate SBOM via Syft and scan with Grype."""
    aah_path = ensure_security_state(project_path, notify=True).security_dir.parent
    sbom_dir = ensure_dir(aah_path / "security" / "sbom")
    raw_dir = ensure_dir(aah_path / "security" / "raw")
    reports_dir = ensure_dir(aah_path / "security" / "reports")

    tools = tool_check.check_tools()
    all_findings: list[dict] = []
    all_errors: list[dict] = []

    # Generate SBOM via Syft
    if not tools.get("syft", {}).get("available", False):
        all_errors.append({
            "tool": "syft",
            "error_type": "not_installed",
            "message": "Syft not found. Install from https://github.com/anchore/syft",
            "stderr_tail": "",
        })
    else:
        formats_to_generate = []
        if sbom_format in ("cyclonedx", "both"):
            formats_to_generate.append(
                ("cyclonedx-json", sbom_dir / "sbom-cyclonedx.json")
            )
        if sbom_format in ("spdx", "both"):
            formats_to_generate.append(
                ("spdx-json", sbom_dir / "sbom-spdx.json")
            )

        for fmt, output_path in formats_to_generate:
            cmd = [
                "syft", f"dir:{project_path}",
                "-o", f"{fmt}={output_path}",
            ]
            returncode, stdout, stderr = _run_tool(cmd)
            if returncode != 0:
                all_errors.append({
                    "tool": "syft",
                    "error_type": "crash",
                    "message": f"Syft {fmt} generation failed (exit {returncode})",
                    "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
                })

    # Scan SBOM with Grype — pick whichever format was generated
    sbom_file = sbom_dir / "sbom-cyclonedx.json"
    if not sbom_file.exists():
        sbom_file = sbom_dir / "sbom-spdx.json"
    if sbom_file.exists() and tools.get("grype", {}).get("available", False):
        grype_output = raw_dir / "grype-results.json"
        cmd = [
            "grype", f"sbom:{sbom_file}",
            "-o", "json",
            "--file", str(grype_output),
        ]
        returncode, stdout, stderr = _run_tool(cmd)
        if returncode in (0, 1):
            all_findings.extend(parsers_phase2.parse_grype_json(grype_output))
        else:
            all_errors.append({
                "tool": "grype",
                "error_type": "crash",
                "message": f"Grype scan failed (exit {returncode})",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })
    elif not tools.get("grype", {}).get("available", False):
        all_errors.append({
            "tool": "grype",
            "error_type": "not_installed",
            "message": "Grype not found. Install from https://github.com/anchore/grype",
            "stderr_tail": "",
        })

    # NOTE: Artifact signing via cosign is planned for a future release.
    # See policy.yaml `supply_chain_signing` and `sbom.sign_artifacts` fields.
    sign_status = "not_requested"

    # Generate report
    _generate_sbom_report(
        all_findings, all_errors, sbom_file, sign_status,
        reports_dir / "sbom-report.md", project_path,
    )

    return {
        "tool": "sbom",
        "findings": all_findings,
        "errors": all_errors,
        "sbom_path": str(sbom_file) if sbom_file.exists() else "",
        "sign_status": sign_status,
    }


def _generate_sbom_report(
    findings: list[dict],
    errors: list[dict],
    sbom_path: Path,
    sign_status: str,
    report_path: Path,
    project_path: Path,
) -> None:
    """Generate SBOM report markdown."""
    lines = [
        "# SBOM and Supply Chain Report",
        "",
        f"**Project:** {project_path.name}",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "## SBOM Generation",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| SBOM File | {'Present' if sbom_path.exists() else 'Not generated'} |",
        f"| Signing Status | {sign_status} |",
        "",
        "## Vulnerability Summary",
        "",
        f"| Severity | Count |",
        f"|----------|-------|",
    ]

    for sev in ("critical", "high", "medium", "low"):
        count = sum(1 for f in findings if f["severity"] == sev)
        lines.append(f"| {sev.title()} | {count} |")

    lines.append(f"| **Total** | **{len(findings)}** |")

    if findings:
        lines.extend(["", "## Vulnerability Details", ""])
        lines.append("| CVE | Severity | Package | Version | Fix | Description |")
        lines.append("|-----|----------|---------|---------|-----|-------------|")
        for f in findings[:30]:
            msg = f["message"][:80].replace("|", "\\|")
            lines.append(
                f"| {f['rule_id']} | {f['severity']} | {f['file']} "
                f"| | | {msg} |"
            )

    if errors:
        lines.extend(["", "## Errors", ""])
        for err in errors:
            lines.append(f"- **{err['tool']}** ({err['error_type']}): {err['message']}")

    lines.append("")
    write_text("\n".join(lines), report_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH SBOM Generator")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Generate SBOM and scan for vulnerabilities")
    run_parser.add_argument(
        "--format",
        choices=["cyclonedx", "spdx", "both"],
        default="cyclonedx",
        help="SBOM format (default: cyclonedx)",
    )
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

    print(f"Generating SBOM ({args.format} format)...")
    print(f"Project: {project_path}")

    result = run_sbom(project_path, args.format)

    findings = result["findings"]
    errors = result["errors"]
    print(f"\nSBOM scan: {len(findings)} vulnerabilities, {len(errors)} errors")

    if errors:
        for err in errors:
            print(f"  {err['tool']}: {err['message']}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
