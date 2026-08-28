#!/usr/bin/env python3
"""Extract license information from installed Python packages.

Provides SPDX-aware license identification and corporate risk scoring
for closed-source projects. Risk tiers reflect copyleft exposure:

  critical — strong copyleft (GPL/AGPL): distributing requires source disclosure
  high     — weak copyleft (LGPL) or unknown: linking/disclosure obligations
  medium   — file-level copyleft (MPL/EPL): modified dep files must be open-sourced
  low      — permissive (MIT/BSD/Apache): safe, maintain attribution
"""

import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# SPDX normalization: messy metadata → canonical SPDX ID
# ---------------------------------------------------------------------------
LICENSE_NORMALIZE: dict[str, str] = {
    # MIT variants
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "the mit license (mit)": "MIT",
    # BSD variants
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "2-clause bsd": "BSD-2-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "0bsd": "0BSD",
    # Apache variants
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    # ISC
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    # PSF
    "psf": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "psfl": "PSF-2.0",
    # MPL
    "mpl 2.0": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    # GPL family
    "gpl": "GPL-3.0-or-later",
    "gpl-2.0": "GPL-2.0-only",
    "gpl-2.0-only": "GPL-2.0-only",
    "gpl-2.0-or-later": "GPL-2.0-or-later",
    "gplv2": "GPL-2.0-only",
    "gplv2+": "GPL-2.0-or-later",
    "gpl-3.0": "GPL-3.0-only",
    "gpl-3.0-only": "GPL-3.0-only",
    "gpl-3.0-or-later": "GPL-3.0-or-later",
    "gplv3": "GPL-3.0-only",
    "gplv3+": "GPL-3.0-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu gpl": "GPL-3.0-or-later",
    # LGPL family
    "lgpl": "LGPL-3.0-or-later",
    "lgpl-2.0": "LGPL-2.0-only",
    "lgpl-2.1": "LGPL-2.1-only",
    "lgpl-2.1-only": "LGPL-2.1-only",
    "lgpl-2.1-or-later": "LGPL-2.1-or-later",
    "lgpl-3.0": "LGPL-3.0-only",
    "lgpl-3.0-only": "LGPL-3.0-only",
    "lgpl-3.0-or-later": "LGPL-3.0-or-later",
    "gnu lgpl": "LGPL-3.0-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    # AGPL
    "agpl-3.0": "AGPL-3.0-only",
    "agpl-3.0-only": "AGPL-3.0-only",
    "agpl-3.0-or-later": "AGPL-3.0-or-later",
    "gnu agpl v3": "AGPL-3.0-only",
    # EPL / CDDL
    "epl-1.0": "EPL-1.0",
    "epl-2.0": "EPL-2.0",
    "eclipse public license 2.0": "EPL-2.0",
    "cddl-1.0": "CDDL-1.0",
    "cddl-1.1": "CDDL-1.1",
    # CC
    "cc0-1.0": "CC0-1.0",
    "cc-by-4.0": "CC-BY-4.0",
    "cc-by-sa-4.0": "CC-BY-SA-4.0",
    # Other permissive
    "unlicense": "Unlicense",
    "the unlicense": "Unlicense",
    "zlib": "Zlib",
    "zlib/libpng": "Zlib",
    "bsl-1.0": "BSL-1.0",
    "boost software license 1.0": "BSL-1.0",
    "public domain": "Unlicense",
    "wtfpl": "WTFPL",
}

# Regex fallbacks for GPL/LGPL/AGPL when exact match fails
_GPL_RE = re.compile(r"\bA?L?GPL[- ]?v?(\d)\.?0?\+?", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Corporate risk model for closed-source projects
# ---------------------------------------------------------------------------
LICENSE_RISK: dict[str, str] = {
    # Critical — strong copyleft, forces source disclosure on distribution
    "GPL-2.0-only": "critical",
    "GPL-2.0-or-later": "critical",
    "GPL-3.0-only": "critical",
    "GPL-3.0-or-later": "critical",
    "AGPL-3.0-only": "critical",
    "AGPL-3.0-or-later": "critical",
    # High — weak copyleft, linking constraints
    "LGPL-2.0-only": "high",
    "LGPL-2.0-or-later": "high",
    "LGPL-2.1-only": "high",
    "LGPL-2.1-or-later": "high",
    "LGPL-3.0-only": "high",
    "LGPL-3.0-or-later": "high",
    # Medium — file-level copyleft
    "MPL-2.0": "medium",
    "EPL-1.0": "medium",
    "EPL-2.0": "medium",
    "CDDL-1.0": "medium",
    "CDDL-1.1": "medium",
    "CC-BY-SA-4.0": "medium",
    "EUPL-1.2": "medium",
    # Low — permissive, safe for closed-source
    "MIT": "low",
    "BSD-2-Clause": "low",
    "BSD-3-Clause": "low",
    "Apache-2.0": "low",
    "ISC": "low",
    "PSF-2.0": "low",
    "Unlicense": "low",
    "0BSD": "low",
    "CC0-1.0": "low",
    "CC-BY-4.0": "low",
    "BSL-1.0": "low",
    "Zlib": "low",
    "WTFPL": "low",
}

RISK_EXPLANATION: dict[str, str] = {
    "critical": "strong copyleft — distributing your software requires releasing source code",
    "high": "weak copyleft or unknown license — may impose linking or disclosure obligations",
    "medium": "file-level copyleft — modified files of this dependency must be open-sourced",
    "low": "permissive — safe for closed-source, maintain attribution in NOTICE file",
}


def _normalize_license(raw: str) -> str:
    """Normalize a raw license string to an SPDX identifier."""
    if not raw or raw == "UNKNOWN":
        return "UNKNOWN"

    # Handle SPDX expressions with OR — take the most permissive
    if " OR " in raw:
        parts = [_normalize_license(p.strip()) for p in raw.split(" OR ")]
        known = [p for p in parts if p != "UNKNOWN"]
        if not known:
            return "UNKNOWN"
        risks = [(p, LICENSE_RISK.get(p, "high")) for p in known]
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        risks.sort(key=lambda x: order.get(x[1], 2))
        return risks[0][0]

    # Handle AND — take the most restrictive
    if " AND " in raw:
        parts = [_normalize_license(p.strip()) for p in raw.split(" AND ")]
        known = [p for p in parts if p != "UNKNOWN"]
        if not known:
            return "UNKNOWN"
        risks = [(p, LICENSE_RISK.get(p, "high")) for p in known]
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        risks.sort(key=lambda x: order.get(x[1], 2), reverse=True)
        return risks[0][0]

    key = raw.strip().lower()

    # Exact match in normalization table
    if key in LICENSE_NORMALIZE:
        return LICENSE_NORMALIZE[key]

    # If the raw string is already a known SPDX ID
    if raw in LICENSE_RISK:
        return raw

    # Regex fallback for GPL/LGPL/AGPL variants
    m = _GPL_RE.search(raw)
    if m:
        full = m.group(0).upper().replace(" ", "")
        if "AGPL" in full:
            return f"AGPL-{m.group(1)}.0-or-later"
        if "LGPL" in full:
            return f"LGPL-{m.group(1)}.0-or-later"
        return f"GPL-{m.group(1)}.0-or-later"

    return "UNKNOWN"


def _find_venv(project_path: Path) -> Path | None:
    """Find the project's virtual environment."""
    for candidate in ("venv", ".venv", "env", ".env"):
        venv = project_path / candidate
        if (venv / "lib").is_dir():
            return venv
    return None


def _read_direct_deps(project_path: Path) -> set[str]:
    """Read direct dependency names from requirements.txt."""
    reqs_file = project_path / "requirements.txt"
    if not reqs_file.exists():
        return set()
    names = set()
    for line in reqs_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[=<>!~\[]", line)[0].strip()
        if name:
            names.add(name.lower().replace("-", "").replace("_", ""))
    return names


FRAMEWORK_PACKAGES = {"rapidsscripts"}


def _read_self_package_name(project_path: Path) -> set[str]:
    """Read the project's own package name so it can be excluded from license scanning.

    Always excludes the AAH framework package itself, plus the project's
    own package if a pyproject.toml is found.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            tomllib = None  # type: ignore[assignment]

    exclude = set(FRAMEWORK_PACKAGES)
    for toml_path in (project_path / "pyproject.toml", project_path / "scripts" / "pyproject.toml"):
        if not toml_path.exists():
            continue
        try:
            if tomllib is not None:
                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                name = data.get("project", {}).get("name") or data.get("name")
            else:
                name = None
                for line in toml_path.read_text(encoding='utf-8').splitlines():
                    m = re.match(r'^name\s*=\s*["\']([^"\']+)["\']', line)
                    if m:
                        name = m.group(1)
                        break
            if name:
                exclude.add(name.lower().replace("-", "").replace("_", ""))
        except Exception:
            continue
    return exclude


def extract_licenses(project_path: Path) -> list[dict]:
    """Extract license info from installed packages using importlib.metadata.

    Returns findings with SPDX license IDs and corporate risk scores.
    """
    venv = _find_venv(project_path)
    python = str(venv / "bin" / "python") if venv else sys.executable

    script = """
import json, sys
from importlib.metadata import distributions

results = []
for dist in distributions():
    meta = dist.metadata
    name = meta["Name"]
    version = meta["Version"]
    license_field = meta.get("License", "") or ""
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License")]
    classifier_lic = classifiers[0].split(" :: ")[-1] if classifiers else ""
    license_expr = meta.get("License-Expression", "") or ""
    best = license_expr or classifier_lic or license_field or "UNKNOWN"
    results.append({"name": name, "version": version, "license": best})
json.dump(results, sys.stdout)
"""
    try:
        proc = subprocess.run(
            [python, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return []
        import json
        packages = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []

    direct_deps = _read_direct_deps(project_path)
    exclude_names = _read_self_package_name(project_path)

    findings = []
    for i, pkg in enumerate(packages):
        name = pkg["name"]
        norm_name = name.lower().replace("-", "").replace("_", "")
        if norm_name in exclude_names:
            continue
        is_direct = norm_name in direct_deps
        raw_license = pkg["license"]

        spdx_id = _normalize_license(raw_license)
        risk = LICENSE_RISK.get(spdx_id, "high")
        explanation = RISK_EXPLANATION[risk]

        findings.append({
            "id": f"license-{i + 1:04d}",
            "tool": "license-scan",
            "rule_id": spdx_id,
            "severity": risk,
            "cwe": "",
            "file": f"{name}@{pkg['version']}",
            "line": 0,
            "message": (
                f"{'[direct]' if is_direct else '[transitive]'} "
                f"{name}=={pkg['version']}: {spdx_id} "
                f"({risk} risk — {explanation})"
            ),
            "suppressed": False,
        })

    return findings


def generate_csv(project_path: Path, output_path: Path | None = None) -> str:
    """Generate a CSV report of all licenses in the project.

    Columns: Package, Version, License (SPDX), Risk, Type, Risk Explanation
    """
    import csv
    import io

    venv = _find_venv(project_path)
    python = str(venv / "bin" / "python") if venv else sys.executable

    script = """
import json, sys
from importlib.metadata import distributions

results = []
for dist in distributions():
    meta = dist.metadata
    name = meta["Name"]
    version = meta["Version"]
    license_field = meta.get("License", "") or ""
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License")]
    classifier_lic = classifiers[0].split(" :: ")[-1] if classifiers else ""
    license_expr = meta.get("License-Expression", "") or ""
    best = license_expr or classifier_lic or license_field or "UNKNOWN"
    results.append({"name": name, "version": version, "license": best})
json.dump(results, sys.stdout)
"""
    try:
        proc = subprocess.run(
            [python, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return ""
        import json
        packages = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ""

    direct_deps = _read_direct_deps(project_path)
    exclude_names = _read_self_package_name(project_path)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Package", "Version", "License (SPDX)", "Risk", "Type", "Risk Explanation"])

    rows = []
    for pkg in packages:
        name = pkg["name"]
        norm_name = name.lower().replace("-", "").replace("_", "")
        if norm_name in exclude_names:
            continue
        is_direct = norm_name in direct_deps
        raw_license = pkg["license"]
        spdx_id = _normalize_license(raw_license)
        risk = LICENSE_RISK.get(spdx_id, "high")
        explanation = RISK_EXPLANATION[risk]
        dep_type = "direct" if is_direct else "transitive"
        rows.append((name, pkg["version"], spdx_id, risk, dep_type, explanation))

    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows.sort(key=lambda r: (risk_order.get(r[3], 1), r[4] != "direct", r[0].lower()))

    for row in rows:
        writer.writerow(row)

    csv_content = buf.getvalue()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(csv_content, encoding='utf-8')

    return csv_content


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AAH Python License Scanner")
    sub = parser.add_subparsers(dest="command")

    csv_parser = sub.add_parser("csv", help="Generate license CSV report")
    csv_parser.add_argument("--project-path", help="Project path (default: auto-resolve)")
    csv_parser.add_argument("--output", help="Output CSV path (default: .aah/security/reports/licenses.csv)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    from aah.core.common.config import resolve_project_path

    explicit = Path(args.project_path) if args.project_path else None
    project_path = resolve_project_path(explicit)
    if project_path is None:
        print("Error: could not resolve project path", file=sys.stderr)
        sys.exit(1)

    from aah.core.security.state import SecurityStateMigrationError, ensure_security_state

    try:
        security_dir = ensure_security_state(project_path, notify=True).security_dir
    except SecurityStateMigrationError as exc:
        print(f"Error: security state migration failed: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.command == "csv":
        if args.output:
            out = Path(args.output)
        else:
            out = security_dir / "reports" / "licenses.csv"

        csv_content = generate_csv(project_path, out)
        if not csv_content:
            print("Error: could not extract license information", file=sys.stderr)
            sys.exit(1)

        print(csv_content)
        print(f"\nCSV saved to: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
