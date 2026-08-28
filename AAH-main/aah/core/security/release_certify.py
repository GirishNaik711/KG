#!/usr/bin/env python3
"""Release certification — scan the AAH framework and store audit reports.

Subcommands:
  certify  — Run full security scan, generate certification, commit to releases/
  list     — List all certified releases
  show     — Show certification details for a version
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import write_text
from aah.core.security import report_format as fmt
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state


def _framework_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _releases_dir() -> Path:
    return _framework_root() / "releases"


def _git_info() -> dict:
    """Get current git commit and user info."""
    info = {"commit": "unknown", "user": "unknown", "branch": "unknown"}
    root = _framework_root()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(root),
        )
        if proc.returncode == 0:
            info["commit"] = proc.stdout.strip()
        proc = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, cwd=str(root),
        )
        if proc.returncode == 0:
            info["user"] = proc.stdout.strip()
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=str(root),
        )
        if proc.returncode == 0:
            info["branch"] = proc.stdout.strip()
    except OSError:
        pass
    return info


def _validate_version(version: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+(-[\w.]+)?$", version))


_REDACT_KEYS = {"Match", "Secret", "match", "secret"}
_SECRET_PATTERN = re.compile(r'(sk-proj-|AKIA|ghp_|glpat-|xox[bpsar]-)\S+')


def _copy_redacted_scan_results(src: Path, dst: Path) -> None:
    """Copy scan-results JSON with sensitive match values redacted."""
    data = json.loads(src.read_text(encoding='utf-8'))

    def _redact(obj):
        if isinstance(obj, dict):
            return {k: ("[REDACTED]" if k in _REDACT_KEYS and isinstance(v, str) else _redact(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(item) for item in obj]
        if isinstance(obj, str):
            return _SECRET_PATTERN.sub("[REDACTED]", obj)
        return obj

    dst.write_text(json.dumps(_redact(data), indent=2), encoding='utf-8')


def certify(version: str, allow_warnings: bool = False) -> dict:
    """Run security scan on the framework and generate certification."""
    root = _framework_root()
    release_dir = _releases_dir() / version

    if release_dir.exists():
        return {"error": f"Release {version} already exists at {release_dir}"}

    print(f"Certifying AAH Framework v{version}")
    print(f"Framework root: {root}")
    print()

    # Run the security scan with --self
    from aah.core.security.run_security_scan import run_scan, _generate_reports
    from aah.core.security import (
        policy as policy_mod, priority as priority_mod,
        suppressions,
    )

    try:
        aah_path = ensure_security_state(root, notify=True).security_dir.parent
    except SecurityStateMigrationError as exc:
        return {"error": f"security state migration failed: {exc}"}
    scan_policy = policy_mod.load_policy(aah_path)

    all_scan_types = ["sast", "secrets", "deps", "licenses", "iac"]
    scan_types = [
        st for st in all_scan_types
        if scan_policy.get("scan_types", {}).get(st, False)
    ]
    if not scan_types:
        scan_types = ["sast", "secrets", "deps", "licenses", "iac"]

    print(f"Running scans: {', '.join(scan_types)}")
    result = run_scan(root, scan_types, project_name="AAH Framework")

    # Extended scans: deep SAST (CodeQL) and SBOM (Syft + Grype)
    extended = scan_policy.get("extended_scan_types", {})
    if extended.get("deep_sast", False):
        from aah.core.security.run_deep_sast import run_deep_sast
        print("Running extended scan: Deep SAST (CodeQL)")
        deep_result = run_deep_sast(root)
        result["findings"].extend(deep_result["findings"])
        result["errors"].extend(deep_result["errors"])

    if extended.get("sbom", False):
        from aah.core.security.run_sbom_scan import run_sbom
        sbom_fmt = scan_policy.get("sbom", {}).get("format", "cyclonedx")
        print(f"Running extended scan: SBOM (Syft + Grype, {sbom_fmt})")
        sbom_result = run_sbom(root, sbom_fmt)
        result["findings"].extend(sbom_result["findings"])
        result["errors"].extend(sbom_result["errors"])

    # Re-apply suppressions and re-evaluate policy if extended scans added findings
    if extended.get("deep_sast") or extended.get("sbom"):
        supp_list = suppressions.load_suppressions(aah_path)
        if supp_list:
            result["findings"] = suppressions.apply_suppressions(
                result["findings"], supp_list,
            )
        result["policy_result"] = policy_mod.evaluate_policy(
            result["findings"], result["errors"], scan_policy,
        )
        active = [f for f in result["findings"] if not f.get("suppressed")]
        result["summary"] = {
            "total_findings": len(result["findings"]),
            "critical": sum(1 for f in active if f["severity"] == "critical"),
            "high": sum(1 for f in active if f["severity"] == "high"),
            "medium": sum(1 for f in active if f["severity"] == "medium"),
            "low": sum(1 for f in active if f["severity"] == "low"),
            "suppressed": sum(1 for f in result["findings"] if f.get("suppressed")),
            "errors": len(result["errors"]),
        }
        # Regenerate all reports with merged findings
        from aah.core.common.io_utils import write_json
        reports_dir = aah_path / "security" / "reports"
        write_json(result, aah_path / "security" / "scan-results-latest.json")
        _generate_reports(result, reports_dir)

    # Apply priority scoring
    priority_mod.score_findings(result["findings"])

    # Check gate
    policy_result = result["policy_result"]
    passed = policy_result["passed"]
    blocking = policy_result.get("blocking_findings", 0)

    if not passed and not allow_warnings:
        print(f"\nCertification FAILED: {blocking} blocking findings.", file=sys.stderr)
        print("Use --allow-warnings to certify with non-blocking findings.", file=sys.stderr)
        return {
            "certified": False,
            "version": version,
            "blocking": blocking,
            "reason": policy_result.get("reason", ""),
        }

    # Collect tool versions
    tool_versions = {
        name: info["version"]
        for name, info in result["tools"].items()
        if info["available"] and info["version"]
    }

    git_info = _git_info()
    timestamp = datetime.now(timezone.utc).isoformat()

    # Count findings by scan type — only include tools that were actually run
    findings = result["findings"]
    errors = result["errors"]
    tool_map = {
        "semgrep": "SAST", "codeql": "Deep SAST", "gitleaks": "Secrets",
        "trivy": "Dependencies", "grype": "SBOM", "license-scan": "Licenses",
        "trivy-iac": "IaC",
    }
    tools_with_findings = {f["tool"] for f in findings}
    tools_with_errors = {e["tool"] for e in errors}
    ran_tools = tools_with_findings | tools_with_errors
    # Always include core scan types even with 0 findings
    core_tools = {"semgrep", "gitleaks", "trivy", "license-scan"}
    ran_tools |= core_tools

    scan_summary = {}
    for tool_name, label in tool_map.items():
        if tool_name not in ran_tools:
            continue
        tool_findings = [f for f in findings if f["tool"] == tool_name and not f.get("suppressed")]
        sc = {s: sum(1 for f in tool_findings if f["severity"] == s) for s in ("critical", "high", "medium", "low")}
        blocking_count = sc["critical"] + sc["high"]
        total = len(tool_findings)
        scan_summary[label] = {
            "total": total,
            "breakdown": f"{sc['critical']}C/{sc['high']}H/{sc['medium']}M/{sc['low']}L",
            "blocking": blocking_count,
        }

    suppressed = [f for f in findings if f.get("suppressed")]

    # Generate certification document
    cert_status = "PASSED" if passed or allow_warnings else "FAILED"
    is_passed = cert_status == "PASSED"

    # Build pie chart data for scan types
    pie_data = {}
    for label, data in scan_summary.items():
        if data["total"]:
            pie_data[label] = data["total"]

    cert_lines = [
        "# AAH Framework Security Certification",
        "",
        fmt.status_banner(is_passed),
        "",
        f"**Version:** {version}",
        f"**Date:** {timestamp[:10]}",
        f"**Certified by:** {git_info['user']}",
        f"**Git commit:** {git_info['commit']}",
        f"**Git branch:** {git_info['branch']}",
        f"**Tool versions:** {', '.join(f'{k} {v}' for k, v in tool_versions.items())}",
        "",
        f"## Certification Status: {cert_status}",
        "",
    ]

    # Mermaid pie chart
    cert_lines.extend(fmt.mermaid_pie("Findings by Scan Type", pie_data))

    cert_lines.extend([
        "## Scan Summary",
        "",
        "| Scan Type | Status | Findings | Breakdown | Blocking |",
        "|-----------|--------|----------|-----------|----------|",
    ])
    for label, data in scan_summary.items():
        if data["blocking"]:
            badge = "**FAIL**"
        elif data["total"] and data["total"] > data["blocking"]:
            badge = "**WARN**"
        else:
            badge = "**PASS**"
        cert_lines.append(
            f"| {label} | {badge} | {data['total']} | {data['breakdown']} | {data['blocking']} |"
        )

    cert_lines.extend([
        "",
        "## Policy Applied",
        "",
    ])
    thresholds = result["policy_result"].get("threshold", {})
    for sev, action in thresholds.items():
        cert_lines.append(f"- **{sev.title()}:** {action}")

    if suppressed:
        cert_lines.extend(["", f"## Suppressions Active: {len(suppressed)}", ""])
        for f in suppressed:
            cert_lines.append(f"- {f['tool']}:{f['rule_id']} in {f['file']}")

    report_manifest = [
        ("sast-report.md", "Static analysis findings (Semgrep)"),
        ("deep-sast-report.md", "Cross-file taint analysis (CodeQL)"),
        ("secrets-report.md", "Hardcoded credentials detection (Gitleaks)"),
        ("dependency-report.md", "Dependency vulnerabilities (Trivy)"),
        ("sbom-report.md", "SBOM vulnerability scan (Syft + Grype)"),
        ("license-report.md", "License compliance assessment"),
        ("iac-report.md", "Infrastructure as Code misconfiguration (Trivy)"),
        ("licenses.csv", "Full dependency license inventory"),
        ("scan-results.json", "Machine-readable scan data"),
    ]

    cert_lines.extend(["", "## Attached Reports", ""])
    reports_dir = aah_path / "security" / "reports"
    for filename, description in report_manifest:
        src = reports_dir / filename
        if src.exists() or filename == "scan-results.json":
            cert_lines.append(f"- [{filename}]({filename}) — {description}")
    cert_lines.append("")

    # Create release directory and copy only reports from this scan
    release_dir.mkdir(parents=True, exist_ok=True)
    for existing in release_dir.glob("*.md"):
        existing.unlink(missing_ok=True)
    for existing in release_dir.glob("*.csv"):
        existing.unlink(missing_ok=True)

    write_text("\n".join(cert_lines), release_dir / "certification.md")

    for filename, _ in report_manifest:
        if filename == "scan-results.json":
            continue
        src = reports_dir / filename
        if src.exists():
            shutil.copy2(src, release_dir / filename)

    # Copy scan results with sensitive match values redacted
    latest = aah_path / "security" / "scan-results-latest.json"
    if latest.exists():
        _copy_redacted_scan_results(latest, release_dir / "scan-results.json")

    print(f"\nCertification: {cert_status}")
    print(f"Reports stored in: {release_dir}")

    return {
        "certified": True,
        "version": version,
        "status": cert_status,
        "release_dir": str(release_dir),
        "findings_total": len(findings),
        "blocking": blocking,
        "scan_summary": scan_summary,
        "git_commit": git_info["commit"],
        "timestamp": timestamp,
    }


def list_releases() -> list[dict]:
    """List all certified releases."""
    releases_dir = _releases_dir()
    if not releases_dir.exists():
        return []

    releases = []
    for d in sorted(releases_dir.iterdir()):
        if d.is_dir() and (d / "certification.md").exists():
            cert_text = (d / "certification.md").read_text(encoding='utf-8')
            status = "PASSED" if "Status: PASSED" in cert_text else "FAILED"
            date_match = re.search(r"\*\*Date:\*\* (.+)", cert_text)
            date = date_match.group(1) if date_match else "unknown"
            commit_match = re.search(r"\*\*Git commit:\*\* (.+)", cert_text)
            commit = commit_match.group(1) if commit_match else "unknown"
            releases.append({
                "version": d.name,
                "status": status,
                "date": date,
                "commit": commit,
            })
    return releases


def show_release(version: str) -> str:
    """Show certification details for a version."""
    cert_path = _releases_dir() / version / "certification.md"
    if not cert_path.exists():
        return f"No certification found for version {version}"
    return cert_path.read_text(encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Release Certification")
    sub = parser.add_subparsers(dest="command")

    cert_parser = sub.add_parser("certify", help="Certify a release")
    cert_parser.add_argument("--version", required=True, help="Release version (semver)")
    cert_parser.add_argument("--allow-warnings", action="store_true",
                             help="Certify even with medium/low findings")

    sub.add_parser("list", help="List certified releases")

    show_parser = sub.add_parser("show", help="Show certification details")
    show_parser.add_argument("--version", required=True, help="Release version")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "certify":
        if not _validate_version(args.version):
            print(f"Invalid version: {args.version}. Use semver (e.g., 1.0.0, 0.2.0-rc.1)", file=sys.stderr)
            sys.exit(1)

        result = certify(args.version, args.allow_warnings)

        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

        if not result.get("certified"):
            sys.exit(2)

        # Commit and tag
        root = _framework_root()
        release_dir = _releases_dir() / args.version
        subprocess.run(
            ["git", "add", str(release_dir)],
            cwd=str(root),
        )
        subprocess.run(
            ["git", "commit", "-m", f"release: security certification for v{args.version}"],
            cwd=str(root),
        )
        subprocess.run(
            ["git", "tag", "-a", f"v{args.version}",
             "-m", f"AAH Framework v{args.version} — security certified"],
            cwd=str(root),
        )
        print(f"\nCommitted and tagged: v{args.version}")

    elif args.command == "list":
        releases = list_releases()
        if not releases:
            print("No certified releases found.")
            return
        print(f"{'Version':<20} {'Status':<10} {'Date':<12} {'Commit'}")
        print("-" * 60)
        for r in releases:
            print(f"{r['version']:<20} {r['status']:<10} {r['date']:<12} {r['commit']}")

    elif args.command == "show":
        print(show_release(args.version))


if __name__ == "__main__":
    main()
