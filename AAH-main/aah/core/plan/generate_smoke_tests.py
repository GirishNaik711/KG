#!/usr/bin/env python3
"""Auto-generate wave-level smoke test definitions from feature specs.

Reads feature Markdown frontmatter for each wave (via
``aah.core.common.feature_utils``), extracts acceptance criteria, endpoints,
and test scenarios, then generates schema-v2 smoke test definitions carrying a
semantic ``request`` + ``assertions`` contract.

Definitions use a canonical nested ``request`` block. Schema v1 and its flat
request fields are not emitted or accepted.

Usage:
    aah run core.plan.generate_smoke_tests generate --project-path . --wave 0
    aah run core.plan.generate_smoke_tests generate-all --project-path .
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.feature_utils import parse_feature_frontmatter
from aah.core.common.io_utils import read_json, write_yaml
from aah.core.common.validators import SMOKE_SCHEMA_VERSION, validate_smoke_wave

_HEALTH_TOKENS = ("/health", "/healthz", "/readyz", "/livez", "/ping", "/status")


def _is_health_path(path: str) -> bool:
    p = (path or "").lower()
    return any(tok in p for tok in _HEALTH_TOKENS)


def _health_assertions(expected_status: int | list[int] = 200) -> list[dict]:
    """Baseline assertion for a health endpoint (semantic, not a snapshot)."""
    values = expected_status if isinstance(expected_status, list) else [expected_status]
    return [{"type": "status_in", "values": values}]


def _extract_smoke_steps_from_feature(feature_data: dict) -> list[dict]:
    """Extract testable smoke steps from a single feature's frontmatter.

    Emits schema-v2 endpoint steps carrying a canonical ``request`` block and
    an ``assertions`` list. Functional / acceptance-criteria / test-case steps
    have no request because they are covered by the test suite.
    """
    steps = []
    feature_id = feature_data.get("id", "unknown")
    description = feature_data.get("description", "")

    # Extract from acceptance criteria (no path — functional steps).
    for i, criterion in enumerate(feature_data.get("acceptance_criteria", [])):
        criterion_text = criterion if isinstance(criterion, str) else criterion.get("description", "")
        steps.append({
            "id": f"{feature_id}-AC{i + 1:02d}",
            "source": "acceptance_criteria",
            "description": criterion_text,
            "type": "functional",
            "automated": True,
        })

    # Extract from test_cases if defined (no path — integration steps).
    for i, test_case in enumerate(feature_data.get("test_cases", [])):
        tc_desc = test_case if isinstance(test_case, str) else test_case.get("description", "")
        steps.append({
            "id": f"{feature_id}-TC{i + 1:02d}",
            "source": "test_cases",
            "description": tc_desc,
            "type": "integration",
            "automated": True,
        })

    # Extract API endpoints if defined (HTTP-probeable — carry request +
    # assertions). Endpoints may declare their own assertion vocabulary; a
    # non-health endpoint that declares none gets a baseline status assertion.
    for endpoint in feature_data.get("endpoints", []):
        if isinstance(endpoint, dict):
            method = str(endpoint.get("method", "GET")).upper()
            path = endpoint.get("path", "")
            headers = endpoint.get("headers") or {}
            expected_status = endpoint.get("expected_status", 200)
            assertions = endpoint.get("assertions")
            if not isinstance(assertions, list):
                assertions = None
            # Health endpoints get a baseline status assertion; a NON-health
            # endpoint that declares none is left empty so validation fails
            # closed (authors MUST declare a semantic assertion).
            if not assertions and _is_health_path(path):
                assertions = _health_assertions(expected_status)
            step = {
                "id": f"{feature_id}-EP-{method}-{path.replace('/', '-').strip('-')}",
                "source": "endpoints",
                "description": f"{method} {path} responds successfully",
                "type": "api_health" if _is_health_path(path) else "api_semantic",
                "automated": True,
                "request": {"method": method, "path": path},
                "assertions": assertions or [],
            }
            if headers:
                step["request"]["headers"] = headers
            steps.append(step)
        elif isinstance(endpoint, str):
            path = endpoint
            method = "GET"
            assertions = _health_assertions() if _is_health_path(path) else []
            steps.append({
                "id": f"{feature_id}-EP-{path.replace('/', '-').strip('-')}",
                "source": "endpoints",
                "description": f"GET {path} responds successfully",
                "type": "api_health" if _is_health_path(path) else "api_semantic",
                "automated": True,
                "request": {"method": method, "path": path},
                "assertions": assertions,
            })

    # If no specific steps extracted, create a generic one from description.
    if not steps and description:
        steps.append({
            "id": f"{feature_id}-SMOKE",
            "source": "description",
            "description": f"Verify: {description[:100]}",
            "type": "functional",
            "automated": True,
        })

    return steps


def _load_wave_feature_data(features_dir: Path, fid: str) -> dict | None:
    """Load a feature's frontmatter by id, from .md files, with prefix fallback."""
    # Exact id match first.
    for md_file in sorted(features_dir.glob("*.md")):
        data = parse_feature_frontmatter(md_file)
        if data and data.get("id") == fid:
            return data
    # Prefix match by filename.
    matches = sorted(features_dir.glob(f"{fid}*.md"))
    for md_file in matches:
        data = parse_feature_frontmatter(md_file)
        if data:
            return data
    return None


def generate_wave_smoke_tests(
    project_path: Path,
    wave_num: int,
) -> dict:
    """Generate schema-v2 smoke test definition for a specific wave.

    Reads ``.aah/plan/waves.json`` and ``.aah/plan/features/*.md``. Fails
    closed (returns an ``error`` key) when a declared non-health endpoint has
    no semantic assertion.
    """
    aah_path = project_path / ".aah"
    features_dir = aah_path / "plan" / "features"

    waves_path = aah_path / "plan" / "waves.json"
    if not waves_path.exists():
        return {"error": "waves.json not found"}
    from aah.core.plan.compute_waves import flatten_waves
    waves_data = read_json(waves_path)
    waves = flatten_waves(waves_data)

    if wave_num >= len(waves):
        return {"error": f"Wave {wave_num} not found (total: {len(waves)})"}

    wave_features = waves[wave_num]
    # Normalize: a wave may be list[str] or a dict with a "features" list.
    if isinstance(wave_features, dict):
        wave_features = wave_features.get("features", [])

    all_steps = []
    features_included = []

    for fid in wave_features:
        feature_data = _load_wave_feature_data(features_dir, fid)
        if feature_data:
            steps = _extract_smoke_steps_from_feature(feature_data)
            all_steps.extend(steps)
            features_included.append(fid)

    smoke_test_def = {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "wave": wave_num,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "features": features_included,
        "total_steps": len(all_steps),
        "steps": all_steps,
        "execution": {
            "timeout_seconds": 120,
            "fail_fast": False,
            "parallel": False,
        },
    }

    validation_errors = validate_smoke_wave(smoke_test_def)
    if validation_errors:
        return {"error": "invalid smoke assertions: " + "; ".join(validation_errors)}

    return smoke_test_def


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Smoke Test Generator")
    sub = parser.add_subparsers(dest="command", required=True)

    gen_p = sub.add_parser("generate", help="Generate smoke tests for a wave")
    gen_p.add_argument("--project-path", type=Path, default=None)
    gen_p.add_argument("--wave", type=int, required=True)

    all_p = sub.add_parser("generate-all", help="Generate smoke tests for all waves")
    all_p.add_argument("--project-path", type=Path, default=None)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(
        getattr(args, "project_path", None)
    )
    aah_path = project_path / ".aah"

    if args.command == "generate":
        smoke_def = generate_wave_smoke_tests(project_path, args.wave)
        if "error" in smoke_def:
            print(f"Error: {smoke_def['error']}", file=sys.stderr)
            sys.exit(1)

        output_dir = aah_path / "plan" / "smoke-tests"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"wave-{args.wave}.yaml"
        write_yaml(smoke_def, output_path)

        json.dump(smoke_def, sys.stdout, indent=2)
        print()
        print(f"Smoke tests for wave {args.wave}: {smoke_def['total_steps']} steps", file=sys.stderr)
        sys.exit(0)

    elif args.command == "generate-all":
        waves_path = aah_path / "plan" / "waves.json"
        if not waves_path.exists():
            print("Error: waves.json not found", file=sys.stderr)
            sys.exit(1)
        from aah.core.plan.compute_waves import flatten_waves
        waves_data = read_json(waves_path)
        total_waves = len(flatten_waves(waves_data))

        output_dir = aah_path / "plan" / "smoke-tests"
        output_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for wave_num in range(total_waves):
            smoke_def = generate_wave_smoke_tests(project_path, wave_num)
            if "error" in smoke_def:
                print(f"Error: wave {wave_num}: {smoke_def['error']}", file=sys.stderr)
                sys.exit(1)
            output_path = output_dir / f"wave-{wave_num}.yaml"
            write_yaml(smoke_def, output_path)
            results.append({"wave": wave_num, "steps": smoke_def["total_steps"]})

        json.dump({"generated": results, "total_waves": total_waves}, sys.stdout, indent=2)
        print()
        print(f"Generated smoke tests for {len(results)} waves", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
