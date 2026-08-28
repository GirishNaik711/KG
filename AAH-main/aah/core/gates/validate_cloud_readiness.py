#!/usr/bin/env python3
"""
Cloud Readiness Validation Gate.

Validates connectivity to cloud services implied by resolved DDR decisions.
This is the ONLY script that ever touches credentials (briefly in RAM).

Exit codes:
  0 — all services pass (or no-services, skip, override acknowledged)
  2 — critical failure (unresolved failures in direct-cloud mode)

CLI:
  aah run core.gates.validate_cloud_readiness \
    --project-path <path> \
    --config <json-string> \
    --deployment-method <method> \
    [--revalidate] [--services <svc1,svc2>] [--check-state]
"""

import argparse
import importlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.io_utils import read_yaml, write_yaml


# ═══════════════════════════════════════════════════════════════════════════════
# Credential Guard — Patterns that indicate secret content
# ═══════════════════════════════════════════════════════════════════════════════

SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"ASIA[0-9A-Z]{16}", "AWS Temporary Access Key ID"),
    # AWS secret access keys are base64-alphabet 40-char runs. Real keys always
    # contain a mix — at least one digit AND at least one upper-case letter —
    # unlike English prose separated by `+`. The lookaheads eliminate false
    # positives on strings like "endpoint+port+database+username+password".
    (r"(?<![A-Za-z0-9/+=])(?=[0-9a-zA-Z/+]*[A-Z])(?=[0-9a-zA-Z/+]*[0-9])[0-9a-zA-Z/+]{40}(?![A-Za-z0-9/+=])", "Possible AWS Secret Access Key"),
    (r"-----BEGIN\s+(RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----", "PEM Private Key"),
    (r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+", "JWT Token"),
    (r"sk-[a-zA-Z0-9-]{20,}", "OpenAI/Anthropic API Key"),
    (r"sk-ant-[a-zA-Z0-9-]{20,}", "Anthropic API Key"),
    (r"xox[boaprs]-[0-9A-Za-z-]{10,}", "Slack Token"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub Personal Access Token"),
    (r"ghs_[A-Za-z0-9]{36}", "GitHub App Installation Token"),
    (r"glpat-[A-Za-z0-9_-]{20}", "GitLab Personal Access Token"),
    (r"dop_v1_[a-f0-9]{64}", "DigitalOcean PAT"),
    (r"AIza[0-9A-Za-z_-]{35}", "GCP API Key"),
    (r'"type"\s*:\s*"service_account"', "GCP Service Account JSON"),
    (r"mongodb(\+srv)?://[^:]+:[^@]+@", "MongoDB Connection String with password"),
    (r"postgres(ql)?://[^:]+:[^@]+@", "PostgreSQL Connection String with password"),
    (r"redis://:[^@]+@", "Redis Connection String with password"),
    # LangChain / LangSmith
    (r"lsv2_pt_[A-Za-z0-9]{20,}", "LangSmith Personal Token"),
    (r"lsv2_sk_[A-Za-z0-9]{20,}", "LangSmith Service Key"),
    (r"ls__[A-Za-z0-9]{20,}", "LangSmith Legacy Key"),
    # Other AI providers
    (r"hf_[A-Za-z0-9]{30,}", "HuggingFace Token"),
    (r"r8_[A-Za-z0-9]{30,}", "Replicate Token"),
    (r"co-[A-Za-z0-9]{30,}", "Cohere API Key"),
    (r"pcsk_[A-Za-z0-9_-]{20,}", "Pinecone API Key"),
    (r"sk-or-[A-Za-z0-9-]{20,}", "OpenRouter Key"),
    # Generic Bearer / x-api-key headers in transcripts
    (r"(?i)x-api-key[\"'\s:=]+[A-Za-z0-9_\-]{20,}", "x-api-key header value"),
    (r"(?i)Bearer\s+[A-Za-z0-9_\-\.]{20,}", "Bearer token in Authorization header"),
]

_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), label) for p, label in SECRET_PATTERNS]


# ═══════════════════════════════════════════════════════════════════════════════
# Handler Dispatch — Maps service_type to (module, function)
# ═══════════════════════════════════════════════════════════════════════════════

HANDLER_DISPATCH = {
    # Keys match the `handler_id` values in
    # aah/_resources/access/cloud-service-catalog.yaml. Seeded services
    # carry service_type = handler_id, so the join is one-to-one.

    # AWS
    "aws-s3":                 ("aws", "test_s3"),
    "aws-rds-postgres":       ("aws", "test_rds_postgres"),
    "aws-dynamodb":           ("aws", "test_dynamodb"),
    "aws-bedrock":            ("aws", "test_bedrock"),
    "aws-bedrock-guardrails": ("aws", "test_bedrock_guardrails"),
    "aws-agentcore-mcp":      ("aws", "test_agentcore_mcp"),
    "aws-agentcore-a2a":      ("aws", "test_agentcore_a2a"),
    "aws-agentcore-runtime":  ("aws", "test_agentcore_runtime"),
    "aws-cloudwatch":         ("aws", "test_cloudwatch"),
    "aws-opensearch":         ("aws", "test_opensearch"),
    "aws-opensearch-serverless": ("aws", "test_opensearch_serverless"),
    "aws-msk":                ("aws", "test_elasticache"),  # reuses tcp-connect handler
    "aws-secrets-manager":    ("aws", "test_secrets_manager"),
    "aws-cost-explorer":      ("aws", "test_cost_explorer"),
    "aws-ec2":                ("aws", "test_ec2"),
    "aws-ecs":                ("aws", "test_ecs"),
    "aws-eks":                ("aws", "test_eks"),
    "aws-lambda":             ("aws", "test_lambda"),
    "aws-app-runner":         ("aws", "test_app_runner"),
    "aws-batch":              ("aws", "test_aws_batch"),
    "aws-elasticache-redis":  ("aws", "test_elasticache"),

    # Azure
    "azure-postgres":         ("azure", "test_azure_postgres"),
    "azure-blob":             ("azure", "test_blob_storage"),
    "azure-key-vault":        ("azure", "test_key_vault"),
    "azure-container-apps":   ("azure", "test_container_apps"),
    "azure-openai":           ("azure", "test_azure_openai"),
    "azure-functions":        ("azure", "test_azure_functions"),
    "azure-cache-redis":      ("azure", "test_azure_cache_redis"),

    # GCP
    "gcp-gcs":                ("gcp", "test_gcs"),
    "gcp-cloud-sql-postgres": ("gcp", "test_cloud_sql"),
    "gcp-secret-manager":     ("gcp", "test_secret_manager"),
    "gcp-gke":                ("gcp", "test_gke"),
    "gcp-vertex-ai":          ("gcp", "test_vertex_ai"),
    "gcp-cloud-run":          ("gcp", "test_cloud_run"),
    "gcp-cloud-run-jobs":     ("gcp", "test_cloud_run_jobs"),
    "gcp-cloud-functions":    ("gcp", "test_cloud_functions"),
    "gcp-compute-engine":     ("gcp", "test_compute_engine"),
    "gcp-cloud-logging":      ("gcp", "test_cloud_logging"),
    "gcp-model-armor":        ("gcp", "test_model_armor"),

    # Cloud-agnostic / self-hosted (`general` provider bucket in the catalog)
    "postgres-generic":       ("general", "test_postgres"),
    "redis-generic":          ("general", "test_redis"),
    "mongodb-generic":        ("general", "test_mongodb"),
    "snowflake":              ("general", "test_snowflake"),
    "neo4j":                  ("general", "test_neo4j"),
    "kafka-generic":          ("general", "test_kafka"),
    "elasticsearch-generic":  ("general", "test_elasticsearch"),
    "databricks":             ("general", "test_http_health"),
    "langsmith":              ("langsmith", "test_langsmith"),
}


def scan_for_secrets(value: str) -> str | None:
    """Scan a string value for credential patterns. Returns label if found, else None."""
    for pattern, label in _COMPILED_PATTERNS:
        if pattern.search(value):
            return label
    return None


def _scan_recursive(obj, violations: list[str], path: str = "") -> None:
    """Walk any nested structure; append to violations when secret-shaped strings appear."""
    if isinstance(obj, str):
        label = scan_for_secrets(obj)
        if label:
            violations.append(f"Secret detected at '{path or '<root>'}': {label}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _scan_recursive(v, violations, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan_recursive(item, violations, f"{path}[{i}]")


def guard_config(config: dict) -> list[str]:
    """
    Scan all values in a config dict for secret patterns.
    Returns list of violations found. Empty list = safe to persist.
    """
    violations: list[str] = []
    _scan_recursive(config, violations)
    return violations


def guard_output(payload, label: str = "output") -> None:
    """
    Refuse to emit any payload (dict / list / str / etc.) that contains a
    secret-shaped substring. Called before writing to stdout, file, or YAML.

    On violation: prints a sanitized error to stderr (no payload echoed) and
    exits with code 2. Never returns the offending payload to the caller.

    This is the last line of defense — handlers should already strip secrets
    before returning, but if any leak slips through, this stops it from
    reaching disk or the conversation transcript.
    """
    violations: list[str] = []
    _scan_recursive(payload, violations)
    if violations:
        # Print only labels + paths, never the matched secret bytes.
        print(json.dumps({
            "error": f"OUTPUT GUARD: refused to emit {label} — secret-shaped values detected",
            "violation_count": len(violations),
            "violations": violations[:10],
            "remediation": (
                "A handler returned a value that matched a known credential pattern. "
                "Fix the handler to not include the secret in its result; never log "
                "or persist credential material. If this is a false positive, "
                "tighten the regex in SECRET_PATTERNS."
            ),
        }), file=sys.stderr)
        sys.exit(2)


def emit_json(payload, label: str = "stdout") -> None:
    """guard_output + json.dumps. Use everywhere we currently write json.dumps to stdout."""
    guard_output(payload, label=label)
    print(json.dumps(payload, default=str))


def resolve_credentials(service_config: dict, project_path: Path) -> dict | None:
    """
    Resolve credentials for a service based on its auth_method.
    Returns credentials dict (used once, then discarded) or None.
    NEVER returns secrets to stdout or persists them to disk.
    """
    auth_method = service_config.get("auth_method", "cloud-default")

    if auth_method in ("cloud-default", "no-auth", "sso-browser", "client-config-file"):
        return None

    if auth_method in ("secret-manager", "full-config-from-secret"):
        secret_path = service_config.get("secret_path", "")
        if not secret_path:
            return None

        provider = service_config.get("provider", "")
        try:
            if provider == "aws":
                import boto3
                profile = service_config.get("aws_profile")
                session = boto3.Session(profile_name=profile) if profile else boto3.Session()
                client = session.client("secretsmanager",
                                        region_name=service_config.get("region", "us-east-1"))
                resp = client.get_secret_value(SecretId=secret_path)
                secret_string = resp.get("SecretString", "")
                try:
                    creds = json.loads(secret_string)
                except json.JSONDecodeError:
                    creds = {"password": secret_string}
                del secret_string
                return creds
            elif provider == "gcp":
                from google.cloud import secretmanager
                client = secretmanager.SecretManagerServiceClient()
                response = client.access_secret_version(name=secret_path)
                payload = response.payload.data.decode("utf-8")
                try:
                    creds = json.loads(payload)
                except json.JSONDecodeError:
                    creds = {"password": payload}
                del payload
                return creds
            elif provider == "azure":
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
                vault_url = service_config.get("vault_url", "")
                secret_name = service_config.get("secret_name", secret_path)
                credential = DefaultAzureCredential()
                client = SecretClient(vault_url=vault_url, credential=credential)
                secret = client.get_secret(secret_name)
                value = secret.value
                try:
                    creds = json.loads(value)
                except json.JSONDecodeError:
                    creds = {"password": value}
                del value
                return creds
        except Exception:
            return None

    return None


def is_skip_marker(value) -> bool:
    """Return True if a user-provided value should skip testing for that service."""
    if value is None:
        return True
    if isinstance(value, str):
        v = value.strip().lower()
        return v in ("", "skip", "later", "not-yet", "pending", "todo", "tbd")
    return False


def has_required_inputs(service_type: str, config: dict) -> bool:
    """
    Return True if the user provided enough info to actually test this service.
    Returns False if the user wants to skip (e.g., resource not yet provisioned).
    """
    auth_method = config.get("auth_method", "")
    # full-config-from-secret carries everything inside the secret blob
    if auth_method == "full-config-from-secret" and config.get("secret_path"):
        return True

    # Each service must have at least one user-provided resource identifier.
    # If the user wants to skip (resource not yet provisioned), they pass
    # blank/skip/later for these fields and the gate records not-tested.
    required_per_service = {
        "s3": ["bucket_name"],
        "rds-postgres": ["endpoint"],
        "azure-postgres": ["hostname"],
        "cloud-sql-postgres": ["connection_name"],
        "postgres-generic": ["hostname"],
        "dynamodb": ["table_name"],
        "ec2": ["instance_id"],
        "ecs": ["cluster_name"],
        "eks": ["cluster_name"],
        "app-runner": ["service_name"],
        "aws-batch": ["job_queue"],
        "azure-container-apps": ["resource_group"],
        "gke": ["project_id", "location"],
        "lambda": ["function_name"],
        "cloud-run-jobs": ["project_id", "region"],
        "cloud-functions": ["project_id", "region"],
        "compute-engine": ["project_id"],
        "azure-functions": ["resource_group"],
        "cloud-run": ["project_id", "region"],
        "elasticache-redis": ["endpoint"],
        "azure-cache-redis": ["hostname"],
        "redis": ["hostname"],
        "azure-blob": ["account_url"],
        "gcs": ["bucket_name"],
        "secrets-manager": ["secret_path"],
        "key-vault": ["vault_url"],
        "gcp-secret-manager": ["project_id"],
        "bedrock": ["model_id"],
        "bedrock-guardrails": ["guardrail_id"],
        "azure-openai": ["endpoint"],
        "vertex-ai": ["project_id"],
        "model-armor": ["project_id"],
        "cloudwatch": ["metric_namespace"],
        "cloud-logging": ["project_id"],
        "cost-explorer": ["account_id"],
        "snowflake": ["account"],
        "neo4j": ["bolt_uri"],
        "kafka": ["bootstrap_servers"],
        "msk": ["bootstrap_servers"],
        "elasticsearch": ["endpoint"],
        "opensearch": ["endpoint"],
        "mongodb": ["hostname"],
        "databricks": ["workspace_url"],
        "http": ["endpoint"],
        "langsmith": ["api_url"],
    }

    required = required_per_service.get(service_type, [])
    if not required:
        return True  # Unknown service type — don't block
    for field in required:
        val = config.get(field)
        if is_skip_marker(val):
            return False
    return True


def dispatch_test(service_type: str, config: dict, credentials: dict | None = None) -> dict:
    """Dispatch connectivity test to the appropriate handler.

    Unknown service_type is a hard failure (passed=False) — silently
    treating unrecognized service_types as passing produces fake receipts
    with no probe ever running, which is worse than a clear error.
    """
    if service_type not in HANDLER_DISPATCH:
        return {
            "passed": False,
            "latency_ms": 0,
            "error": (
                f"No handler registered for service_type={service_type!r}. "
                f"Check that the catalog handler_id in cloud-service-catalog.yaml "
                f"matches a key in HANDLER_DISPATCH, or add a dispatch entry."
            ),
            "error_class": "NoHandlerRegistered",
        }

    module_name, func_name = HANDLER_DISPATCH[service_type]

    try:
        mod = importlib.import_module(f"aah.core.cloud.handlers.{module_name}")
        handler_fn = getattr(mod, func_name)
    except (ImportError, AttributeError) as e:
        return {"passed": False, "latency_ms": 0, "error": f"Handler load failed: {e}"}

    import inspect
    sig = inspect.signature(handler_fn)
    if "credentials" in sig.parameters and credentials:
        return handler_fn(config, credentials=credentials)
    return handler_fn(config)


def apply_deployment_strictness(result: dict, deployment_method: str) -> dict:
    """Apply deployment method rules to a test result."""
    if deployment_method == "local-only":
        return {"passed": True, "latency_ms": 0, "error": None, "note": "Skipped — local-only deployment"}

    if deployment_method == "local-validation-then-cloud":
        if not result.get("passed"):
            result["status"] = "skipped"
            result["note"] = "Unreachable — local-validation fallback accepted"
            result["passed"] = True

    return result


def run_validation(
    services: list[dict],
    deployment_method: str,
    project_path: Path,
    revalidate: bool = False,
    service_filter: list[str] | None = None,
) -> dict:
    """
    Run connectivity tests for all services.

    Returns a result dict with outcome, counts, and per-service details.
    """
    if deployment_method == "local-only":
        return {
            "outcome": "skip",
            "reason": "Deployment method is local-only — no cloud validation needed",
            "services_tested": 0,
            "services_passed": 0,
            "services_failed": 0,
            "details": [],
        }

    if not services:
        return {
            "outcome": "no-services",
            "reason": "No cloud services detected from resolved decisions",
            "services_tested": 0,
            "services_passed": 0,
            "services_failed": 0,
            "details": [],
        }

    results = []
    for svc in services:
        service_type = svc.get("type") or svc.get("service_type", "")
        svc_id = svc.get("id", "")

        if service_filter and service_type not in service_filter and svc_id not in service_filter:
            continue

        if not revalidate and svc.get("status") == "pass":
            results.append({**svc, "skipped_reason": "already validated"})
            continue

        # Skip if user didn't provide a real resource identifier for this service
        if not has_required_inputs(service_type, svc):
            results.append({
                "passed": False,
                "status": "not-tested",
                "latency_ms": 0,
                "error": None,
                "service_type": service_type,
                "service_id": svc_id,
                "display_name": svc.get("name") or svc.get("display_name", ""),
                "criticality": svc.get("criticality", "advisory"),
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "note": "User skipped — resource not yet provided. Re-run later to validate.",
            })
            continue

        credentials = resolve_credentials(svc, project_path)

        test_config = {k: v for k, v in svc.items() if k not in ("id", "name", "type", "service_type",
                                                                    "criticality", "source_slug", "source_ddr",
                                                                    "status", "latency_ms", "validated_at", "error")}

        # When auth_method is full-config-from-secret, the secret blob is the source
        # of truth — endpoint/port/database/username come from the blob, overriding
        # any placeholder values the user may have typed at the prompt.
        if svc.get("auth_method") == "full-config-from-secret" and credentials:
            for field in ("endpoint", "hostname", "port", "database", "username",
                          "account", "warehouse", "bolt_uri", "connection_name"):
                if field in credentials:
                    test_config[field] = credentials[field]

        raw_result = dispatch_test(service_type, test_config, credentials)

        if credentials:
            del credentials

        result = apply_deployment_strictness(raw_result, deployment_method)
        result["service_type"] = service_type
        result["service_id"] = svc_id
        result["display_name"] = svc.get("name") or svc.get("display_name", "")
        result["criticality"] = svc.get("criticality", "advisory")
        result["validated_at"] = datetime.now(timezone.utc).isoformat()
        results.append(result)

    passed_count = sum(1 for r in results if r.get("passed"))
    failed_count = sum(1 for r in results if not r.get("passed") and "skipped_reason" not in r)
    tested_count = len([r for r in results if "skipped_reason" not in r])

    critical_failures = [
        r for r in results
        if not r.get("passed") and r.get("criticality") == "critical" and "skipped_reason" not in r
    ]

    if critical_failures and deployment_method == "direct-cloud":
        outcome = "fail"
    elif failed_count > 0:
        outcome = "partial"
    else:
        outcome = "passed"

    return {
        "outcome": outcome,
        "services_tested": tested_count,
        "services_passed": passed_count,
        "services_failed": failed_count,
        "critical_failures": len(critical_failures),
        "details": results,
    }


def get_aws_identity(profile: str) -> dict | None:
    """Fetch AWS caller identity for the given profile. Returns identity dict or None on failure."""
    try:
        import boto3
        session = boto3.Session(profile_name=profile)
        client = session.client("sts")
        identity = client.get_caller_identity()
        return {
            "identity_check": "ok",
            "profile": profile,
            "account": identity.get("Account"),
            "arn": identity.get("Arn"),
            "user_id": identity.get("UserId"),
        }
    except Exception:
        return None


def _validate_deploy_service(svc: dict, target_cloud: str, project_path: Path) -> dict:
    """
    Validate a deploy-infrastructure service (Cloud Run, Artifact Registry, ECR, ECS).

    Runs the service's validation_command (lightweight CLI call) and returns
    a structured result compatible with the deploy_services output.
    """
    import platform
    import shutil
    import subprocess

    service_name = svc.get("service_name", "")
    description = svc.get("description", "")
    criticality = svc.get("criticality", "critical")
    command = svc.get("validation_command", "")

    if not command:
        return {
            "service_name": service_name,
            "description": description,
            "criticality": criticality,
            "status": "available",
            "note": "No validation command defined",
        }

    # Get region/project for command interpolation
    region = "us-central1" if target_cloud == "gcp" else "us-east-1"
    project = ""

    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path)
        region = manifest.get("stack_choices", {}).get("region") or region

    if target_cloud == "gcp":
        try:
            r = subprocess.run(
                ["gcloud", "config", "get-value", "project"],
                capture_output=True, text=True, timeout=5,
                shell=(platform.system() == "Windows"),
            )
            if r.returncode == 0 and r.stdout.strip():
                project = r.stdout.strip()
        except Exception:
            pass

    # Interpolate command
    command = command.format(region=region, project=project)

    # Run validation
    parts = command.split()
    cli_name = parts[0]

    # Resolve CLI (handle Windows .cmd extensions)
    resolved = shutil.which(cli_name)
    if not resolved and platform.system() == "Windows":
        for ext in (".cmd", ".bat", ".exe"):
            resolved = shutil.which(cli_name + ext)
            if resolved:
                break

    if not resolved:
        return {
            "service_name": service_name,
            "description": description,
            "criticality": criticality,
            "status": "unavailable",
            "error": f"'{cli_name}' CLI not installed or not in PATH",
        }

    parts[0] = resolved
    try:
        r = subprocess.run(
            parts,
            capture_output=True, text=True, timeout=20,
            shell=(platform.system() == "Windows"),
        )
        if r.returncode == 0:
            return {
                "service_name": service_name,
                "description": description,
                "criticality": criticality,
                "status": "available",
            }
        else:
            return {
                "service_name": service_name,
                "description": description,
                "criticality": criticality,
                "status": "unavailable",
                "error": r.stderr.strip()[:200] if r.stderr else f"Exit code {r.returncode}",
            }
    except subprocess.TimeoutExpired:
        return {
            "service_name": service_name,
            "description": description,
            "criticality": criticality,
            "status": "unavailable",
            "error": "Validation command timed out",
        }
    except Exception as e:
        return {
            "service_name": service_name,
            "description": description,
            "criticality": criticality,
            "status": "unavailable",
            "error": str(e),
        }


def save_cloud_readiness(
    project_path: Path,
    services_config: list[dict],
    validation_results: dict,
    deployment_method: str,
    provider: str | None,
) -> Path:
    """Save cloud readiness state to .aah/architecture/cloud-readiness.yaml."""
    aah_path = project_path / ".aah"
    arch_path = aah_path / "architecture"
    arch_path.mkdir(parents=True, exist_ok=True)
    output_path = arch_path / "cloud-readiness.yaml"

    now = datetime.now(timezone.utc).isoformat()
    outcome = validation_results.get("outcome", "pending")

    existing = {}
    if output_path.exists():
        existing = read_yaml(output_path) or {}

    # Merge mode: keep prior services, update/add the ones passed in this run.
    # Key on `id` (svc-NNN) rather than service_type — service_type is not
    # unique when multiple slugs route to the same handler (e.g. guardrail-service
    # and pii-detection-service both route to aws-bedrock-guardrails, producing
    # two distinct seeded rows with the same service_type but different ids).
    existing_by_id = {}
    for s in existing.get("services", []):
        key = s.get("id")
        if key:
            existing_by_id[key] = s

    details = validation_results.get("details", [])

    for i, svc in enumerate(services_config):
        svc_id = svc.get("id") or svc.get("service_id") or ""
        svc_type = svc.get("type") or svc.get("service_type", "")
        base = existing_by_id.get(svc_id, {}) if svc_id else {}
        base.update(svc)
        if not base.get("id"):
            base["id"] = svc_id or f"svc-{len(existing_by_id) + i + 1:03d}"
        base["type"] = svc_type
        base["service_type"] = svc_type

        # Match this run's result to this specific service by id first
        # (uniquely identifies the row), then service_type as fallback.
        detail = next(
            (d for d in details if d.get("service_id") == base.get("id")),
            None,
        )
        if detail is None:
            detail = next(
                (d for d in details if d.get("service_type") == svc_type),
                None,
            )
        if detail:
            # Honor explicit not-tested status from the run loop
            if detail.get("status") == "not-tested":
                base["status"] = "not-tested"
            elif detail.get("passed") is True:
                base["status"] = "pass"
            elif detail.get("passed") is False:
                base["status"] = "fail"
            # If detail.get("passed") is None (missing entirely), don't
            # clobber the existing status — this was the bug that made
            # previously-passed rows flip to "fail" when a later run
            # returned partial data.
            base["latency_ms"] = detail.get("latency_ms", base.get("latency_ms", 0))
            base["error"] = detail.get("error", base.get("error"))
            base["validated_at"] = detail.get("validated_at", now)
            if detail.get("note"):
                base["note"] = detail["note"]
            # Persist any verified_* metadata returned by handlers
            for k, v in detail.items():
                if k.startswith("verified_"):
                    base[k] = v

        existing_by_id[base["id"]] = base

    updated_services = list(existing_by_id.values())

    # Compute aggregate gate_status based on ALL accumulated services.
    # passed: every service is pass/override
    # pending: at least one is fail OR not-tested
    statuses = [s.get("status") for s in updated_services]
    has_fail = any(st == "fail" for st in statuses)
    has_not_tested = any(st == "not-tested" for st in statuses)
    all_resolved = all(st in ("pass", "override") for st in statuses)
    if all_resolved:
        gate_status = "passed"
    elif has_fail or has_not_tested:
        gate_status = "pending"
    else:
        gate_status = "pending"

    # ═══ Deploy Infrastructure Validation (DDR-SHARED-017 aware) ═══
    # If the project uses a RAPIDS-managed deploy target (cloud-run / ecs-express),
    # validate the deploy-specific services (Cloud Run API, Artifact Registry, ECR, ECS)
    # and compute tier prediction — all in one pass.
    deploy_services = []
    tier_prediction = None

    try:
        from aah.core.cloud.registry_extractor import extract_services_from_registry
        deploy_extraction = extract_services_from_registry(project_path)

        is_managed = (
            deploy_extraction.get("is_cloudrun")
            or deploy_extraction.get("is_ecs_express")
            or deploy_extraction.get("is_cloudless")
        )

        if is_managed:
            target_cloud = deploy_extraction.get("target_cloud")
            raw_deploy_services = deploy_extraction.get("services", [])

            # Validate each deploy service (lightweight CLI check)
            for svc in raw_deploy_services:
                svc_result = _validate_deploy_service(svc, target_cloud, project_path)
                deploy_services.append(svc_result)

            # Run tier prediction (read-only org policy checks)
            if target_cloud and (deploy_extraction.get("is_cloudrun") or deploy_extraction.get("is_ecs_express")):
                from aah.core.cloud.validate_cloud_readiness import _check_tier_prediction
                tier_prediction = _check_tier_prediction(target_cloud, project_path)

    except Exception:
        # Non-critical — don't let this block the main gate
        pass

    output = {
        "schema_version": "1.0",
        "cloud_provider": provider or existing.get("cloud_provider"),
        "deployment_method": deployment_method,
        "gate_status": gate_status,
        "validated_at": now,
        "services": updated_services,
        "overrides": existing.get("overrides", []),
    }

    # Add deploy infrastructure results if present
    if deploy_services:
        output["deploy_services"] = deploy_services
    if tier_prediction:
        output["tier_prediction"] = tier_prediction





    # Last line of defense: refuse to persist anything that matches a known
    # credential pattern. Handlers should already strip secrets, but this
    # blocks the file from being written if anything slipped through.
    guard_output(output, label=str(output_path))
    write_yaml(output, output_path)
    return output_path


def check_existing_state(project_path: Path) -> dict | None:
    """Check if cloud-readiness.yaml already exists and return its state."""
    output_path = project_path / ".aah" / "architecture" / "cloud-readiness.yaml"
    if not output_path.exists():
        return None
    return read_yaml(output_path)


def main() -> None:
    from aah.core.gates.readiness_banner import print_banner
    print_banner("cloud")

    from aah.core.common.runguard import require_rapids_run
    require_rapids_run("validate_cloud_readiness")

    parser = argparse.ArgumentParser(
        description="Cloud Readiness Validation Gate"
    )
    parser.add_argument(
        "--project-path", type=Path, required=True,
        help="Path to the project directory containing .aah/"
    )
    parser.add_argument(
        "--deployment-method", type=str, default="direct-cloud",
        choices=["local-only", "local-validation-then-cloud", "direct-cloud"],
        help="Deployment method (controls strictness)"
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="JSON string with service configurations collected from user"
    )
    parser.add_argument(
        "--revalidate", action="store_true",
        help="Re-run tests for already-validated services"
    )
    parser.add_argument(
        "--services", type=str, default=None,
        help="Comma-separated list of service types to test (default: all)"
    )
    parser.add_argument(
        "--check-state", action="store_true",
        help="Only check existing state, don't run tests"
    )
    parser.add_argument(
        "--profile", type=str, default=None,
        help="AWS CLI profile name to use (scoped to this process only, does not affect parent shell)"
    )
    parser.add_argument(
        "--skip", action="store_true",
        help="Explicitly skip cloud validation. Records gate_status=skipped so the analysis gate accepts it."
    )
    parser.add_argument(
        "--skip-reason", type=str, default="User chose to handle infrastructure validation later",
        help="Reason for skipping (recorded in cloud-readiness.yaml)"
    )
    parser.add_argument(
        "--override-service", type=str, default=None,
        help="Record an override for a specific service_type. Use with --override-reason."
    )
    parser.add_argument(
        "--override-reason", type=str, default=None,
        help="Reason for the override (required when using --override-service)"
    )
    parser.add_argument(
        "--seed", action="store_true",
        help="Seed cloud-readiness.yaml. Without --routings, emits routing inputs "
             "for the /aah-access skill to classify. With --routings, consumes the "
             "skill's decisions and writes cloud-readiness.yaml."
    )
    parser.add_argument(
        "--routings", type=str, default=None,
        help="JSON payload from the /aah-access skill: {routings: [{slug_id, "
             "response, kind: matched|unmatched|not-a-service, handler_id?, "
             "reason?}]}. Consumed by --seed to produce cloud-readiness.yaml."
    )
    parser.add_argument(
        "--extra-services", type=str, default=None,
        help="JSON payload of services NOT declared in the decision registry "
             "but that should still be probed (e.g. AWS AgentCore MCP runtime "
             "ARNs collected from the user at skill Step 2.b). Shape: "
             "{extra_services: [{handler_id, config, display_name?, "
             "criticality?}]}. Each entry becomes a seeded row with "
             "status=not-tested; the handler's required_user_input[] is "
             "copied from the catalog. Independent of --routings count "
             "assertion (which only covers slug_decisions[])."
    )
    args = parser.parse_args()

    project_path = resolve_project_path(args.project_path)
    if project_path is None:
        print(json.dumps({"error": "Could not resolve project path"}))
        sys.exit(1)

    if args.check_state:
        state = check_existing_state(project_path)
        if state:
            print(json.dumps(state, default=str))
        else:
            print(json.dumps({"gate_status": "not-started", "services": []}))
        sys.exit(0)

    if args.seed:
        # Two-phase seed:
        #   Phase 1 (no --routings): emit routing inputs for the /aah-access skill.
        #   Phase 2 (with --routings): consume skill decisions, write cloud-readiness.yaml.
        from aah.core.cloud.registry_extractor import (
            build_service_from_handler,
            collect_routing_inputs,
            detect_cloud_provider,
            load_catalog,
        )
        from aah.core.common.config import resolve_framework_root

        aah_path = project_path / ".aah"
        arch_path = aah_path / "architecture"
        arch_path.mkdir(parents=True, exist_ok=True)
        registry_path = aah_path / "discuss" / "decision-registry.yaml"
        if not registry_path.exists():
            print(json.dumps({"error": ".aah/discuss/decision-registry.yaml not found — run /aah-discuss first"}), file=sys.stderr)
            sys.exit(1)

        registry = read_yaml(registry_path)
        framework_root = resolve_framework_root()
        if framework_root is None:
            print(json.dumps({"error": "cannot determine framework root"}), file=sys.stderr)
            sys.exit(1)
        catalog = load_catalog(framework_root)
        provider = detect_cloud_provider(registry)
        routing_inputs = collect_routing_inputs(registry, catalog, provider)

        # ── Phase 1: emit routing inputs and stop ──
        if not args.routings:
            print(json.dumps({
                "outcome": "routing-required",
                "provider": provider,
                "slug_decisions": routing_inputs["slug_decisions"],
                "handlers": routing_inputs["handlers"],
                "next_step": (
                    "The /aah-access skill (or any LLM caller) should classify each "
                    "slug_decision by matching it against the handlers[] descriptions, "
                    "then re-invoke this command with --routings '<json>' where the "
                    "json shape is {\"routings\": [{\"slug_id\": ..., \"response\": ..., "
                    "\"kind\": \"matched\"|\"unmatched\"|\"not-a-service\", "
                    "\"handler_id\": ...?, \"reason\": ...?}]}."
                ),
            }, default=str))
            sys.exit(0)

        # ── Phase 2: consume routings and write cloud-readiness.yaml ──
        try:
            routings_payload = json.loads(args.routings)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"--routings is not valid JSON: {e}"}), file=sys.stderr)
            sys.exit(1)

        routings = routings_payload.get("routings", [])
        handlers = routing_inputs["handlers"]
        now = datetime.now(timezone.utc).isoformat()

        seeded_services = []
        unmatched_services = []
        dropped_not_a_service = []

        # Preserve prior user resolutions across re-seed.
        output_path = arch_path / "cloud-readiness.yaml"
        existing = read_yaml(output_path) or {} if output_path.exists() else {}
        existing_unmatched_by_slug = {
            u.get("slug_id"): u for u in (existing.get("unmatched_services") or [])
            if isinstance(u, dict) and u.get("slug_id")
        }

        for i, r in enumerate(routings):
            slug_id = r.get("slug_id")
            response = r.get("response")
            kind = r.get("kind")

            if kind == "matched":
                handler_id = r.get("handler_id")
                svc = build_service_from_handler(slug_id, response, handler_id, handlers)
                if svc is None:
                    unmatched_services.append({
                        "slug_id": slug_id,
                        "response": response,
                        "reason": f"Router picked handler_id={handler_id!r} but that handler_id is not in the catalog.",
                        "resolution": "pending",
                        "resolution_note": "",
                        "detected_at": now,
                    })
                    continue
                seeded_services.append({
                    "id": f"svc-{len(seeded_services)+1:03d}",
                    "name": svc["display_name"],
                    "display_name": svc["display_name"],
                    "type": svc["service_type"],
                    "service_type": svc["service_type"],
                    "handler_id": svc["handler_id"],
                    "provider": svc["provider"],
                    "criticality": svc["criticality"],
                    "source_slug": svc["source_slug"],
                    "resolved_option": svc["resolved_option"],
                    "auth_options": svc["auth_options"],
                    "required_user_input": svc["required_user_input"],
                    "optional_user_input": svc["optional_user_input"],
                    "sdk_package": svc.get("sdk_package"),
                    "routed_by": svc["routed_by"],
                    "status": "not-tested",
                    "validated_at": now,
                    "note": "Seeded via skill-side routing — awaiting user input to validate",
                })
            elif kind == "unmatched":
                prior = existing_unmatched_by_slug.get(slug_id, {})
                unmatched_services.append({
                    "slug_id": slug_id,
                    "response": response,
                    "reason": r.get("reason", "Router could not find a matching handler."),
                    "resolution": prior.get("resolution", "pending"),
                    "resolution_note": prior.get("resolution_note", ""),
                    "detected_at": now,
                })
            elif kind == "not-a-service":
                dropped_not_a_service.append({"slug_id": slug_id, "response": response})
            else:
                # Unrecognized kind — surface as unmatched with a diagnostic reason.
                unmatched_services.append({
                    "slug_id": slug_id,
                    "response": response,
                    "reason": f"Router returned unrecognized kind={kind!r}",
                    "resolution": "pending",
                    "resolution_note": "",
                    "detected_at": now,
                })

        # ── Extra services (registry-independent, e.g. MCP runtime ARNs) ──
        # These come from user input at skill Step 2.b — they are NOT declared
        # as slugs in the decision registry, so they can't flow through the
        # routings[] payload without breaking the count assertion. Merge them
        # into seeded_services[] directly using the handler_id to look up the
        # catalog entry's input schema.
        if args.extra_services:
            try:
                extra_payload = json.loads(args.extra_services)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"--extra-services is not valid JSON: {e}"}), file=sys.stderr)
                sys.exit(1)
            handlers_by_id = {h.get("handler_id"): h for h in handlers if h.get("handler_id")}
            for e in extra_payload.get("extra_services", []):
                handler_id = e.get("handler_id")
                cfg = e.get("config") or {}
                if not handler_id or handler_id not in handlers_by_id:
                    unmatched_services.append({
                        "slug_id": handler_id or "<no-handler_id>",
                        "response": cfg,
                        "reason": (
                            f"--extra-services entry names handler_id={handler_id!r} "
                            f"which is not in the catalog. Add the handler entry first."
                        ),
                        "resolution": "pending",
                        "resolution_note": "",
                        "detected_at": now,
                    })
                    continue
                h = handlers_by_id[handler_id]
                seeded_services.append({
                    "id": f"svc-{len(seeded_services)+1:03d}",
                    "name": e.get("display_name") or handler_id,
                    "display_name": e.get("display_name") or handler_id,
                    "type": handler_id,
                    "service_type": handler_id,
                    "handler_id": handler_id,
                    "provider": h.get("provider", "general"),
                    "criticality": e.get("criticality") or h.get("criticality_default", "advisory"),
                    "source_slug": "extra-services",
                    "resolved_option": None,
                    "auth_options": h.get("auth_options", []),
                    "required_user_input": h.get("required_user_input", []),
                    "optional_user_input": h.get("optional_user_input", []),
                    "sdk_package": h.get("sdk_package"),
                    "routed_by": "skill-side-extra-services",
                    "status": "not-tested",
                    "validated_at": now,
                    "note": "Seeded via --extra-services (not from decision registry)",
                    # Pre-populate the fields the handler needs from the caller's config.
                    **{k: v for k, v in cfg.items() if k not in ("handler_id",)},
                })

        output = {
            "schema_version": "2.0",
            "cloud_provider": provider,
            "deployment_method": args.deployment_method,
            "gate_status": "pending",
            "validated_at": now,
            "services": seeded_services,
            "unmatched_services": unmatched_services,
            "dropped_not_a_service": dropped_not_a_service,
            "overrides": existing.get("overrides", []),
        }

        # ═══ Deploy Infrastructure (DDR-SHARED-017 aware) ═══
        # Also detect and validate deploy-specific services during seed
        deploy_services = []
        tier_prediction = None
        try:
            from aah.core.cloud.registry_extractor import extract_services_from_registry
            deploy_extraction = extract_services_from_registry(project_path)

            is_managed = (
                deploy_extraction.get("is_cloudrun")
                or deploy_extraction.get("is_ecs_express")
                or deploy_extraction.get("is_cloudless")
            )

            if is_managed:
                target_cloud = deploy_extraction.get("target_cloud")
                raw_deploy_services = deploy_extraction.get("services", [])

                for svc in raw_deploy_services:
                    svc_result = _validate_deploy_service(svc, target_cloud, project_path)
                    deploy_services.append(svc_result)

                if target_cloud and (deploy_extraction.get("is_cloudrun") or deploy_extraction.get("is_ecs_express")):
                    from aah.core.cloud.validate_cloud_readiness import _check_tier_prediction
                    tier_prediction = _check_tier_prediction(target_cloud, project_path)
        except Exception:
            pass

        if deploy_services:
            output["deploy_services"] = deploy_services
        if tier_prediction:
            output["tier_prediction"] = tier_prediction

        aah_path.mkdir(parents=True, exist_ok=True)
        write_yaml(output, output_path)

        print(json.dumps({
            "outcome": "seeded",
            "total_services": len(seeded_services),
            "unmatched_services": len(unmatched_services),
            "deploy_services": len(deploy_services),
            "tier_prediction": tier_prediction,
            "provider": provider,
            "services": [
                {"id": s["id"], "name": s["name"], "type": s["type"], "criticality": s["criticality"], "status": s["status"]}
                for s in seeded_services
            ],
            "unmatched": [
                {"slug_id": u["slug_id"], "response": u["response"], "resolution": u["resolution"]}
                for u in unmatched_services
            ],
        }, default=str))
        sys.exit(0)

    if args.override_service:
        if not args.override_reason:
            print(json.dumps({"error": "--override-reason is required when using --override-service"}), file=sys.stderr)
            sys.exit(1)
        aah_path = project_path / ".aah"
        output_path = aah_path / "architecture" / "cloud-readiness.yaml"
        if not output_path.exists():
            print(json.dumps({"error": "cloud-readiness.yaml does not exist — run gate first"}), file=sys.stderr)
            sys.exit(1)
        state = read_yaml(output_path) or {}
        overrides = state.get("overrides") or []
        # Resolve service_id from service_type
        target_id = None
        for svc in state.get("services", []):
            if svc.get("type") == args.override_service or svc.get("service_type") == args.override_service:
                target_id = svc.get("id")
                svc["status"] = "override"
                break
        overrides.append({
            "service_type": args.override_service,
            "service_id": target_id,
            "reason": args.override_reason,
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        })
        state["overrides"] = overrides
        # Recompute gate_status
        statuses = [s.get("status") for s in state.get("services", [])]
        all_resolved = all(st in ("pass", "override") for st in statuses)
        state["gate_status"] = "passed" if all_resolved else "pending"
        state["validated_at"] = datetime.now(timezone.utc).isoformat()
        write_yaml(state, output_path)
        print(json.dumps({
            "outcome": "override-recorded",
            "service_type": args.override_service,
            "reason": args.override_reason,
            "gate_status": state["gate_status"],
        }))
        sys.exit(0)

    if args.skip:
        aah_path = project_path / ".aah"
        arch_path = aah_path / "architecture"
        arch_path.mkdir(parents=True, exist_ok=True)
        output_path = arch_path / "cloud-readiness.yaml"
        now = datetime.now(timezone.utc).isoformat()
        skip_record = {
            "schema_version": "1.0",
            "cloud_provider": None,
            "deployment_method": args.deployment_method,
            "gate_status": "skipped",
            "skip_reason": args.skip_reason,
            "validated_at": now,
            "services": [],
            "overrides": [],
        }
        write_yaml(skip_record, output_path)
        print(json.dumps({
            "outcome": "skip",
            "reason": args.skip_reason,
            "services_tested": 0,
            "services_passed": 0,
            "services_failed": 0,
            "details": [],
            "note": "User explicitly skipped cloud validation. Run `aah run core.gates.validate_cloud_readiness` later to validate.",
        }))
        sys.exit(0)

    if args.deployment_method == "local-only":
        result = {
            "outcome": "skip",
            "reason": "Deployment method is local-only — no cloud validation needed",
            "services_tested": 0,
            "services_passed": 0,
            "services_failed": 0,
            "details": [],
        }
        save_cloud_readiness(project_path, [], result, "local-only", None)
        print(json.dumps(result))
        sys.exit(0)

    # ═══ Identity Check: report who we're running as ═══
    if args.profile:
        identity = get_aws_identity(args.profile)
        if identity:
            print(json.dumps(identity), file=sys.stderr)
        else:
            print(json.dumps({
                "error": "AWS identity check failed — could not call sts:GetCallerIdentity",
                "profile": args.profile,
            }), file=sys.stderr)
            sys.exit(2)

    services_config = []
    if args.config:
        try:
            config_data = json.loads(args.config)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --config JSON: {e}"}))
            sys.exit(1)

        violations = guard_config(config_data)
        if violations:
            print(json.dumps({
                "error": "CREDENTIAL GUARD: Secret values detected in config — refusing to save",
                "violation_count": len(violations),
            }), file=sys.stderr)
            sys.exit(2)

        if isinstance(config_data, list):
            user_provided = config_data
        elif isinstance(config_data, dict) and "services" in config_data:
            user_provided = config_data["services"]
        else:
            user_provided = [config_data]

        # Merge user-provided service config with any existing seeded entries.
        # Match by service_type so re-running for one service doesn't wipe others.
        state = check_existing_state(project_path)
        existing_services = (state or {}).get("services", []) if state else []
        existing_by_type = {
            (s.get("type") or s.get("service_type")): s
            for s in existing_services
            if s.get("type") or s.get("service_type")
        }
        for svc in user_provided:
            stype = svc.get("type") or svc.get("service_type")
            if stype and stype in existing_by_type:
                merged = {**existing_by_type[stype], **svc}
                existing_by_type[stype] = merged
            elif stype:
                existing_by_type[stype] = svc
        services_config = list(existing_by_type.values())
    else:
        state = check_existing_state(project_path)
        if state and state.get("services"):
            services_config = state["services"]

    if not services_config:
        result = {
            "outcome": "no-services",
            "reason": "No service configurations provided",
            "services_tested": 0,
            "services_passed": 0,
            "services_failed": 0,
            "details": [],
        }
        print(json.dumps(result))
        sys.exit(0)

    service_filter = None
    if args.services:
        service_filter = [s.strip() for s in args.services.split(",")]

    provider = None
    if services_config:
        providers = [s.get("provider") for s in services_config if s.get("provider")]
        if providers:
            provider = max(set(providers), key=providers.count)

    if args.profile:
        for svc in services_config:
            svc["aws_profile"] = args.profile

    result = run_validation(
        services_config,
        args.deployment_method,
        project_path,
        revalidate=args.revalidate,
        service_filter=service_filter,
    )

    save_cloud_readiness(
        project_path, services_config, result, args.deployment_method, provider
    )

    print(json.dumps(result, default=str))

    if result["outcome"] == "fail":
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
