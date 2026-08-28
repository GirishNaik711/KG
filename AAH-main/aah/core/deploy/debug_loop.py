#!/usr/bin/env python3
"""
Platform log fetch and deployment failure diagnosis.

Fetches logs from the deployed platform (Cloud Run / ECS Express) and
pattern-matches common deployment failures to suggest fixes.

Used by the deploy pipeline when health check fails after deployment.

Usage:
    aah run core.deploy.debug_loop fetch-logs --target cloud-run --service my-backend --region us-central1
    aah run core.deploy.debug_loop diagnose --logs <json-logs>
"""

import argparse
import json
import re
import subprocess
import sys


# ---------------------------------------------------------------------------
# Error patterns and suggested fixes
# ---------------------------------------------------------------------------

ERROR_PATTERNS: list[dict] = [
    {
        "error_type": "port_mismatch",
        "patterns": [
            r"Connection refused.*:(\d+)",
            r"bind.*address already in use",
            r"listen EADDRINUSE",
            r"failed to listen on port",
        ],
        "suggested_fix": "Update Dockerfile CMD or app code to listen on $PORT env var (Cloud Run/ECS injects this)",
        "confidence": "high",
    },
    {
        "error_type": "module_not_found",
        "patterns": [
            r"ModuleNotFoundError: No module named '([^']+)'",
            r"ImportError: cannot import name '([^']+)'",
            r"Cannot find module '([^']+)'",
            r"Error: Cannot find module",
        ],
        "suggested_fix": "Add missing package to requirements.txt / pyproject.toml / package.json and rebuild",
        "confidence": "high",
    },
    {
        "error_type": "missing_env_var",
        "patterns": [
            r"KeyError: '([^']+)'",
            r"Environment variable '?([A-Z_]+)'? (?:is )?not set",
            r"Missing required environment variable:? '?([A-Z_]+)'?",
            r"undefined.*process\.env\.([A-Z_]+)",
        ],
        "suggested_fix": "Add missing environment variable to deploy command --set-env-vars",
        "confidence": "high",
    },
    {
        "error_type": "oom_killed",
        "patterns": [
            r"OOMKilled",
            r"Memory limit.*exceeded",
            r"out of memory",
            r"SIGKILL.*memory",
        ],
        "suggested_fix": "Increase --memory allocation (e.g., --memory 1Gi) in deploy command",
        "confidence": "high",
    },
    {
        "error_type": "permission_denied",
        "patterns": [
            r"PermissionDenied",
            r"permission denied",
            r"Access denied",
            r"403 Forbidden",
            r"UnauthorizedOperation",
        ],
        "suggested_fix": "Check IAM permissions — ensure service account has required roles",
        "confidence": "medium",
    },
    {
        "error_type": "timeout",
        "patterns": [
            r"deadline exceeded",
            r"timeout.*(\d+)s",
            r"request timeout",
            r"context deadline exceeded",
        ],
        "suggested_fix": "Increase request timeout (Cloud Run: --timeout, ECS: task timeout) or optimize startup",
        "confidence": "medium",
    },
    {
        "error_type": "crash_loop",
        "patterns": [
            r"container.*exited with code [1-9]",
            r"Back-off restarting",
            r"CrashLoopBackOff",
            r"process exited with status",
        ],
        "suggested_fix": "Application is crashing on startup — check the full stack trace above for root cause",
        "confidence": "medium",
    },
    {
        "error_type": "import_error",
        "patterns": [
            r"ImportError: attempted relative import",
            r"ImportError: circular import",
            r"SyntaxError:",
            r"IndentationError:",
        ],
        "suggested_fix": "Fix Python import issue — check for circular imports or syntax errors",
        "confidence": "high",
    },
    {
        "error_type": "file_not_found",
        "patterns": [
            r"FileNotFoundError:.*'([^']+)'",
            r"ENOENT.*no such file",
            r"No such file or directory",
        ],
        "suggested_fix": "File referenced in code doesn't exist in the container — check Dockerfile COPY statements",
        "confidence": "medium",
    },
]


# ---------------------------------------------------------------------------
# Log fetching
# ---------------------------------------------------------------------------

def fetch_logs(target: str, service_name: str, region: str, **config) -> str:
    """
    Fetch platform logs using the target-specific command from serverless_registry.

    Args:
        target: "cloud-run" or "ecs-express"
        service_name: deployed service name
        region: cloud region
        **config: additional config (project_id for GCP, etc.)

    Returns:
        Raw log text (or error message if fetch fails)
    """
    from aah.core.deploy.serverless_registry import get_target

    target_config = get_target(target)
    if not target_config:
        return f"ERROR: Unknown target '{target}'"

    log_cmd = target_config["log_fetch_cmd"].format(
        service=service_name,
        region=region,
        **config,
    )

    try:
        r = subprocess.run(
            log_cmd.split(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return r.stdout
        return f"Log fetch failed (exit {r.returncode}): {r.stderr[:500]}"
    except FileNotFoundError:
        cli_name = log_cmd.split()[0]
        return f"ERROR: '{cli_name}' CLI not found. Ensure cloud CLI is installed."
    except subprocess.TimeoutExpired:
        return "ERROR: Log fetch timed out after 30s"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

def diagnose(logs: str) -> list[dict]:
    """
    Pattern-match common deployment failures from log text.

    Returns list of diagnosed issues, sorted by confidence:
    [{error_type, pattern_matched, suggested_fix, confidence, extracted_value}]
    """
    if not logs or logs.startswith("ERROR:"):
        return [{
            "error_type": "log_fetch_failed",
            "pattern_matched": logs[:200] if logs else "Empty logs",
            "suggested_fix": "Could not fetch logs — check cloud CLI auth and service name",
            "confidence": "low",
            "extracted_value": None,
        }]

    diagnoses = []

    for pattern_group in ERROR_PATTERNS:
        for pattern in pattern_group["patterns"]:
            match = re.search(pattern, logs, re.IGNORECASE | re.MULTILINE)
            if match:
                extracted = match.group(1) if match.lastindex and match.lastindex >= 1 else None
                diagnoses.append({
                    "error_type": pattern_group["error_type"],
                    "pattern_matched": match.group(0)[:200],
                    "suggested_fix": pattern_group["suggested_fix"],
                    "confidence": pattern_group["confidence"],
                    "extracted_value": extracted,
                })
                break  # One match per error type is enough

    # Sort by confidence (high first)
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    diagnoses.sort(key=lambda d: confidence_order.get(d["confidence"], 3))

    if not diagnoses:
        diagnoses.append({
            "error_type": "unknown",
            "pattern_matched": None,
            "suggested_fix": "No known error pattern matched. Review the full logs for stack traces.",
            "confidence": "low",
            "extracted_value": None,
        })

    return diagnoses


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Platform log fetch and failure diagnosis")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser("fetch-logs", help="Fetch logs from deployed platform")
    fetch_p.add_argument("--target", type=str, required=True, choices=["cloud-run", "ecs-express"])
    fetch_p.add_argument("--service", type=str, required=True)
    fetch_p.add_argument("--region", type=str, required=True)
    fetch_p.add_argument("--project", type=str, default="")

    diag_p = sub.add_parser("diagnose", help="Diagnose from log text")
    diag_p.add_argument("--logs", type=str, default=None, help="Log text (or read from stdin)")

    args = parser.parse_args()

    if args.command == "fetch-logs":
        logs = fetch_logs(args.target, args.service, args.region, project=args.project)
        print(logs)

    elif args.command == "diagnose":
        if args.logs:
            logs = args.logs
        else:
            logs = sys.stdin.read()

        results = diagnose(logs)
        json.dump({"diagnoses": results, "count": len(results)}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
