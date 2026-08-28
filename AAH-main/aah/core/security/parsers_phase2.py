#!/usr/bin/env python3
"""Phase 2 parsers: CodeQL SARIF, ZAP JSON, Nuclei JSONL, Grype JSON."""

import json
from pathlib import Path

from aah.core.security.parsers import _safe_parse


CODEQL_SEVERITY_MAP = {
    "error": "high",
    "warning": "medium",
    "note": "low",
    "none": "low",
}

ZAP_RISK_MAP = {
    3: "high",
    2: "medium",
    1: "low",
    0: "low",
}

NUCLEI_SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "low",
    "unknown": "low",
}

GRYPE_SEVERITY_MAP = {
    "Critical": "critical",
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "Negligible": "low",
    "Unknown": "low",
}


def parse_codeql_sarif(raw_path: Path, project_path: Path | None = None) -> list[dict]:
    """Parse CodeQL SARIF v2.1.0 output into normalized findings."""
    data = _safe_parse(raw_path)
    if data is None:
        return []

    findings = []
    counter = 0

    for run in data.get("runs", []):
        results = run.get("results", [])
        for result in results:
            counter += 1
            level = result.get("level", "warning")
            severity = CODEQL_SEVERITY_MAP.get(level, "medium")

            rule_id = result.get("ruleId", "")
            message = result.get("message", {}).get("text", "")

            file_path = ""
            line = 0
            locations = result.get("locations", [])
            if locations:
                phys = locations[0].get("physicalLocation", {})
                artifact = phys.get("artifactLocation", {})
                file_path = artifact.get("uri", "")
                region = phys.get("region", {})
                line = region.get("startLine", 0)

            if project_path:
                try:
                    file_path = str(Path(file_path).resolve().relative_to(project_path.resolve()))
                except ValueError:
                    pass

            cwe = ""
            rule_props = result.get("properties", {})
            tags = rule_props.get("tags", [])
            for tag in tags:
                if tag.startswith("cwe-") or tag.startswith("CWE-"):
                    cwe = tag.upper()
                    break

            findings.append({
                "id": f"codeql-{counter:04d}",
                "tool": "codeql",
                "rule_id": rule_id,
                "severity": severity,
                "cwe": cwe,
                "file": file_path,
                "line": line,
                "message": message[:300],
                "suppressed": False,
            })

    return findings


def parse_zap_json(raw_path: Path) -> list[dict]:
    """Parse OWASP ZAP JSON report into normalized findings."""
    data = _safe_parse(raw_path)
    if data is None:
        return []

    findings = []
    counter = 0

    sites = data.get("site", [])
    if isinstance(sites, dict):
        sites = [sites]

    for site in sites:
        alerts = site.get("alerts", [])
        for alert in alerts:
            counter += 1
            risk = int(alert.get("riskcode", 1))
            severity = ZAP_RISK_MAP.get(risk, "medium")

            cwe_id = alert.get("cweid", "")
            cwe = f"CWE-{cwe_id}" if cwe_id and cwe_id != "-1" else ""

            instances = alert.get("instances", [])
            url = instances[0].get("uri", "") if instances else ""

            findings.append({
                "id": f"zap-{counter:04d}",
                "tool": "zap",
                "rule_id": alert.get("alertRef", alert.get("pluginid", "")),
                "severity": severity,
                "cwe": cwe,
                "file": url,
                "line": 0,
                "message": (
                    f"{alert.get('name', '')}: {alert.get('desc', '')[:200]}"
                ),
                "suppressed": False,
            })

    return findings


def parse_nuclei_json(raw_path: Path) -> list[dict]:
    """Parse Nuclei JSONL output into normalized findings.

    Nuclei outputs one JSON object per line (JSONL format).
    """
    if not raw_path.exists():
        return []

    findings = []
    counter = 0

    try:
        with open(raw_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue

                counter += 1
                info = result.get("info", {})
                severity_raw = info.get("severity", "unknown").lower()
                severity = NUCLEI_SEVERITY_MAP.get(severity_raw, "low")

                cwe = ""
                classification = info.get("classification", {})
                cwe_list = classification.get("cwe-id", [])
                if cwe_list:
                    cwe = str(cwe_list[0])
                    if not cwe.startswith("CWE-"):
                        cwe = f"CWE-{cwe}"

                findings.append({
                    "id": f"nuclei-{counter:04d}",
                    "tool": "nuclei",
                    "rule_id": result.get("template-id", result.get("templateID", "")),
                    "severity": severity,
                    "cwe": cwe,
                    "file": result.get("matched-at", result.get("matched", "")),
                    "line": 0,
                    "message": (
                        f"{info.get('name', '')}: "
                        f"{info.get('description', result.get('matcher-name', ''))[:200]}"
                    ),
                    "suppressed": False,
                })
    except OSError:
        return []

    return findings


def parse_grype_json(raw_path: Path) -> list[dict]:
    """Parse Grype JSON output into normalized findings."""
    data = _safe_parse(raw_path)
    if data is None:
        return []

    findings = []
    counter = 0

    matches = data.get("matches", [])
    for match in matches:
        counter += 1
        vuln = match.get("vulnerability", {})
        artifact = match.get("artifact", {})

        severity_raw = vuln.get("severity", "Unknown")
        severity = GRYPE_SEVERITY_MAP.get(severity_raw, "low")

        fix_versions = vuln.get("fix", {}).get("versions", [])
        fix_str = fix_versions[0] if fix_versions else "no fix"

        findings.append({
            "id": f"grype-{counter:04d}",
            "tool": "grype",
            "rule_id": vuln.get("id", ""),
            "severity": severity,
            "cwe": "",
            "file": f"{artifact.get('name', '')}@{artifact.get('version', '')}",
            "line": 0,
            "message": (
                f"{artifact.get('name', '')} {artifact.get('version', '')}"
                f" -> {fix_str}"
                f": {vuln.get('description', vuln.get('id', ''))[:200]}"
            ),
            "suppressed": False,
        })

    return findings
