"""
aah.core.common.readiness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Loads validated cloud-readiness and data-schema-snapshot files from a
project's ``.aah/`` directory and renders the service coordinates
into deploy-time environment variables.

This module is consumed by:
- ``deploy/env_injector.py``  (injects resource env vars during deploy)
- ``plan/synthesize_analysis_context.py``  (surfaces resources to plan agents)
- ``implement/load_impl_context.py``  (wires features to real resource names)

The pipeline's cloud-readiness gate already validated that these
resources exist and are accessible. This module trusts that gate — it
just reads the config and renders coordinates. Secret *values* are
never emitted; only secret *paths* (ARN, Secret Manager name, SSM
param path) so the app can fetch at runtime via its own SDK.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from aah.core.common.hashing import canonical_json_hash as _canonical_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime-profile schema/rule versions and policy constants
# ---------------------------------------------------------------------------

# schema_version tracks the on-disk profile shape; rule_version tracks the
# resolution rules that map registry+readiness inputs to a profile. Either
# bump invalidates a prior confirmation (its hash changes).
RUNTIME_PROFILE_SCHEMA_VERSION = 1
RUNTIME_PROFILE_RULE_VERSION = 1

# Topologies that involve cloud provider anchors and therefore require the
# cloud-platform owner to co-confirm the provider anchor fields.
CLOUD_TOPOLOGIES = frozenset({"local-cloud-ready", "full-integrated-cloud"})

# Environment variable names that must never be silently overridden across
# precedence layers. A conflict on any of these across layers fails before
# startup rather than letting a later layer shadow an earlier value.
DEFAULT_PROTECTED_ENV_KEYS = frozenset({
    "PATH", "LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH",
    "AAH_VERIFICATION_WRITE", "HOME", "SHELL",
})

# Observable-boundary vocabulary. A profile's observable_boundaries must be a
# subset of these — the runtime lifecycle engine probes only these.
OBSERVABLE_BOUNDARIES = ("http", "cli", "library")


def _aah_path(project_or_aah_path: Path) -> Path:
    path = Path(project_or_aah_path)
    return path if path.name == ".aah" else path / ".aah"


def _valid_port(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return port if 1 <= port <= 65535 else None


def _read_yaml_mapping(path: Path) -> dict:
    from aah.core.common.io_utils import read_yaml
    try:
        value = read_yaml(path) if path.exists() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def resolve_runtime_port(project_or_aah_path: Path) -> int:
    """Resolve checkpoint port, stack port, primary port, then 8000."""
    aah_path = _aah_path(project_or_aah_path)
    config = _read_yaml_mapping(aah_path / "plan" / "checkpoint-config.yaml")
    checkpoint = config.get("checkpoint_configuration")
    system = checkpoint.get("system_checkpoints") if isinstance(checkpoint, dict) else None
    port = _valid_port(system.get("port") if isinstance(system, dict) else None)
    if port is not None:
        return port
    manifest = _read_yaml_mapping(aah_path / "manifest.yaml")
    stack = manifest.get("stack_choices") if isinstance(manifest, dict) else None
    if isinstance(stack, dict):
        for name in ("port", "primary_port"):
            port = _valid_port(stack.get(name))
            if port is not None:
                return port
    return 8000

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_runtime_resources(aah_path: Path) -> dict[str, Any] | None:
    """
    Load validated runtime resources from the .aah directory.

    Reads ``cloud-readiness.yaml`` and optionally ``data-schema-snapshot.yaml``.
    Returns a dict with keys:
        - cloud_provider: str (aws | gcp | azure)
        - deployment_method: str (direct-cloud | cloudless-managed | etc.)
        - services: list[dict]  (each with coordinates, secret_ref, etc.)
        - data: dict  (databases, storage, apis)

    Returns None if cloud-readiness.yaml does not exist or cannot be parsed.
    """
    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed; cannot load cloud-readiness")
        return None

    cr_path = aah_path / "architecture" / "cloud-readiness.yaml"
    if not cr_path.exists():
        logger.debug("No cloud-readiness.yaml at %s", cr_path)
        return None

    try:
        with open(cr_path, "r", encoding="utf-8") as f:
            cr = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to parse cloud-readiness.yaml: %s", exc)
        return None

    if not isinstance(cr, dict):
        return None

    # Build the resources structure expected by consumers
    resources: dict[str, Any] = {
        "cloud_provider": cr.get("cloud_provider"),
        "deployment_method": cr.get("deployment_method"),
        "gate_status": cr.get("gate_status"),
        "services": _normalize_services(cr.get("services") or []),
    }

    # Optionally load data-schema-snapshot (validated DB schemas, storage
    # prefixes, API integrations)
    data = _load_data_snapshot(aah_path)
    resources["data"] = data

    # If no services and no data, treat as empty
    if not resources["services"] and not any(data.values()):
        return None

    return resources


def render_runtime_env_vars(resources: dict[str, Any]) -> dict[str, str]:
    """
    Translate validated runtime resources into flat env-var pairs.

    Each service emits vars prefixed by its ``env_prefix`` (defaults to
    UPPER(service_type)). For example an RDS service with
    ``service_type: rds-postgres`` emits::

        RDS_POSTGRES_HOST=mydb.xxx.us-east-1.rds.amazonaws.com
        RDS_POSTGRES_PORT=5432
        RDS_POSTGRES_DB=appdb
        RDS_POSTGRES_SECRET_ARN=arn:aws:secretsmanager:...

    Secret values are NEVER emitted. Only reference paths (ARNs, param
    names) so the app fetches them at runtime.
    """
    env_vars: dict[str, str] = {}

    for svc in resources.get("services") or []:
        prefix = _env_prefix(svc)

        # Emit coordinate pairs
        coordinates = svc.get("coordinates") or {}
        for key, value in coordinates.items():
            var_name = f"{prefix}_{key.upper()}"
            env_vars[var_name] = str(value)

        # Emit secret references (paths only, never values)
        secret_ref = svc.get("secret_ref") or {}
        for key, value in secret_ref.items():
            var_name = f"{prefix}_{key.upper()}"
            env_vars[var_name] = str(value)

        # Emit auth_method for the app to know how to authenticate
        auth_method = svc.get("auth_method")
        if auth_method:
            env_vars[f"{prefix}_AUTH_METHOD"] = auth_method

    # Emit data-layer coordinates (storage buckets, table names)
    data = resources.get("data") or {}

    for db in data.get("databases") or []:
        prefix = _env_prefix(db)
        for key, value in (db.get("coordinates") or {}).items():
            env_vars[f"{prefix}_{key.upper()}"] = str(value)
        for key, value in (db.get("secret_ref") or {}).items():
            env_vars[f"{prefix}_{key.upper()}"] = str(value)

    for store in data.get("storage") or []:
        prefix = _env_prefix(store)
        for key, value in (store.get("coordinates") or {}).items():
            env_vars[f"{prefix}_{key.upper()}"] = str(value)

    for api in data.get("apis") or []:
        prefix = _env_prefix(api)
        for key, value in (api.get("coordinates") or {}).items():
            env_vars[f"{prefix}_{key.upper()}"] = str(value)
        for key, value in (api.get("secret_ref") or {}).items():
            env_vars[f"{prefix}_{key.upper()}"] = str(value)

    return env_vars


def render_cloud_auth_env(aah_path: Path) -> dict[str, str]:
    """Cross-cutting cloud auth env derived from the raw ``cloud-readiness.yaml``.

    Emits the SDK-level auth context the app-under-test (and the runtime
    validator) need but that no single feature declares: the AWS named profile
    ``/aah-access`` captured (persisted per-service as ``aws_profile``) and the
    region. These are NOT secrets — a profile name is a local ``~/.aws`` alias
    and the region is public — and they already live in ``cloud-readiness.yaml``.

    Read straight from the raw services (not the normalized/hashed structure) so
    this never perturbs the runtime-profile ``readiness_hash``. Returns ``{}``
    for non-AWS providers or when no profile was captured.
    """
    try:
        import yaml
    except ImportError:
        return {}

    cr_path = Path(aah_path) / "architecture" / "cloud-readiness.yaml"
    if not cr_path.exists():
        return {}
    try:
        cr = yaml.safe_load(cr_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Failed to parse cloud-readiness.yaml for auth env: %s", exc)
        return {}
    if not isinstance(cr, dict):
        return {}
    if str(cr.get("cloud_provider") or "").lower() != "aws":
        return {}

    profile = ""
    region = ""
    for svc in cr.get("services") or []:
        if not isinstance(svc, dict):
            continue
        if not profile and svc.get("aws_profile"):
            profile = str(svc["aws_profile"])
        if not region:
            config = svc.get("config") or {}
            if isinstance(config, dict) and config.get("region"):
                region = str(config["region"])

    out: dict[str, str] = {}
    if profile:
        out["AWS_PROFILE"] = profile
    if region:
        out["AWS_REGION"] = region
        out["AWS_DEFAULT_REGION"] = region
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_services(raw_services: list[dict]) -> list[dict]:
    """
    Normalize cloud-readiness services into the structure expected by
    consumers (with ``coordinates``, ``secret_ref``, ``auth_method``).

    Cloud-readiness.yaml services have a ``config`` dict with
    service-specific keys. We translate those into a standard
    ``coordinates`` dict based on service_type.
    """
    normalized = []
    for svc in raw_services:
        if not isinstance(svc, dict):
            continue

        out: dict[str, Any] = {
            "id": svc.get("id"),
            "display_name": svc.get("display_name") or svc.get("name"),
            "service_type": svc.get("service_type") or svc.get("type"),
            "provider": svc.get("provider"),
            "status": svc.get("status"),
            "criticality": svc.get("criticality"),
            "auth_method": _resolve_auth_method(svc),
        }

        # Build coordinates from config + well-known keys
        config = svc.get("config") or {}
        coordinates = _extract_coordinates(svc.get("service_type") or "", config, svc)
        out["coordinates"] = coordinates

        # Secret references — never include actual values
        secret_ref = _extract_secret_refs(svc.get("service_type") or "", config, svc)
        out["secret_ref"] = secret_ref

        # Preserve env_prefix override if specified
        if svc.get("env_prefix"):
            out["env_prefix"] = svc["env_prefix"]

        normalized.append(out)

    return normalized


def _resolve_auth_method(svc: dict) -> str | None:
    """Extract auth method from service config or auth_options."""
    config = svc.get("config") or {}
    if config.get("auth_method"):
        return config["auth_method"]

    auth_options = svc.get("auth_options") or []
    if auth_options and isinstance(auth_options, list):
        return auth_options[0].get("method")

    return None


def _extract_coordinates(service_type: str, config: dict, svc: dict) -> dict[str, str]:
    """
    Map service config to coordinate key-value pairs based on service type.

    These are non-secret connection parameters the app needs to find
    the resource.
    """
    coords: dict[str, str] = {}
    st = service_type.lower().replace("-", "_")

    # RDS / Aurora / Postgres / MySQL
    if any(k in st for k in ("rds", "postgres", "mysql", "aurora")):
        if config.get("host"):
            coords["host"] = config["host"]
        if config.get("endpoint"):
            coords["host"] = config["endpoint"]
        if config.get("port"):
            coords["port"] = str(config["port"])
        if config.get("database") or config.get("db_name"):
            coords["db"] = config.get("database") or config.get("db_name")
        if config.get("region"):
            coords["region"] = config["region"]

    # S3
    elif "s3" in st:
        if config.get("bucket") or config.get("bucket_name"):
            coords["bucket"] = config.get("bucket") or config.get("bucket_name")
        if config.get("region"):
            coords["region"] = config["region"]
        if config.get("prefix"):
            coords["prefix"] = config["prefix"]

    # DynamoDB
    elif "dynamo" in st:
        if config.get("table") or config.get("table_name"):
            coords["table"] = config.get("table") or config.get("table_name")
        if config.get("region"):
            coords["region"] = config["region"]

    # ElastiCache / Redis
    elif any(k in st for k in ("redis", "elasticache", "cache")):
        if config.get("host") or config.get("endpoint"):
            coords["host"] = config.get("host") or config.get("endpoint")
        if config.get("port"):
            coords["port"] = str(config["port"])

    # SQS
    elif "sqs" in st:
        if config.get("queue_url"):
            coords["queue_url"] = config["queue_url"]
        if config.get("queue_name"):
            coords["queue_name"] = config["queue_name"]
        if config.get("region"):
            coords["region"] = config["region"]

    # SNS
    elif "sns" in st:
        if config.get("topic_arn"):
            coords["topic_arn"] = config["topic_arn"]
        if config.get("region"):
            coords["region"] = config["region"]

    # ECS (compute — cluster name is the coordinate)
    elif "ecs" in st:
        if config.get("cluster_name"):
            coords["cluster"] = config["cluster_name"]
        if config.get("region"):
            coords["region"] = config["region"]

    # Cloud Run / GCR
    elif any(k in st for k in ("cloud_run", "cloudrun", "gcr")):
        if config.get("service_url"):
            coords["service_url"] = config["service_url"]
        if config.get("project"):
            coords["project"] = config["project"]
        if config.get("region"):
            coords["region"] = config["region"]

    # GCS (Google Cloud Storage)
    elif "gcs" in st:
        if config.get("bucket") or config.get("bucket_name"):
            coords["bucket"] = config.get("bucket") or config.get("bucket_name")
        if config.get("project"):
            coords["project"] = config["project"]

    # Cloud SQL
    elif "cloud_sql" in st or "cloudsql" in st:
        if config.get("instance_connection_name"):
            coords["instance"] = config["instance_connection_name"]
        if config.get("host"):
            coords["host"] = config["host"]
        if config.get("port"):
            coords["port"] = str(config["port"])
        if config.get("database"):
            coords["db"] = config["database"]

    # Fallback: dump all config keys that look like coordinates
    else:
        for key, val in config.items():
            if key in ("auth_method", "auth_options"):
                continue
            if isinstance(val, str) and val:
                coords[key] = val
            elif isinstance(val, (int, float)):
                coords[key] = str(val)

    return coords


def _extract_secret_refs(service_type: str, config: dict, svc: dict) -> dict[str, str]:
    """
    Extract secret reference paths (ARNs, SSM params, secret names).

    Only paths are extracted — never actual credential values.
    """
    refs: dict[str, str] = {}

    # Common secret-ref patterns. ``secret_path`` is the field /aah-access
    # actually persists for secret-manager references (see the cloud-readiness
    # gate's ``resolve_credentials``), so it must be captured for the ARN/path to
    # reach the app — only the reference is emitted, never the secret value.
    for key in ("secret_arn", "secret_name", "secret_path", "ssm_param",
                "password_arn", "credentials_arn", "connection_string_arn",
                "api_key_arn", "secret_ref", "kms_key_id"):
        if config.get(key):
            refs[key] = config[key]

    # Also check top-level svc dict
    if svc.get("secret_ref") and isinstance(svc["secret_ref"], dict):
        refs.update(svc["secret_ref"])

    return refs


def _env_prefix(resource: dict) -> str:
    """
    Determine the env var prefix for a resource.

    Priority: explicit env_prefix > UPPER(service_type) > UPPER(id)
    """
    if resource.get("env_prefix"):
        return resource["env_prefix"].upper()

    st = resource.get("service_type") or resource.get("type") or resource.get("id") or "RESOURCE"
    # Normalize: rds-postgres -> RDS_POSTGRES
    return st.upper().replace("-", "_").replace(" ", "_")


# ---------------------------------------------------------------------------
# Runtime profile resolution
# ---------------------------------------------------------------------------


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _profile_hash(hashed_block: dict[str, Any]) -> str:
    """Deterministic hash over a profile's hashed block.

    ``profile_hash`` itself is excluded so the field can be embedded in the
    same dict without a chicken-and-egg dependency.
    """
    block = {k: v for k, v in hashed_block.items() if k != "profile_hash"}
    return _canonical_hash(block)


def _extract_provider_anchor(resources: dict[str, Any] | None) -> dict[str, str]:
    """Pull provider/project/region anchor from validated readiness.

    Deterministic: takes the first non-empty coordinate found while walking
    services then data resources in file order.
    """
    provider = ""
    project = ""
    region = ""
    if isinstance(resources, dict):
        provider = str(resources.get("cloud_provider") or "")
        buckets: list[dict] = []
        buckets.extend(resources.get("services") or [])
        data = resources.get("data") or {}
        for key in ("databases", "storage", "apis"):
            buckets.extend(data.get(key) or [])
        for res in buckets:
            coords = (res.get("coordinates") or {}) if isinstance(res, dict) else {}
            if not project and coords.get("project"):
                project = str(coords["project"])
            if not region and coords.get("region"):
                region = str(coords["region"])
    return {"provider": provider, "project": project, "region": region}


def _observable_boundaries(topology: str, resources: dict[str, Any] | None) -> list[str]:
    """Observable boundaries applicable to this deployment (deterministic)."""
    boundaries = {"library"}
    env_vars = render_runtime_env_vars(resources) if isinstance(resources, dict) else {}
    has_surface = bool(env_vars) or topology in CLOUD_TOPOLOGIES
    if has_surface:
        boundaries.add("http")
    if topology in {"full-local", "local-cloud-ready"}:
        boundaries.add("cli")
    return sorted(boundaries)


def _required_checks(topology: str, boundaries: list[str]) -> list[str]:
    """Runtime checks required for this topology (deterministic, sorted)."""
    checks = {"build", "boot"}
    if "http" in boundaries:
        checks.add("smoke")
    if topology in CLOUD_TOPOLOGIES:
        checks.add("cloud_readiness")
    return sorted(checks)


def _project_anchor(aah_path: Path) -> dict[str, str]:
    """Project-identity anchor. Binds a confirmation to this project.

    Uses the manifest ``project_name`` — a copied confirmation dropped into a
    different project resolves to a different anchor (wrong-project detection).
    """
    name = ""
    try:
        import yaml

        manifest_path = Path(aah_path) / "manifest.yaml"
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}
            if isinstance(manifest, dict):
                name = str(manifest.get("project_name") or "")
    except Exception:
        name = ""
    return {"project_name": name}


def resolve_runtime_profile(
    aah_path: Path,
    registry_context: dict[str, Any],
    readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve a versioned runtime profile from confirmed decisions + readiness.

    ``registry_context`` is the ``context`` block of decision-registry.yaml. If
    it carries a ``runtime_validation`` block that is used directly; otherwise
    the block is derived from the ``/aah-discuss`` slug registry (the
    ``development-methodology`` / ``docker-installed`` slugs) via
    ``runtime_decision.derive_runtime_validation``. ``readiness`` is the dict
    returned by ``load_runtime_resources`` (or None when no cloud-readiness is
    present).

    Raises ``ValueError`` when no resolved runtime decisions exist — a profile
    can never be resolved from missing inputs (the gate then fails closed).

    Returns a flat profile dict. Every field except ``profile_hash`` is inside
    the hashed block; timestamps and confirmation status are added later by the
    proposal writer and live OUTSIDE the hashed block.
    """
    runtime_validation = (registry_context or {}).get("runtime_validation")
    if not isinstance(runtime_validation, dict):
        # No pre-materialized block → derive it from the discuss slug registry.
        # The discuss registry lives at the project root (parent of .aah).
        from aah.core.common.runtime_decision import derive_runtime_validation

        runtime_validation = derive_runtime_validation(aah_path.parent)
    if not isinstance(runtime_validation, dict):
        raise ValueError(
            "runtime_validation could not be resolved; set the "
            "'development-methodology' decision in the discuss registry "
            "before resolving a runtime profile"
        )

    topology = str(runtime_validation.get("deployment_topology") or "")
    transport = runtime_validation.get("local_transport")
    decision_hash = str(runtime_validation.get("source_hash") or "")
    # Source entries are slug-keyed (``slug_id``); tolerate the legacy
    # ``ddr_id`` key for any pre-existing materialized blocks.
    source_decisions = [
        str(src.get("slug_id") or src.get("ddr_id"))
        for src in runtime_validation.get("source_decisions") or []
        if isinstance(src, dict) and (src.get("slug_id") or src.get("ddr_id"))
    ]

    # Normalised readiness for hashing: the resources dict is already
    # values-free (paths only), and _canonical_hash sorts keys so the hash is
    # stable regardless of file key order.
    normalized_readiness = readiness if isinstance(readiness, dict) else {}
    readiness_hash = _canonical_hash(normalized_readiness)

    boundaries = _observable_boundaries(topology, readiness)
    allowed_env_keys = sorted(
        (render_runtime_env_vars(readiness) if isinstance(readiness, dict) else {}).keys()
    )

    profile: dict[str, Any] = {
        "schema_version": RUNTIME_PROFILE_SCHEMA_VERSION,
        "rule_version": RUNTIME_PROFILE_RULE_VERSION,
        "deployment_topology": topology,
        "decision_hash": decision_hash,
        "readiness_hash": readiness_hash,
        "required_checks": _required_checks(topology, boundaries),
        "provider_anchor": _extract_provider_anchor(readiness),
        "project_anchor": _project_anchor(aah_path),
        "source_decisions": source_decisions,
        "allowed_env_keys": allowed_env_keys,
        "observable_boundaries": boundaries,
    }
    if transport is not None:
        profile["local_transport"] = str(transport)

    profile["profile_hash"] = _profile_hash(profile)
    from aah.core.common.validators import validate_runtime_profile

    errors = validate_runtime_profile(profile)
    if errors:
        raise ValueError("invalid runtime profile: " + "; ".join(errors))
    return profile


# ---------------------------------------------------------------------------
# Validated environment assembly
# ---------------------------------------------------------------------------


def _valid_env_key(key: str) -> bool:
    if not key:
        return False
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in key)


def _strip_env_quotes(value: str) -> str:
    """Strip a single matching pair of surrounding quotes. No interpolation."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_file(
    path: Path,
    declared_keys,
    protected_keys,
    *,
    ignore_undeclared: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Parse a ``.env`` file WITHOUT interpolation or command execution.

    Values are taken literally: ``$VAR``, ``${VAR}`` and ``$(cmd)`` are NEVER
    expanded. A single pair of surrounding quotes is stripped; everything else
    is preserved byte-for-byte.

    Rejects (as error strings, aggregated — never raises):
      * malformed lines (no ``=`` / invalid key name),
      * duplicate keys,
      * protected keys,
      * keys not declared in the profile allowlist, unless
        ``ignore_undeclared`` is enabled for a scoped consumer.

    Returns ``(parsed, errors)``. ``parsed`` contains only clean, admitted
    keys; ``errors`` is empty on a fully valid file.
    """
    declared = set(declared_keys or [])
    protected = set(protected_keys or [])
    parsed: dict[str, str] = {}
    errors: list[str] = []

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f".env unreadable: {type(exc).__name__}"]

    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            errors.append(f".env line {lineno}: malformed (no '=')")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _strip_env_quotes(value.strip())
        if not _valid_env_key(key):
            errors.append(f".env line {lineno}: invalid key name '{key}'")
            continue
        if key in seen:
            errors.append(f".env line {lineno}: duplicate key '{key}'")
            continue
        seen.add(key)
        if key in protected:
            errors.append(f".env line {lineno}: protected key '{key}' may not be set via .env")
            continue
        if key not in declared:
            if ignore_undeclared:
                continue
            errors.append(f".env line {lineno}: undeclared key '{key}' not in profile allowlist")
            continue
        parsed[key] = value

    return parsed, errors


def assemble_runtime_env(
    profile: dict[str, Any],
    process_env,
    project_env,
    cmd_env,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Assemble the runtime environment with deterministic layered precedence.

    Precedence (lowest to highest): inherited ``process_env`` < validated
    project ``.env`` (``project_env``) < adapter ``cmd_env``. Only keys in
    ``profile['allowed_env_keys']`` are admitted; the ambient process
    environment is silently filtered to the allowlist, while a curated layer
    (project/cmd) that carries an undeclared key fails closed.

    A protected key (PATH, PYTHONPATH, …) set by any curated layer is a policy
    violation and raises ``ValueError`` BEFORE any startup — a later layer must
    never silently shadow the platform environment.

    Returns ``(resolved_env, evidence)``. Evidence records source layer NAMES
    and per-key ``value_sha256`` ONLY — never a raw value.
    """
    allowed = set(profile.get("allowed_env_keys") or [])
    protected = set(DEFAULT_PROTECTED_ENV_KEYS) | set(profile.get("protected_env_keys") or [])

    layers = [
        ("process_env", dict(process_env or {})),
        ("project_env", dict(project_env or {})),
        ("cmd_env", dict(cmd_env or {})),
    ]

    # Curated layers may not touch protected keys or undeclared keys.
    for name, layer in layers[1:]:
        for key in layer:
            if key in protected:
                raise ValueError(
                    f"protected env key '{key}' may not be overridden by {name}"
                )
            if key not in allowed:
                raise ValueError(
                    f"undeclared env key '{key}' not in profile allowlist ({name})"
                )

    resolved: dict[str, str] = {}
    provenance: dict[str, list[str]] = {}
    for name, layer in layers:
        for key, value in layer.items():
            if key not in allowed:
                continue
            resolved[key] = str(value)
            provenance.setdefault(key, []).append(name)

    evidence = {
        "allowed_env_keys": sorted(allowed),
        "resolved_keys": sorted(resolved),
        "sources": {
            key: {
                "layers": provenance[key],
                "final_source": provenance[key][-1],
                "value_sha256": _sha256_hex(resolved[key]),
            }
            for key in sorted(resolved)
        },
    }
    return resolved, evidence


def _load_data_snapshot(aah_path: Path) -> dict[str, Any]:
    """
    Load data-schema-snapshot.yaml if it exists.

    Returns a dict with optional keys: databases, storage, apis
    """
    try:
        import yaml
    except ImportError:
        return {"databases": [], "storage": [], "apis": []}

    snap_path = aah_path / "architecture" / "data-schema-snapshot.yaml"
    if not snap_path.exists():
        return {"databases": [], "storage": [], "apis": []}

    try:
        with open(snap_path, "r", encoding="utf-8") as f:
            snap = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to parse data-schema-snapshot.yaml: %s", exc)
        return {"databases": [], "storage": [], "apis": []}

    if not isinstance(snap, dict):
        return {"databases": [], "storage": [], "apis": []}

    return {
        "databases": snap.get("databases") or [],
        "storage": snap.get("storage") or [],
        "apis": snap.get("apis") or [],
    }
