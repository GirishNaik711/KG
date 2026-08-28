#!/usr/bin/env python3
"""Normalize raw scan tool output into the unified finding schema."""

import json
import sys
from pathlib import Path

from aah.core.security.remediation import get_remediation


SEMGREP_SEVERITY_MAP = {
    "ERROR": "high",
    "WARNING": "medium",
    "INFO": "low",
}

TRIVY_SEVERITY_MAP = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
    "UNKNOWN": "low",
}

GITLEAKS_SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}

GITLEAKS_RULE_SEVERITY: dict[str, str] = {
    "private-key": "critical",
    "aws-access-key-id": "critical",
    "aws-secret-access-key": "critical",
    "gcp-service-account": "critical",
    "github-pat": "critical",
    "github-fine-grained-pat": "critical",
    "gitlab-pat": "critical",
    "slack-bot-token": "critical",
    "stripe-secret-key": "critical",
    "twilio-api-key": "critical",
    "sendgrid-api-key": "critical",
    "heroku-api-key": "critical",
    "shopify-access-token": "critical",
    "generic-api-key": "high",
    "jwt": "high",
    "slack-webhook-url": "high",
    "entropy": "medium",
    "password-in-url": "medium",
}


def _safe_parse(raw_path: Path) -> dict | list | None:
    """Read JSON from a file, returning None on parse errors."""
    if not raw_path.exists():
        return None
    try:
        with open(raw_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: failed to parse {raw_path}: {e}", file=sys.stderr)
        return None


def _read_source_line(file_path: str, line: int) -> str:
    """Read a specific line from a source file for code snippet."""
    try:
        p = Path(file_path)
        if not p.exists() or line < 1:
            return ""
        lines = p.read_text(encoding='utf-8').splitlines()
        if line <= len(lines):
            return lines[line - 1].strip()
    except OSError:
        pass
    return ""


def parse_semgrep_json(raw_path: Path, project_path: Path | None = None) -> list[dict]:
    """Parse Semgrep JSON output into normalized findings."""
    data = _safe_parse(raw_path)
    if data is None:
        return []

    findings = []
    results = data.get("results", []) if isinstance(data, dict) else []

    for i, result in enumerate(results):
        severity_raw = result.get("extra", {}).get("severity", "WARNING")
        severity = SEMGREP_SEVERITY_MAP.get(severity_raw, "medium")

        file_path = result.get("path", "")
        abs_path = file_path
        if project_path:
            try:
                file_path = str(Path(file_path).resolve().relative_to(project_path.resolve()))
            except ValueError:
                pass

        line = result.get("start", {}).get("line", 0)

        metadata = result.get("extra", {}).get("metadata", {})
        cwe_list = metadata.get("cwe", [])
        cwe = cwe_list[0] if isinstance(cwe_list, list) and cwe_list else ""
        if isinstance(cwe, str) and ": " in cwe:
            cwe = cwe.split(": ")[0]

        owasp_list = metadata.get("owasp", [])
        owasp = owasp_list[0] if isinstance(owasp_list, list) and owasp_list else ""

        code_snippet = _read_source_line(abs_path, line)

        rule_id = result.get("check_id", "")
        remediation = get_remediation(cwe, rule_id)

        findings.append({
            "id": f"semgrep-{i + 1:04d}",
            "tool": "semgrep",
            "rule_id": rule_id,
            "severity": severity,
            "cwe": cwe,
            "owasp": owasp,
            "file": file_path,
            "line": line,
            "col_start": result.get("start", {}).get("col", 0),
            "col_end": result.get("end", {}).get("col", 0),
            "message": result.get("extra", {}).get("message", ""),
            "code_snippet": code_snippet,
            "fix_description": remediation["fix"],
            "fix_example": remediation["after"],
            "reference_url": remediation["reference"],
            "effort": remediation["effort"],
            "suppressed": False,
        })

    return findings


def parse_trivy_json(raw_path: Path, scan_type: str = "fs") -> list[dict]:
    """Parse Trivy JSON output into normalized findings."""
    data = _safe_parse(raw_path)
    if data is None:
        return []

    findings = []
    counter = 0

    results_list = data.get("Results", []) if isinstance(data, dict) else []

    for result in results_list:
        target = result.get("Target", "")

        # Skip vulnerability parsing for license-only scans to avoid
        # duplicating findings from the separate deps scan.
        vulnerabilities = (result.get("Vulnerabilities") or []) if scan_type != "license" else []

        for vuln in vulnerabilities:
            counter += 1
            severity = TRIVY_SEVERITY_MAP.get(
                vuln.get("Severity", "UNKNOWN"), "low"
            )

            cwe_ids = vuln.get("CweIDs") or []
            cwe = cwe_ids[0] if cwe_ids else ""
            primary_url = vuln.get("PrimaryURL", "")
            published = vuln.get("PublishedDate", "")

            pkg_name = vuln.get("PkgName", "")
            installed = vuln.get("InstalledVersion", "")
            fixed = vuln.get("FixedVersion", "no fix")

            remediation = get_remediation(cwe)

            findings.append({
                "id": f"trivy-{scan_type}-{counter:04d}",
                "tool": "trivy",
                "rule_id": vuln.get("VulnerabilityID", ""),
                "severity": severity,
                "cwe": cwe,
                "owasp": "",
                "file": target,
                "line": 0,
                "col_start": 0,
                "col_end": 0,
                "message": (
                    f"{pkg_name} {installed} -> {fixed}"
                    f": {vuln.get('Title', vuln.get('Description', ''))[:200]}"
                ),
                "code_snippet": f"{pkg_name}=={installed}",
                "fix_description": f"Upgrade {pkg_name} to {fixed}" if fixed != "no fix" else f"No fix available yet — monitor {primary_url}",
                "fix_example": f"{pkg_name}=={fixed}" if fixed != "no fix" else "",
                "reference_url": primary_url,
                "published_date": published[:10] if published else "",
                "effort": "small" if fixed != "no fix" else "medium",
                "suppressed": False,
            })

        misconfigs = result.get("Misconfigurations") or []
        for misconf in misconfigs:
            counter += 1
            severity = TRIVY_SEVERITY_MAP.get(
                misconf.get("Severity", "UNKNOWN"), "low"
            )
            remediation = get_remediation(misconf.get("ID", ""))
            findings.append({
                "id": f"trivy-{scan_type}-{counter:04d}",
                "tool": "trivy",
                "rule_id": misconf.get("ID", ""),
                "severity": severity,
                "cwe": "",
                "owasp": "",
                "file": target,
                "line": 0,
                "col_start": 0,
                "col_end": 0,
                "message": (
                    f"{misconf.get('Title', '')}: {misconf.get('Message', '')}"
                ),
                "code_snippet": "",
                "fix_description": misconf.get("Resolution", remediation["fix"]),
                "fix_example": "",
                "reference_url": misconf.get("PrimaryURL", remediation["reference"]),
                "effort": remediation["effort"],
                "suppressed": False,
            })

        licenses = result.get("Licenses") or []
        for lic in licenses:
            counter += 1
            severity = TRIVY_SEVERITY_MAP.get(
                lic.get("Severity", "UNKNOWN"), "low"
            )
            findings.append({
                "id": f"trivy-{scan_type}-{counter:04d}",
                "tool": "trivy",
                "rule_id": lic.get("Name", ""),
                "severity": severity,
                "cwe": "",
                "owasp": "",
                "file": lic.get("PkgName", target),
                "line": 0,
                "col_start": 0,
                "col_end": 0,
                "message": (
                    f"License: {lic.get('Name', 'unknown')}"
                    f" ({lic.get('Category', 'unknown')} category)"
                    f" in {lic.get('PkgName', '?')}"
                ),
                "code_snippet": "",
                "fix_description": "",
                "fix_example": "",
                "reference_url": "",
                "effort": "",
                "suppressed": False,
            })

    return findings


def parse_gitleaks_json(raw_path: Path) -> list[dict]:
    """Parse Gitleaks JSON output into normalized findings."""
    data = _safe_parse(raw_path)
    if data is None:
        return []

    if not isinstance(data, list):
        return []

    findings = []
    for i, leak in enumerate(data):
        rule_id = leak.get("RuleID", "")
        severity = GITLEAKS_RULE_SEVERITY.get(rule_id, "high")
        remediation = get_remediation("CWE-798")
        file_path = leak.get("File", "")
        line = leak.get("StartLine", 0)
        code_snippet = _read_source_line(file_path, line)

        findings.append({
            "id": f"gitleaks-{i + 1:04d}",
            "tool": "gitleaks",
            "rule_id": rule_id,
            "severity": severity,
            "cwe": "CWE-798",
            "owasp": "A07:2021",
            "file": file_path,
            "line": line,
            "col_start": 0,
            "col_end": 0,
            "message": (
                f"Secret detected: {leak.get('Description', leak.get('RuleID', ''))}"
                f" (match: {leak.get('Match', '')[:80]})"
            ),
            "code_snippet": code_snippet,
            "fix_description": remediation["fix"],
            "fix_example": remediation["after"],
            "reference_url": remediation["reference"],
            "effort": remediation["effort"],
            "suppressed": False,
        })

    return findings
