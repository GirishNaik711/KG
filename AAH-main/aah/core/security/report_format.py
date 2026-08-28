"""Shared formatting helpers for security scan reports.

Produces executive-friendly, deduplicated markdown with visual hierarchy.
All functions return strings or lists of markdown lines.
"""

from __future__ import annotations

from collections import OrderedDict

SEVERITY_ICONS = {
    "critical": "[CRIT]",
    "high": "[HIGH]",
    "medium": "[MED]",
    "low": "[LOW]",
}

SEVERITY_ORDER = ["critical", "high", "medium", "low"]

EFFORT_ICONS = {"small": "", "medium": "", "large": ""}

LANG_FENCE = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "java": "java",
    "go": "go",
    "ruby": "ruby",
    "csharp": "csharp",
    "default": "",
}


def severity_badge(severity: str) -> str:
    tag = SEVERITY_ICONS.get(severity, "")
    return f"**{tag}**"


def status_banner(passed: bool) -> str:
    if passed:
        return "> **GATE STATUS: PASSED** — No blocking findings detected."
    return "> **GATE STATUS: FAILED** — Blocking findings require remediation before release."


def severity_bar(critical: int, high: int, medium: int, low: int) -> str:
    total = critical + high + medium + low
    if total == 0:
        return ""
    parts = []
    for label, count in [("Critical", critical), ("High", high),
                         ("Medium", medium), ("Low", low)]:
        if count:
            pct = round(count / total * 100)
            parts.append(f"**{label}:** {count} ({pct}%)")
    return " | ".join(parts)


def effort_label(effort: str) -> str:
    return effort if effort else "—"


def code_fence_lang(language: str) -> str:
    return LANG_FENCE.get(language, "")


def severity_counts(findings: list[dict]) -> dict[str, int]:
    active = [f for f in findings if not f.get("suppressed")]
    return {s: sum(1 for f in active if f["severity"] == s) for s in SEVERITY_ORDER}


def severity_counts_str(sc: dict[str, int]) -> str:
    return f"{sc['critical']}C / {sc['high']}H / {sc['medium']}M / {sc['low']}L"


def group_findings_by_rule(findings: list[dict]) -> list[dict]:
    """Group findings by rule_id. Returns list of group dicts sorted by severity then count."""
    groups: OrderedDict[str, dict] = OrderedDict()
    for f in findings:
        key = f["rule_id"]
        if key not in groups:
            groups[key] = {
                "rule_id": f["rule_id"],
                "tool": f.get("tool", ""),
                "severity": f["severity"],
                "cwe": f.get("cwe", ""),
                "owasp": f.get("owasp", ""),
                "message": f["message"],
                "fix_description": f.get("fix_description", ""),
                "fix_example": f.get("fix_example", ""),
                "reference_url": f.get("reference_url", ""),
                "effort": f.get("effort", ""),
                "count": 0,
                "locations": [],
            }
        groups[key]["count"] += 1
        groups[key]["locations"].append({
            "id": f["id"],
            "file": f["file"],
            "line": f.get("line", 0),
            "code_snippet": f.get("code_snippet", ""),
        })

    result = list(groups.values())
    result.sort(key=lambda g: (SEVERITY_ORDER.index(g["severity"]), -g["count"]))
    return result


def executive_summary(findings: list[dict], groups: list[dict]) -> list[str]:
    """Generate executive summary lines for a detail report."""
    sc = severity_counts(findings)
    lines = [
        "## Executive Summary",
        "",
        f"This scan identified **{len(findings)} findings** across "
        f"**{len(groups)} distinct rule{'s' if len(groups) != 1 else ''}**.",
        "",
        f"| Severity | Count |",
        f"|----------|------:|",
    ]
    for sev in SEVERITY_ORDER:
        if sc[sev]:
            lines.append(f"| {severity_badge(sev)} | {sc[sev]} |")
    lines.append("")
    return lines


def scan_status_badge(findings: list[dict]) -> str:
    """Return a pass/warn/fail badge string based on findings severity."""
    sc = severity_counts(findings)
    if sc["critical"] or sc["high"]:
        return "**FAIL**"
    if sc["medium"]:
        return "**WARN**"
    return "**PASS**"


def mermaid_pie(title: str, segments: dict[str, int]) -> list[str]:
    """Generate a Mermaid pie chart. Omits segments with zero value."""
    non_zero = {k: v for k, v in segments.items() if v}
    if not non_zero:
        return []
    lines = [
        "```mermaid",
        f'pie title {title}',
    ]
    for label, value in non_zero.items():
        lines.append(f'    "{label}" : {value}')
    lines.extend(["```", ""])
    return lines


def locations_table(locations: list[dict], label: str = "affected locations") -> list[str]:
    """Collapsible table of finding locations."""
    count = len(locations)
    lines = [
        "<details>",
        f"<summary><strong>{count} {label}</strong> (click to expand)</summary>",
        "",
        "| # | File | Line |",
        "|--:|------|-----:|",
    ]
    for i, loc in enumerate(locations, 1):
        snippet = loc.get("code_snippet", "").replace("|", "\\|").strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        lines.append(f"| {i} | `{loc['file']}` | {loc['line']} |")
    lines.extend(["", "</details>", ""])
    return lines


def short_rule(rule_id: str) -> str:
    return rule_id.split(".")[-1] if "." in rule_id else rule_id
