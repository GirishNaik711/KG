#!/usr/bin/env python3
"""Shared validation primitives for schema checking, file existence, and structure validation."""

from pathlib import Path
from typing import Any


class ValidationError(Exception):
    """Raised when validation fails."""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]


def validate_required_fields(data: dict, required: list[str], context: str = "") -> list[str]:
    """Check that all required fields exist in a dict. Returns list of error messages."""
    errors = []
    for field in required:
        if field not in data or data[field] is None:
            prefix = f"{context}: " if context else ""
            errors.append(f"{prefix}missing required field '{field}'")
    return errors


def validate_field_type(data: dict, field: str, expected_type: type, context: str = "") -> list[str]:
    """Validate that a field, if present, is of the expected type."""
    if field not in data:
        return []
    if not isinstance(data[field], expected_type):
        prefix = f"{context}: " if context else ""
        actual = type(data[field]).__name__
        expected = expected_type.__name__
        return [f"{prefix}field '{field}' expected {expected}, got {actual}"]
    return []


def validate_file_exists(path: Path, context: str = "") -> list[str]:
    """Validate that a file exists. Returns list of error messages."""
    if not path.exists():
        prefix = f"{context}: " if context else ""
        return [f"{prefix}file not found: {path}"]
    return []


def validate_dir_exists(path: Path, context: str = "") -> list[str]:
    """Validate that a directory exists. Returns list of error messages."""
    if not path.is_dir():
        prefix = f"{context}: " if context else ""
        return [f"{prefix}directory not found: {path}"]
    return []


def validate_dict_schema(data: dict, schema: dict[str, dict[str, Any]], context: str = "") -> list[str]:
    """
    Validate a dict against a simple schema.

    (Renamed from the YAML-feature era; this never touched YAML — the caller
    parses the .md to a dict first, then validates the dict here.)

    Schema format:
        {
            "field_name": {
                "required": True/False,
                "type": str/int/list/dict/etc,
                "allowed_values": [...]  # optional
            }
        }
    """
    errors = []
    prefix = f"{context}: " if context else ""

    for field, rules in schema.items():
        if rules.get("required", False) and (field not in data or data[field] is None):
            errors.append(f"{prefix}missing required field '{field}'")
            continue

        if field not in data or data[field] is None:
            continue

        expected_type = rules.get("type")
        if expected_type and not isinstance(data[field], expected_type):
            actual = type(data[field]).__name__
            if isinstance(expected_type, tuple):
                expected = "/".join(t.__name__ for t in expected_type)
            else:
                expected = expected_type.__name__
            errors.append(f"{prefix}field '{field}' expected {expected}, got {actual}")

        allowed = rules.get("allowed_values")
        if allowed and data[field] not in allowed:
            errors.append(f"{prefix}field '{field}' value '{data[field]}' not in {allowed}")

        nested_validator = rules.get("validator")
        if nested_validator and (not expected_type or isinstance(data[field], expected_type)):
            errors.extend(nested_validator(data[field], f"{context}.{field}" if context else field))

    return errors


VERIFICATION_RISK_FIELDS = (
    "security_scope", "auth_scope", "payment_scope", "credential_scope",
    "critical_nfr", "regulated_data", "external_integration",
    "shared_interface", "shared_schema", "shared_contract", "concurrency",
    "migration", "integrity",
)


def validate_verification_risk_metadata(data: dict, context: str = "risk_metadata") -> list[str]:
    """Validate an explicitly declared risk block; legacy omission remains valid."""
    errors = []
    for field in VERIFICATION_RISK_FIELDS:
        if field not in data:
            errors.append(f"{context}: missing required field '{field}'")
        elif type(data[field]) is not bool:
            errors.append(f"{context}: field '{field}' expected bool")
    return errors


def validate_required_env_keys(data: list, context: str = "required_env") -> list[str]:
    """Validate a feature's environment-key names without accepting values."""
    from aah.core.common.readiness import DEFAULT_PROTECTED_ENV_KEYS

    errors: list[str] = []
    seen: set[str] = set()
    for index, raw_key in enumerate(data):
        item_context = f"{context}[{index}]"
        if not isinstance(raw_key, str):
            errors.append(f"{item_context}: expected str, got {type(raw_key).__name__}")
            continue
        key = raw_key.strip()
        if not key:
            errors.append(f"{item_context}: environment key must not be empty")
            continue
        if not (key[0].isalpha() or key[0] == "_") or not all(
            char.isalnum() or char == "_" for char in key
        ):
            errors.append(f"{item_context}: invalid environment key '{key}'")
            continue
        if key in DEFAULT_PROTECTED_ENV_KEYS:
            errors.append(f"{item_context}: protected environment key '{key}' is not allowed")
            continue
        if key in seen:
            errors.append(f"{item_context}: duplicate environment key '{key}'")
            continue
        seen.add(key)
    return errors


def acceptance_criterion_fields(ac: object, index: int) -> tuple[str | None, str]:
    """Return the stable id/description used by feature-contract readers."""
    if isinstance(ac, dict):
        return ac.get("id"), ac.get("description", "") or ""
    if isinstance(ac, str):
        return f"AC{index + 1}", ac
    return None, str(ac)


def validate_feature_contract(feature: dict, context: str = "") -> list[str]:
    """Structural validation of a feature's optional acceptance_criteria / test_cases.

    v2 (lean/TDD): ``acceptance_criteria`` and ``test_cases`` are no longer part
    of the feature spec. Tests are emergent via TDD in the build phase, and the
    definition of done is the passing test suite the implementer writes — there
    is no plan-time AC↔test coverage contract to enforce. This function is now a
    light structural check that fires ONLY when a legacy/brownfield feature still
    carries these fields: it verifies they are lists and that any ids present are
    unique. New (5-section) features carry neither field and pass trivially.

    Returns a list of error strings (does NOT raise — matches the
    validate_dict_schema convention; the caller decides whether to exit).
    """
    errors: list[str] = []
    prefix = f"{context}: " if context else ""

    acs = feature.get("acceptance_criteria")
    tcs = feature.get("test_cases")

    if acs is not None and not isinstance(acs, list):
        errors.append(
            f"{prefix}acceptance_criteria must be a list, got {type(acs).__name__}"
        )
    if tcs is not None and not isinstance(tcs, list):
        errors.append(
            f"{prefix}test_cases must be a list, got {type(tcs).__name__}"
        )

    # Cheap duplicate-id sanity on any legacy content — no coverage premise.
    if isinstance(acs, list):
        seen_ac: set[str] = set()
        for idx, ac in enumerate(acs):
            ac_id = acceptance_criterion_fields(ac, idx)[0]
            if ac_id:
                if ac_id in seen_ac:
                    errors.append(f"{prefix}duplicate acceptance-criterion id '{ac_id}'")
                seen_ac.add(ac_id)
    if isinstance(tcs, list):
        seen_tc: set[str] = set()
        for tc in tcs:
            if isinstance(tc, dict):
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and tc_id.strip():
                    if tc_id in seen_tc:
                        errors.append(f"{prefix}duplicate test-case id '{tc_id}'")
                    seen_tc.add(tc_id)

    return errors


def validate_aah_dir_structure(aah_path: Path) -> list[str]:
    """Validate the .aah/ directory has the expected structure."""
    expected_dirs = [
        "research",
        "analysis",
        "analysis/decisions",
        "plan",
        "plan/specs",
        "plan/features",
        "plan/sprint-contracts",
        "build",
        "build/test-results",
        "build/quality-results",
        "deploy",
        "deploy/infra",
        "brownfield",
        "audit",
    ]
    expected_files = [
        "manifest.yaml",
    ]

    errors = []
    for d in expected_dirs:
        dir_path = aah_path / d
        if not dir_path.is_dir():
            errors.append(f"missing directory: .aah/{d}")

    for f in expected_files:
        file_path = aah_path / f
        if not file_path.exists():
            errors.append(f"missing file: .aah/{f}")

    return errors


MANIFEST_SCHEMA = {
    "project_name": {"required": True, "type": str},
    "project_type": {"required": True, "type": str, "allowed_values": ["greenfield", "brownfield"]},
    "current_phase": {
        "required": True,
        "type": str,
        "allowed_values": ["init", "discuss", "architecture", "plan", "build", "deploy", "complete"],
    },
    "complexity_tier": {
        "required": False,
        "type": str,
        "allowed_values": ["trivial", "moderate", "significant", "complex"],
    },
    "industry_domain_path": {"required": False, "type": str},
    "execution_mode": {
        "required": False,
        "type": str,
        "allowed_values": ["aah_root", "delegated"],
    },
    "delegated_to": {"required": False, "type": str},
    "delegation_timestamp": {"required": False, "type": str},
    "synthesis_mode": {
        "required": False,
        "type": str,
        "allowed_values": ["light", "standard", "full"],
    },
    "features": {"required": False, "type": dict},
    # Optional issue-tracker sync config; read by core.version_control.config.
    "version_control": {"required": False, "type": dict},
}


FEATURE_SCHEMA = {
    "id": {"required": True, "type": str},
    "title": {"required": True, "type": str},
    "module_ref": {"required": True, "type": str},
    "description": {"required": True, "type": str},
    "layers": {"required": True, "type": list},
    "dependencies": {"required": False, "type": list},
    # v2 (lean/TDD): File Scope, Acceptance Criteria and Test Cases were removed
    # from the feature spec — modules own their scope, and tests are emergent via
    # TDD in the build phase. Kept as OPTIONAL so legacy/brownfield feature files
    # that still carry them continue to validate.
    "file_scope": {"required": False, "type": list},
    "acceptance_criteria": {"required": False, "type": list},
    "test_cases": {"required": False, "type": list},
    # ``status`` is also the provider-neutral lifecycle projected to issue
    # trackers.  Keep the legacy build-state spellings readable during the
    # migration, but accept every lifecycle value written by update-lifecycle.
    "status": {
        "required": False,
        "type": str,
        "allowed_values": [
            "pending", "in_progress", "passed", "failed",
            "planned", "queued", "implementing", "in_qa", "done",
            "blocked", "rework", "cancelled",
        ],
    },
    "constraints": {"required": False, "type": list},
    "knowledge_used": {"required": False, "type": (dict, list, str)},
    # Markdown contracts with ``###`` subsections parse this descriptive field
    # as a list; dict and plain-string forms are also established formats.
    "implementation_reasoning": {"required": False, "type": (dict, list, str)},
    "applicable_standards": {"required": False, "type": list},
    "layer": {"required": False, "type": str},
    "risk_metadata": {
        "required": False,
        "type": dict,
        "validator": validate_verification_risk_metadata,
    },
    "verification_risk": {"required": False, "type": dict},
    "security_scope": {"required": False, "type": bool},
    "auth_scope": {"required": False, "type": bool},
    "authentication_scope": {"required": False, "type": bool},
    "payment_scope": {"required": False, "type": bool},
    "credential_scope": {"required": False, "type": bool},
    "critical_nfr": {"required": False, "type": bool},
    "regulated_data": {"required": False, "type": bool},
    "external_integration": {"required": False, "type": bool},
    # Env keys an external integration needs at test time (keys only — values
    # live in the gitignored root .env, never committed). Presence gates the
    # test run: run_feature_tests blocks with no_signal if any key is unset.
    "required_env": {
        "required": False,
        "type": list,
        "validator": validate_required_env_keys,
    },
    "shared_interface": {"required": False, "type": bool},
    "shared_schema": {"required": False, "type": bool},
    "shared_contract": {"required": False, "type": bool},
    "concurrency": {"required": False, "type": bool},
    "migration": {"required": False, "type": bool},
    "integrity": {"required": False, "type": bool},
    "verification_profile_override": {
        "required": False,
        "type": str,
        "allowed_values": ["standard", "deep"],
    },
}


WORKSPACE_SCHEMA = {
    "workspace_name": {"required": True, "type": str},
    "projects": {"required": True, "type": list},
}


INTAKE_SCHEMA = {
    "version": {"required": True, "type": str},
    "status": {
        "required": True,
        "type": str,
        "allowed_values": ["not_started", "in_progress", "complete"],
    },
    "problem_statement": {"required": False, "type": str},
    "project_type": {
        "required": False,
        "type": str,
        "allowed_values": ["new_system", "enhancement", "bug_fix", "migration"],
    },
    "rounds": {"required": True, "type": list},
    # industry_domain is a dict with path/confidence/source/rationale/classified_at
    # written by aah.core.domain_briefs.classifier. None means no classifier
    # has run or the user declined a domain.
    "industry_domain": {"required": False, "type": dict},
}


# ---------------------------------------------------------------------------
# Runtime profile shape validation
# ---------------------------------------------------------------------------

_HEX64 = set("0123456789abcdef")

# Observable-boundary vocabulary; a profile's observable_boundaries must be a
# subset. Kept as a literal here so validators has no import-time dependency on
# readiness (which imports yaml lazily); readiness holds the canonical tuple.
OBSERVABLE_BOUNDARY_VOCAB = frozenset({"http", "cli", "library"})

# Field names that must NEVER carry a secret value inside a runtime profile.
# A profile records env-key NAMES only; a value-bearing secret field is a
# redaction breach and is rejected.
_PROFILE_FORBIDDEN_SECRET_FIELDS = (
    "env_values", "env", "secrets", "secret_values", "credentials",
    "resolved_env", "values",
)


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= _HEX64


RUNTIME_PROFILE_SCHEMA = {
    "schema_version": {"required": True, "type": int},
    "rule_version": {"required": True, "type": int},
    "deployment_topology": {"required": True, "type": str},
    "decision_hash": {"required": True, "type": str},
    "readiness_hash": {"required": True, "type": str},
    "profile_hash": {"required": True, "type": str},
    "required_checks": {"required": True, "type": list},
    "provider_anchor": {"required": True, "type": dict},
    "project_anchor": {"required": True, "type": dict},
    "source_decisions": {"required": True, "type": list},
    "allowed_env_keys": {"required": True, "type": list},
    "observable_boundaries": {"required": True, "type": list},
}


def validate_runtime_profile(profile: dict, context: str = "runtime_profile") -> list[str]:
    """Validate a resolved runtime profile's shape. Returns list of errors.

    Pure/aggregating (never raises). Enforces:
      * required fields present with correct types (via RUNTIME_PROFILE_SCHEMA),
      * schema_version / rule_version present as ints,
      * decision/readiness/profile hashes are 64-char lowercase hex,
      * required_checks is a list[str]; allowed_env_keys is a list[str],
      * observable_boundaries is a subset of the boundary vocabulary,
      * NO value-bearing secret field is present (names-only invariant).
    """
    errors: list[str] = []
    prefix = f"{context}: " if context else ""

    if not isinstance(profile, dict):
        return [f"{prefix}profile must be a mapping, got {type(profile).__name__}"]

    errors.extend(validate_dict_schema(profile, RUNTIME_PROFILE_SCHEMA, context=context))

    for field in ("decision_hash", "readiness_hash", "profile_hash"):
        if field in profile and profile[field] is not None and not _is_hex64(profile[field]):
            errors.append(f"{prefix}field '{field}' must be 64-char lowercase hex")

    checks = profile.get("required_checks")
    if isinstance(checks, list) and not all(isinstance(c, str) for c in checks):
        errors.append(f"{prefix}required_checks must be a list of strings")

    keys = profile.get("allowed_env_keys")
    if isinstance(keys, list) and not all(isinstance(k, str) for k in keys):
        errors.append(f"{prefix}allowed_env_keys must be a list of strings")

    boundaries = profile.get("observable_boundaries")
    if isinstance(boundaries, list):
        extra = [b for b in boundaries if b not in OBSERVABLE_BOUNDARY_VOCAB]
        if extra:
            errors.append(
                f"{prefix}observable_boundaries {extra} not subset of {sorted(OBSERVABLE_BOUNDARY_VOCAB)}"
            )

    for field in _PROFILE_FORBIDDEN_SECRET_FIELDS:
        if field in profile:
            errors.append(
                f"{prefix}field '{field}' is forbidden: runtime profile records env-key names only, never values"
            )

    return errors


# ---------------------------------------------------------------------------
# Semantic smoke assertion vocabulary
# ---------------------------------------------------------------------------

SMOKE_SCHEMA_VERSION = 2

# The supported semantic assertion types. Anything outside this set is rejected
# at plan validation — smoke assertions are a bounded vocabulary, not arbitrary
# code.
ALLOWED_ASSERTION_TYPES = frozenset({
    "status_in",
    "content_type_is",
    "header_present",
    "header_absent",
    "json_path_exists",
    "json_type_is",
    "json_equals",
    "collection_length_lte",
})

# Assertion keys that would capture a full response body / raw snapshot — these
# are volatile and forbidden (a smoke assertion must be a semantic claim).
_FORBIDDEN_SNAPSHOT_KEYS = frozenset({
    "body_equals", "response_equals", "raw_body", "body_snapshot",
    "full_body", "response_body", "snapshot",
})

# Keys that smuggle arbitrary evaluation into an assertion.
_FORBIDDEN_EXPRESSION_KEYS = frozenset({
    "eval", "expr", "expression", "lambda", "python", "exec", "code",
})

# json_type_is allowed type names.
_JSON_TYPE_NAMES = frozenset({
    "object", "array", "string", "number", "boolean", "null", "integer",
})


def _is_secret_target(name: str) -> bool:
    lower = str(name).lower()
    if lower in ("authorization",):
        return True
    return lower.endswith("_secret") or lower.endswith("_token") or lower.endswith("_arn") \
        or "secret" in lower or "password" in lower


def validate_smoke_assertion(assertion: dict, context: str = "assertion") -> list[str]:
    """Validate a single semantic smoke assertion. Returns list of errors.

    Rejects unsupported types, full-body snapshots, arbitrary expressions, and
    secret-bearing assertions. Pure/aggregating.
    """
    errors: list[str] = []
    prefix = f"{context}: " if context else ""

    if not isinstance(assertion, dict):
        return [f"{prefix}assertion must be a mapping, got {type(assertion).__name__}"]

    # Forbidden snapshot / expression keys (checked regardless of declared type).
    for key in assertion:
        kl = str(key).lower()
        if kl in _FORBIDDEN_SNAPSHOT_KEYS:
            errors.append(f"{prefix}full-body snapshot assertion '{key}' is forbidden")
        if kl in _FORBIDDEN_EXPRESSION_KEYS:
            errors.append(f"{prefix}arbitrary-expression assertion '{key}' is forbidden")

    a_type = assertion.get("type")
    if a_type is None:
        errors.append(f"{prefix}missing assertion 'type'")
        return errors
    if a_type not in ALLOWED_ASSERTION_TYPES:
        errors.append(
            f"{prefix}unsupported assertion type '{a_type}' (allowed: {sorted(ALLOWED_ASSERTION_TYPES)})"
        )
        return errors

    # Secret-bearing assertions: reject if a header/target names a secret.
    for target_key in ("header", "name", "key"):
        target = assertion.get(target_key)
        if isinstance(target, str) and _is_secret_target(target):
            errors.append(f"{prefix}assertion on secret target '{target}' is forbidden")

    if a_type == "status_in":
        values = assertion.get("values")
        if not isinstance(values, list) or not values or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in values
        ):
            errors.append(f"{prefix}status_in requires a non-empty list of int status codes")
    elif a_type == "content_type_is":
        if not isinstance(assertion.get("value"), str) or not assertion["value"].strip():
            errors.append(f"{prefix}content_type_is requires a non-empty string 'value'")
    elif a_type in ("header_present", "header_absent"):
        if not isinstance(assertion.get("header"), str) or not assertion["header"].strip():
            errors.append(f"{prefix}{a_type} requires a non-empty string 'header'")
    elif a_type == "json_path_exists":
        if not isinstance(assertion.get("path"), str) or not assertion["path"].strip():
            errors.append(f"{prefix}json_path_exists requires a non-empty string 'path'")
    elif a_type == "json_type_is":
        if not isinstance(assertion.get("path"), str) or not assertion["path"].strip():
            errors.append(f"{prefix}json_type_is requires a non-empty string 'path'")
        if assertion.get("json_type") not in _JSON_TYPE_NAMES:
            errors.append(
                f"{prefix}json_type_is requires 'json_type' in {sorted(_JSON_TYPE_NAMES)}"
            )
    elif a_type == "json_equals":
        if not isinstance(assertion.get("path"), str) or not assertion["path"].strip():
            errors.append(f"{prefix}json_equals requires a non-empty string 'path'")
        if "value" not in assertion:
            errors.append(f"{prefix}json_equals requires a 'value'")
    elif a_type == "collection_length_lte":
        if not isinstance(assertion.get("path"), str) or not assertion["path"].strip():
            errors.append(f"{prefix}collection_length_lte requires a non-empty string 'path'")
        bound = assertion.get("max")
        if not (isinstance(bound, int) and not isinstance(bound, bool) and bound >= 0):
            errors.append(f"{prefix}collection_length_lte requires integer 'max' >= 0")

    return errors


_LEGACY_SMOKE_STEP_FIELDS = frozenset({
    "method", "path", "headers", "body", "expected_status",
    "expected_body_contains",
})


def _step_is_health(step: dict) -> bool:
    """A health step probes a health/readiness/liveness path or is so typed."""
    if str(step.get("type", "")).lower() in ("api_health", "health"):
        return True
    request = step.get("request") if isinstance(step.get("request"), dict) else {}
    path = str(request.get("path") or "").lower()
    return any(tok in path for tok in ("/health", "/healthz", "/readyz", "/livez", "/ping", "/status"))


def validate_smoke_assertions(step: dict, context: str = "smoke_step") -> list[str]:
    """Validate the assertion contract of a single smoke step.

    A declared non-health endpoint (a step whose ``request.path`` is not a
    health probe) MUST carry >=1 semantic assertion, and each assertion must
    validate. Health steps and non-endpoint (functional/AC/test-case) steps
    have no assertion requirement. Pure/aggregating.
    """
    errors: list[str] = []
    prefix = f"{context}: " if context else ""

    if not isinstance(step, dict):
        return [f"{prefix}step must be a mapping, got {type(step).__name__}"]

    request = step.get("request") if isinstance(step.get("request"), dict) else {}
    path = request.get("path")
    is_endpoint = isinstance(path, str) and path.strip() != ""
    assertions = step.get("assertions")

    if assertions is not None and not isinstance(assertions, list):
        errors.append(f"{prefix}assertions must be a list, got {type(assertions).__name__}")
        assertions = None

    if is_endpoint and not _step_is_health(step):
        if not assertions:
            errors.append(
                f"{prefix}non-health endpoint '{path}' requires >=1 semantic assertion"
            )

    for idx, assertion in enumerate(assertions or []):
        errors.extend(
            validate_smoke_assertion(assertion, context=f"{context}.assertions[{idx}]")
        )

    return errors


def validate_smoke_wave(smoke_def: dict, context: str = "smoke_wave") -> list[str]:
    """Validate a full generated smoke-wave definition. Returns list of errors.

    Requires schema_version >= 2, then validates every step's request and assertion
    contract. Pure/aggregating.
    """
    errors: list[str] = []
    prefix = f"{context}: " if context else ""

    if not isinstance(smoke_def, dict):
        return [f"{prefix}smoke definition must be a mapping"]

    version = smoke_def.get("schema_version")
    if type(version) is not int or version < SMOKE_SCHEMA_VERSION:
        errors.append(
            f"{prefix}schema_version must be an integer >= {SMOKE_SCHEMA_VERSION}"
        )

    steps = smoke_def.get("steps")
    if not isinstance(steps, list):
        errors.append(f"{prefix}steps must be a list")
        steps = []

    for idx, step in enumerate(steps or []):
        if isinstance(step, dict):
            legacy_fields = sorted(_LEGACY_SMOKE_STEP_FIELDS.intersection(step))
            request = step.get("request")
            is_endpoint = str(step.get("type", "")).startswith("api_")
            # The former official v2 writer emitted flat compatibility aliases
            # alongside the canonical request/assertions. Tolerate that exact
            # dual shape; flat-only v2 API steps remain invalid.
            if legacy_fields and not (is_endpoint and isinstance(request, dict)):
                errors.append(
                    f"{context}.steps[{idx}]: legacy flat field(s) require a "
                    f"canonical API request: {legacy_fields}"
                )
            if request is None and is_endpoint:
                errors.append(f"{context}.steps[{idx}]: API step requires request")
            elif request is not None and not isinstance(request, dict):
                errors.append(f"{context}.steps[{idx}]: request must be a mapping")
            elif isinstance(request, dict):
                for field in ("method", "path"):
                    value = request.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(
                            f"{context}.steps[{idx}]: request.{field} must be a "
                            "non-empty string"
                        )
        errors.extend(validate_smoke_assertions(step, context=f"{context}.steps[{idx}]"))

    return errors
