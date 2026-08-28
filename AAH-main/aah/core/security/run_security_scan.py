#!/usr/bin/env python3
"""Main orchestrator for security scanning.

Entry point: aah run core.security.run_security_scan run [options]
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.io_utils import ensure_dir, read_yaml, write_json, write_text
from aah.core.security import parsers, policy, priority, suppressions, tool_check
from aah.core.security import report_format as fmt
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state

# Bundled packs work offline without network access.
SEMGREP_RULES: dict[str, list[str]] = {
    "python": ["--config", "p/python"],
    "javascript": ["--config", "p/javascript"],
    "typescript": ["--config", "p/typescript"],
    "java": ["--config", "p/java"],
    "go": ["--config", "p/golang"],
    "ruby": ["--config", "p/ruby"],
    "csharp": ["--config", "p/csharp"],
    "default": ["--config", "p/default"],
}

ALL_SCAN_TYPES = ["sast", "secrets", "deps", "licenses", "containers", "iac"]

TOOL_TIMEOUT = 600


def _run_tool(cmd: list[str], raw_output: Path, timeout: int = TOOL_TIMEOUT) -> tuple[int, str]:
    """Run a scan tool, writing stdout to raw_output. Returns (returncode, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.stdout:
            raw_output.parent.mkdir(parents=True, exist_ok=True)
            raw_output.write_text(proc.stdout, encoding='utf-8')
        return proc.returncode, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, f"Tool timed out after {timeout}s"
    except OSError as e:
        return -1, str(e)


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


def _detect_language(project_path: Path) -> str:
    """Detect primary language from manifest or file extensions."""
    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path) or {}
        stack = manifest.get("stack_choices", {})
        lang = stack.get("language", stack.get("primary_language", ""))
        if lang:
            return lang.lower()

    extensions = {}
    sampled = 0
    for pattern in ("*", "*/*"):
        for f in project_path.glob(pattern):
            if f.is_file() and f.suffix:
                ext = f.suffix.lower()
                extensions[ext] = extensions.get(ext, 0) + 1
                sampled += 1
                if sampled >= 1000:
                    break
        if sampled >= 1000:
            break

    ext_to_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".rb": "ruby",
        ".cs": "csharp",
    }

    best_lang = "default"
    best_count = 0
    for ext, count in extensions.items():
        lang = ext_to_lang.get(ext)
        if lang and count > best_count:
            best_lang = lang
            best_count = count

    return best_lang


def run_scan(
    project_path: Path,
    scan_types: list[str],
    severity_override: str | None = None,
    project_name: str | None = None,
) -> dict:
    """Run security scans and return normalized results."""
    aah_path = ensure_security_state(project_path, notify=True).security_dir.parent
    raw_dir = ensure_dir(aah_path / "security" / "raw")
    results_dir = ensure_dir(aah_path / "security" / "scan-results")
    reports_dir = ensure_dir(aah_path / "security" / "reports")

    tools = tool_check.check_tools()
    scan_policy = policy.load_policy(aah_path)
    language = _detect_language(project_path)
    timestamp = datetime.now(timezone.utc).isoformat()

    if severity_override:
        for sev in policy.SEVERITY_ORDER:
            scan_policy["severity_thresholds"][sev] = (
                "block" if sev == severity_override
                or policy.SEVERITY_ORDER.index(sev)
                <= policy.SEVERITY_ORDER.index(severity_override)
                else "ignore"
            )

    all_findings: list[dict] = []
    all_errors: list[dict] = []

    # SAST via Semgrep
    if "sast" in scan_types and tools["semgrep"]["available"]:
        rule_config = SEMGREP_RULES.get(language, SEMGREP_RULES["default"])
        raw_file = raw_dir / "semgrep-results.json"
        cmd = ["semgrep", "scan", *rule_config, "--json", str(project_path)]

        custom_rules = aah_path / "security" / "semgrep-rules"
        if custom_rules.is_dir() and any(custom_rules.glob("*.yaml")):
            cmd.extend(["--config", str(custom_rules)])

        returncode, stderr = _run_tool(cmd, raw_file)

        if returncode in (0, 1):
            all_findings.extend(parsers.parse_semgrep_json(raw_file, project_path))
        elif returncode == 2 and "invalid rule" in stderr.lower():
            all_errors.append({
                "tool": "semgrep",
                "error_type": "parse_error",
                "message": "Rule parse error",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path),
            })
        elif returncode != 0:
            all_errors.append({
                "tool": "semgrep",
                "error_type": "crash",
                "message": f"Exited with code {returncode}",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })

    # Secrets via Gitleaks
    if "secrets" in scan_types and tools["gitleaks"]["available"]:
        raw_file = raw_dir / "gitleaks-results.json"
        cmd = [
            "gitleaks", "detect",
            "--source", str(project_path),
            "--report-format", "json",
            "--report-path", str(raw_file),
            "--no-banner",
        ]
        # Gitleaks writes its report via --report-path; use a separate path
        # for stdout capture to avoid overwriting the report file.
        returncode, stderr = _run_tool(cmd, raw_dir / "gitleaks-stdout.json")

        if returncode in (0, 1):
            all_findings.extend(parsers.parse_gitleaks_json(raw_file))
        else:
            all_errors.append({
                "tool": "gitleaks",
                "error_type": "crash",
                "message": f"Exited with code {returncode}",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })

    # Dependency scan via Trivy
    if "deps" in scan_types and tools["trivy"]["available"]:
        raw_file = raw_dir / "trivy-results.json"
        cmd = [
            "trivy", "fs",
            "--exit-code", "1",
            "--format", "json",
            "--output", str(raw_file),
            str(project_path),
        ]
        returncode, stderr = _run_tool(cmd, raw_file)

        if returncode in (0, 1):
            all_findings.extend(parsers.parse_trivy_json(raw_file, "fs"))
        else:
            all_errors.append({
                "tool": "trivy",
                "error_type": "crash",
                "message": f"trivy fs exited with code {returncode}",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })

    # License scan via Trivy
    if "licenses" in scan_types and tools["trivy"]["available"]:
        raw_file = raw_dir / "trivy-license-results.json"
        cmd = [
            "trivy", "fs",
            "--license-full",
            "--exit-code", "1",
            "--format", "json",
            "--output", str(raw_file),
            str(project_path),
        ]
        returncode, stderr = _run_tool(cmd, raw_file)

        license_findings: list[dict] = []
        if returncode in (0, 1):
            license_findings = parsers.parse_trivy_json(raw_file, "license")
        else:
            all_errors.append({
                "tool": "trivy",
                "error_type": "crash",
                "message": f"trivy fs --license-full exited with code {returncode}",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })

        # Trivy can't extract licenses from Python requirements.txt — fall
        # back to importlib.metadata for Python projects.
        if not license_findings and language == "python":
            from aah.core.security.python_licenses import extract_licenses, generate_csv
            license_findings = extract_licenses(project_path)
            generate_csv(project_path, reports_dir / "licenses.csv")

        all_findings.extend(license_findings)

    # Container image scan via Trivy
    if "containers" in scan_types and tools["trivy"]["available"]:
        manifest_path = project_path / ".aah" / "manifest.yaml"
        image_name = ""
        if manifest_path.exists():
            manifest = read_yaml(manifest_path) or {}
            image_name = manifest.get("stack_choices", {}).get("container_image", "")

        if image_name:
            raw_file = raw_dir / "trivy-image-results.json"
            cmd = [
                "trivy", "image",
                "--exit-code", "1",
                "--format", "json",
                "--output", str(raw_file),
                image_name,
            ]
            returncode, stderr = _run_tool(cmd, raw_file)

            if returncode in (0, 1):
                all_findings.extend(parsers.parse_trivy_json(raw_file, "image"))
            else:
                all_errors.append({
                    "tool": "trivy",
                    "error_type": "crash",
                    "message": f"trivy image exited with code {returncode}",
                    "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
                })

    # IaC scan via Trivy
    if "iac" in scan_types and tools["trivy"]["available"]:
        raw_file = raw_dir / "trivy-iac-results.json"
        cmd = [
            "trivy", "config",
            "--exit-code", "1",
            "--format", "json",
            "--output", str(raw_file),
            str(project_path),
        ]
        returncode, stderr = _run_tool(cmd, raw_file)

        if returncode in (0, 1):
            iac_findings = parsers.parse_trivy_json(raw_file, "iac")
            for f in iac_findings:
                f["tool"] = "trivy-iac"
            all_findings.extend(iac_findings)
        else:
            all_errors.append({
                "tool": "trivy-iac",
                "error_type": "crash",
                "message": f"trivy config exited with code {returncode}",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            })

    # If no scans ran (no tools available), record an error so the gate
    # fails with scan_incomplete rather than silently passing.
    if not all_findings and not all_errors:
        available_tools = [
            name for name, info in tools.items() if info["available"]
        ]
        needed = {
            "sast": "semgrep", "secrets": "gitleaks",
            "deps": "trivy", "licenses": "trivy",
            "containers": "trivy", "iac": "trivy",
        }
        missing = [
            needed[st] for st in scan_types
            if st in needed and not tools.get(needed[st], {}).get("available")
        ]
        if missing:
            all_errors.append({
                "tool": "system",
                "error_type": "no_tools_available",
                "message": (
                    f"No scanning tools available for requested types: "
                    f"{', '.join(scan_types)}. "
                    f"Missing: {', '.join(sorted(set(missing)))}"
                ),
                "stderr_tail": "",
            })

    # Apply suppressions
    supp_list = suppressions.load_suppressions(aah_path)
    if supp_list:
        all_findings = suppressions.apply_suppressions(all_findings, supp_list)

    # Evaluate policy
    policy_result = policy.evaluate_policy(all_findings, all_errors, scan_policy)

    # Score and rank findings by priority
    all_findings = priority.score_findings(all_findings)

    # Build summary
    active = [f for f in all_findings if not f.get("suppressed")]
    summary = {
        "total_findings": len(all_findings),
        "critical": sum(1 for f in active if f["severity"] == "critical"),
        "high": sum(1 for f in active if f["severity"] == "high"),
        "medium": sum(1 for f in active if f["severity"] == "medium"),
        "low": sum(1 for f in active if f["severity"] == "low"),
        "suppressed": sum(1 for f in all_findings if f.get("suppressed")),
        "errors": len(all_errors),
    }

    manifest_path = project_path / ".aah" / "manifest.yaml"
    resolved_name = "unknown"
    if manifest_path.exists():
        resolved_name = (read_yaml(manifest_path) or {}).get("project_name", "unknown")
    if project_name:
        resolved_name = project_name

    result = {
        "scan_timestamp": timestamp,
        "project_name": resolved_name,
        "language": language,
        "tools": {
            name: {"available": info["available"], "version": info["version"]}
            for name, info in tools.items()
        },
        "summary": summary,
        "findings": all_findings,
        "errors": all_errors,
        "policy_result": policy_result,
    }

    # Persist results
    latest_path = aah_path / "security" / "scan-results-latest.json"
    write_json(result, latest_path)

    archive_name = timestamp.replace(":", "-").replace("+", "_") + ".json"
    write_json(result, results_dir / archive_name)

    # Generate reports — one per scan type + central summary
    _generate_reports(result, reports_dir)

    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

SCAN_TYPE_TOOLS = {
    "sast": ("semgrep",),
    "secrets": ("gitleaks",),
    "deps": ("trivy",),
    "licenses": ("license-scan",),
    "iac": ("trivy",),
    "deep_sast": ("codeql",),
    "sbom": ("grype",),
}


def _generate_reports(result: dict, reports_dir: Path) -> None:
    """Generate per-scan-type reports and a central summary."""
    # Clear stale reports from previous scans
    for stale in reports_dir.glob("*-report.md"):
        stale.unlink(missing_ok=True)

    findings = result["findings"]
    errors = result["errors"]

    generated = []

    # SAST report
    sast = [f for f in findings if f["tool"] == "semgrep"]
    if sast:
        path = reports_dir / "sast-report.md"
        _write_sast_report(result, sast, path)
        generated.append(("SAST (Semgrep)", path.name, sast))

    # Deep SAST report (CodeQL)
    deep_sast = [f for f in findings if f["tool"] == "codeql"]
    if deep_sast:
        path = reports_dir / "deep-sast-report.md"
        _write_deep_sast_report(result, deep_sast, path)
        generated.append(("Deep SAST (CodeQL)", path.name, deep_sast))

    # Secrets report
    secrets = [f for f in findings if f["tool"] == "gitleaks"]
    if secrets:
        path = reports_dir / "secrets-report.md"
        _write_secrets_report(result, secrets, path)
        generated.append(("Secrets (Gitleaks)", path.name, secrets))

    # Dependency vulnerability report
    deps = [f for f in findings if f["tool"] == "trivy"]
    if deps:
        path = reports_dir / "dependency-report.md"
        _write_deps_report(result, deps, path)
        generated.append(("Dependencies (Trivy)", path.name, deps))

    # SBOM vulnerability report (Grype)
    sbom = [f for f in findings if f["tool"] == "grype"]
    if sbom:
        path = reports_dir / "sbom-report.md"
        _write_sbom_report(result, sbom, path)
        generated.append(("SBOM (Syft + Grype)", path.name, sbom))

    # License report
    licenses = [f for f in findings if f["tool"] == "license-scan"]
    if licenses:
        path = reports_dir / "license-report.md"
        _write_license_report(result, licenses, path)
        generated.append(("Licenses", path.name, licenses))

    # IaC report
    iac = [f for f in findings if f.get("tool") == "trivy-iac"]
    if iac:
        path = reports_dir / "iac-report.md"
        _write_iac_report(result, iac, path)
        generated.append(("IaC (Trivy)", path.name, iac))

    # Central summary linking to all reports
    _write_central_report(result, generated, errors, reports_dir / "security-scan-report.md")


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    return fmt.severity_counts(findings)


def _write_sast_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    groups = fmt.group_findings_by_rule(findings)
    lang = fmt.code_fence_lang(result.get("language", ""))
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# SAST Report (Semgrep)",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Findings:** {len(findings)} ({fmt.severity_counts_str(sc)})",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
    ]

    lines.extend(fmt.executive_summary(findings, groups))

    lines.extend(["## Findings by Rule", ""])

    for g in groups:
        rule = fmt.short_rule(g["rule_id"])
        cwe = g["cwe"]
        owasp = g["owasp"]
        classification = f" ({cwe})" if cwe else ""
        if owasp:
            classification += f" [{owasp}]"

        lines.append(
            f"### {rule} — {g['message'][:80]}{classification} "
            f"{fmt.severity_badge(g['severity'])} — {g['count']} occurrence{'s' if g['count'] != 1 else ''}"
        )
        lines.append("")

        if g["fix_description"]:
            lines.append(f"**Fix:** {g['fix_description']}")
        if g["fix_example"]:
            lines.append(f"```{lang}")
            lines.append(g["fix_example"])
            lines.append("```")
        lines.append("")

        if g["reference_url"]:
            lines.append(f"**Reference:** {g['reference_url']}  ")
        lines.append(f"**Effort:** {fmt.effort_label(g['effort'])}")
        lines.append("")

        lines.extend(fmt.locations_table(g["locations"]))
        lines.append("---")
        lines.append("")

    write_text("\n".join(lines), path)


def _write_secrets_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    groups = fmt.group_findings_by_rule(findings)
    lang = fmt.code_fence_lang(result.get("language", ""))
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# Secrets Detection Report (Gitleaks)",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Secrets Found:** {len(findings)} ({fmt.severity_counts_str(sc)})",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
    ]

    lines.extend(fmt.executive_summary(findings, groups))

    lines.extend(["## Findings by Detection Rule", ""])

    for g in groups:
        lines.append(
            f"### {g['rule_id']} — Hardcoded Credentials ({g['cwe'] or 'CWE-798'}) "
            f"{fmt.severity_badge(g['severity'])} — {g['count']} occurrence{'s' if g['count'] != 1 else ''}"
        )
        lines.append("")

        lines.append(f"**Description:** {g['message'][:120]}")
        lines.append("")

        lines.extend([
            "**Remediation steps:**",
            "1. Remove the hardcoded secret from source code",
            "2. Add the variable to `.env` (gitignored) or a secrets manager",
            "3. Update code to read from `os.environ`",
            "4. Rotate the exposed credential immediately",
            "5. Audit access logs for unauthorized use during exposure window",
            "",
        ])

        if g["fix_example"]:
            lines.append("**Fix example:**")
            lines.append(f"```{lang}")
            lines.append(g["fix_example"])
            lines.append("```")
            lines.append("")

        if g["reference_url"]:
            lines.append(f"**Reference:** {g['reference_url']}  ")
        lines.append(f"**Effort:** {fmt.effort_label(g['effort'])}")
        lines.append("")

        lines.extend(fmt.locations_table(g["locations"]))
        lines.append("---")
        lines.append("")

    write_text("\n".join(lines), path)


def _write_deps_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# Dependency Vulnerability Report (Trivy)",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Vulnerabilities:** {len(findings)} ({fmt.severity_counts_str(sc)})",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
    ]

    groups = fmt.group_findings_by_rule(findings)
    lines.extend(fmt.executive_summary(findings, groups))

    for sev in fmt.SEVERITY_ORDER:
        sev_findings = [f for f in findings if f["severity"] == sev and not f.get("suppressed")]
        if not sev_findings:
            continue

        lines.append(f"## {fmt.severity_badge(sev)} ({len(sev_findings)} finding{'s' if len(sev_findings) != 1 else ''})")
        lines.append("")
        lines.append("| CVE / Rule | Package | Description | Fix | Effort |")
        lines.append("|------------|---------|-------------|-----|--------|")
        for f in sev_findings:
            rule = fmt.short_rule(f["rule_id"])
            pkg = f"`{f.get('code_snippet', f['file'])}`"
            msg = f["message"][:60].replace("|", "\\|")
            fix = f["fix_description"][:40].replace("|", "\\|") if f.get("fix_description") else "—"
            effort = fmt.effort_label(f.get("effort", ""))
            lines.append(f"| {rule} | {pkg} | {msg} | {fix} | {effort} |")
        lines.append("")

    write_text("\n".join(lines), path)


def _write_license_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    direct = [f for f in findings if "[direct]" in f["message"]]
    transitive = [f for f in findings if "[transitive]" in f["message"]]
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# License Compliance Report",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Packages Scanned:** {len(findings)} ({len(direct)} direct, {len(transitive)} transitive)  ",
        f"**Risk Summary:** {fmt.severity_counts_str(sc)}",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
        "## Risk Assessment (closed-source context)",
        "",
        "| Risk | Meaning | Action Required |",
        "|------|---------|-----------------|",
        f"| {fmt.severity_badge('critical')} | Strong copyleft (GPL/AGPL) | **Must remove** — distributing requires source disclosure |",
        f"| {fmt.severity_badge('high')} | Unknown or weak copyleft (LGPL) | **Review immediately** — may impose disclosure obligations |",
        f"| {fmt.severity_badge('medium')} | File-level copyleft (MPL/EPL) | **Acceptable with care** — don't modify the dependency's source |",
        f"| {fmt.severity_badge('low')} | Permissive (MIT/BSD/Apache) | **Safe** — maintain attribution in NOTICE file |",
        "",
    ]

    action_items = [f for f in findings if f["severity"] in ("critical", "high", "medium")]
    if action_items:
        lines.extend(["## Action Items", ""])
        for f in action_items:
            badge = fmt.severity_badge(f["severity"])
            tail = f["message"].split("—")[-1].strip() if "—" in f["message"] else ""
            lines.append(f"- {badge} **{f['file']}**: {f['rule_id']} — {tail}")
        lines.append("")

    lines.extend(["## Direct Dependencies", ""])
    lines.append("| Package | License (SPDX) | Risk |")
    lines.append("|---------|---------------|------|")
    for f in direct:
        lines.append(f"| {f['file']} | {f['rule_id']} | {fmt.severity_badge(f['severity'])} |")

    lines.extend(["", "## Transitive Dependencies", ""])
    lines.append("| Package | License (SPDX) | Risk |")
    lines.append("|---------|---------------|------|")
    for f in sorted(transitive, key=lambda x: fmt.SEVERITY_ORDER.index(x["severity"])):
        lines.append(f"| {f['file']} | {f['rule_id']} | {fmt.severity_badge(f['severity'])} |")

    lines.extend(["", "*CSV export: licenses.csv*", ""])
    write_text("\n".join(lines), path)


def _write_deep_sast_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    groups = fmt.group_findings_by_rule(findings)
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# Deep SAST Report (CodeQL)",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Findings:** {len(findings)} ({fmt.severity_counts_str(sc)})",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
    ]

    lines.extend(fmt.executive_summary(findings, groups))
    lines.extend(["## Findings by Rule", ""])

    for g in groups:
        rule = fmt.short_rule(g["rule_id"])
        cwe = g["cwe"]
        classification = f" ({cwe})" if cwe else ""

        lines.append(
            f"### {rule}{classification} "
            f"{fmt.severity_badge(g['severity'])} — {g['count']} occurrence{'s' if g['count'] != 1 else ''}"
        )
        lines.append("")
        lines.append(f"**Description:** {g['message'][:200]}")
        lines.append("")
        lines.extend(fmt.locations_table(g["locations"]))
        lines.append("---")
        lines.append("")

    write_text("\n".join(lines), path)


def _write_sbom_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    groups = fmt.group_findings_by_rule(findings)
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# SBOM Vulnerability Report (Syft + Grype)",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Vulnerabilities:** {len(findings)} ({fmt.severity_counts_str(sc)})",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
    ]

    lines.extend(fmt.executive_summary(findings, groups))
    lines.extend(["## Vulnerabilities by CVE", ""])

    for g in groups:
        rule = fmt.short_rule(g["rule_id"])
        lines.append(
            f"### {rule} "
            f"{fmt.severity_badge(g['severity'])} — {g['count']} occurrence{'s' if g['count'] != 1 else ''}"
        )
        lines.append("")
        lines.append(f"**Description:** {g['message'][:200]}")
        lines.append("")
        lines.extend(fmt.locations_table(g["locations"], "affected packages"))
        lines.append("---")
        lines.append("")

    write_text("\n".join(lines), path)


def _write_iac_report(result: dict, findings: list[dict], path: Path) -> None:
    sc = fmt.severity_counts(findings)
    groups = fmt.group_findings_by_rule(findings)
    passed = not (sc["critical"] or sc["high"])

    lines = [
        "# Infrastructure as Code Report (Trivy)",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Findings:** {len(findings)} ({fmt.severity_counts_str(sc)})",
        "",
        fmt.severity_bar(sc["critical"], sc["high"], sc["medium"], sc["low"]),
        "",
    ]

    lines.extend(fmt.executive_summary(findings, groups))
    lines.extend(["## Findings by Rule", ""])

    for g in groups:
        rule = fmt.short_rule(g["rule_id"])
        lines.append(
            f"### {rule} — {g['message'][:80]} "
            f"{fmt.severity_badge(g['severity'])} — {g['count']} occurrence{'s' if g['count'] != 1 else ''}"
        )
        lines.append("")

        if g["fix_description"]:
            lines.append(f"**Fix:** {g['fix_description']}")
            lines.append("")
        if g["reference_url"]:
            lines.append(f"**Reference:** {g['reference_url']}  ")
        lines.append(f"**Effort:** {fmt.effort_label(g['effort'])}")
        lines.append("")
        lines.extend(fmt.locations_table(g["locations"]))
        lines.append("---")
        lines.append("")

    write_text("\n".join(lines), path)


def _write_central_report(
    result: dict,
    generated: list[tuple[str, str, list[dict]]],
    errors: list[dict],
    path: Path,
) -> None:
    s = result["summary"]
    pr = result["policy_result"]
    passed = pr["passed"]

    lines = [
        "# Security Scan Report",
        "",
        fmt.status_banner(passed),
        "",
        f"**Project:** {result['project_name']}  ",
        f"**Scan Time:** {result['scan_timestamp']}  ",
        f"**Gate Result:** {'PASSED' if passed else 'FAILED'} ({pr['reason']})",
        "",
        "## Risk Overview",
        "",
        fmt.severity_bar(s["critical"], s["high"], s["medium"], s["low"]),
        "",
        "| Metric | Count |",
        "|--------|------:|",
        f"| {fmt.severity_badge('critical')} | {s['critical']} |",
        f"| {fmt.severity_badge('high')} | {s['high']} |",
        f"| {fmt.severity_badge('medium')} | {s['medium']} |",
        f"| {fmt.severity_badge('low')} | {s['low']} |",
        f"| **Total** | **{s['total_findings']}** |",
        f"| Suppressed | {s['suppressed']} |",
        f"| Errors | {s['errors']} |",
        "",
    ]

    pie_segments = {}
    for sev in fmt.SEVERITY_ORDER:
        if s[sev]:
            pie_segments[sev.title()] = s[sev]
    lines.extend(fmt.mermaid_pie("Findings by Severity", pie_segments))

    ranked = sorted(
        [f for f in result["findings"] if not f.get("suppressed") and f.get("priority_rank")],
        key=lambda f: f.get("priority_rank", 999),
    )
    if ranked:
        top = ranked[:10]
        lines.extend([
            "## Fix These First",
            "",
            "| Rank | Score | Severity | File | Finding | Effort |",
            "|-----:|------:|----------|------|---------|--------|",
        ])
        for f in top:
            cwe = f.get("cwe", "")
            rule = fmt.short_rule(f["rule_id"])
            desc = f"{rule} ({cwe})" if cwe else rule
            file_loc = f"`{f['file']}:{f['line']}`" if f.get("line") else f"`{f['file']}`"
            lines.append(
                f"| {f.get('priority_rank', '-')} "
                f"| {f.get('priority_score', '-')} "
                f"| {fmt.severity_badge(f['severity'])} "
                f"| {file_loc} "
                f"| {desc} "
                f"| {fmt.effort_label(f.get('effort', ''))} |"
            )
        lines.append("")

    lines.extend([
        "## Detailed Reports",
        "",
        "| Scan Type | Status | Findings | Report |",
        "|-----------|--------|----------|--------|",
    ])
    for label, filename, findings in generated:
        sc = fmt.severity_counts(findings)
        badge = fmt.scan_status_badge(findings)
        count_str = f"{len(findings)} ({sc['critical']}C/{sc['high']}H/{sc['medium']}M/{sc['low']}L)"
        lines.append(f"| {label} | {badge} | {count_str} | [{filename}]({filename}) |")

    lines.extend([
        "",
        "## Tools",
        "",
        "| Tool | Available | Version |",
        "|------|-----------|---------|",
    ])
    for name, info in result["tools"].items():
        status = "Yes" if info["available"] else "—"
        lines.append(f"| {name} | {status} | {info['version']} |")

    blocking = [
        f for f in result["findings"]
        if not f.get("suppressed") and f["severity"] in ("critical", "high")
    ]
    if blocking:
        blocking_groups = fmt.group_findings_by_rule(blocking)
        lines.extend(["", "## Blocking Findings (Critical/High)", ""])
        lines.append("| Rule | Severity | Tool | Occurrences | Top File |")
        lines.append("|------|----------|------|------------:|----------|")
        for g in blocking_groups:
            rule = fmt.short_rule(g["rule_id"])
            top_file = g["locations"][0]["file"] if g["locations"] else "—"
            lines.append(
                f"| {rule} | {fmt.severity_badge(g['severity'])} "
                f"| {g['tool'] or '—'} "
                f"| {g['count']} | `{top_file}` |"
            )

    if errors:
        lines.extend(["", "## Errors", ""])
        for err in errors:
            lines.append(f"- **{err['tool']}** ({err['error_type']}): {err['message']}")

    lines.append("")
    write_text("\n".join(lines), path)


def _merge_extended_results(
    result: dict,
    extra_findings: list[dict],
    extra_errors: list[dict],
    project_path: Path,
    scan_policy: dict,
) -> None:
    """Merge additional scan findings into the main result and re-evaluate.

    Mutates ``result`` in place: extends findings/errors, applies suppressions,
    re-scores priority, re-evaluates policy, rebuilds summary, persists results,
    and regenerates reports.
    """
    result["findings"].extend(extra_findings)
    result["errors"].extend(extra_errors)

    aah_path = ensure_security_state(project_path).security_dir.parent

    supp_list = suppressions.load_suppressions(aah_path)
    if supp_list:
        result["findings"] = suppressions.apply_suppressions(
            result["findings"], supp_list,
        )

    result["findings"] = priority.score_findings(result["findings"])

    result["policy_result"] = policy.evaluate_policy(
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

    write_json(result, aah_path / "security" / "scan-results-latest.json")
    _generate_reports(result, aah_path / "security" / "reports")


def _resolve_scan_types(requested: str, scan_policy: dict) -> list[str]:
    """Resolve requested scan types against policy configuration."""
    if requested == "all":
        enabled = scan_policy.get("scan_types", {})
        return [st for st in ALL_SCAN_TYPES if enabled.get(st, False)]

    types = [t.strip() for t in requested.split(",")]
    return [t for t in types if t in ALL_SCAN_TYPES]


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Security Scanner")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run security scans")
    run_parser.add_argument(
        "--scan-types",
        default="all",
        help="Comma-separated scan types or 'all' (default: all)",
    )
    run_parser.add_argument(
        "--severity",
        choices=["critical", "high", "medium", "low"],
        help="Override minimum blocking severity",
    )
    run_parser.add_argument(
        "--project-path",
        help="Explicit project path (default: auto-resolve)",
    )
    run_parser.add_argument(
        "--phase2",
        action="store_true",
        help="Include Phase 2 scans (CodeQL deep SAST + SBOM generation)",
    )
    run_parser.add_argument(
        "--dast",
        action="store_true",
        help="Run DAST scans (ZAP + Nuclei) — requires target URL in policy.yaml",
    )
    run_parser.add_argument(
        "--self",
        action="store_true",
        help="Scan the AAH framework itself (not a project)",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    is_self_scan = getattr(args, "self", False)
    if is_self_scan:
        project_path = Path(__file__).resolve().parents[3]
    else:
        explicit = Path(args.project_path) if args.project_path else None
        project_path = resolve_project_path(explicit)
    if project_path is None:
        print("Error: could not resolve project path", file=sys.stderr)
        sys.exit(1)

    try:
        aah_path = ensure_security_state(project_path, notify=True).security_dir.parent
    except SecurityStateMigrationError as exc:
        print(f"Error: security state migration failed: {exc}", file=sys.stderr)
        sys.exit(2)
    scan_policy = policy.load_policy(aah_path)
    scan_types = _resolve_scan_types(args.scan_types, scan_policy)

    if not scan_types:
        print("No scan types enabled. Check policy.yaml scan_types.", file=sys.stderr)
        sys.exit(1)

    print(f"Running security scan: {', '.join(scan_types)}")
    print(f"Project: {project_path}")
    print()

    name_override = "AAH Framework" if is_self_scan else None
    result = run_scan(project_path, scan_types, args.severity, project_name=name_override)

    # Phase 2 and DAST extensions: collect all extra findings first,
    # then merge once to avoid redundant re-computation.
    extra_findings: list[dict] = []
    extra_errors: list[dict] = []

    if getattr(args, "phase2", False):
        from aah.core.security.run_deep_sast import run_deep_sast
        from aah.core.security.run_sbom_scan import run_sbom

        print("\n--- Phase 2: Deep SAST (CodeQL) ---")
        deep_result = run_deep_sast(project_path)
        extra_findings.extend(deep_result["findings"])
        extra_errors.extend(deep_result["errors"])

        print("--- Phase 2: SBOM (Syft + Grype) ---")
        sbom_result = run_sbom(project_path, scan_policy.get("sbom", {}).get("format", "cyclonedx"))
        extra_findings.extend(sbom_result["findings"])
        extra_errors.extend(sbom_result["errors"])

    if getattr(args, "dast", False):
        from aah.core.security.run_dast_scan import run_dast
        print("\n--- DAST Scan (ZAP + Nuclei) ---")
        dast_result = run_dast(project_path)
        extra_findings.extend(dast_result["findings"])
        extra_errors.extend(dast_result["errors"])
        print(f"DAST: {len(dast_result['findings'])} findings, {len(dast_result['errors'])} errors")

    if extra_findings or extra_errors:
        _merge_extended_results(result, extra_findings, extra_errors, project_path, scan_policy)

    s = result["summary"]
    pr = result["policy_result"]

    print(f"\nScan complete: {s['total_findings']} findings "
          f"(C:{s['critical']} H:{s['high']} M:{s['medium']} L:{s['low']})")
    if s["suppressed"]:
        print(f"  {s['suppressed']} findings suppressed")
    if s["errors"]:
        print(f"  {s['errors']} scan errors")
    print(f"\nGate: {'PASSED' if pr['passed'] else 'FAILED'} ({pr['reason']})")

    if not pr["passed"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
