"""Functional tests for runtime-decision derivation from the discuss registry.

NO MOCKS: every test seeds a REAL discuss slug registry via the discuss CRUD
API and exercises the real ``derive_runtime_validation`` producer. The runtime
gate no longer reads DDRs — runtime intent comes from the ``/aah-discuss`` slug
registry (``development-methodology`` → topology, ``docker-installed`` →
transport).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aah.core.common.runtime_decision import (
    derive_runtime_validation,
    VALID_RUNTIME_TOPOLOGIES,
    VALID_LOCAL_TRANSPORTS,
)
from aah.core.discuss import registry as dr


def _seed(project_root: Path, *, methodology: str | None, docker: str | None = None) -> None:
    """Seed a real discuss registry with the runtime slugs."""
    dr.op_init(project_root, project_name="demo", complexity="mvp",
               archetype="", force=False)
    if methodology is not None:
        dr.op_add_decision(
            project_root, slug_id="development-methodology", area="infrastructure",
            question="How will your team develop across local & cloud environments?",
            options_presented=[], options_eliminated=[],
            response=methodology, response_type="single-select", source="user",
        )
    if docker is not None:
        dr.op_add_decision(
            project_root, slug_id="docker-installed", area="infrastructure",
            question="Is a container runtime installed?",
            options_presented=[], options_eliminated=[],
            response=docker, response_type="single-select", source="user",
        )


@pytest.mark.parametrize(
    "methodology,docker,expected_transport",
    [
        ("full-local", "yes", "docker-compose"),
        ("full-local", "no", "localhost"),
        ("local-cloud-ready", "yes", "docker-compose"),
        ("local-cloud-ready", "no", "localhost"),
    ],
)
def test_local_topology_derives_transport(tmp_path, methodology, docker, expected_transport):
    """Local topologies derive local_transport from the docker-installed slug."""
    _seed(tmp_path, methodology=methodology, docker=docker)
    rv = derive_runtime_validation(tmp_path)
    assert rv is not None
    assert rv["deployment_topology"] == methodology
    assert rv["deployment_topology"] in VALID_RUNTIME_TOPOLOGIES
    assert rv["local_transport"] == expected_transport
    assert rv["local_transport"] in VALID_LOCAL_TRANSPORTS
    # source_decisions reference slug ids, never DDR ids.
    slugs = [s["slug_id"] for s in rv["source_decisions"]]
    assert slugs == ["development-methodology", "docker-installed"]
    assert "DDR" not in str(rv["source_decisions"])
    assert len(rv["source_hash"]) == 64


def test_cloud_topology_omits_transport(tmp_path):
    """full-integrated-cloud has no local transport (prohibited-when rule)."""
    _seed(tmp_path, methodology="full-integrated-cloud", docker="yes")
    rv = derive_runtime_validation(tmp_path)
    assert rv is not None
    assert rv["deployment_topology"] == "full-integrated-cloud"
    assert "local_transport" not in rv
    slugs = [s["slug_id"] for s in rv["source_decisions"]]
    assert slugs == ["development-methodology"]


def test_missing_registry_returns_none(tmp_path):
    """No discuss registry at all → None (caller fails closed)."""
    assert derive_runtime_validation(tmp_path) is None


def test_missing_topology_slug_returns_none(tmp_path):
    """Registry present but no development-methodology slug → None."""
    _seed(tmp_path, methodology=None, docker="yes")
    assert derive_runtime_validation(tmp_path) is None


def test_invalid_topology_value_returns_none(tmp_path):
    """An unrecognized methodology value is not a valid topology → None."""
    _seed(tmp_path, methodology="something-invalid", docker="yes")
    assert derive_runtime_validation(tmp_path) is None


def test_source_hash_stable_and_binds_inputs(tmp_path):
    """source_hash is deterministic for identical inputs and changes with them."""
    _seed(tmp_path, methodology="full-local", docker="yes")
    rv1 = derive_runtime_validation(tmp_path)
    rv2 = derive_runtime_validation(tmp_path)
    assert rv1["source_hash"] == rv2["source_hash"]

    # A different docker answer changes the derived transport AND the hash.
    dr.op_revise(tmp_path, slug_id="docker-installed", response="no", source="user")
    rv3 = derive_runtime_validation(tmp_path)
    assert rv3["local_transport"] == "localhost"
    assert rv3["source_hash"] != rv1["source_hash"]
