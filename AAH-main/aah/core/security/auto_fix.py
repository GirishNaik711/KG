#!/usr/bin/env python3
"""Auto-remediation commands for security scan findings.

Three-tier fix pipeline:
  Tier 1 — Deterministic fixes (deps upgrade, secret externalization)
  Tier 2 — Semgrep autofix (rule-authored code patches)
  Tier 3 — Claude-assisted (interactive AI remediation via skill)

Subcommands:
  classify     — Classify findings into fix tiers (JSON output)
  fix-deps     — Upgrade vulnerable dependencies in requirements.txt
  fix-secrets  — Generate .env.example and replacement code for hardcoded secrets
  fix-sast     — Re-run Semgrep with --autofix to apply rule-based code fixes
  fix-iac      — Apply deterministic IaC fixes where possible
  run-pipeline — Run tiers 1 and 2, output tier 3 remaining as JSON
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state


def _load_scan_results(project_path: Path) -> dict | None:
    security_dir = ensure_security_state(project_path, notify=True).security_dir
    results_file = security_dir / "scan-results-latest.json"
    if not results_file.exists():
        return None
    return json.loads(results_file.read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_findings(project_path: Path) -> dict:
    """Classify scan findings into fix tiers."""
    results = _load_scan_results(project_path)
    if results is None:
        return {"error": "No scan results found. Run /aah-security-scan first."}

    tier1_deps = []
    tier1_secrets = []
    tier2_semgrep = []
    tier3_claude = []
    skipped = []

    for f in results["findings"]:
        if f.get("suppressed"):
            skipped.append(f)
            continue

        tool = f["tool"]

        if tool == "license-scan":
            skipped.append(f)
        elif tool == "trivy" and f.get("fix_example") and "==" in f.get("fix_example", ""):
            tier1_deps.append(f)
        elif tool == "gitleaks":
            tier1_secrets.append(f)
        elif tool == "semgrep":
            tier2_semgrep.append(f)
        elif tool == "trivy" and not f.get("fix_example"):
            tier3_claude.append(f)
        else:
            tier3_claude.append(f)

    return {
        "tier1_deps": tier1_deps,
        "tier1_secrets": tier1_secrets,
        "tier2_semgrep": tier2_semgrep,
        "tier3_claude": tier3_claude,
        "skipped": skipped,
        "summary": {
            "tier1_deps": len(tier1_deps),
            "tier1_secrets": len(tier1_secrets),
            "tier2_semgrep": len(tier2_semgrep),
            "tier3_claude": len(tier3_claude),
            "skipped": len(skipped),
            "total": len(results["findings"]),
        },
    }


# ---------------------------------------------------------------------------
# Tier 1: Deterministic fixes
# ---------------------------------------------------------------------------

def fix_deps(project_path: Path) -> dict:
    """Upgrade vulnerable dependencies in requirements.txt."""
    results = _load_scan_results(project_path)
    if results is None:
        return {"error": "No scan results found. Run /aah-security-scan first."}

    trivy_findings = [
        f for f in results["findings"]
        if f["tool"] == "trivy" and f.get("fix_example") and not f.get("suppressed")
    ]

    if not trivy_findings:
        return {"upgraded": [], "skipped": [], "message": "No fixable dependency vulnerabilities found."}

    reqs_file = project_path / "requirements.txt"
    if not reqs_file.exists():
        return {"error": "No requirements.txt found."}

    lines = reqs_file.read_text(encoding='utf-8').splitlines()
    upgraded = []
    skip_list = []
    seen_packages = set()

    for finding in trivy_findings:
        fix = finding["fix_example"]
        if "==" not in fix:
            skip_list.append({"id": finding["id"], "reason": "No pinned fix version"})
            continue

        pkg_name, target_version = fix.split("==", 1)
        pkg_lower = pkg_name.lower().replace("-", "").replace("_", "")

        if pkg_lower in seen_packages:
            continue
        seen_packages.add(pkg_lower)

        matched = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            line_pkg = re.split(r"[=<>!~\[]", stripped)[0].strip()
            if line_pkg.lower().replace("-", "").replace("_", "") == pkg_lower:
                old_line = lines[i]
                # >= (not ==) so future security patches are also pulled in
                lines[i] = f"{pkg_name}>={target_version}"
                upgraded.append({
                    "package": pkg_name,
                    "old": old_line.strip(),
                    "new": lines[i],
                    "cves": [f["rule_id"] for f in trivy_findings
                             if f["fix_example"].split("==")[0].lower().replace("-", "").replace("_", "") == pkg_lower],
                })
                matched = True
                break

        if not matched:
            skip_list.append({"id": finding["id"], "package": pkg_name, "reason": "Not found in requirements.txt"})

    if upgraded:
        reqs_file.write_text("\n".join(lines) + "\n", encoding='utf-8')

    return {
        "upgraded": upgraded,
        "skipped": skip_list,
        "message": f"Upgraded {len(upgraded)} package(s). Run `pip install -r requirements.txt` to apply.",
    }


def fix_secrets(project_path: Path) -> dict:
    """Generate .env.example and replacement code for hardcoded secrets."""
    results = _load_scan_results(project_path)
    if results is None:
        return {"error": "No scan results found. Run /aah-security-scan first."}

    secret_findings = [
        f for f in results["findings"]
        if f["tool"] == "gitleaks" and not f.get("suppressed")
    ]

    if not secret_findings:
        return {"env_vars": [], "message": "No hardcoded secrets found."}

    env_vars = []

    for finding in secret_findings:
        file_path = finding["file"]
        line_num = finding["line"]

        if not file_path.endswith(".py"):
            continue

        abs_path = project_path / file_path
        if not abs_path.exists():
            continue

        source_lines = abs_path.read_text(encoding='utf-8').splitlines()
        if line_num < 1 or line_num > len(source_lines):
            continue

        source_line = source_lines[line_num - 1]
        match = re.match(r"^(\s*)(\w+)\s*=\s*[\"'](.+)[\"']", source_line)
        if not match:
            continue

        indent, var_name, _var_value = match.groups()

        env_vars.append({
            "var_name": var_name,
            "placeholder": f"<your-{var_name.lower().replace('_', '-')}-here>",
            "file": file_path,
            "line": line_num,
            "original_line": source_line.strip(),
            "replacement_line": f'{indent}{var_name} = os.environ["{var_name}"]',
        })

    if env_vars:
        env_example_path = project_path / ".env.example"
        env_lines = ["# Environment variables — copy to .env and fill in real values", ""]
        for ev in env_vars:
            env_lines.append(f"{ev['var_name']}={ev['placeholder']}")
        env_example_path.write_text("\n".join(env_lines) + "\n", encoding='utf-8')

        gitignore_path = project_path / ".gitignore"
        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding='utf-8')
            if ".env" not in content:
                gitignore_path.write_text(content.rstrip() + "\n.env\n", encoding='utf-8')
        else:
            gitignore_path.write_text(".env\n", encoding='utf-8')

    return {
        "env_vars": env_vars,
        "env_example_path": str(project_path / ".env.example") if env_vars else "",
        "message": (
            f"Found {len(env_vars)} secret(s) to externalize.\n"
            f"Generated .env.example with placeholder values.\n"
            f"Apply replacements below, then copy .env.example to .env and fill in real values."
        ),
    }


# ---------------------------------------------------------------------------
# Tier 2: Semgrep autofix
# ---------------------------------------------------------------------------

def fix_sast_autofix(project_path: Path) -> dict:
    """Re-run Semgrep with --autofix to apply rule-specific code fixes."""
    results = _load_scan_results(project_path)
    if results is None:
        return {"error": "No scan results found.", "applied": 0, "modified_files": [], "message": ""}

    language = results.get("language", "default")

    # Deferred import: run_security_scan doesn't import auto_fix, so this is safe
    from aah.core.security.run_security_scan import SEMGREP_RULES
    rule_config = SEMGREP_RULES.get(language, SEMGREP_RULES["default"])

    cmd = ["semgrep", "scan", *rule_config, "--autofix", "--json", str(project_path)]

    custom_rules = ensure_security_state(project_path).security_dir / "semgrep-rules"
    if custom_rules.is_dir() and any(custom_rules.glob("*.yaml")):
        cmd.extend(["--config", str(custom_rules)])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "Semgrep timed out", "applied": 0, "modified_files": [], "message": ""}
    except OSError as e:
        return {"error": str(e), "applied": 0, "modified_files": [], "message": ""}

    if proc.returncode not in (0, 1):
        return {
            "error": f"Semgrep exited with code {proc.returncode}",
            "applied": 0,
            "modified_files": [],
            "message": proc.stderr[-500:] if proc.stderr else "",
        }

    modified_files = []
    applied = 0
    try:
        output = json.loads(proc.stdout) if proc.stdout else {}
        for result in output.get("results", []):
            if result.get("extra", {}).get("fix"):
                file_path = result.get("path", "")
                try:
                    file_path = str(Path(file_path).resolve().relative_to(project_path.resolve()))
                except ValueError:
                    pass
                if file_path not in modified_files:
                    modified_files.append(file_path)
                applied += 1
    except json.JSONDecodeError:
        pass

    return {
        "applied": applied,
        "modified_files": modified_files,
        "error": None,
        "message": (
            f"Semgrep autofix applied {applied} fix(es) across {len(modified_files)} file(s)."
            if applied else "No Semgrep autofix rules matched the current findings."
        ),
    }


# ---------------------------------------------------------------------------
# Tier 2b: Deterministic fixers for custom AAH rules
# ---------------------------------------------------------------------------

_FSTRING_QUOTED_INTERP = re.compile(r"'?\{([^}]+)\}'?")


def _fix_fstring_sql(line: str) -> str | None:
    """Rewrite a line or joined multi-line f-string SQL execute to parameterized form.

    Returns the fixed line, or None if the pattern doesn't match.
    """
    # Single-line: conn.execute(f"SELECT ... WHERE id = {var}")
    m = re.match(
        r"^(\s*)(.*?\.execute\()\s*f([\"'])(.*?)\3\s*\)(.*)$",
        line,
    )
    # Multi-line triple-quote
    if not m:
        m = re.match(
            r'^(\s*)(.*?\.execute\(\s*)f("""|\'\'\')\s*(.*?)\3\s*\)(.*)$',
            line,
            re.DOTALL,
        )
    # Multi-line joined: indent + execute(\n + f"..."\n + )
    if not m:
        m = re.match(
            r'^(\s*)(.*?\.execute\(\s*)\n\s*f(["\'])(.*?)\3\s*\n\s*\)(.*)$',
            line,
            re.DOTALL,
        )
    if not m:
        return None

    indent, prefix, _quote, sql_body, suffix = m.groups()
    prefix = prefix.rstrip()

    params = []
    def _replace_interp(match):
        var = match.group(1).strip()
        params.append(var)
        return "?"

    fixed_sql = _FSTRING_QUOTED_INTERP.sub(_replace_interp, sql_body)

    if not params:
        return None

    if len(params) == 1:
        params_str = f"({params[0]},)"
    else:
        params_str = f"({', '.join(params)})"

    if prefix.rstrip().endswith("("):
        return f'{indent}{prefix}"{fixed_sql}", {params_str}){suffix}'
    return f'{indent}{prefix}("{fixed_sql}", {params_str}){suffix}'


def _fix_format_sql(line: str) -> str | None:
    """Rewrite .format() SQL execute to parameterized form."""
    m = re.match(
        r'^(\s*)(.*?\.execute\()\s*(["\'])(.*?)\3\.format\(([^)]+)\)\s*\)(.*)$',
        line,
    )
    if not m:
        return None

    indent, prefix, _quote, sql_body, format_args, suffix = m.groups()

    placeholders = re.findall(r"\{[^}]*\}", sql_body)
    if not placeholders:
        return None

    fixed_sql = re.sub(r"\{[^}]*\}", "?", sql_body)
    args = [a.strip() for a in format_args.split(",")]

    if len(args) == 1:
        params_str = f"({args[0]},)"
    else:
        params_str = f"({', '.join(args)})"

    return f'{indent}{prefix}"{fixed_sql}", {params_str}){suffix}'


def _fix_percent_sql(line: str) -> str | None:
    """Rewrite % formatted SQL execute to parameterized form."""
    m = re.match(
        r'^(\s*)(.*?\.execute\()\s*(["\'])(.*?)\3\s*%\s*\(([^)]+)\)\s*\)(.*)$',
        line,
    )
    if not m:
        return None

    indent, prefix, _quote, sql_body, percent_args, suffix = m.groups()

    fixed_sql = re.sub(r"%[sd]", "?", sql_body)
    args = [a.strip() for a in percent_args.split(",")]

    if len(args) == 1:
        params_str = f"({args[0]},)"
    else:
        params_str = f"({', '.join(args)})"

    return f'{indent}{prefix}"{fixed_sql}", {params_str}){suffix}'


_RULE_FIXERS = {
    "sqlite3-fstring-execute": _fix_fstring_sql,
    "sqlite3-format-execute": _fix_format_sql,
    "sqlite3-percent-execute": _fix_percent_sql,
}


def fix_custom_sast(project_path: Path) -> dict:
    """Apply deterministic fixes for custom AAH Semgrep rules.

    Handles SQL injection patterns (f-string, .format(), %) by rewriting
    to parameterized queries. Pure Python string manipulation — no LLM.
    """
    results = _load_scan_results(project_path)
    if results is None:
        return {"error": "No scan results found.", "applied": 0, "modified_files": [], "message": ""}

    injection_findings = []
    for f in results["findings"]:
        if f["tool"] != "semgrep" or f.get("suppressed"):
            continue
        short_rule = f["rule_id"].split(".")[-1] if "." in f["rule_id"] else f["rule_id"]
        if short_rule in _RULE_FIXERS:
            injection_findings.append({**f, "_short_rule": short_rule})

    if not injection_findings:
        return {"applied": 0, "modified_files": [], "error": None, "message": "No custom SAST rules to fix."}

    files_to_fix: dict[str, list[dict]] = {}
    for f in injection_findings:
        files_to_fix.setdefault(f["file"], []).append(f)

    applied = 0
    modified_files = []

    for file_path, findings in files_to_fix.items():
        abs_path = project_path / file_path
        if not abs_path.exists():
            continue

        lines = abs_path.read_text(encoding='utf-8').splitlines()
        changed = False

        findings_by_line = sorted(findings, key=lambda f: f["line"], reverse=True)

        for finding in findings_by_line:
            line_num = finding["line"]
            if line_num < 1 or line_num > len(lines):
                continue

            fixer = _RULE_FIXERS.get(finding["_short_rule"])
            if not fixer:
                continue

            # Try single-line fix first
            original = lines[line_num - 1]
            fixed = fixer(original)
            if fixed and fixed != original:
                lines[line_num - 1] = fixed
                applied += 1
                changed = True
                continue

            # Try multi-line: join up to 5 lines from the finding line
            for span in range(2, 6):
                end = min(line_num - 1 + span, len(lines))
                joined = "\n".join(lines[line_num - 1:end])
                fixed = fixer(joined)
                if fixed and fixed != joined:
                    fixed_single = re.sub(r"\n\s*", " ", fixed)
                    indent = re.match(r"^(\s*)", lines[line_num - 1]).group(1)
                    lines[line_num - 1] = indent + fixed_single.strip()
                    for j in range(line_num, end):
                        lines[j] = ""
                    applied += 1
                    changed = True
                    break

        if changed:
            cleaned = []
            prev_blank = False
            for ln in lines:
                is_blank = ln.strip() == ""
                if is_blank and prev_blank:
                    continue
                cleaned.append(ln)
                prev_blank = is_blank
            abs_path.write_text("\n".join(cleaned) + "\n", encoding='utf-8')
            modified_files.append(file_path)

    return {
        "applied": applied,
        "modified_files": modified_files,
        "error": None,
        "message": (
            f"Custom SAST fixer applied {applied} fix(es) across {len(modified_files)} file(s)."
            if applied else "No custom SAST patterns matched for deterministic fixing."
        ),
    }


# ---------------------------------------------------------------------------
# Tier 1 extension: IaC fixes
# ---------------------------------------------------------------------------

def fix_iac(project_path: Path) -> dict:
    """Apply deterministic IaC fixes where possible.

    Currently a placeholder — all IaC findings route to ``skipped`` because
    misconfigurations (Dockerfile pin versions, Terraform configs) cannot be
    fixed deterministically without environment context. The ``applied`` list
    will remain empty until specific safe-fix rules are added.
    """
    results = _load_scan_results(project_path)
    if results is None:
        return {"error": "No scan results found.", "applied": [], "skipped": [], "message": ""}

    iac_findings = [
        f for f in results["findings"]
        if f["tool"] == "trivy-iac" and not f.get("suppressed")
    ]

    if not iac_findings:
        return {"applied": [], "skipped": [], "message": "No IaC findings to fix."}

    applied = []
    skipped = []

    for finding in iac_findings:
        file_path = project_path / finding["file"]
        rule_id = finding.get("rule_id", "")

        if not file_path.exists():
            skipped.append({"id": finding["id"], "reason": "File not found"})
            continue

        content = file_path.read_text(encoding='utf-8')

        if file_path.name == "Dockerfile" and "DS001" in rule_id:
            # Pin :latest tag — only if we can detect the pattern
            if re.search(r"^FROM\s+\S+:latest\s*$", content, re.MULTILINE):
                skipped.append({
                    "id": finding["id"],
                    "reason": "Cannot determine pinned version automatically — needs manual review",
                })
                continue

        skipped.append({
            "id": finding["id"],
            "rule_id": rule_id,
            "file": finding["file"],
            "reason": "No deterministic fix available — use Tier 3 (Claude-assisted)",
        })

    return {
        "applied": applied,
        "skipped": skipped,
        "message": (
            f"Applied {len(applied)} IaC fix(es), {len(skipped)} need manual review."
        ),
    }


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

def run_fix_pipeline(project_path: Path, tiers: list[int] | None = None) -> dict:
    """Run the auto-fix pipeline for specified tiers.

    Tiers 1-2 are automated. Tier 3 findings are returned for the skill
    to handle interactively via Claude.
    """
    if tiers is None:
        tiers = [1, 2]

    classification = classify_findings(project_path)
    if "error" in classification:
        return {"error": classification["error"]}

    tier1_deps_result = None
    tier1_secrets_result = None
    tier1_iac_result = None
    tier2_result = None

    if 1 in tiers:
        if classification["tier1_deps"]:
            tier1_deps_result = fix_deps(project_path)
        if classification["tier1_secrets"]:
            tier1_secrets_result = fix_secrets(project_path)
        tier1_iac_result = fix_iac(project_path)

    tier2_custom_result = None

    if 2 in tiers:
        if classification["tier2_semgrep"]:
            tier2_result = fix_sast_autofix(project_path)
            tier2_custom_result = fix_custom_sast(project_path)

    tier1_fixed = 0
    if tier1_deps_result and "upgraded" in tier1_deps_result:
        tier1_fixed += len(tier1_deps_result["upgraded"])
    if tier1_secrets_result and "env_vars" in tier1_secrets_result:
        tier1_fixed += len(tier1_secrets_result["env_vars"])

    tier2_fixed = 0
    if tier2_result and tier2_result.get("applied"):
        tier2_fixed = tier2_result["applied"]
    if tier2_custom_result and tier2_custom_result.get("applied"):
        tier2_fixed += tier2_custom_result["applied"]

    tier3_remaining = classification["tier3_claude"]

    return {
        "classification": classification["summary"],
        "tier1_deps_result": tier1_deps_result,
        "tier1_secrets_result": tier1_secrets_result,
        "tier1_iac_result": tier1_iac_result,
        "tier2_result": tier2_result,
        "tier2_custom_result": tier2_custom_result,
        "tier3_remaining": tier3_remaining,
        "summary": {
            "tier1_fixed": tier1_fixed,
            "tier2_fixed": tier2_fixed,
            "tier3_pending": len(tier3_remaining),
            "total_before": classification["summary"]["total"],
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Security Auto-Fix")
    sub = parser.add_subparsers(dest="command")

    for name, help_text in [
        ("classify", "Classify findings into fix tiers"),
        ("fix-deps", "Upgrade vulnerable dependencies"),
        ("fix-secrets", "Externalize hardcoded secrets"),
        ("fix-sast", "Run Semgrep autofix"),
        ("fix-iac", "Apply deterministic IaC fixes"),
        ("run-pipeline", "Run tiers 1 and 2, output tier 3 remaining"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--project-path", help="Project path")
        if name == "run-pipeline":
            p.add_argument("--tiers", default="1,2", help="Comma-separated tier numbers (default: 1,2)")

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

    if args.command == "classify":
        result = classify_findings(project_path)
        json.dump(result, sys.stdout, indent=2, default=str)
        print()

    elif args.command == "fix-deps":
        result = fix_deps(project_path)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(result["message"])
        for u in result["upgraded"]:
            print(f"  Upgraded: {u['old']} -> {u['new']} (fixes {', '.join(u['cves'])})")

    elif args.command == "fix-secrets":
        result = fix_secrets(project_path)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(result["message"])
        for ev in result["env_vars"]:
            print(f"  {ev['file']}:{ev['line']}")
            print(f"    Before: {ev['original_line']}")
            print(f"    After:  {ev['replacement_line']}")

    elif args.command == "fix-sast":
        result = fix_sast_autofix(project_path)
        if result.get("error"):
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(result["message"])
        for f in result["modified_files"]:
            print(f"  Modified: {f}")

    elif args.command == "fix-iac":
        result = fix_iac(project_path)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(result["message"])

    elif args.command == "run-pipeline":
        tier_list = [int(t.strip()) for t in args.tiers.split(",")]
        result = run_fix_pipeline(project_path, tier_list)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2, default=str)
        print()


if __name__ == "__main__":
    main()
