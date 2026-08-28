#!/usr/bin/env python3
"""Pre-check library licenses and vulnerabilities before installation.

Queries PyPI JSON API and OSV.dev to evaluate packages BEFORE they enter
requirements.txt. Produces findings in the same unified schema as existing
security scans.

Entry point: aah run core.security.pre_check check [options]
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from aah.core.security.python_licenses import (
    LICENSE_RISK,
    RISK_EXPLANATION,
    _normalize_license,
)

PYPI_API = "https://pypi.org/pypi/{package}/json"
PYPI_VERSION_API = "https://pypi.org/pypi/{package}/{version}/json"
OSV_API = "https://api.osv.dev/v1/query"
REQUEST_TIMEOUT = 15

CVSS_SEVERITY = [
    (9.0, "critical"),
    (7.0, "high"),
    (4.0, "medium"),
    (0.0, "low"),
]

SECURITY_PATTERNS: dict[str, dict] = {
    "sql-direct-access": {
        "indicators": [r"\braw sql\b", r"\bcursor\.execute\b", r"\bsqlalchemy\.text\(", r"\bf[\"']SELECT\b"],
        "controls": [r"\bparameterized quer", r"\bprepared statement", r"\bORM\b", r"\bsqlalchemy session\b"],
        "cwe": "CWE-89",
        "risk": "SQL injection — direct query construction without parameterized queries",
    },
    "jwt-no-validation": {
        "indicators": [r"\bjwt\.decode\b", r"\bJWT\b.*\btoken\b"],
        "controls": [r"\bverif.*signature\b", r"\balgorithm.*(?:RS256|HS256|ES256)\b", r"\bvalidat.*token\b"],
        "cwe": "CWE-345",
        "risk": "Authentication bypass — JWT handling without signature validation requirement",
    },
    "file-upload": {
        "indicators": [r"\bfile upload\b", r"\bmultipart\b.*\bupload\b", r"\bsave.*file\b"],
        "controls": [r"\bvalidat.*file type\b", r"\bsanitiz\b", r"\bfile.*size.*limit\b", r"\ballowlist\b"],
        "cwe": "CWE-434",
        "risk": "Arbitrary file upload — file handling without sanitization controls",
    },
    "cors-wildcard": {
        "indicators": [r"\bcors\b.*\*", r"allow.origins.*\*", r"Access-Control-Allow-Origin.*\*"],
        "controls": [r"\bspecific.*origin\b", r"\btrusted.*origin\b", r"\bwhitelist\b"],
        "cwe": "CWE-942",
        "risk": "Cross-origin data leakage — CORS wildcard without origin restriction",
    },
    "no-rate-limiting": {
        "indicators": [r"\bpublic.*(?:api|endpoint)\b", r"\blogin.*endpoint\b", r"\bauth.*endpoint\b"],
        "controls": [r"\brate.*limit\b", r"\bthrottl\b", r"\brequests? per (?:second|minute)\b"],
        "cwe": "CWE-770",
        "severity": "medium",
        "risk": "Denial of service — public endpoint without rate limiting",
    },
}


# ---------------------------------------------------------------------------
# PyPI API
# ---------------------------------------------------------------------------

def _fetch_json(url: str, data: bytes | None = None, method: str = "GET") -> dict | None:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "rapids-pre-check/1.0")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None


def _fetch_pypi_metadata(package: str, version: str | None = None) -> dict | None:
    if version:
        url = PYPI_VERSION_API.format(package=package, version=version)
    else:
        url = PYPI_API.format(package=package)
    return _fetch_json(url)


def _extract_license_from_pypi(info: dict) -> str:
    license_expr = info.get("license_expression") or ""
    if license_expr and license_expr != "UNKNOWN":
        return license_expr

    classifiers = info.get("classifiers") or []
    for c in classifiers:
        if c.startswith("License"):
            parts = c.split(" :: ")
            if len(parts) >= 3:
                return parts[-1]

    return info.get("license") or "UNKNOWN"


def _fetch_osv_vulnerabilities(package: str, version: str) -> list[dict]:
    payload = json.dumps({
        "package": {"name": package, "ecosystem": "PyPI"},
        "version": version,
    }).encode()
    data = _fetch_json(OSV_API, data=payload, method="POST")
    if not data:
        return []
    return data.get("vulns", [])


def _osv_severity(vuln: dict) -> str:
    for sev_entry in vuln.get("severity", []):
        # OSV provides CVSS score as a vector string; extract numeric base score
        score_str = sev_entry.get("score", "")
        score = None
        # Try direct numeric score first (some entries use this)
        try:
            score = float(score_str)
        except (ValueError, TypeError):
            pass
        # Parse CVSS vector: extract base score after "CVSS:X.X/"
        if score is None and "CVSS" in score_str:
            base_match = re.search(r"CVSS:\d+\.\d+/", score_str)
            if base_match:
                # CVSS vector doesn't embed the score — use the type field
                sev_type = sev_entry.get("type", "").upper()
                if sev_type == "CVSS_V3":
                    # Map from vector AV/AC/PR/UI metrics is complex;
                    # fall through to database_specific
                    pass
        if score is not None:
            for threshold, level in CVSS_SEVERITY:
                if score >= threshold:
                    return level
    db_severity = vuln.get("database_specific", {}).get("severity", "").lower()
    if db_severity in ("critical", "high", "medium", "low"):
        return db_severity
    # Check ecosystem-specific severity from GHSA
    for sev_entry in vuln.get("severity", []):
        sev_type = sev_entry.get("type", "")
        if sev_type == "ECOSYSTEM":
            eco_sev = sev_entry.get("score", "").lower()
            if eco_sev in ("critical", "high", "medium", "low"):
                return eco_sev
    return "medium"


def _osv_aliases(vuln: dict) -> list[str]:
    aliases = list(vuln.get("aliases", []))
    vid = vuln.get("id", "")
    if vid and vid not in aliases:
        aliases.insert(0, vid)
    return aliases


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def check_package(
    name: str,
    version: str | None = None,
    check_vulnerabilities: bool = True,
) -> dict:
    """Check a single package for license risk and known vulnerabilities."""
    result: dict = {
        "package": name,
        "version": version or "latest",
        "license_spdx": "UNKNOWN",
        "license_risk": "high",
        "license_explanation": RISK_EXPLANATION.get("high", ""),
        "vulnerabilities": [],
        "maintenance": {},
        "findings": [],
        "error": None,
    }

    pypi = _fetch_pypi_metadata(name, version)
    if pypi is None:
        result["error"] = f"Could not fetch metadata for {name} from PyPI"
        return result

    info = pypi.get("info", {})
    resolved_version = info.get("version", version or "unknown")
    result["version"] = resolved_version

    raw_license = _extract_license_from_pypi(info)
    spdx = _normalize_license(raw_license)
    risk = LICENSE_RISK.get(spdx, "high")

    result["license_spdx"] = spdx
    result["license_risk"] = risk
    result["license_explanation"] = RISK_EXPLANATION.get(risk, "")
    result["maintenance"] = {
        "latest_version": resolved_version,
        "requires_python": info.get("requires_python", ""),
        "home_page": info.get("home_page") or info.get("project_url", ""),
    }

    if risk in ("critical", "high"):
        result["findings"].append({
            "id": f"pre-check-lic-{name}",
            "tool": "pre-check",
            "rule_id": spdx,
            "severity": risk,
            "cwe": "",
            "file": f"{name}@{resolved_version}",
            "line": 0,
            "message": f"License risk: {name}=={resolved_version} uses {spdx} ({RISK_EXPLANATION.get(risk, '')})",
            "suppressed": False,
        })

    if check_vulnerabilities and resolved_version != "unknown":
        vulns = _fetch_osv_vulnerabilities(name, resolved_version)
        for v in vulns:
            sev = _osv_severity(v)
            aliases = _osv_aliases(v)
            cve_id = next((a for a in aliases if a.startswith("CVE-")), aliases[0] if aliases else v.get("id", ""))
            summary = v.get("summary", v.get("details", ""))[:200]

            result["vulnerabilities"].append({
                "id": cve_id,
                "severity": sev,
                "summary": summary,
                "aliases": aliases,
            })

            result["findings"].append({
                "id": f"pre-check-vuln-{cve_id}",
                "tool": "pre-check",
                "rule_id": cve_id,
                "severity": sev,
                "cwe": "",
                "file": f"{name}@{resolved_version}",
                "line": 0,
                "message": f"Vulnerability: {name}=={resolved_version} — {cve_id}: {summary}",
                "suppressed": False,
            })

    return result


def check_requirements(file_path: Path, check_vulnerabilities: bool = True) -> list[dict]:
    """Check all packages in a requirements.txt file."""
    if not file_path.exists():
        return [{"error": f"File not found: {file_path}"}]

    results = []
    for line in file_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        pkg_name = re.split(r"[=<>!~\[;]", line)[0].strip()
        version_match = re.search(r"==\s*([\w.]+)", line)
        version = version_match.group(1) if version_match else None
        if pkg_name:
            results.append(check_package(pkg_name, version, check_vulnerabilities))
            time.sleep(0.1)

    return results


# ---------------------------------------------------------------------------
# Security pattern validation
# ---------------------------------------------------------------------------

def validate_security_patterns(specs_dir: Path) -> list[dict]:
    """Scan spec files for risky architecture patterns missing security controls."""
    findings = []
    counter = 0

    for spec_file in sorted(specs_dir.glob("SPEC-*.md")):
        content = spec_file.read_text(encoding='utf-8')

        for pattern_id, pattern in SECURITY_PATTERNS.items():
            has_indicator = any(re.search(ind, content, re.IGNORECASE) for ind in pattern["indicators"])
            if not has_indicator:
                continue

            has_control = any(re.search(ctrl, content, re.IGNORECASE) for ctrl in pattern["controls"])
            if has_control:
                continue

            counter += 1
            findings.append({
                "id": f"pre-check-pattern-{counter:04d}",
                "tool": "pre-check",
                "rule_id": pattern_id,
                "severity": pattern.get("severity", "high"),
                "cwe": pattern["cwe"],
                "file": spec_file.name,
                "line": 0,
                "message": pattern["risk"],
                "suppressed": False,
            })

    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_package_result(r: dict) -> None:
    if r.get("error"):
        print(f"  ERROR: {r['error']}", file=sys.stderr)
        return

    risk_marker = {"critical": "[BLOCK]", "high": "[BLOCK]", "medium": "[WARN]", "low": "[OK]"}
    marker = risk_marker.get(r["license_risk"], "[?]")
    print(f"  {r['package']}=={r['version']}")
    print(f"    License: {r['license_spdx']} — {marker} {r['license_risk']} risk")
    if r["license_explanation"]:
        print(f"    {r['license_explanation']}")

    if r["vulnerabilities"]:
        print(f"    Vulnerabilities: {len(r['vulnerabilities'])} found")
        for v in r["vulnerabilities"][:5]:
            print(f"      {v['id']} ({v['severity']}): {v['summary'][:80]}")
        if len(r["vulnerabilities"]) > 5:
            print(f"      ... and {len(r['vulnerabilities']) - 5} more")
    else:
        print("    Vulnerabilities: none known")


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Pre-Check: Library License & Vulnerability Scanner")
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="Check a package or requirements file")
    check_p.add_argument("--package", help="Package name to check")
    check_p.add_argument("--version", help="Specific version (default: latest)")
    check_p.add_argument("--requirements", help="Path to requirements.txt")
    check_p.add_argument("--no-vulns", action="store_true", help="Skip vulnerability check")
    check_p.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")

    patterns_p = sub.add_parser("validate-patterns", help="Validate security patterns in specs")
    patterns_p.add_argument("--project-path", help="Project path (default: auto-resolve)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check":
        check_vulns = not args.no_vulns

        if args.requirements:
            results = check_requirements(Path(args.requirements), check_vulns)
        elif args.package:
            results = [check_package(args.package, args.version, check_vulns)]
        else:
            print("Error: specify --package or --requirements", file=sys.stderr)
            sys.exit(1)

        if args.json_output:
            json.dump(results, sys.stdout, indent=2, default=str)
            print()
        else:
            all_findings = []
            for r in results:
                _print_package_result(r)
                all_findings.extend(r.get("findings", []))
            if all_findings:
                blocking = [f for f in all_findings if f["severity"] in ("critical", "high")]
                print(f"\n  Total findings: {len(all_findings)} ({len(blocking)} blocking)")
            else:
                print("\n  All clear — no license or vulnerability issues found.")

    elif args.command == "validate-patterns":
        from aah.core.common.config import resolve_project_path
        explicit = Path(args.project_path) if args.project_path else None
        project_path = resolve_project_path(explicit)
        if project_path is None:
            print("Error: could not resolve project path", file=sys.stderr)
            sys.exit(1)

        specs_dir = project_path / ".aah" / "plan" / "specs"
        if not specs_dir.is_dir():
            print(f"No specs directory found at {specs_dir}", file=sys.stderr)
            sys.exit(1)

        findings = validate_security_patterns(specs_dir)
        if findings:
            print(f"Security pattern findings: {len(findings)}")
            for f in findings:
                print(f"  [{f['severity'].upper()}] {f['file']}: {f['message']} ({f['cwe']})")
        else:
            print("No security pattern issues found in specs.")


if __name__ == "__main__":
    main()
