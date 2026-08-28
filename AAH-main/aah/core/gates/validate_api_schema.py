#!/usr/bin/env python3
"""API-schema validation gate — architecture-phase close + optional build-phase parity.

Called from `/aah-arch` Step 7b once the schema files exist. Runs 3 checks
across every `.aah/architecture/schema/MOD-*-api-schema.yaml`:

  1. OpenAPI 3.1 spec validation (openapi-spec-validator) — per file.
     Catches malformed docs, missing required fields, invalid types, and
     unresolved intra-file $refs.

  2. Module ↔ file consistency. Every `MOD-NNN` prefix in the schema
     directory must resolve to a module id in `module-map.yaml`, and every
     API-exposing module in `module-map.yaml` must have a schema file.

  3. Wireframe binding coverage. Every per-tag `data dependency:` line in
     `.aah/architecture/applications_wireframes/NN-*.md` must resolve to an
     `operationId` in some module's schema file.

  4. (opt-in via ``--check-runtime``) Runtime OpenAPI parity. Boots the
     built app (default entry ``app.main:app``, override with
     ``--app-entry``) and diffs the runtime ``/openapi.json`` against the
     declared schema files:
       - Every declared ``(path, method)`` exists at runtime.
       - Runtime ``operationId`` matches declared ``operationId``.
       - Runtime 200-response schema $ref name matches declared 200 $ref name.
     Callable from ``aah/core/build/quality_checks.py:run-all`` so the
     build-phase standards gate can catch code-vs-schema drift before merge.
     Skips cleanly (returns ``[]``) if the app can't be imported — early
     waves may not have wired the entrypoint yet.

Exit codes:
  0 — all checks pass (or no schema/ directory found — nothing to gate)
  1 — one or more checks failed; per-issue lines on stderr

CLI:
  aah run core.gates.validate_api_schema --project-path <path>
  aah run core.gates.validate_api_schema --project-path <path> \\
      --check-runtime --app-entry app.main:app
  aah run core.gates.validate_api_schema render-prompt \\
      --project-path <path> --feature-id F-MOD000-04
    # Emits a markdown ``## API Schema Contract`` block ready to inline
    # into the aah-build Step-9 implementer dispatch prompt. Empty output
    # (exit 0) for features without ``api_contracts.produces[]``.

Runtime dep: `openapi-spec-validator` (add to pyproject.toml [project.dependencies]).
             Imported lazily so a missing install fails LOUDLY at gate time
             rather than at package import.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_yaml


# ── Path helpers ───────────────────────────────────────────────────────────

def _schema_dir(project_path: Path) -> Path:
    return project_path / ".aah" / "architecture" / "schema"


def _module_map_path(project_path: Path) -> Path:
    return project_path / ".aah" / "architecture" / "module-map.yaml"


def _wireframes_dir(project_path: Path) -> Path:
    return project_path / ".aah" / "architecture" / "applications_wireframes"


_MOD_FILENAME = re.compile(r"^(MOD-\d+)-api-schema\.ya?ml$", re.IGNORECASE)


def _module_id_from_filename(schema_file: Path) -> str | None:
    """Extract `MOD-NNN` from a `MOD-NNN-api-schema.yaml` filename. None if it doesn't match."""
    m = _MOD_FILENAME.match(schema_file.name)
    return m.group(1).upper() if m else None


# ── Check 1: OpenAPI spec validation ───────────────────────────────────────

def _check_spec_validity(schema_files: list[Path]) -> list[str]:
    """Run openapi-spec-validator on each file. Return per-file error strings."""
    try:
        from openapi_spec_validator import validate  # lazy import
    except ImportError:
        return [
            "openapi-spec-validator is not installed. "
            "Add it to pyproject.toml [project.dependencies] and re-run."
        ]

    issues: list[str] = []
    for f in schema_files:
        try:
            spec = read_yaml(f)
        except Exception as e:
            issues.append(f"{f.name}: cannot parse as YAML: {e}")
            continue
        try:
            validate(spec)
        except Exception as e:
            # openapi-spec-validator raises validators-specific exceptions; capture
            # the first line of the message so the gate output stays scannable.
            first_line = str(e).splitlines()[0] if str(e) else e.__class__.__name__
            issues.append(f"{f.name}: OpenAPI validation failed: {first_line}")
    return issues


# ── Check 2: filename ↔ module-map.yaml consistency ────────────────────────

def _check_module_consistency(
    schema_files: list[Path],
    module_map_path: Path,
) -> list[str]:
    """Filename MOD-NNN prefixes must match module ids in module-map.yaml."""
    issues: list[str] = []

    if not module_map_path.exists():
        issues.append(
            f"module-map.yaml not found at {module_map_path.relative_to(module_map_path.parent.parent.parent)} — "
            "cannot verify module ownership"
        )
        return issues

    try:
        module_map = read_yaml(module_map_path)
    except Exception as e:
        issues.append(f"module-map.yaml cannot be parsed: {e}")
        return issues

    map_ids = {m.get("id") for m in (module_map.get("modules") or []) if m.get("id")}
    file_ids: set[str] = set()

    for f in schema_files:
        mod_id = _module_id_from_filename(f)
        if mod_id is None:
            issues.append(
                f"{f.name}: filename doesn't match `MOD-NNN-api-schema.yaml` pattern"
            )
            continue
        file_ids.add(mod_id)
        if mod_id not in map_ids:
            issues.append(
                f"{f.name}: module {mod_id} not found in module-map.yaml"
            )

    # A module in the map with no schema file is only a problem if the module
    # exposes an API. We can't know that from module-map.yaml alone without
    # inspecting `layers`, so we surface it as a soft warning-style issue —
    # the gate still fails, letting the reviewer confirm.
    for mid in sorted(map_ids - file_ids):
        # MOD-000 is the walking skeleton; typically no API of its own.
        # Skip it to avoid false-positive noise. Other modules must have a
        # schema file OR be explicitly excluded by the reviewer.
        if mid == "MOD-000":
            continue
        issues.append(
            f"{mid} in module-map.yaml has no matching schema/{mid}-api-schema.yaml"
        )

    return issues


# ── Check 3: wireframe → operationId coverage ──────────────────────────────

_DATA_DEP_LINE = re.compile(
    r"data\s+dependency\s*[:\-]\s*`?([A-Za-z0-9_/{}.\-]+)`?",
    re.IGNORECASE,
)


def _collect_operation_ids(schema_files: list[Path]) -> set[str]:
    """Union of every operationId across every module's schema file."""
    op_ids: set[str] = set()
    for f in schema_files:
        try:
            spec = read_yaml(f)
        except Exception:
            continue  # spec-validity check will report parse errors
        for _, methods in (spec.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                # Skip non-operation keys (`parameters`, `summary`, `description`, `servers`).
                if method not in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}:
                    continue
                if isinstance(op, dict) and op.get("operationId"):
                    op_ids.add(str(op["operationId"]))
    return op_ids


def _check_wireframe_coverage(
    wireframes_dir: Path,
    op_ids: set[str],
) -> list[str]:
    """Every wireframe `data dependency:` value must exist as an operationId."""
    if not wireframes_dir.exists():
        return []  # no frontend / no wireframes → nothing to check

    issues: list[str] = []
    for wf in sorted(wireframes_dir.glob("*.md")):
        if wf.name.lower() == "readme.md":
            continue
        try:
            text = wf.read_text(encoding="utf-8")
        except Exception as e:
            issues.append(f"{wf.name}: cannot read wireframe: {e}")
            continue
        for match in _DATA_DEP_LINE.finditer(text):
            dep = match.group(1).strip()
            # Skip empty / placeholder values.
            if not dep or dep in {"none", "n/a", "-"}:
                continue
            # A wireframe legend may list either an operationId directly OR a
            # URL path like /orders. We accept a match on either — plan-critic
            # can tighten this later.
            if dep in op_ids:
                continue
            if dep.startswith("/"):
                # Path form — accept for now; strict resolution is optional.
                continue
            issues.append(
                f"{wf.name}: data dependency {dep!r} has no matching operationId in any module's schema file"
            )
    return issues


# ── Check 4: runtime OpenAPI parity (opt-in, build-phase) ──────────────────

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _load_app_openapi(project_path: Path, app_entry: str) -> tuple[dict | None, str | None]:
    """Import ``app_entry`` (``module:attr``) and return its OpenAPI dict.

    Returns ``(openapi_dict, None)`` on success, or ``(None, reason)`` if the
    app can't be imported or doesn't expose an ``openapi()`` callable.
    ``reason`` is a short human-readable string used to skip (not fail) the
    check when the app isn't wired yet — e.g. early waves.
    """
    import sys as _sys

    if ":" not in app_entry:
        return None, f"--app-entry must be 'module:attr' (got {app_entry!r})"

    module_name, attr_name = app_entry.split(":", 1)

    # Prepend project_path to sys.path so 'app.main' resolves against the
    # subject's checkout, not whatever install site aah lives at.
    project_str = str(project_path)
    inserted = project_str not in _sys.path
    if inserted:
        _sys.path.insert(0, project_str)
    try:
        try:
            module = __import__(module_name, fromlist=[attr_name])
        except Exception as e:
            return None, f"cannot import {module_name!r}: {e.__class__.__name__}: {e}"
        app = getattr(module, attr_name, None)
        if app is None:
            return None, f"{app_entry!r}: attribute {attr_name!r} not found on module"
        openapi_fn = getattr(app, "openapi", None)
        if openapi_fn is None or not callable(openapi_fn):
            return None, f"{app_entry!r}: object has no callable openapi()"
        try:
            return openapi_fn(), None
        except Exception as e:
            return None, f"{app_entry!r}.openapi() raised {e.__class__.__name__}: {e}"
    finally:
        if inserted:
            try:
                _sys.path.remove(project_str)
            except ValueError:
                pass


def _extract_ref_name(schema_obj: Any) -> str | None:
    """Extract the terminal component name from a ``$ref`` like
    ``'#/components/schemas/HealthResponse'``. Returns None if not a $ref."""
    if not isinstance(schema_obj, dict):
        return None
    ref = schema_obj.get("$ref")
    if not isinstance(ref, str):
        return None
    return ref.rsplit("/", 1)[-1] if "/" in ref else ref


def _iter_operations(spec: dict) -> "list[tuple[str, str, dict]]":
    """Yield (path, method, operation_dict) for every operation in an OpenAPI spec."""
    out: list[tuple[str, str, dict]] = []
    for pth, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            out.append((pth, method.lower(), op))
    return out


def _check_runtime_parity(
    schema_files: list[Path],
    project_path: Path,
    app_entry: str,
) -> list[str]:
    """Boot the app, diff its runtime OpenAPI against every declared schema file.

    Reports drift as one issue per endpoint attribute. Returns ``[]`` (no
    issues, no fail) if the app can't be imported — build order may not have
    reached the assembly feature yet.
    """
    runtime, skip_reason = _load_app_openapi(project_path, app_entry)
    if runtime is None:
        # Not a failure — the runtime check is best-effort until the app is
        # wired. Emit a discoverable note but no gate-fail.
        print(
            f"[validate_api_schema] runtime parity SKIPPED ({skip_reason})",
            file=sys.stderr,
        )
        return []

    # Index runtime ops by (path, method) → op dict for O(1) lookup below.
    runtime_index: dict[tuple[str, str], dict] = {
        (p, m): op for (p, m, op) in _iter_operations(runtime)
    }

    issues: list[str] = []
    for schema_file in schema_files:
        try:
            declared = read_yaml(schema_file)
        except Exception:
            continue  # Check 1 already reported parse errors
        for pth, method, decl_op in _iter_operations(declared):
            key = (pth, method)
            runtime_op = runtime_index.get(key)
            if runtime_op is None:
                issues.append(
                    f"{schema_file.name}: {method.upper()} {pth} declared in schema "
                    "but not present at runtime"
                )
                continue
            # operationId parity
            decl_op_id = decl_op.get("operationId")
            runtime_op_id = runtime_op.get("operationId")
            if decl_op_id and runtime_op_id != decl_op_id:
                issues.append(
                    f"{schema_file.name}: {method.upper()} {pth} operationId drift — "
                    f"declared {decl_op_id!r}, runtime emits {runtime_op_id!r}"
                )
            # 200-response $ref parity (only checked when declared)
            decl_200 = (decl_op.get("responses") or {}).get("200") or {}
            decl_ref_name = _extract_ref_name(
                ((decl_200.get("content") or {}).get("application/json") or {}).get("schema")
            )
            if decl_ref_name:
                runtime_200 = (runtime_op.get("responses") or {}).get("200") or {}
                runtime_ref_name = _extract_ref_name(
                    ((runtime_200.get("content") or {}).get("application/json") or {}).get(
                        "schema"
                    )
                )
                if runtime_ref_name != decl_ref_name:
                    issues.append(
                        f"{schema_file.name}: {method.upper()} {pth} 200 schema drift — "
                        f"declared $ref .../{decl_ref_name}, runtime .../{runtime_ref_name}"
                    )
    return issues


# ── Prompt-side helper: inline schema slices into implementer prompts ─────
#
# Used by ``aah-build`` Step 9 to give the implementer the exact contract
# they must satisfy — path, method, operationId, response ref, and the
# resolved component definitions — inlined verbatim rather than by-reference.
# This closes the drift class where the implementer opens the feature.md
# but never actually reads the schema file it references.


_API_CONTRACTS_BLOCK = re.compile(
    r"##\s+API\s+Contracts\s*\n+```(?:yaml)?\s*\n(?P<body>.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)


def _parse_api_contracts(feature_md: Path) -> dict[str, list[dict]]:
    """Parse the ``produces[]`` and ``consumes[]`` lists from a feature.md.

    Returns ``{"produces": [...], "consumes": [...]}``. Either list may be
    empty. Returns ``{"produces": [], "consumes": []}`` when the feature
    has no ``## API Contracts`` section or the section is unparseable —
    caller treats "no contract" the same as "empty".

    ``produces`` = the feature IMPLEMENTS this endpoint (must match schema).
    ``consumes`` = the feature CALLS this endpoint (must type its client
    against the schema).
    """
    empty = {"produces": [], "consumes": []}
    try:
        text = feature_md.read_text(encoding="utf-8")
    except Exception:
        return empty
    m = _API_CONTRACTS_BLOCK.search(text)
    if not m:
        return empty
    body = m.group("body").strip()
    if not body:
        return empty
    try:
        import yaml  # PyYAML is already a transitive dep of aah
        parsed = yaml.safe_load(body)
    except Exception:
        return empty
    if not isinstance(parsed, dict):
        return empty
    # Accept both {api_contracts: {produces/consumes: [...]}} and top-level.
    contracts = parsed.get("api_contracts", parsed)
    if not isinstance(contracts, dict):
        return empty

    def _clean(key: str) -> list[dict]:
        raw = contracts.get(key)
        if not isinstance(raw, list):
            return []
        return [p for p in raw if isinstance(p, dict)]

    return {"produces": _clean("produces"), "consumes": _clean("consumes")}


def _find_operation_by_id(spec: dict, operation_id: str) -> tuple[str, str, dict] | None:
    """Locate (path, method, op_dict) for a given operationId in an OpenAPI spec."""
    for pth, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if isinstance(op, dict) and op.get("operationId") == operation_id:
                return pth, method.lower(), op
    return None


def _render_component(name: str, component: dict) -> str:
    """Render one ``components.schemas.<name>`` entry as an indented YAML block.

    Only surfaces the fields that are contract-load-bearing: ``type``,
    ``required``, and each property's ``type`` / ``enum`` / ``format``.
    Nested $refs are shown by name only — the implementer can chase them
    to the declared component if needed.
    """
    if not isinstance(component, dict):
        return f"  (component {name!r} is not an object schema — inspect the schema file)"
    lines: list[str] = [f"Component {name}:"]
    ctype = component.get("type")
    if ctype:
        lines.append(f"  type: {ctype}")
    required = component.get("required")
    if isinstance(required, list) and required:
        lines.append(f"  required: {required}")
    properties = component.get("properties")
    if isinstance(properties, dict) and properties:
        lines.append("  properties:")
        for prop_name, prop_def in properties.items():
            lines.append(f"    {prop_name}:")
            if not isinstance(prop_def, dict):
                lines.append("      (opaque)")
                continue
            for key in ("type", "format", "enum", "minimum", "maximum", "minLength", "maxLength"):
                if key in prop_def:
                    lines.append(f"      {key}: {prop_def[key]}")
            if "$ref" in prop_def:
                lines.append(f"      $ref: {prop_def['$ref']} [nested component]")
            desc = prop_def.get("description")
            if desc:
                # Show only first line to keep the prompt compact.
                first = str(desc).splitlines()[0].strip()
                if first:
                    lines.append(f"      description: {first}")
    return "\n".join(lines)


def resolve_api_contract_for_prompt(project_path: Path, feature_id: str) -> str:
    """Return a ready-to-inline ``## API Schema Contract`` markdown block.

    Reads ``.aah/plan/features/<feature_id>.md``, parses its ``## API Contracts``
    block, resolves each ``produces[]`` entry against the referenced schema
    file, and renders the endpoint slice + component definitions.

    Returns an empty string when the feature has no API contract — safe to
    call unconditionally from Step 9 of aah-build.
    """
    feature_md = project_path / ".aah" / "plan" / "features" / f"{feature_id}.md"
    if not feature_md.exists():
        return ""

    contracts = _parse_api_contracts(feature_md)
    produces = contracts["produces"]
    consumes = contracts["consumes"]
    if not produces and not consumes:
        return ""

    # Cache schema files we've already parsed to avoid re-reading the same
    # module schema for multiple endpoints on one feature.
    schema_cache: dict[Path, dict] = {}

    def _load_schema(rel: str) -> dict | None:
        path = (project_path / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        if not path.exists():
            # Fall back to .aah/architecture/<rel> — some feature.md files
            # store the path relative to the architecture directory.
            alt = (project_path / ".aah" / "architecture" / rel).resolve()
            if alt.exists():
                path = alt
            else:
                return None
        if path not in schema_cache:
            try:
                schema_cache[path] = read_yaml(path)
            except Exception:
                return None
        return schema_cache[path]

    blocks: list[str] = []
    seen_components: set[tuple[Path, str]] = set()

    def _render_endpoints(entries: list[dict], role: str) -> None:
        """Append rendered blocks for a produces[] or consumes[] list to ``blocks``.

        ``role`` is either ``"produces"`` (feature IMPLEMENTS the endpoint —
        prompt with router directive) or ``"consumes"`` (feature CALLS the
        endpoint — prompt with typed-client directive). Both share the same
        endpoint-slice + component-resolution logic.
        """
        if not entries:
            return

        if role == "produces":
            blocks.append("## API Schema Contract — YOU MUST IMPLEMENT (MUST match exactly)")
        else:
            blocks.append("## API Schema Contract — YOU CONSUME (client must match server)")
        blocks.append("")

        endpoint_idx = 0
        for entry in entries:
            endpoint_idx += 1
            op_id = entry.get("operation_id")
            schema_rel = entry.get("schema_file")
            request_component = entry.get("request_schema")
            response_component = entry.get("response_schema")

            if not op_id or not schema_rel:
                blocks.append(
                    f"Endpoint {endpoint_idx}: (incomplete {role}[] entry — "
                    f"operation_id={op_id!r}, schema_file={schema_rel!r})"
                )
                blocks.append("")
                continue

            spec = _load_schema(schema_rel)
            if spec is None:
                blocks.append(
                    f"Endpoint {endpoint_idx}: schema file {schema_rel!r} not found under project"
                )
                blocks.append("")
                continue

            located = _find_operation_by_id(spec, op_id)
            if located is None:
                blocks.append(
                    f"Endpoint {endpoint_idx}: operationId {op_id!r} not present in {schema_rel}"
                )
                blocks.append("")
                continue
            pth, method, op = located

            blocks.append(f"Source: {schema_rel}")
            blocks.append("")
            blocks.append(f"Endpoint {endpoint_idx}:")
            blocks.append(f"  path: {pth}")
            blocks.append(f"  method: {method.upper()}")
            if role == "produces":
                directive = (
                    f"[set via @router.{method}(..., operation_id=\"{op_id}\")]"
                )
            else:
                directive = (
                    f"[type the client call against operationId {op_id!r} — "
                    f"do not invent a different endpoint]"
                )
            blocks.append(f"  operationId: {op_id}          {directive}")

            if request_component:
                rb = op.get("requestBody") or {}
                rb_schema = ((rb.get("content") or {}).get("application/json") or {}).get("schema") or {}
                rb_ref = _extract_ref_name(rb_schema)
                blocks.append("  requestBody:")
                blocks.append("    content-type: application/json")
                blocks.append(
                    f"    body_ref: {request_component}"
                    + (
                        f" (schema references {rb_ref})"
                        if rb_ref and rb_ref != request_component
                        else ""
                    )
                )

            r200 = (op.get("responses") or {}).get("200") or {}
            r200_schema = ((r200.get("content") or {}).get("application/json") or {}).get("schema") or {}
            r200_ref = _extract_ref_name(r200_schema)
            if r200_ref or response_component:
                blocks.append("  response 200:")
                blocks.append("    content-type: application/json")
                ref_display = response_component or r200_ref or "(unknown)"
                note = ""
                if response_component and r200_ref and response_component != r200_ref:
                    note = f" (schema declares $ref .../{r200_ref})"
                blocks.append(f"    body_ref: {ref_display}{note}")

            for status_code, rdef in sorted((op.get("responses") or {}).items()):
                if status_code == "200":
                    continue
                if not isinstance(rdef, dict):
                    continue
                rdef_schema = ((rdef.get("content") or {}).get("application/json") or {}).get("schema") or {}
                rdef_ref = _extract_ref_name(rdef_schema)
                desc = rdef.get("description") or ""
                first = str(desc).splitlines()[0].strip() if desc else ""
                blocks.append(f"  response {status_code}:")
                if rdef_ref:
                    blocks.append(f"    body_ref: {rdef_ref}")
                if first:
                    blocks.append(f"    description: {first}")

            blocks.append("")

            components = ((spec.get("components") or {}).get("schemas")) or {}
            for comp_name in (request_component, response_component):
                if not comp_name:
                    continue
                key = (Path(schema_rel), comp_name)
                if key in seen_components:
                    continue
                seen_components.add(key)
                comp_def = components.get(comp_name)
                if comp_def is None:
                    blocks.append(f"Component {comp_name}: (not defined in {schema_rel})")
                else:
                    blocks.append(_render_component(comp_name, comp_def))
                blocks.append("")

    _render_endpoints(produces, "produces")
    _render_endpoints(consumes, "consumes")

    if not blocks:
        return ""

    footer_bits: list[str] = []
    if produces:
        footer_bits.append(
            "Non-negotiable for IMPLEMENTED endpoints: operationId, path, method, "
            "response component name, and `required`/`enum` constraints on "
            "properties are contract commitments. Diverging from them fails the "
            "build-phase standards gate (see "
            "`aah run core.gates.validate_api_schema --check-runtime`)."
        )
    if consumes:
        footer_bits.append(
            "Non-negotiable for CONSUMED endpoints: your typed client must match "
            "the schema exactly — same path, same method, request/response shapes "
            "matching the declared components. Do not invent endpoints or field "
            "names; if the schema is missing something you need, raise via "
            "aah-fix instead of adapting."
        )
    blocks.append("\n\n".join(footer_bits))

    return "\n".join(blocks) + "\n"


# ── Main entrypoint ────────────────────────────────────────────────────────

def validate_api_schema(
    project_path: Path,
    *,
    check_runtime: bool = False,
    app_entry: str = "app.main:app",
) -> tuple[bool, list[str]]:
    """Run all checks. Return (passed, issues).

    Returns (True, []) if the schema/ directory doesn't exist — the gate is
    a no-op for projects without an API layer.

    When ``check_runtime`` is True, Check 4 (runtime parity) is added. It
    boots the app defined by ``app_entry`` and diffs its OpenAPI against the
    declared schemas.
    """
    schema_dir = _schema_dir(project_path)
    if not schema_dir.exists():
        return True, []  # no schema/ dir → project has no API layer → skip gate

    schema_files = sorted(
        [p for p in schema_dir.glob("*.yaml") if p.is_file()]
        + [p for p in schema_dir.glob("*.yml") if p.is_file()]
    )

    if not schema_files:
        return True, []  # empty schema/ dir → same treatment

    issues: list[str] = []
    issues.extend(_check_spec_validity(schema_files))
    issues.extend(_check_module_consistency(schema_files, _module_map_path(project_path)))
    op_ids = _collect_operation_ids(schema_files)
    issues.extend(_check_wireframe_coverage(_wireframes_dir(project_path), op_ids))
    if check_runtime:
        issues.extend(_check_runtime_parity(schema_files, project_path, app_entry))

    return (not issues), issues


def _resolve_project_path_from_args_or_stdin(cli_project_path: Path | None) -> Path | None:
    """Shared project-path resolution used by both subcommands."""
    from aah.core.common.config import resolve_project_path

    if cli_project_path is not None:
        return resolve_project_path(cli_project_path)
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}
    cwd = hook_input.get("cwd")
    return resolve_project_path(Path(cwd) if cwd else None)


def _cmd_render_prompt(argv: list[str]) -> None:
    """Emit the ``## API Schema Contract`` block for a feature to stdout.

    Prints an empty string (exit 0) for features without ``api_contracts`` —
    aah-build appends the output unconditionally to the dispatch prompt, so
    an absent-contract feature just gets nothing extra.
    """
    parser = argparse.ArgumentParser(
        prog="aah run core.gates.validate_api_schema render-prompt",
        description="Render inlined API-schema slices for a feature's implementer prompt",
    )
    parser.add_argument("--project-path", type=Path)
    parser.add_argument("--feature-id", type=str, required=True)
    args, _ = parser.parse_known_args(argv)

    project_path = _resolve_project_path_from_args_or_stdin(args.project_path)
    if project_path is None:
        # No project resolvable — emit empty block, don't fail.
        sys.exit(0)

    block = resolve_api_contract_for_prompt(project_path, args.feature_id)
    if block:
        sys.stdout.write(block)
    sys.exit(0)


def main() -> None:
    # Subcommand dispatch. Baseline invocation (no subcommand) preserves
    # the original architecture-phase behavior verbatim.
    if len(sys.argv) > 1 and sys.argv[1] == "render-prompt":
        _cmd_render_prompt(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(description="API-schema validation gate")
    parser.add_argument("--project-path", type=Path)
    parser.add_argument(
        "--check-runtime",
        action="store_true",
        help="Add Check 4: boot the app and diff runtime OpenAPI against declared schemas",
    )
    parser.add_argument(
        "--app-entry",
        type=str,
        default="app.main:app",
        help="FastAPI/Starlette entrypoint as 'module:attr' (default: app.main:app)",
    )
    args, _ = parser.parse_known_args()

    project_path = _resolve_project_path_from_args_or_stdin(args.project_path)
    if project_path is None:
        sys.exit(0)  # no project — nothing to gate

    passed, issues = validate_api_schema(
        project_path,
        check_runtime=args.check_runtime,
        app_entry=args.app_entry,
    )

    if not passed:
        print("API-schema validation FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
