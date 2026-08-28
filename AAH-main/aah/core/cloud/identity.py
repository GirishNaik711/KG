#!/usr/bin/env python3
"""
Cloud identity discovery and reporting.

Auto-discovers region / project / subscription from cloud CLI config and
environment variables BEFORE falling back to explicit failure. Always
prints a one-line identity banner so the user knows which principal
RAPIDS authenticated as on every cloud.

Used by: validate_data_readiness, validate_cloud_readiness.
"""

import json
import os
import subprocess
import sys


# ──────────────────────────────────────────────────────────────────────────────
# AWS
# ──────────────────────────────────────────────────────────────────────────────

def aws_default_region(profile: str | None = None) -> str | None:
    """Discover AWS region from env, then from `aws configure get region`."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region
    try:
        cmd = ["aws", "configure", "get", "region"]
        if profile:
            cmd.extend(["--profile", profile])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def aws_identity(profile: str | None = None) -> dict:
    """
    Return AWS identity + region for the given profile.
    Always returns a dict with `provider` and either auth fields or `error`.
    """
    try:
        import boto3
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        arn = ident.get("Arn", "") or ""
        username = arn.rsplit("/", 1)[-1] if "/" in arn else arn
        region = session.region_name or aws_default_region(profile)
        return {
            "provider": "aws",
            "authenticated": True,
            "username": username,
            "arn": arn,
            "account": ident.get("Account"),
            "user_id": ident.get("UserId"),
            "region": region,
            "region_source": _aws_region_source(profile, region),
            "profile": profile,
        }
    except ImportError:
        return {"provider": "aws", "authenticated": False,
                "error": "boto3 not installed"}
    except Exception as e:
        return {"provider": "aws", "authenticated": False,
                "error": str(e), "profile": profile}


def _aws_region_source(profile: str | None, region: str | None) -> str | None:
    if not region:
        return None
    if os.environ.get("AWS_REGION") == region:
        return "env:AWS_REGION"
    if os.environ.get("AWS_DEFAULT_REGION") == region:
        return "env:AWS_DEFAULT_REGION"
    return f"aws-cli-config(profile={profile or 'default'})"


# ──────────────────────────────────────────────────────────────────────────────
# GCP
# ──────────────────────────────────────────────────────────────────────────────

def gcp_identity() -> dict:
    """
    Return GCP identity + project from Application Default Credentials,
    falling back to gcloud CLI config.
    """
    account = None
    project = None
    src_account = None
    src_project = None
    try:
        r = subprocess.run(["gcloud", "config", "get-value", "account"],
                           capture_output=True, text=True, timeout=5)
        account = (r.stdout or "").strip() or None
        if account:
            src_account = "gcloud-config"
    except Exception:
        pass

    try:
        import google.auth
        _, adc_project = google.auth.default()
        if adc_project:
            project = adc_project
            src_project = "google.auth.default()"
    except Exception:
        pass

    if not project:
        try:
            r = subprocess.run(["gcloud", "config", "get-value", "project"],
                               capture_output=True, text=True, timeout=5)
            project = (r.stdout or "").strip() or None
            if project:
                src_project = "gcloud-config"
        except Exception:
            pass

    if not account and not project:
        return {"provider": "gcp", "authenticated": False,
                "error": "no GCP credentials found (run `gcloud auth application-default login`)"}

    return {
        "provider": "gcp",
        "authenticated": True,
        "username": account,
        "account": account,
        "account_source": src_account,
        "project": project,
        "project_source": src_project,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Azure
# ──────────────────────────────────────────────────────────────────────────────

def azure_identity() -> dict:
    """Return Azure identity + subscription from `az account show`."""
    try:
        r = subprocess.run(["az", "account", "show", "--output", "json"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"provider": "azure", "authenticated": False,
                    "error": (r.stderr or "az account show failed").strip()}
        info = json.loads(r.stdout or "{}")
        user = (info.get("user") or {}).get("name")
        return {
            "provider": "azure",
            "authenticated": True,
            "username": user,
            "subscription": info.get("id"),
            "subscription_name": info.get("name"),
            "tenant": info.get("tenantId"),
            "subscription_source": "az-cli",
        }
    except FileNotFoundError:
        return {"provider": "azure", "authenticated": False,
                "error": "az CLI not installed"}
    except Exception as e:
        return {"provider": "azure", "authenticated": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Field discovery — config → env → CLI → None
# ──────────────────────────────────────────────────────────────────────────────

def discover_aws_region(config: dict, profile: str | None = None) -> tuple[str | None, str]:
    """Return (region, source). Source explains where the value came from."""
    if config.get("region"):
        return config["region"], "config"
    env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_region:
        return env_region, "env"
    cli_region = aws_default_region(profile)
    if cli_region:
        return cli_region, f"aws-cli-config(profile={profile or 'default'})"
    return None, "not-found"


def discover_gcp_project(config: dict) -> tuple[str | None, str]:
    """Return (project, source)."""
    if config.get("project_id"):
        return config["project_id"], "config"
    if config.get("project"):
        return config["project"], "config"
    try:
        import google.auth
        _, p = google.auth.default()
        if p:
            return p, "google.auth.default()"
    except Exception:
        pass
    try:
        r = subprocess.run(["gcloud", "config", "get-value", "project"],
                           capture_output=True, text=True, timeout=5)
        p = (r.stdout or "").strip()
        if p:
            return p, "gcloud-config"
    except Exception:
        pass
    return None, "not-found"


# ──────────────────────────────────────────────────────────────────────────────
# Banner
# ──────────────────────────────────────────────────────────────────────────────

def print_identity_banner(identity: dict, stream=sys.stderr) -> None:
    """One-line banner so the user always sees the principal in use."""
    if not identity:
        return
    p = identity.get("provider", "?").upper()
    if not identity.get("authenticated"):
        print(f"[{p}] NOT AUTHENTICATED — {identity.get('error', 'unknown')}", file=stream)
        return
    user = identity.get("username") or "(unknown)"
    if identity["provider"] == "aws":
        scope = (
            f"account={identity.get('account')} "
            f"region={identity.get('region') or '(none)'} "
            f"profile={identity.get('profile') or 'default'}"
        )
    elif identity["provider"] == "gcp":
        scope = f"project={identity.get('project') or '(none)'}"
    elif identity["provider"] == "azure":
        sub = identity.get("subscription_name") or identity.get("subscription") or "(none)"
        scope = f"subscription={sub}"
    else:
        scope = ""
    print(f"[{p}] authenticated as {user}  {scope}", file=stream)


def _anchor_fields_match(identity: dict, expected: dict) -> bool:
    """Compare the identity's account/project/region/subscription against the
    declared profile anchor. Only fields present AND non-empty in ``expected``
    are compared; a field the anchor does not pin is not a mismatch. Every
    pinned field must equal the discovered value (string comparison).
    """
    for key in ("account", "project", "region", "subscription"):
        want = expected.get(key)
        if want is None or str(want).strip() == "":
            continue
        got = identity.get(key)
        if got is None or str(got).strip() != str(want).strip():
            return False
    return True


def identity_for_provider(
    provider: str,
    profile: str | None = None,
    *,
    expected_anchor: dict | None = None,
) -> dict:
    """Return identity dict for the given provider name.

    When ``expected_anchor`` is supplied (the validated runtime profile's
    ``provider_anchor``), the returned identity carries two extra fields:
    ``anchor_expected`` (the anchor asked for) and ``anchor_match`` (bool). The
    caller treats a False ``anchor_match`` as ``no_signal`` — a real principal
    that is not the one the profile pinned must never be accepted as proof. The
    kwarg is optional and backward-compatible: with ``expected_anchor=None`` the
    result is byte-identical to the pre-anchor behaviour.
    """
    if provider == "aws":
        identity = aws_identity(profile=profile)
    elif provider == "gcp":
        identity = gcp_identity()
    elif provider == "azure":
        identity = azure_identity()
    else:
        identity = {"provider": provider, "authenticated": False,
                    "error": f"unknown provider: {provider}"}

    if expected_anchor is not None:
        identity = dict(identity)
        identity["anchor_expected"] = dict(expected_anchor)
        identity["anchor_match"] = bool(
            identity.get("authenticated")
            and _anchor_fields_match(identity, expected_anchor)
        )
    return identity


def all_identities(aws_profile: str | None = None) -> list[dict]:
    """Return identity for every cloud RAPIDS can authenticate against."""
    return [
        aws_identity(profile=aws_profile),
        gcp_identity(),
        azure_identity(),
    ]


# Map service_type -> provider for callers that don't want to hardcode it
_SERVICE_TO_PROVIDER = {
    # AWS
    "rds-postgres": "aws", "dynamodb": "aws", "s3": "aws", "ec2": "aws",
    "ecs": "aws", "eks": "aws", "lambda": "aws", "app-runner": "aws",
    "aws-batch": "aws",
    "elasticache-redis": "aws", "cloudwatch": "aws",
    "secrets-manager": "aws", "bedrock": "aws", "bedrock-guardrails": "aws",
    "cost-explorer": "aws", "opensearch": "aws", "msk": "aws",
    # GCP
    "cloud-sql-postgres": "gcp", "gcs": "gcp", "gke": "gcp", "vertex-ai": "gcp",
    "cloud-run": "gcp", "cloud-run-jobs": "gcp", "cloud-functions": "gcp",
    "compute-engine": "gcp", "cloud-logging": "gcp", "model-armor": "gcp",
    "gcp-secret-manager": "gcp",
    # Azure
    "azure-postgres": "azure", "azure-blob": "azure", "azure-container-apps": "azure",
    "azure-functions": "azure", "azure-cache-redis": "azure", "azure-openai": "azure",
    "key-vault": "azure",
}


def provider_for_service(service_type: str) -> str | None:
    return _SERVICE_TO_PROVIDER.get(service_type)


def main() -> None:
    """CLI: print identity banners for all available clouds."""
    from aah.core.common.runguard import require_rapids_run
    require_rapids_run("cloud identity")

    import argparse
    parser = argparse.ArgumentParser(description="Show cloud identities RAPIDS will use")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of banners")
    args = parser.parse_args()

    identities = all_identities(aws_profile=args.profile)
    if args.json:
        print(json.dumps(identities, indent=2, default=str))
        return
    for ident in identities:
        print_identity_banner(ident, stream=sys.stdout)


if __name__ == "__main__":
    main()
