#!/usr/bin/env python3
"""
Guided CLI authentication flow for the serverless deploy pipeline.

The existing cloud_credential_helper.py only CHECKS if credentials exist.
This module handles the FULL guided auth flow as a pipeline gate:
check → guide → re-validate → verify permissions.

Usage:
    aah run core.deploy.cloud_auth check --cloud gcp
    aah run core.deploy.cloud_auth validate-permissions --cloud aws --route ecs-express
    aah run core.deploy.cloud_auth get-context --cloud gcp
"""

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

_SHELL = platform.system() == "Windows"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run wrapper that uses shell=True on Windows for .cmd resolution."""
    return subprocess.run(cmd, shell=_SHELL, **kwargs)


# ---------------------------------------------------------------------------
# Route-specific IAM permissions
# ---------------------------------------------------------------------------

REQUIRED_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    "aws": {
        "ecs-express": [
            "AmazonECS_FullAccess (ecs:CreateService, ecs:RegisterTaskDefinition, ecs:UpdateService)",
            "AmazonEC2ContainerRegistryFullAccess (ecr:CreateRepository, ecr:PutImage)",
            "ElasticLoadBalancingFullAccess (elbv2:CreateLoadBalancer, elbv2:CreateTargetGroup)",
            "AmazonEC2FullAccess (ec2:CreateSecurityGroup, ec2:AuthorizeSecurityGroupIngress)",
            "CloudWatchLogsFullAccess (logs:CreateLogGroup, logs:PutLogEvents)",
            "AWSCodeBuildAdminAccess (codebuild:CreateProject, codebuild:StartBuild)",
            "AmazonS3FullAccess (s3:PutObject for CodeBuild source uploads)",
        ],
        "cloudless": [
            "BedrockAgentCoreFullAccess",
            "AmazonEC2ContainerRegistryFullAccess",
            "AmazonS3FullAccess",
            "CloudWatchLogsFullAccess",
        ],
        "agentcore": [
            "BedrockAgentCoreFullAccess (bedrock-agentcore:*, bedrock-agentcore-control:*)",
            "Bedrock model access enabled in the target region",
            "AdministratorAccess or CDK bootstrap perms (cloudformation:*, iam:*, s3:*, ecr:* — AgentCore CLI uses CDK)",
            "AWSAmplify* (amplify:CreateApp, CreateBranch, CreateDeployment, StartDeployment, UpdateApp, GetJob)",
            "AWSLambda_FullAccess + iam:CreateRole/PassRole + apigatewayv2:* (only if a browser frontend needs the signing proxy)",
        ],
    },
    "gcp": {
        "cloud-run": [
            "roles/run.admin (deploy, update services)",
            "roles/artifactregistry.admin (push container images)",
            "roles/cloudbuild.builds.editor (if using Cloud Build)",
            "roles/logging.viewer (for debug log reading)",
        ],
        "cloudless": [
            "roles/aiplatform.admin",
            "roles/storage.admin",
            "roles/iam.serviceAccountUser",
        ],
    },
}

AUTH_INSTRUCTIONS: dict[str, str] = {
    "aws": (
        "Run `! aws configure` (or `! aws sso login`) in your terminal to authenticate.\n"
        "Then set your default region: `! aws configure set region us-east-1`"
    ),
    "gcp": (
        "Run `! gcloud auth login` in your terminal to authenticate via browser.\n"
        "Then set your project: `! gcloud config set project <YOUR_PROJECT_ID>`"
    ),
}


# ---------------------------------------------------------------------------
# Authentication check
# ---------------------------------------------------------------------------


def authenticate(cloud: str, project_path: Path | None = None, profile: str | None = None) -> dict:
    """
    Full authentication gate.

    Steps:
    1. Check if credentials already valid (via CLI commands)
    2. If valid → return {authenticated: True, identity: {...}}
    3. If invalid → return {authenticated: False, instructions: "...", error: "..."}

    For AWS: if multiple profiles exist and no profile specified, returns
    the list of available profiles so the skill can ask the user to pick one.

    Args:
        cloud: "aws" or "gcp"
        project_path: optional project path for context
        profile: AWS profile name to use (if None, uses default or lists profiles)

    The skill layer handles AskUserQuestion for interactive guidance.
    After user authenticates, call this again to re-validate.

    Note: The `!` prefix in instructions is used because aws configure and
    gcloud auth login are interactive commands that need terminal access.
    """
    if cloud == "aws":
        return _check_aws_auth(profile=profile)
    elif cloud == "gcp":
        return _check_gcp_auth()
    else:
        return {
            "authenticated": False,
            "identity": None,
            "error": f"Unknown cloud: {cloud}",
            "instructions": None,
        }


def list_aws_profiles() -> list[dict]:
    """
    List all configured AWS CLI profile names.

    Does NOT validate each profile (no API calls). Just reads profile names
    from AWS CLI config. Validation happens AFTER user picks a profile.

    Returns list of {profile_name, is_default}
    """
    profiles = []

    # Get list of profile names (fast — reads local config only, no API calls)
    try:
        r = _run(
            ["aws", "configure", "list-profiles"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        profile_names = [p.strip() for p in r.stdout.strip().splitlines() if p.strip()]
    except Exception:
        return []

    for name in profile_names:
        profiles.append({
            "profile_name": name,
            "is_default": name == "default",
        })

    return profiles


def _check_aws_auth(profile: str | None = None) -> dict:
    """
    Check AWS credentials via sts get-caller-identity.

    If profile is specified, validates that specific profile.
    If profile is None:
      - ALWAYS lists available profiles first
      - If multiple profiles exist, returns the list so user can choose
      - If only one profile exists, auto-selects it
    """
    # If no profile specified: list profiles and return them for user selection
    # Do NOT validate — just list names (fast, no API calls)
    if not profile:
        profiles = list_aws_profiles()

        if len(profiles) == 0:
            return {
                "authenticated": False,
                "identity": None,
                "profile": None,
                "error": "No AWS profiles configured.",
                "instructions": AUTH_INSTRUCTIONS["aws"],
                "available_profiles": None,
            }

        # Always return profile list for user to confirm/select
        # Even if there's only one — user should confirm which profile to use
        return {
            "authenticated": False,
            "identity": None,
            "profile": None,
            "error": "Select an AWS profile for deployment.",
            "instructions": None,
            "available_profiles": profiles,
        }

    # Validate the selected (or auto-selected) profile
    cmd = ["aws", "sts", "get-caller-identity"]
    if profile:
        cmd.extend(["--profile", profile])

    try:
        r = _run(cmd, capture_output=True, text=True, timeout=15)

        if r.returncode == 0:
            identity = json.loads(r.stdout)
            return {
                "authenticated": True,
                "identity": {
                    "account_id": identity.get("Account", ""),
                    "arn": identity.get("Arn", ""),
                    "user_id": identity.get("UserId", ""),
                },
                "profile": profile or "default",
                "error": None,
                "instructions": None,
                "available_profiles": None,
            }

        return {
            "authenticated": False,
            "identity": None,
            "profile": profile,
            "error": r.stderr.strip()[:200] or "AWS credentials not configured",
            "instructions": AUTH_INSTRUCTIONS["aws"],
            "available_profiles": None,
        }
    except FileNotFoundError:
        return {
            "authenticated": False,
            "identity": None,
            "profile": None,
            "error": "AWS CLI not installed or not in PATH",
            "instructions": "Install AWS CLI: https://aws.amazon.com/cli/\n" + AUTH_INSTRUCTIONS["aws"],
            "available_profiles": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "authenticated": False,
            "identity": None,
            "profile": profile,
            "error": "AWS auth check timed out (network issue?)",
            "instructions": AUTH_INSTRUCTIONS["aws"],
            "available_profiles": None,
        }
    except Exception as e:
        return {
            "authenticated": False,
            "identity": None,
            "profile": profile,
            "error": str(e),
            "instructions": AUTH_INSTRUCTIONS["aws"],
            "available_profiles": None,
        }


def _check_gcp_auth() -> dict:
    """Check GCP credentials via gcloud auth print-access-token."""
    try:
        r = _run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            # Get account email
            acct = _run(
                ["gcloud", "config", "get-value", "account"],
                capture_output=True, text=True, timeout=10,
            )
            # Get project
            proj = _run(
                ["gcloud", "config", "get-value", "project"],
                capture_output=True, text=True, timeout=10,
            )
            account = acct.stdout.strip() if acct.returncode == 0 else ""
            project = proj.stdout.strip() if proj.returncode == 0 else ""

            if not project:
                return {
                    "authenticated": True,
                    "identity": {"email": account, "project": None},
                    "error": "Authenticated but no project set",
                    "instructions": "Run: `! gcloud config set project <YOUR_PROJECT_ID>`",
                }

            return {
                "authenticated": True,
                "identity": {"email": account, "project": project},
                "error": None,
                "instructions": None,
            }
        return {
            "authenticated": False,
            "identity": None,
            "error": r.stderr.strip()[:200] or "GCP credentials not configured",
            "instructions": AUTH_INSTRUCTIONS["gcp"],
        }
    except FileNotFoundError:
        return {
            "authenticated": False,
            "identity": None,
            "error": "gcloud CLI not installed or not in PATH",
            "instructions": "Install gcloud: https://cloud.google.com/sdk/docs/install\n" + AUTH_INSTRUCTIONS["gcp"],
        }
    except subprocess.TimeoutExpired:
        return {
            "authenticated": False,
            "identity": None,
            "error": "GCP auth check timed out (network issue?)",
            "instructions": AUTH_INSTRUCTIONS["gcp"],
        }
    except Exception as e:
        return {
            "authenticated": False,
            "identity": None,
            "error": str(e),
            "instructions": AUTH_INSTRUCTIONS["gcp"],
        }


# ---------------------------------------------------------------------------
# Permission validation
# ---------------------------------------------------------------------------


def validate_permissions(cloud: str, route: str, profile: str | None = None) -> dict:
    """
    After auth succeeds, verify required IAM policies/roles.

    This does a best-effort check — not all permissions can be verified
    programmatically. Returns the required list so the user can self-check.

    Returns:
        {
            "sufficient": bool,  # True if basic check passes
            "required_permissions": [...],
            "verified": [...],  # Permissions we could verify
            "instructions": str,
        }
    """
    cloud_perms = REQUIRED_PERMISSIONS.get(cloud, {})
    required = cloud_perms.get(route, cloud_perms.get("cloudless", []))

    if cloud == "aws":
        return _validate_aws_permissions(route, required, profile=profile)
    elif cloud == "gcp":
        return _validate_gcp_permissions(route, required)

    return {
        "sufficient": False,
        "required_permissions": required,
        "verified": [],
        "instructions": f"Unknown cloud: {cloud}",
    }


def _validate_aws_permissions(route: str, required: list[str], profile: str | None = None) -> dict:
    """
    Best-effort AWS permission check.
    Verifies basic access by testing a non-destructive API call.
    """
    verified = []
    profile_args = ["--profile", profile] if profile else []

    # Test ECR access (non-destructive)
    if route == "ecs-express":
        try:
            r = _run(
                ["aws", "ecr", "describe-repositories", "--max-items", "1"] + profile_args,
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 or "RepositoryNotFoundException" in r.stderr:
                verified.append("ECR access")
        except Exception:
            pass

        # Test ECS access
        try:
            r = _run(
                ["aws", "ecs", "list-clusters", "--max-results", "1"] + profile_args,
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                verified.append("ECS access")
        except Exception:
            pass

        # Test ELB access (needed for ALB creation)
        try:
            r = _run(
                ["aws", "elbv2", "describe-load-balancers", "--page-size", "1"] + profile_args,
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                verified.append("ELB access")
        except Exception:
            pass

        # Test CodeBuild access (needed for remote image builds)
        try:
            r = _run(
                ["aws", "codebuild", "list-projects", "--sort-by", "NAME", "--max-results", "1"] + profile_args,
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                verified.append("CodeBuild access")
        except Exception:
            pass

    # AgentCore route: probe Amplify + Bedrock AgentCore control-plane (non-destructive).
    # CDK/IAM perms can't be probed cheaply, so a single verified call is enough to pass.
    if route == "agentcore":
        try:
            r = _run(
                ["aws", "amplify", "list-apps", "--max-results", "1"] + profile_args,
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                verified.append("Amplify access")
        except Exception:
            pass
        try:
            r = _run(
                ["aws", "bedrock-agentcore-control", "list-agent-runtimes", "--max-results", "1"] + profile_args,
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                verified.append("Bedrock AgentCore access")
        except Exception:
            pass

    # cloudless + agentcore rely on CDK/managed runtimes whose perms can't be fully probed.
    sufficient = len(verified) > 0 or route in ("cloudless", "agentcore")

    return {
        "sufficient": sufficient,
        "required_permissions": required,
        "verified": verified,
        "instructions": (
            "Ensure the following IAM policies are attached to your user/role:\n"
            + "\n".join(f"  - {p}" for p in required)
        ) if not sufficient else "",
    }


def _validate_gcp_permissions(route: str, required: list[str]) -> dict:
    """
    Best-effort GCP permission check.
    Verifies basic access by testing non-destructive API calls.
    """
    verified = []

    if route == "cloud-run":
        # Test Cloud Run access
        try:
            r = _run(
                ["gcloud", "run", "services", "list", "--limit=1", "--format=json"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                verified.append("Cloud Run access")
        except Exception:
            pass

        # Test Artifact Registry access
        try:
            r = _run(
                ["gcloud", "artifacts", "repositories", "list", "--limit=1", "--format=json"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                verified.append("Artifact Registry access")
        except Exception:
            pass

    sufficient = len(verified) > 0 or route == "cloudless"

    return {
        "sufficient": sufficient,
        "required_permissions": required,
        "verified": verified,
        "instructions": (
            "Ensure the following IAM roles are granted to your account:\n"
            + "\n".join(f"  - {p}" for p in required)
        ) if not sufficient else "",
    }


# ---------------------------------------------------------------------------
# Cloud context extraction
# ---------------------------------------------------------------------------


def get_cloud_context(cloud: str) -> dict:
    """
    Extract account/project ID, region, and other context needed by deploy agents.

    Returns:
        AWS: {account_id, region, identity_arn}
        GCP: {project_id, region, identity_email}
    """
    if cloud == "aws":
        return _get_aws_context()
    elif cloud == "gcp":
        return _get_gcp_context()
    return {"error": f"Unknown cloud: {cloud}"}


def _get_aws_context() -> dict:
    """Extract AWS account ID, region, identity."""
    context = {"account_id": None, "region": None, "identity_arn": None}

    try:
        r = _run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            identity = json.loads(r.stdout)
            context["account_id"] = identity.get("Account")
            context["identity_arn"] = identity.get("Arn")
    except Exception:
        pass

    try:
        r = _run(
            ["aws", "configure", "get", "region"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            context["region"] = r.stdout.strip()
        else:
            context["region"] = "us-east-1"  # Default
    except Exception:
        context["region"] = "us-east-1"

    return context


def _get_gcp_context() -> dict:
    """Extract GCP project ID, region, identity."""
    context = {"project_id": None, "region": None, "identity_email": None}

    try:
        r = _run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            context["project_id"] = r.stdout.strip()
    except Exception:
        pass

    try:
        r = _run(
            ["gcloud", "config", "get-value", "compute/region"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            context["region"] = r.stdout.strip()
        else:
            context["region"] = "us-central1"  # Default
    except Exception:
        context["region"] = "us-central1"

    try:
        r = _run(
            ["gcloud", "config", "get-value", "account"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            context["identity_email"] = r.stdout.strip()
    except Exception:
        pass

    return context


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def discover_iam_roles(profile: str | None, region: str | None) -> dict:
    """
    Discover IAM roles required for ECS deployment.

    Checks well-known role names. Returns found ARNs or clear error
    messages explaining what's missing and how to create them.

    Called during the authenticate stage so the deploy skill can
    fail fast with actionable instructions instead of failing
    5 minutes into a CodeBuild run.

    Returns:
        {
            "codebuild_role": "arn:..." or null,
            "execution_role": "arn:..." or null,
            "errors": [...],
            "instructions": [...],
        }
    """
    from aah.core.deploy.ecs_deploy import discover_iam_roles as _discover

    # Get account ID for context
    ctx = _get_aws_context()
    account_id = ctx.get("account_id", "UNKNOWN")
    region = region or ctx.get("region", "us-east-1")
    profile = profile or "default"

    return _discover(profile, region, account_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Guided CLI authentication flow")
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Check if credentials are valid")
    check_p.add_argument("--cloud", type=str, required=True, choices=["aws", "gcp"])
    check_p.add_argument("--profile", type=str, default=None, help="AWS profile name to use")

    profiles_p = sub.add_parser("list-profiles", help="List available AWS profiles")

    val_p = sub.add_parser("validate-permissions", help="Verify IAM permissions")
    val_p.add_argument("--cloud", type=str, required=True, choices=["aws", "gcp"])
    val_p.add_argument("--route", type=str, required=True)
    val_p.add_argument("--profile", type=str, default=None, help="AWS profile name")

    ctx_p = sub.add_parser("get-context", help="Get cloud context (account, region)")
    ctx_p.add_argument("--cloud", type=str, required=True, choices=["aws", "gcp"])
    ctx_p.add_argument("--profile", type=str, default=None, help="AWS profile name")

    roles_p = sub.add_parser("discover-roles", help="Discover IAM roles for ECS deploy")
    roles_p.add_argument("--profile", type=str, default=None, help="AWS profile name")
    roles_p.add_argument("--region", type=str, default=None, help="AWS region")

    args = parser.parse_args()

    if args.command == "check":
        result = authenticate(args.cloud, profile=getattr(args, "profile", None))
        json.dump(result, sys.stdout, indent=2)
        print()
        # Exit 0 if authenticated OR if returning profile list (not a failure, just needs selection)
        if result["authenticated"] or result.get("available_profiles"):
            sys.exit(0)
        sys.exit(1)

    elif args.command == "list-profiles":
        profiles = list_aws_profiles()
        json.dump({"profiles": profiles, "count": len(profiles)}, sys.stdout, indent=2)
        print()

    elif args.command == "validate-permissions":
        result = validate_permissions(args.cloud, args.route, profile=getattr(args, "profile", None))
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["sufficient"] else 1)

    elif args.command == "get-context":
        result = get_cloud_context(args.cloud)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "discover-roles":
        result = discover_iam_roles(
            profile=getattr(args, "profile", None),
            region=getattr(args, "region", None),
        )
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if not result["errors"] else 1)


if __name__ == "__main__":
    main()
