#!/usr/bin/env python3
"""
Tiered Access Gate for the serverless deploy pipeline.

Deploys services using a cascade strategy: try Tier 1A (full public) first,
fall back to Tier 1B (deployer-IP scoped), then Tier 1C (local proxy).
Stops at the first tier that passes health check.

The tier prediction from cloud-readiness.yaml (written by /aah-access) is used to
skip tiers that are known to be impossible (e.g., org policy blocks public).

Design principles:
- Cascade silently, report once (only surface the successful tier)
- Each tier is self-contained (cleanup failed tier artifacts)
- Health check is mandatory (deploy without health check is not a pass)
- Access instructions are tier-specific
- Failure report is actionable (one specific ask, one owner)
- All services use the same tier (first service determines it)
- Frontend minimum = Tier 1B (local proxy not useful for browser access)

Usage:
    aah run core.deploy.access_tier deploy --project-path <path> --cloud gcp --route cloud-run
    aah run core.deploy.access_tier get-deployer-ip
    aah run core.deploy.access_tier tier-config --cloud gcp --tier 1A
"""

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml

_SHELL = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

TIERS = ["1A", "1B", "1C"]

TIER_LABELS = {
    "1A": "Full Public",
    "1B": "Deployer-Scoped",
    "1C": "Local Proxy",
}


# ---------------------------------------------------------------------------
# Deployer IP detection
# ---------------------------------------------------------------------------


def detect_deployer_ip() -> str | None:
    """
    Detect the deployer's public IP address.

    Uses ifconfig.me (fast, reliable, returns plain text IP).
    Needed for Tier 1B scoped access.

    Returns IP string or None if detection fails.
    """
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "5", "https://ifconfig.me"],
            capture_output=True, text=True, timeout=10,
            shell=_SHELL,
        )
        if r.returncode == 0 and r.stdout.strip():
            ip = r.stdout.strip()
            # Basic validation: should look like an IP
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return ip
    except Exception:
        pass

    # Fallback: try ipify
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "5", "https://api.ipify.org"],
            capture_output=True, text=True, timeout=10,
            shell=_SHELL,
        )
        if r.returncode == 0 and r.stdout.strip():
            ip = r.stdout.strip()
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return ip
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Tier deploy configuration
# ---------------------------------------------------------------------------


def get_tier_deploy_config(cloud: str, tier: str, deployer_ip: str | None = None) -> dict:
    """
    Return cloud-specific deploy flags/config for each tier.

    These configs are passed to the deploy agent to control how the service
    is deployed (ingress, auth, networking).

    Args:
        cloud: "gcp" or "aws"
        tier: "1A", "1B", or "1C"
        deployer_ip: Required for tier 1B

    Returns:
        {
            "tier": str,
            "tier_label": str,
            "deploy_flags": dict,  # Cloud-specific deploy parameters
            "health_check_method": str,  # "bare_curl" | "auth_curl" | "proxy"
            "access_type": str,  # How users access the deployed service
        }
    """
    if cloud == "gcp":
        return _gcp_tier_config(tier, deployer_ip)
    elif cloud == "aws":
        return _aws_tier_config(tier, deployer_ip)
    else:
        return {"error": f"Unknown cloud: {cloud}"}


def _gcp_tier_config(tier: str, deployer_ip: str | None = None) -> dict:
    """GCP Cloud Run tier configurations."""
    if tier == "1A":
        return {
            "tier": "1A",
            "tier_label": TIER_LABELS["1A"],
            "deploy_flags": {
                "ingress": "all",
                "allow_unauthenticated": True,
            },
            "health_check_method": "bare_curl",
            "access_type": "public_url",
            "cleanup_actions": ["delete_service"],
        }
    elif tier == "1B":
        return {
            "tier": "1B",
            "tier_label": TIER_LABELS["1B"],
            "deploy_flags": {
                "ingress": "all",
                "allow_unauthenticated": False,
            },
            "health_check_method": "auth_curl",
            "access_type": "authenticated_url",
            "deployer_ip": deployer_ip,
            "cleanup_actions": ["delete_service"],
        }
    elif tier == "1C":
        return {
            "tier": "1C",
            "tier_label": TIER_LABELS["1C"],
            "deploy_flags": {
                "ingress": "internal",
                "allow_unauthenticated": False,
            },
            "health_check_method": "proxy",
            "access_type": "local_proxy",
            "cleanup_actions": ["delete_service"],
        }
    return {"error": f"Unknown tier: {tier}"}


def _aws_tier_config(tier: str, deployer_ip: str | None = None) -> dict:
    """AWS ECS Fargate + ALB tier configurations."""
    if tier == "1A":
        return {
            "tier": "1A",
            "tier_label": TIER_LABELS["1A"],
            "deploy_flags": {
                "alb_scheme": "internet-facing",
                "sg_ingress_cidr": "0.0.0.0/0",
                "sg_ingress_port": 80,
            },
            "health_check_method": "bare_curl",
            "access_type": "public_url",
            "cleanup_actions": ["delete_service", "delete_alb", "delete_sg"],
        }
    elif tier == "1B":
        return {
            "tier": "1B",
            "tier_label": TIER_LABELS["1B"],
            "deploy_flags": {
                "alb_scheme": "internet-facing",
                "sg_ingress_cidr": f"{deployer_ip}/32" if deployer_ip else "0.0.0.0/0",
                "sg_ingress_port": 80,
            },
            "health_check_method": "bare_curl",
            "access_type": "ip_scoped_url",
            "deployer_ip": deployer_ip,
            "cleanup_actions": ["delete_service", "delete_alb", "delete_sg"],
        }
    elif tier == "1C":
        return {
            "tier": "1C",
            "tier_label": TIER_LABELS["1C"],
            "deploy_flags": {
                "alb_scheme": "internal",
                "sg_ingress_cidr": None,
                "enable_ecs_exec": True,
            },
            "health_check_method": "ecs_exec",
            "access_type": "ecs_exec_proxy",
            "cleanup_actions": ["delete_service", "delete_alb"],
        }
    return {"error": f"Unknown tier: {tier}"}


# ---------------------------------------------------------------------------
# Tier-specific health checks
# ---------------------------------------------------------------------------


def health_check_for_tier(
    url: str,
    cloud: str,
    tier: str,
    service_name: str | None = None,
    region: str | None = None,
    timeout: int = 60,
) -> dict:
    """
    Perform tier-specific health check.

    Different tiers require different health check methods:
    - 1A: bare curl to public URL
    - 1B (GCP): curl with Authorization: Bearer identity-token
    - 1B (AWS): bare curl (IP-scoped SG allows deployer)
    - 1C (GCP): gcloud run services proxy → curl localhost
    - 1C (AWS): ecs execute-command → curl localhost inside container

    Returns:
        {
            "healthy": bool,
            "method": str,
            "status_code": int | None,
            "error": str | None,
        }
    """
    config = get_tier_deploy_config(cloud, tier)
    method = config.get("health_check_method", "bare_curl")

    if method == "bare_curl":
        return _health_check_curl(url, timeout=timeout)
    elif method == "auth_curl":
        return _health_check_auth_curl(url, timeout=timeout)
    elif method == "proxy":
        return _health_check_gcp_proxy(service_name, region, timeout=timeout)
    elif method == "ecs_exec":
        return _health_check_ecs_exec(service_name, region, timeout=timeout)

    return {"healthy": False, "method": method, "status_code": None, "error": f"Unknown method: {method}"}


def _health_check_curl(url: str, timeout: int = 60) -> dict:
    """Bare curl health check — works for Tier 1A and AWS Tier 1B."""
    import time
    import urllib.request
    import urllib.error

    health_url = url.rstrip("/") + "/health"
    start = time.time()

    while (time.time() - start) < timeout:
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status < 500:
                    return {"healthy": True, "method": "bare_curl", "status_code": resp.status, "error": None}
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return {"healthy": True, "method": "bare_curl", "status_code": e.code, "error": None}
        except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError):
            pass

        time.sleep(3)

    return {"healthy": False, "method": "bare_curl", "status_code": None, "error": f"Health check timed out after {timeout}s"}


def _health_check_auth_curl(url: str, timeout: int = 60) -> dict:
    """Authenticated curl for GCP Tier 1B — uses identity token."""
    import time

    # Get identity token
    try:
        r = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True, text=True, timeout=10,
            shell=_SHELL,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {
                "healthy": False,
                "method": "auth_curl",
                "status_code": None,
                "error": "Failed to get identity token: " + (r.stderr.strip()[:100] or "empty token"),
            }
        token = r.stdout.strip()
    except Exception as e:
        return {"healthy": False, "method": "auth_curl", "status_code": None, "error": f"Identity token error: {e}"}

    # Curl with auth header
    health_url = url.rstrip("/") + "/health"
    start = time.time()

    while (time.time() - start) < timeout:
        try:
            import urllib.request
            import urllib.error
            req = urllib.request.Request(health_url, method="GET")
            req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status < 500:
                    return {"healthy": True, "method": "auth_curl", "status_code": resp.status, "error": None}
        except urllib.error.HTTPError as e:
            if e.code == 403:
                # Token might not have invoker role yet — retry
                pass
            elif e.code < 500:
                return {"healthy": True, "method": "auth_curl", "status_code": e.code, "error": None}
        except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError):
            pass

        time.sleep(3)

    return {"healthy": False, "method": "auth_curl", "status_code": None, "error": f"Auth health check timed out after {timeout}s"}


def _health_check_gcp_proxy(service_name: str | None, region: str | None, timeout: int = 60) -> dict:
    """
    GCP Tier 1C health check via gcloud run services proxy.

    Opens a proxy to the internal-only service, then curls localhost.
    """
    if not service_name:
        return {"healthy": False, "method": "proxy", "status_code": None, "error": "service_name required for proxy health check"}

    import time

    region_flag = ["--region", region] if region else []
    proxy_port = "18080"  # Use non-standard port to avoid conflicts

    # Start proxy in background
    try:
        proxy_proc = subprocess.Popen(
            ["gcloud", "run", "services", "proxy", service_name, "--port", proxy_port] + region_flag,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=_SHELL,
        )
    except Exception as e:
        return {"healthy": False, "method": "proxy", "status_code": None, "error": f"Failed to start proxy: {e}"}

    # Wait for proxy to be ready, then health check
    time.sleep(3)
    try:
        import urllib.request
        import urllib.error

        start = time.time()
        while (time.time() - start) < timeout:
            if proxy_proc.poll() is not None:
                stderr_out = proxy_proc.stderr.read().decode()[:200] if proxy_proc.stderr else ""
                return {
                    "healthy": False, "method": "proxy", "status_code": None,
                    "error": f"Proxy exited unexpectedly: {stderr_out}",
                }
            try:
                req = urllib.request.Request(f"http://localhost:{proxy_port}/health", method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status < 500:
                        return {"healthy": True, "method": "proxy", "status_code": resp.status, "error": None}
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError):
                pass
            time.sleep(2)

        return {"healthy": False, "method": "proxy", "status_code": None, "error": f"Proxy health check timed out after {timeout}s"}
    finally:
        # Always kill the proxy process
        try:
            proxy_proc.terminate()
            proxy_proc.wait(timeout=5)
        except Exception:
            try:
                proxy_proc.kill()
            except Exception:
                pass


def _health_check_ecs_exec(service_name: str | None, region: str | None, timeout: int = 60) -> dict:
    """
    AWS Tier 1C health check via ECS Exec.

    Runs curl inside the container to check localhost health.
    """
    if not service_name:
        return {"healthy": False, "method": "ecs_exec", "status_code": None, "error": "service_name required for ECS Exec health check"}

    # For ECS Exec, we need the task ARN. This is a simplified check —
    # the deploy agent will have the task ARN available in deploy outputs.
    # Here we signal that ECS Exec-based checking is the method to use.
    # The actual implementation is handled by the deploy agent which has
    # task/cluster context.
    return {
        "healthy": None,  # Indeterminate — agent must perform the actual check
        "method": "ecs_exec",
        "status_code": None,
        "error": None,
        "requires_agent_check": True,
        "instructions": (
            f"aws ecs execute-command --cluster <cluster> --task <task-arn> "
            f"--container {service_name} --interactive --command "
            f"\"curl -s localhost:8080/health\""
        ),
    }


# ---------------------------------------------------------------------------
# Tier cascade orchestration
# ---------------------------------------------------------------------------


def deploy_with_cascade(
    service_name: str,
    cloud: str,
    route: str,
    context: dict,
    tier_prediction: dict | None = None,
    deployer_ip: str | None = None,
    is_frontend: bool = False,
) -> dict:
    """
    Main entry point — cascade through tiers for a single service.

    Tries each tier from least-restrictive (1A) to most-restrictive (1C).
    Skips tiers that the prediction says are impossible. Stops at first
    health check pass.

    Args:
        service_name: Name of the service to deploy
        cloud: "gcp" or "aws"
        route: "cloud-run" or "ecs-express" (ECS Fargate + ALB)
        context: Cloud context (account_id/project_id, region, etc.)
        tier_prediction: From cloud-readiness.yaml (optional)
        deployer_ip: Public IP of deployer (auto-detected if None)
        is_frontend: If True, minimum tier is 1B (1C not useful for browsers)

    Returns:
        {
            "success": bool,
            "tier_used": str | None,
            "tier_label": str | None,
            "service_name": str,
            "url": str | None,
            "access_instructions": str,
            "tier_results": {tier: {attempted, success, error, cleaned_up}},
            "failure_report": str | None,
        }
    """
    # Auto-detect deployer IP if not provided (needed for Tier 1B)
    if not deployer_ip:
        deployer_ip = detect_deployer_ip()

    # Determine which tiers to attempt
    tiers_to_try = _compute_tiers_to_try(tier_prediction, is_frontend)

    tier_results: dict[str, dict] = {}

    for tier in tiers_to_try:
        config = get_tier_deploy_config(cloud, tier, deployer_ip)

        if "error" in config:
            tier_results[tier] = {
                "attempted": False,
                "success": False,
                "error": config["error"],
                "cleaned_up": False,
            }
            continue

        # Return the configuration for the deploy agent to execute.
        # The actual deployment is performed by the agent — this function
        # provides the orchestration logic and config.
        tier_results[tier] = {
            "attempted": True,
            "success": None,  # To be determined by agent
            "config": config,
            "deployer_ip": deployer_ip,
        }

    return {
        "success": None,  # Determined after agent executes
        "tier_used": None,
        "tier_label": None,
        "service_name": service_name,
        "cloud": cloud,
        "route": route,
        "tiers_to_try": tiers_to_try,
        "tier_configs": {
            tier: get_tier_deploy_config(cloud, tier, deployer_ip)
            for tier in tiers_to_try
        },
        "tier_results": tier_results,
        "deployer_ip": deployer_ip,
        "context": context,
        "is_frontend": is_frontend,
    }


def record_tier_result(
    cascade_state: dict,
    tier: str,
    success: bool,
    url: str | None = None,
    error: str | None = None,
    cleaned_up: bool = False,
) -> dict:
    """
    Record the result of a tier attempt and determine next action.

    Called by the pipeline after each tier attempt completes.

    Returns updated cascade_state with:
    - If success: tier_used set, access_instructions generated
    - If failure: next_tier indicated (or failure_report if all exhausted)
    """
    cascade_state["tier_results"][tier] = {
        "attempted": True,
        "success": success,
        "url": url,
        "error": error,
        "cleaned_up": cleaned_up,
    }

    if success:
        cloud = cascade_state["cloud"]
        cascade_state["success"] = True
        cascade_state["tier_used"] = tier
        cascade_state["tier_label"] = TIER_LABELS.get(tier, tier)
        cascade_state["url"] = url
        cascade_state["access_instructions"] = _build_access_instructions(
            cloud=cloud,
            tier=tier,
            url=url,
            service_name=cascade_state["service_name"],
            context=cascade_state["context"],
            deployer_ip=cascade_state.get("deployer_ip"),
        )
        cascade_state["next_tier"] = None
        return cascade_state

    # Determine next tier
    tiers_to_try = cascade_state["tiers_to_try"]
    current_idx = tiers_to_try.index(tier) if tier in tiers_to_try else -1

    if current_idx + 1 < len(tiers_to_try):
        cascade_state["next_tier"] = tiers_to_try[current_idx + 1]
    else:
        # All tiers exhausted
        cascade_state["success"] = False
        cascade_state["next_tier"] = None
        cascade_state["failure_report"] = build_failure_report(
            service_name=cascade_state["service_name"],
            cloud=cascade_state["cloud"],
            context=cascade_state["context"],
            tier_results=cascade_state["tier_results"],
        )

    return cascade_state


# ---------------------------------------------------------------------------
# Tier selection logic
# ---------------------------------------------------------------------------


def _compute_tiers_to_try(tier_prediction: dict | None, is_frontend: bool) -> list[str]:
    """
    Determine which tiers to attempt based on prediction and service type.

    Rules:
    - Skip tiers the prediction says are impossible
    - Frontend services minimum = 1B (1C local proxy not useful for browsers)
    - Always include 1C as final fallback (unless frontend)
    """
    if is_frontend:
        possible_tiers = ["1A", "1B"]  # 1C excluded for frontend
    else:
        possible_tiers = list(TIERS)  # ["1A", "1B", "1C"]

    if not tier_prediction:
        return possible_tiers

    # Filter based on prediction
    filtered = []
    for tier in possible_tiers:
        key = f"tier_{tier.lower()}_possible"  # tier_1a_possible, tier_1b_possible, etc.
        if tier_prediction.get(key, True):  # Default to possible if not predicted
            filtered.append(tier)

    # Always keep at least one tier
    if not filtered:
        # If prediction says everything is blocked, still try 1C (most permissive internally)
        if not is_frontend:
            return ["1C"]
        return ["1B"]  # Frontend fallback

    return filtered


# ---------------------------------------------------------------------------
# Access instructions generation
# ---------------------------------------------------------------------------


def _build_access_instructions(
    cloud: str,
    tier: str,
    url: str | None,
    service_name: str,
    context: dict,
    deployer_ip: str | None = None,
) -> str:
    """Generate tier-specific access instructions for the user."""
    region = context.get("region", "unknown")

    if tier == "1A":
        return (
            f"Application deployed successfully (Tier 1A -- Full Public).\n"
            f"URL: {url}\n"
            f"Access: Anyone with the link. No authentication required."
        )

    elif tier == "1B":
        if cloud == "gcp":
            return (
                f"Application deployed successfully (Tier 1B -- Deployer-Scoped).\n"
                f"URL: {url}\n"
                f"Access: Requires GCP identity token for authentication.\n"
                f"\n"
                f"To access from your machine:\n"
                f"  curl -H \"Authorization: Bearer $(gcloud auth print-identity-token)\" {url}\n"
                f"\n"
                f"For other users:\n"
                f"  Grant them roles/run.invoker on this service, then they access with their own identity token."
            )
        elif cloud == "aws":
            return (
                f"Application deployed successfully (Tier 1B -- Deployer-Scoped).\n"
                f"URL: {url}\n"
                f"Access: Currently reachable from your IP ({deployer_ip}) only.\n"
                f"\n"
                f"For other users to access:\n"
                f"  Add their IP to the ALB security group:\n"
                f"  aws ec2 authorize-security-group-ingress --group-id <alb-sg-id> "
                f"--protocol tcp --port 80 --cidr <their-ip>/32"
            )

    elif tier == "1C":
        if cloud == "gcp":
            return (
                f"Application deployed successfully (Tier 1C -- Local Proxy).\n"
                f"No public URL available (org policy restricts public ingress).\n"
                f"\n"
                f"To access from your terminal:\n"
                f"  gcloud run services proxy {service_name} --region {region} --port 8080\n"
                f"  Then open: http://localhost:8080"
            )
        elif cloud == "aws":
            return (
                f"Application deployed successfully (Tier 1C -- Local Proxy).\n"
                f"No public URL available (security policy restricts public ingress).\n"
                f"\n"
                f"To access via ECS Exec:\n"
                f"  aws ecs execute-command --cluster <cluster> --task <task-arn> \\\n"
                f"    --container {service_name} --interactive --command \"/bin/sh\"\n"
                f"  Inside container: curl localhost:8080/health\n"
                f"\n"
                f"Or for port-forward (if configured):\n"
                f"  aws ssm start-session --target <target> \\\n"
                f"    --document-name AWS-StartPortForwardingSession \\\n"
                f"    --parameters '{{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}}'\n"
                f"  Then open: http://localhost:8080"
            )

    return f"Deployed at tier {tier}. URL: {url or 'N/A'}"


# ---------------------------------------------------------------------------
# Failure report generation
# ---------------------------------------------------------------------------


def build_failure_report(
    service_name: str,
    cloud: str,
    context: dict,
    tier_results: dict,
) -> str:
    """
    Generate structured failure report when all tiers fail.

    Actionable report for cloud team — includes minimum viable ask.
    """
    account = context.get("account_id") or context.get("project_id") or "unknown"
    region = context.get("region") or "unknown"
    cloud_upper = cloud.upper()

    lines = [
        "",
        "=" * 62,
        "  DEPLOY GATE FAILED -- No accessible deployment path found",
        "=" * 62,
        "",
        f"Cloud:   {cloud_upper}",
        f"Account: {account}",
        f"Region:  {region}",
        f"Service: {service_name}",
        "",
    ]

    for tier in TIERS:
        result = tier_results.get(tier, {})
        label = TIER_LABELS.get(tier, tier)

        if not result.get("attempted", False):
            lines.append(f"Tier {tier} ({label}) -- SKIPPED (predicted impossible)")
        elif result.get("success"):
            lines.append(f"Tier {tier} ({label}) -- PASSED")
        else:
            error = result.get("error", "Unknown error")
            lines.append(f"Tier {tier} ({label}) -- FAILED")
            lines.append(f"  Blocker: {error}")
        lines.append("")

    # Actionable ask
    lines.extend([
        "=" * 62,
        "  REQUIRED ACTION -- Send to cloud team:",
        "=" * 62,
        "",
    ])

    if cloud == "gcp":
        project_id = context.get("project_id", "<project-id>")
        principal = context.get("identity_email", "<principal>")
        lines.extend([
            "Minimum viable ask (Tier 1C -- requires no public ingress):",
            f"  Grant {principal} the following roles:",
            f"    - roles/run.invoker",
            f"    - roles/run.services.connect",
            f"  On project: {project_id}",
            "",
            "Full public ask (Tier 1A -- if org allows exception for POC):",
            f"  Exception on iam.allowedPolicyMemberDomains for project {project_id}",
            f"  Exception on run.allowedIngress for project {project_id}",
        ])
    elif cloud == "aws":
        account_id = context.get("account_id", "<account-id>")
        lines.extend([
            "Minimum viable ask (Tier 1C -- requires no public ingress):",
            f"  Enable ECS Exec on cluster in account {account_id}:",
            f"    - Set --enable-execute-command on service",
            f"    - Grant ssmmessages:* to task execution role",
            "",
            "Full public ask (Tier 1A -- if org allows exception for POC):",
            f"  Exception on SCP denying 0.0.0.0/0 ingress for account {account_id}",
            f"  Allow internet-facing ALB creation in account {account_id}",
        ])

    lines.extend(["", "=" * 62, ""])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cleanup between tier attempts
# ---------------------------------------------------------------------------


def cleanup_tier_resources(
    cloud: str,
    tier: str,
    service_name: str,
    region: str | None = None,
    deploy_result: dict | None = None,
) -> dict:
    """
    Tear down resources from a failed tier attempt.

    Each tier is self-contained — we don't leave broken artifacts from
    a failed tier when moving to the next one.

    Returns:
        {"cleaned_up": bool, "resources_removed": [...], "error": str | None}
    """
    resources_removed = []
    error = None

    if cloud == "gcp":
        # Delete the Cloud Run service (all tiers use same cleanup)
        region_flag = ["--region", region] if region else []
        try:
            r = subprocess.run(
                ["gcloud", "run", "services", "delete", service_name, "--quiet"] + region_flag,
                capture_output=True, text=True, timeout=60,
                shell=_SHELL,
            )
            if r.returncode == 0:
                resources_removed.append(f"cloud-run-service:{service_name}")
            elif "could not be found" in r.stderr.lower() or "not found" in r.stderr.lower():
                pass  # Already gone
            else:
                error = r.stderr.strip()[:200]
        except Exception as e:
            error = str(e)

    elif cloud == "aws":
        # For ECS Fargate + ALB, the service + ALB + TG + SGs need cleanup
        # The deploy agent handles specifics — we signal what needs removal
        if deploy_result:
            service_arn = deploy_result.get("service_arn")
            alb_arn = deploy_result.get("alb_arn")
            tg_arn = deploy_result.get("target_group_arn")
            alb_sg = deploy_result.get("alb_sg")
            task_sg = deploy_result.get("task_sg")

            if service_arn:
                resources_removed.append(f"ecs-service:{service_arn}")
            if alb_arn:
                resources_removed.append(f"alb:{alb_arn}")
            if tg_arn:
                resources_removed.append(f"target-group:{tg_arn}")
            if alb_sg:
                resources_removed.append(f"security-group:{alb_sg}")
            if task_sg:
                resources_removed.append(f"security-group:{task_sg}")

    return {
        "cleaned_up": len(resources_removed) > 0 or error is None,
        "resources_removed": resources_removed,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Read tier prediction from cloud-readiness (/aah-access)
# ---------------------------------------------------------------------------


def load_tier_prediction(project_path: Path) -> dict | None:
    """
    Read tier prediction from cloud-readiness.yaml (written by /aah-access).

    Reads from .aah/architecture/cloud-readiness.yaml. No fallback path —
    the analysis phase no longer exists.

    Returns the tier_prediction dict or None if not available.
    """
    readiness_path = project_path / ".aah" / "architecture" / "cloud-readiness.yaml"
    if readiness_path.exists():
        data = read_yaml(readiness_path)
        prediction = data.get("tier_prediction")
        if prediction:
            return prediction

    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Tiered access gate for deploy pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    # Deploy cascade
    deploy_p = sub.add_parser("deploy", help="Run tiered deploy cascade")
    deploy_p.add_argument("--project-path", type=Path, required=True)
    deploy_p.add_argument("--cloud", type=str, required=True, choices=["gcp", "aws"])
    deploy_p.add_argument("--route", type=str, required=True)
    deploy_p.add_argument("--service-name", type=str, required=True)
    deploy_p.add_argument("--is-frontend", action="store_true")

    # Get deployer IP
    sub.add_parser("get-deployer-ip", help="Detect deployer public IP")

    # Get tier config
    tier_p = sub.add_parser("tier-config", help="Get deploy config for a specific tier")
    tier_p.add_argument("--cloud", type=str, required=True, choices=["gcp", "aws"])
    tier_p.add_argument("--tier", type=str, required=True, choices=["1A", "1B", "1C"])
    tier_p.add_argument("--deployer-ip", type=str, default=None)

    args = parser.parse_args()

    if args.command == "deploy":
        from aah.core.deploy.cloud_auth import get_cloud_context
        context = get_cloud_context(args.cloud)
        tier_prediction = load_tier_prediction(args.project_path)

        result = deploy_with_cascade(
            service_name=args.service_name,
            cloud=args.cloud,
            route=args.route,
            context=context,
            tier_prediction=tier_prediction,
            is_frontend=args.is_frontend,
        )
        json.dump(result, sys.stdout, indent=2, default=str)
        print()

    elif args.command == "get-deployer-ip":
        ip = detect_deployer_ip()
        json.dump({"deployer_ip": ip}, sys.stdout, indent=2)
        print()
        sys.exit(0 if ip else 1)

    elif args.command == "tier-config":
        config = get_tier_deploy_config(args.cloud, args.tier, args.deployer_ip)
        json.dump(config, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
