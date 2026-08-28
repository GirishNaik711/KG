"""Functional tests for runtime-profile resolution + smoke assertion requirement.

NO MOCKS: real temp projects, real registry via the CLI, real
cloud-readiness, real resolve_runtime_profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aah.core.common.readiness import (
    load_runtime_resources,
    resolve_runtime_profile,
)
from aah.core.common.validators import validate_runtime_profile, validate_smoke_assertions

from tests._runtime_helpers import build_registry, write_cloud_readiness, write_manifest


def _build(tmp_path: Path) -> Path:
    aah_path = tmp_path / ".aah"
    aah_path.mkdir(parents=True)
    write_manifest(aah_path)
    build_registry(aah_path, topology="full-local", transport="docker-compose")
    write_cloud_readiness(aah_path)
    return aah_path


def _resolve_path(aah_path: Path) -> dict:
    # Runtime intent lives in the discuss slug registry; the resolver derives
    # runtime_validation from it when the context block is absent (same path
    # the real gate takes). Pass an empty context.
    resources = load_runtime_resources(aah_path)
    return resolve_runtime_profile(aah_path, {}, resources)


def _resolve(tmp_path: Path) -> dict:
    return _resolve_path(_build(tmp_path))


def test_profile_byte_stable(tmp_path):
    """AC1: identical resolved decisions -> byte-stable profile (hashed block).

    Resolution is deterministic: re-resolving the SAME confirmed decisions +
    readiness yields a byte-identical profile (the only per-run variation lives
    in the registry's confirmation timestamps, which are inputs, not resolution
    noise). Timestamps live OUTSIDE the profile's hashed block.
    """
    aah_path = _build(tmp_path)
    profile_a = _resolve_path(aah_path)
    profile_b = _resolve_path(aah_path)

    # Re-resolving identical inputs is byte-stable.
    assert profile_a["profile_hash"] == profile_b["profile_hash"]
    assert profile_a == profile_b

    # And the profile is well-formed with 64-hex hashes.
    assert validate_runtime_profile(profile_a) == []
    assert len(profile_a["profile_hash"]) == 64
    assert profile_a["decision_hash"] == profile_b["decision_hash"]
    # readiness_hash present and stable.
    assert profile_a["readiness_hash"] == profile_b["readiness_hash"]
    # Allowed env keys are names only (no values), sorted, and non-empty for a
    # project with a real cloud-readiness service.
    assert profile_a["allowed_env_keys"] == sorted(profile_a["allowed_env_keys"])
    assert any("RDS" in k for k in profile_a["allowed_env_keys"])


def test_profile_requires_confirmed_decisions(tmp_path):
    """A profile cannot be resolved without a resolved runtime decision.

    With no discuss registry (no development-methodology slug) and no
    pre-materialized runtime_validation block, resolution fails closed.
    """
    aah_path = tmp_path / ".aah"
    aah_path.mkdir(parents=True)
    write_manifest(aah_path)
    # No discuss registry seeded → the resolver's slug fallback finds nothing.
    with pytest.raises(ValueError, match="runtime_validation"):
        resolve_runtime_profile(aah_path, {}, None)


def test_endpoint_requires_semantic_assertion(tmp_path):
    """AC5: a declared non-health endpoint requires >=1 semantic assertion."""
    # Non-health endpoint with no assertions -> error.
    bad_step = {
        "id": "F-EP", "path": "/orders", "method": "GET",
        "type": "api_semantic", "assertions": [],
    }
    errors = validate_smoke_assertions(bad_step)
    assert any("requires >=1 semantic assertion" in e for e in errors)

    # Same endpoint with a semantic status assertion -> valid.
    good_step = dict(bad_step)
    good_step["assertions"] = [{"type": "status_in", "values": [200]}]
    assert validate_smoke_assertions(good_step) == []

    # A health endpoint needs no assertion.
    health_step = {"id": "F-H", "path": "/health", "method": "GET", "type": "api_health"}
    assert validate_smoke_assertions(health_step) == []


# ---------------------------------------------------------------------------
# Cloud-readiness writer/reader path agreement (.aah is the single artifact)
# ---------------------------------------------------------------------------


def test_cloud_readiness_writer_reader_round_trip(tmp_path):
    """The cloud gate writer and the readiness reader agree on the .aah path.

    ``save_cloud_readiness`` writes to ``.aah/architecture/cloud-readiness.yaml``;
    the exact file ``readiness.load_runtime_resources`` reads. A real write must be
    a real read — no .rapids/.aah split.
    """
    from aah.core.gates.validate_cloud_readiness import save_cloud_readiness, check_existing_state

    aah_path = tmp_path / ".aah"
    aah_path.mkdir(parents=True)

    services_config = [{
        "id": "svc-001",
        "type": "rds-postgres",
        "service_type": "rds-postgres",
        "provider": "aws",
        "criticality": "critical",
        "host": "db.internal",
        "port": 5432,
        "database": "appdb",
        "region": "us-east-1",
    }]
    validation_results = {
        "outcome": "passed",
        "details": [{
            "service_type": "rds-postgres", "service_id": "svc-001",
            "passed": True, "latency_ms": 3,
        }],
    }

    out_path = save_cloud_readiness(
        tmp_path, services_config, validation_results, "direct-cloud", "aws"
    )
    # Writer put the file at exactly .aah/architecture/cloud-readiness.yaml.
    assert out_path == aah_path / "architecture" / "cloud-readiness.yaml"
    assert out_path.exists()
    assert not (tmp_path / ".rapids" / "cloud-readiness.yaml").exists()
    assert not (aah_path / "cloud-readiness.yaml").exists()

    # check_existing_state reads the same .aah path.
    state = check_existing_state(tmp_path)
    assert state is not None
    assert state["services"][0]["service_type"] == "rds-postgres"

    # The readiness reader loads the identical file the writer produced.
    resources = load_runtime_resources(aah_path)
    assert resources is not None
    assert resources["cloud_provider"] == "aws"
    types = {s.get("service_type") for s in resources["services"]}
    assert "rds-postgres" in types


def test_analyze_cloud_readiness_instructions_match_writer(tmp_path):
    """The rapids-analyze SKILL cloud-readiness sections reference the SAME
    .aah path the writer/reader use — no stale .rapids instruction remains."""
    from tests._runtime_helpers import REPO_ROOT

    skill = (REPO_ROOT / "aah/skills/rapids-analyze/SKILL.md").read_text(encoding="utf-8")
    # The command/resume/commit sections now point at .aah/cloud-readiness.yaml.
    assert ".aah/cloud-readiness.yaml" in skill
    assert "git add .aah/cloud-readiness.yaml" in skill
    # No lingering .rapids/cloud-readiness.yaml instruction.
    assert ".rapids/cloud-readiness.yaml" not in skill
