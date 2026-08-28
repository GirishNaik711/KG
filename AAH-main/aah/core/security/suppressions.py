#!/usr/bin/env python3
"""Suppression management for security scan findings."""

from datetime import date, datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from aah.core.common.io_utils import read_yaml, write_yaml


def load_suppressions(rapids_path: Path) -> list[dict]:
    """Load suppressions from .aah/security/suppressions.yaml."""
    from aah.core.security.state import ensure_security_state

    # Preserve the pre-1.0 ``rapids_path=`` keyword compatibility surface.
    aah_path = ensure_security_state(rapids_path.parent).security_dir.parent
    supp_file = aah_path / "security" / "suppressions.yaml"
    if not supp_file.exists():
        return []
    data = read_yaml(supp_file)
    return data.get("suppressions", []) if isinstance(data, dict) else []


def save_suppressions(rapids_path: Path, suppressions: list[dict]) -> None:
    """Write suppressions to .aah/security/suppressions.yaml.

    No file locking is implemented. Concurrent writes from parallel scan
    processes are unsupported; scans are expected to run sequentially.
    """
    from aah.core.security.state import ensure_security_state

    # Preserve the pre-1.0 ``rapids_path=`` keyword compatibility surface.
    aah_path = ensure_security_state(rapids_path.parent).security_dir.parent
    supp_file = aah_path / "security" / "suppressions.yaml"
    write_yaml({"suppressions": suppressions}, supp_file)


def is_suppressed(finding: dict, suppression: dict) -> bool:
    """Check if a finding matches a suppression rule.

    Matching order:
    1. Exact finding_id match (tool-specific)
    2. CWE + file_pattern match (tool-agnostic fallback)
    """
    finding_id = suppression.get("finding_id")
    if finding_id:
        tool_rule = f"{finding['tool']}:{finding.get('rule_id', '')}"
        if tool_rule == finding_id:
            file_pattern = suppression.get("file", "")
            if not file_pattern or finding.get("file", "") == file_pattern:
                return True

    supp_cwe = suppression.get("cwe", "")
    supp_pattern = suppression.get("file_pattern", "")
    if supp_cwe and supp_pattern:
        finding_cwe = finding.get("cwe", "")
        finding_file = finding.get("file", "")
        if finding_cwe == supp_cwe and fnmatch(finding_file, supp_pattern):
            return True

    return False


def apply_suppressions(
    findings: list[dict], suppressions: list[dict]
) -> list[dict]:
    """Mark findings that match active suppressions as suppressed."""
    active = [s for s in suppressions if not _is_expired(s)]

    for finding in findings:
        for suppression in active:
            if is_suppressed(finding, suppression):
                finding["suppressed"] = True
                break

    return findings


def check_expired(suppressions: list[dict]) -> list[dict]:
    """Return suppressions that have passed their review_date."""
    return [s for s in suppressions if _is_expired(s)]


def _is_expired(suppression: dict) -> bool:
    """Check if a suppression has passed its review_date."""
    review = suppression.get("review_date")
    if not review:
        return False
    try:
        if isinstance(review, date) and not isinstance(review, datetime):
            return review < date.today()
        review_dt = datetime.fromisoformat(str(review))
        if review_dt.tzinfo is None:
            review_dt = review_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > review_dt
    except (ValueError, TypeError):
        return False
