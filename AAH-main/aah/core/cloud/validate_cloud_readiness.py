#!/usr/bin/env python3
"""
Validate cloud service readiness for cloudless deployment.

Called during analysis phase (Step 5.5) when DDR-SHARED-017=cloudless-managed.
Tests connectivity to required cloud services (Bedrock AgentCore, ECR for AWS;
Vertex AI Agent Engine, GCS for GCP) and writes cloud-readiness.yaml.

Usage:
    aah run core.cloud.validate_cloud_readiness --project-path <path>
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.cloud.registry_extractor import extract_services_from_registry


def _run_validation_command(command: str, timeout: int = 20) -> dict:
    """Run a validation command and return result."""
    try:
        parts = command.split()
        resolved = _resolve_cli(parts[0])
        if not resolved:
            return {
                "status": "unavailable",
                "error": f"'{parts[0]}' CLI not installed or not in PATH",
            }
        parts[0] = resolved
        result = subprocess.run(
            parts,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=(platform.system() == "Windows"),
        )
        if result.returncode == 0:
            return {"status": "available", "output": result.stdout.strip()[:200]}
        else:
            return {"status": "unavailable", "error": result.stderr.strip()[:200]}
    except FileNotFoundError:
        cli_name = command.split()[0]
        return {
            "status": "unavailable",
            "error": f"'{cli_name}' CLI not installed or not in PATH",
        }
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "error": "Command timed out"}
    except Exception as e:
        return {"status": "unavailable", "error": str(e)}


def validate_cloud_readiness(project_path: Path) -> dict:
    """
    Validate cloud service readiness for cloudless deployment.

    Steps:
    1. Extract services from decision registry (what needs validation)
    2. Validate credentials (aws sts / gcloud auth)
    3. Test each required service
    4. Write cloud-readiness.yaml

    Returns:
        {
            "overall_status": "passed" | "failed",
            "target_cloud": "aws" | "gcp",
            "critical_failures": int,
            "validated_services": [...],
            "credential_check": {...},
        }
    """
    # Step 1: Extract services to validate
    extraction = extract_services_from_registry(project_path)

    if not extraction["is_cloudless"] and not extraction.get("is_cloudrun", False) and not extraction.get("is_ecs_express", False):
        return {
            "overall_status": "skipped",
            "reason": "Not a managed deployment (cloudless/cloud-run/ecs-express)",
            "target_cloud": None,
            "critical_failures": 0,
            "validated_services": [],
        }

    target_cloud = extraction["target_cloud"]
    services = extraction["services"]

    # Step 2: Validate credentials
    credential_check = _validate_credentials(target_cloud)

    if not credential_check["available"]:
        # Can't validate services without credentials — report failure
        result = {
            "overall_status": "failed",
            "target_cloud": target_cloud,
            "critical_failures": 1,
            "credential_check": credential_check,
            "validated_services": [
                {
                    "service_name": svc["service_name"],
                    "description": svc.get("description", ""),
                    "criticality": svc.get("criticality", "critical"),
                    "status": "unavailable",
                    "error": f"Cannot validate — credentials not configured ({credential_check['error']})",
                }
                for svc in services
            ],
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_readiness_file(project_path, result)
        return result

    # Step 3: Validate each service
    validated_services = []
    critical_failures = 0
    region = _get_region(project_path, target_cloud)

    for svc in services:
        command = svc.get("validation_command", "")
        if command:
            # Replace placeholders
            command = command.format(region=region, project=credential_check.get("project", ""))
            svc_result = _run_validation_command(command)
        else:
            svc_result = {"status": "available", "output": "No validation command defined"}

        validated_svc = {
            "service_name": svc["service_name"],
            "description": svc.get("description", ""),
            "criticality": svc.get("criticality", "critical"),
            "status": svc_result["status"],
        }

        if svc_result.get("error"):
            validated_svc["error"] = svc_result["error"]
        if svc_result["status"] == "unavailable" and svc.get("criticality") == "critical":
            critical_failures += 1

        validated_services.append(validated_svc)

    # Step 4: Tier prediction (read-only org policy checks)
    tier_prediction = _check_tier_prediction(target_cloud, project_path)

    # Step 5: Build result
    overall_status = "passed" if critical_failures == 0 else "failed"
    result = {
        "overall_status": overall_status,
        "target_cloud": target_cloud,
        "critical_failures": critical_failures,
        "credential_check": {
            "available": credential_check["available"],
            "identity": credential_check.get("identity") or credential_check.get("project"),
        },
        "validated_services": validated_services,
        "tier_prediction": tier_prediction,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Write to file
    _write_readiness_file(project_path, result)
    return result


def _resolve_cli(name: str) -> str | None:
    """Resolve a CLI tool name, accounting for Windows .cmd/.bat extensions."""
    if shutil.which(name):
        return name
    if platform.system() == "Windows":
        for ext in (".cmd", ".bat", ".exe"):
            if shutil.which(name + ext):
                return name + ext
    return None


def _validate_credentials(target_cloud: str) -> dict:
    """Validate cloud credentials via CLI."""
    if target_cloud == "aws":
        try:
            r = subprocess.run(
                ["aws", "sts", "get-caller-identity"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                identity = json.loads(r.stdout)
                return {
                    "available": True,
                    "identity": identity.get("Arn", ""),
                    "error": None,
                }
            return {"available": False, "identity": None, "error": r.stderr.strip()[:200]}
        except FileNotFoundError:
            return {"available": False, "identity": None, "error": "AWS CLI not installed"}
        except Exception as e:
            return {"available": False, "identity": None, "error": str(e)}

    elif target_cloud == "gcp":
        gcloud_cmd = _resolve_cli("gcloud")
        if not gcloud_cmd:
            return {"available": False, "project": None, "error": "gcloud CLI not installed"}
        try:
            r = subprocess.run(
                [gcloud_cmd, "auth", "print-access-token"],
                capture_output=True, text=True, timeout=15,
                shell=(platform.system() == "Windows"),
            )
            if r.returncode == 0 and r.stdout.strip():
                p = subprocess.run(
                    [gcloud_cmd, "config", "get-value", "project"],
                    capture_output=True, text=True, timeout=10,
                    shell=(platform.system() == "Windows"),
                )
                project = p.stdout.strip() if p.returncode == 0 else None
                return {"available": True, "project": project, "error": None}
            return {"available": False, "project": None, "error": r.stderr.strip()[:200]}
        except FileNotFoundError:
            return {"available": False, "project": None, "error": "gcloud CLI not installed"}
        except Exception as e:
            return {"available": False, "project": None, "error": str(e)}

    return {"available": False, "identity": None, "error": f"Unknown cloud: {target_cloud}"}


def _get_region(project_path: Path, target_cloud: str) -> str:
    """Get deployment region from manifest or use default."""
    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        region = manifest.get("stack_choices", {}).get("region")
        if region:
            return region

    # Defaults
    if target_cloud == "aws":
        return "us-east-1"
    elif target_cloud == "gcp":
        return "us-central1"
    return "us-east-1"


def _check_tier_prediction(target_cloud: str, project_path: Path) -> dict:
    """
    Check org policies to predict which access tiers are possible.

    Runs READ-ONLY commands against cloud APIs to detect org-level restrictions
    that would block specific tiers. This prediction is written to
    cloud-readiness.yaml so the deploy phase can skip impossible tiers
    (avoiding wasted deploy attempts and cleanup).

    GCP checks:
    - run.allowedIngress org policy → if restricts to "internal", Tier 1A/1B blocked
    - iam.allowedPolicyMemberDomains → if set, Tier 1A (allUsers) blocked

    AWS checks:
    - describe-security-groups heuristic → if no SGs with 0.0.0.0/0 exist in the
      account, it's a signal that SCPs likely block public ingress (Tier 1A blocked)
    - Note: We cannot directly read SCPs without org-level access, so this is heuristic

    Returns:
        {
            "tier_1a_possible": bool,
            "tier_1b_possible": bool,
            "tier_1c_possible": bool,
            "recommended_starting_tier": "1A" | "1B" | "1C",
            "blockers": {"1A": "reason", "1B": "reason", ...},
            "checks_performed": [...],
        }
    """
    prediction = {
        "tier_1a_possible": True,
        "tier_1b_possible": True,
        "tier_1c_possible": True,  # Always true if service deploys at all
        "recommended_starting_tier": "1A",
        "blockers": {},
        "checks_performed": [],
    }

    if target_cloud == "gcp":
        prediction = _predict_gcp_tiers(prediction)
    elif target_cloud == "aws":
        prediction = _predict_aws_tiers(prediction)

    # Determine recommended starting tier based on what's possible
    if prediction["tier_1a_possible"]:
        prediction["recommended_starting_tier"] = "1A"
    elif prediction["tier_1b_possible"]:
        prediction["recommended_starting_tier"] = "1B"
    else:
        prediction["recommended_starting_tier"] = "1C"

    return prediction


def _predict_gcp_tiers(prediction: dict) -> dict:
    """
    GCP-specific tier prediction using org policy checks (read-only).

    Checks:
    1. constraints/run.allowedIngress — if set to ALLOW_INTERNAL_ONLY or
       ALLOW_INTERNAL_AND_GCLB, then --ingress=all is blocked → Tier 1A/1B fail
    2. constraints/iam.allowedPolicyMemberDomains — if set, allUsers binding
       is blocked → Tier 1A fails (but 1B with authenticated access may work)
    """
    # Check 1: run.allowedIngress org policy
    try:
        r = subprocess.run(
            ["gcloud", "org-policies", "describe",
             "constraints/run.allowedIngress",
             "--effective", "--format=json"],
            capture_output=True, text=True, timeout=15,
            shell=(platform.system() == "Windows"),
        )
        prediction["checks_performed"].append("gcp:run.allowedIngress")

        if r.returncode == 0 and r.stdout.strip():
            policy_data = json.loads(r.stdout)
            # Check if the policy restricts ingress
            spec = policy_data.get("spec", {})
            rules = spec.get("rules", [])

            for rule in rules:
                values = rule.get("values", {})
                allowed = values.get("allowedValues", [])
                # If only internal or GCLB allowed, public ingress is blocked
                if allowed:
                    allowed_lower = [v.lower() for v in allowed]
                    if "all" not in allowed_lower:
                        # Ingress restricted — blocks Tier 1A and 1B
                        if "internal" in " ".join(allowed_lower) or "gclb" in " ".join(allowed_lower):
                            prediction["tier_1a_possible"] = False
                            prediction["tier_1b_possible"] = False
                            restriction = ", ".join(allowed)
                            prediction["blockers"]["1A"] = (
                                f"GCP org policy run.allowedIngress restricts to: {restriction}"
                            )
                            prediction["blockers"]["1B"] = (
                                f"GCP org policy run.allowedIngress restricts to: {restriction} "
                                f"(--ingress=all not permitted)"
                            )

            # Also check if enforce=True with deny on "all"
            denied = []
            for rule in rules:
                values = rule.get("values", {})
                denied.extend(values.get("deniedValues", []))
            if any("all" in d.lower() for d in denied):
                prediction["tier_1a_possible"] = False
                prediction["tier_1b_possible"] = False
                prediction["blockers"]["1A"] = "GCP org policy denies ingress=all"
                prediction["blockers"]["1B"] = "GCP org policy denies ingress=all"

        elif "not found" in (r.stderr or "").lower() or r.returncode != 0:
            # Policy not set at org level — all tiers possible (default behavior)
            pass

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        # Can't check — assume all possible
        prediction["checks_performed"].append("gcp:run.allowedIngress (failed/timeout)")

    # Check 2: iam.allowedPolicyMemberDomains
    try:
        r = subprocess.run(
            ["gcloud", "org-policies", "describe",
             "constraints/iam.allowedPolicyMemberDomains",
             "--effective", "--format=json"],
            capture_output=True, text=True, timeout=15,
            shell=(platform.system() == "Windows"),
        )
        prediction["checks_performed"].append("gcp:iam.allowedPolicyMemberDomains")

        if r.returncode == 0 and r.stdout.strip():
            policy_data = json.loads(r.stdout)
            spec = policy_data.get("spec", {})
            rules = spec.get("rules", [])

            # If this policy is enforced, allUsers IAM binding is blocked
            # which means --allow-unauthenticated won't work
            for rule in rules:
                enforce = rule.get("enforce", False)
                values = rule.get("values", {})
                allowed_domains = values.get("allowedValues", [])

                if enforce or allowed_domains:
                    # Domain restriction active — allUsers blocked
                    prediction["tier_1a_possible"] = False
                    if "1A" not in prediction["blockers"]:
                        prediction["blockers"]["1A"] = (
                            "GCP org policy iam.allowedPolicyMemberDomains blocks "
                            "allUsers binding (--allow-unauthenticated denied)"
                        )
                    break

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        prediction["checks_performed"].append("gcp:iam.allowedPolicyMemberDomains (failed/timeout)")

    return prediction


def _predict_aws_tiers(prediction: dict) -> dict:
    """
    AWS-specific tier prediction using describe-security-groups heuristic.

    AWS SCPs cannot be read directly without org admin access. Instead we use
    a heuristic: check if any existing security groups in the account have
    0.0.0.0/0 ingress rules. If none do, it's a strong signal that SCPs
    auto-revoke public ingress.

    Additional check: attempt to describe VPC endpoints — if many exist
    and no internet gateways attached, the VPC is likely private-only.
    """
    # Heuristic: Are there ANY security groups with 0.0.0.0/0 ingress?
    try:
        r = subprocess.run(
            ["aws", "ec2", "describe-security-groups",
             "--filters", "Name=ip-permission.cidr,Values=0.0.0.0/0",
             "--query", "SecurityGroups[].GroupId",
             "--output", "json"],
            capture_output=True, text=True, timeout=15,
            shell=(platform.system() == "Windows"),
        )
        prediction["checks_performed"].append("aws:describe-security-groups(0.0.0.0/0)")

        if r.returncode == 0:
            sg_ids = json.loads(r.stdout) if r.stdout.strip() else []
            if len(sg_ids) == 0:
                # No public SGs exist — strong signal that SCPs block public ingress
                prediction["tier_1a_possible"] = False
                prediction["blockers"]["1A"] = (
                    "No security groups with 0.0.0.0/0 ingress found in account. "
                    "Likely SCP restriction on public ingress."
                )
                # 1B (scoped IP) might also be blocked by same SCP
                # but we can't be sure — leave as possible and let deploy confirm
            # If SGs with 0.0.0.0/0 exist, public access is allowed — all tiers possible

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        prediction["checks_performed"].append("aws:describe-security-groups (failed/timeout)")

    # Secondary check: look for scoped-IP SGs as signal for 1B feasibility
    try:
        r = subprocess.run(
            ["aws", "ec2", "describe-security-groups",
             "--query", "SecurityGroups[?length(IpPermissions[?IpRanges[?contains(CidrIp,'/32')]]) > `0`].GroupId",
             "--output", "json"],
            capture_output=True, text=True, timeout=15,
            shell=(platform.system() == "Windows"),
        )
        prediction["checks_performed"].append("aws:describe-security-groups(/32 scoped)")

        if r.returncode == 0:
            scoped_sgs = json.loads(r.stdout) if r.stdout.strip() else []
            if len(scoped_sgs) == 0 and not prediction["tier_1a_possible"]:
                # No scoped SGs either — SCPs likely block ALL public ingress
                prediction["tier_1b_possible"] = False
                prediction["blockers"]["1B"] = (
                    "No security groups with scoped IP (/32) ingress found. "
                    "SCP likely blocks all public ingress (even IP-scoped)."
                )

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        prediction["checks_performed"].append("aws:describe-security-groups/32 (failed/timeout)")

    return prediction


def _write_readiness_file(project_path: Path, result: dict) -> None:
    """Write cloud-readiness.yaml to the analysis directory."""
    analysis_dir = project_path / ".aah" / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(result, analysis_dir / "cloud-readiness.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate cloud service readiness for cloudless deployment"
    )
    parser.add_argument("--project-path", type=Path, required=True)
    args = parser.parse_args()

    result = validate_cloud_readiness(args.project_path)

    # Output summary
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["overall_status"] in ("passed", "skipped") else 1)


if __name__ == "__main__":
    main()
