#!/usr/bin/env python3
"""Evaluate security scan findings against severity policy."""

import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_yaml

DEFAULT_POLICY = {
    "policy_version": "2.0",
    "severity_thresholds": {
        "critical": "block",
        "high": "block",
        "medium": "warn",
        "low": "ignore",
    },
    "scan_types": {
        "sast": True,
        "secrets": True,
        "deps": True,
        "licenses": True,
        "containers": False,
        "iac": True,
    },
    "extended_scan_types": {
        "deep_sast": True,
        "sbom": True,
        "supply_chain_signing": False,
        "dast_crawl": False,
        "dast_templates": False,
        "continuous_monitoring": False,
        "runtime_monitoring": False,
        "logging_validation": False,
    },
    "dast": {
        "target_url": "",
        "zap_scan_mode": "baseline",
        "nuclei_tags": ["cve", "misconfig", "exposure"],
        "auth_context": "",
    },
    "sbom": {
        "format": "cyclonedx",
        "sign_artifacts": False,
    },
    "suppression_review_period_days": 90,
    "max_age_hours": 24,
}

SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def load_policy(rapids_path: Path) -> dict:
    """Load policy from .aah/security/policy.yaml or return defaults.

    Projects can create .aah/security/policy.yaml to override any field
    in DEFAULT_POLICY. The file is merged with defaults — only specified
    fields are overridden, unspecified fields retain their default values.
    """
    from aah.core.security.state import ensure_security_state

    # Preserve the pre-1.0 ``rapids_path=`` keyword while canonicalizing the
    # supplied state root to .aah.
    aah_path = ensure_security_state(rapids_path.parent).security_dir.parent
    policy_file = aah_path / "security" / "policy.yaml"
    if policy_file.exists():
        loaded = read_yaml(policy_file) or {}
        merged = {**DEFAULT_POLICY, **loaded}
        for nested_key in (
            "severity_thresholds",
            "scan_types",
            "extended_scan_types",
            "dast",
            "sbom",
        ):
            merged[nested_key] = {
                **DEFAULT_POLICY[nested_key],
                **(loaded.get(nested_key) or {}),
            }
        unknown_top = set(loaded.keys()) - set(DEFAULT_POLICY.keys())
        for key in sorted(unknown_top):
            print(f"Warning: unknown policy key '{key}' in {policy_file}", file=sys.stderr)

        for nested_key in ("severity_thresholds", "scan_types", "extended_scan_types", "dast", "sbom"):
            nested_loaded = loaded.get(nested_key)
            if isinstance(nested_loaded, dict):
                unknown_nested = set(nested_loaded.keys()) - set(DEFAULT_POLICY[nested_key].keys())
                for key in sorted(unknown_nested):
                    print(
                        f"Warning: unknown policy key '{nested_key}.{key}' in {policy_file}",
                        file=sys.stderr,
                    )

        return merged
    return dict(DEFAULT_POLICY)


def evaluate_policy(findings: list[dict], errors: list[dict], policy: dict) -> dict:
    """Evaluate findings against policy thresholds.

    Returns the policy_result dict for inclusion in scan results.
    """
    thresholds = policy.get(
        "severity_thresholds", DEFAULT_POLICY["severity_thresholds"]
    )
    max_age = policy.get("max_age_hours", 24)

    active_findings = [f for f in findings if not f.get("suppressed", False)]

    blocking = []
    for finding in active_findings:
        severity = finding.get("severity", "medium").lower()
        action = thresholds.get(severity, "ignore")
        if action == "block":
            blocking.append(finding)

    if errors:
        return {
            "passed": False,
            "reason": "scan_incomplete",
            "threshold": thresholds,
            "blocking_findings": len(blocking),
            "max_age_hours": max_age,
        }

    if blocking:
        return {
            "passed": False,
            "reason": "blocking_findings",
            "threshold": thresholds,
            "blocking_findings": len(blocking),
            "max_age_hours": max_age,
        }

    if not findings and not errors:
        return {
            "passed": False,
            "reason": "no_scan_data",
            "threshold": thresholds,
            "blocking_findings": 0,
            "max_age_hours": max_age,
        }

    return {
        "passed": True,
        "reason": "all_clear",
        "threshold": thresholds,
        "blocking_findings": 0,
        "max_age_hours": max_age,
    }


def check_staleness(scan_timestamp: str, max_age_hours: int) -> bool:
    """Return True if the scan result is stale (older than max_age_hours)."""
    try:
        scan_time = datetime.fromisoformat(scan_timestamp)
        if scan_time.tzinfo is None:
            scan_time = scan_time.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - scan_time
        return age.total_seconds() > max_age_hours * 3600
    except (ValueError, TypeError):
        return True
