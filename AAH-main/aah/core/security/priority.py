#!/usr/bin/env python3
"""Priority scoring for security findings.

Computes a composite score so developers know what to fix first.
Higher score = fix first.
"""

SEVERITY_WEIGHT = {
    "critical": 10.0,
    "high": 7.0,
    "medium": 4.0,
    "low": 1.0,
}

EFFORT_INVERSE = {
    "small": 1.5,
    "medium": 1.0,
    "large": 0.7,
    "": 0.8,
}


def _exposure_factor(finding: dict) -> float:
    """Estimate exposure based on file location."""
    file_path = finding.get("file", "").lower()
    if "test" in file_path or "spec" in file_path:
        return 0.5
    if "config" in file_path or ".env" in file_path:
        return 1.2
    if file_path.endswith((".txt", ".cfg", ".toml", ".yaml", ".yml")):
        return 1.0
    # Source code in main app
    return 1.5


def score_findings(findings: list[dict]) -> list[dict]:
    """Add priority_score and priority_rank to each finding.

    Mutates findings in-place and returns them sorted by score descending.
    """
    for f in findings:
        severity = f.get("severity", "medium")
        effort = f.get("effort", "")
        sev_w = SEVERITY_WEIGHT.get(severity, 4.0)
        eff_w = EFFORT_INVERSE.get(effort, 0.8)
        exp_w = _exposure_factor(f)
        f["priority_score"] = round(sev_w * exp_w * eff_w, 2)

    active = [f for f in findings if not f.get("suppressed")]
    active.sort(key=lambda f: f.get("priority_score", 0), reverse=True)
    for rank, f in enumerate(active, 1):
        f["priority_rank"] = rank

    return findings
