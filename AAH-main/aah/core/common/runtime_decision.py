#!/usr/bin/env python3
"""Derive the runtime-validation block from the /aah-discuss slug registry.

The runtime-verification gates (``readiness.resolve_runtime_profile`` →
``runtime_profile`` / ``build.verify``) consume a ``runtime_validation`` block
of shape::

    {deployment_topology, local_transport?, source_decisions, source_hash}

Historically that block was materialized from confirmed DDR-SHARED-009/011.
The new AAH harness retired DDRs; decisions now live in the discuss slug
registry (``aah.core.discuss.registry``). This module is the replacement
producer: it reads the slug answers and derives the same block shape, so the
downstream resolver and both gates are unchanged.

Slug mapping:
  - ``development-methodology`` → ``deployment_topology`` (identical vocabulary:
    ``full-local`` / ``local-cloud-ready`` / ``full-integrated-cloud``).
  - ``docker-installed`` → ``local_transport`` for local topologies
    (``yes`` → ``docker-compose``, else ``localhost``); omitted for
    ``full-integrated-cloud`` (which prohibits a local transport).

Returns ``None`` when the discuss registry is absent or the topology slug is
unresolved/invalid — the caller treats that as "no signal" and fails closed.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.common.hashing import canonical_json_hash

# Topology + transport vocabularies (previously in registry.py). The topology
# values are identical to the discuss `development-methodology` option values.
VALID_RUNTIME_TOPOLOGIES = {"full-local", "local-cloud-ready", "full-integrated-cloud"}
VALID_LOCAL_TRANSPORTS = {"docker-compose", "localhost"}
LOCAL_TOPOLOGIES = {"full-local", "local-cloud-ready"}

# Discuss slug_ids this producer reads.
TOPOLOGY_SLUG = "development-methodology"
DOCKER_SLUG = "docker-installed"


def _source_entry(slug_id: str, value) -> dict:
    """Canonical, hashable snapshot of one contributing slug decision."""
    canonical = {"slug_id": slug_id, "response": value}
    return {**canonical, "canonical_hash": canonical_json_hash(canonical)}


def derive_runtime_validation(project_root: Path) -> dict | None:
    """Derive the ``runtime_validation`` block from the discuss slug registry.

    ``project_root`` is the project directory (the parent of ``.aah``); the
    discuss registry lives at ``discuss.registry.registry_path(project_root)``.
    Returns the derived block, or ``None`` when it cannot be resolved (missing
    registry, unresolved/invalid topology slug).
    """
    # Lazy import: discuss is a sibling package; import here keeps this module
    # importable in isolation and avoids any load-order coupling.
    from aah.core.discuss import registry as discuss_registry
    from aah.core.discuss.guidance import collect_answers

    try:
        data = discuss_registry.op_read(project_root)
    except (FileNotFoundError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    answers = collect_answers(data)

    topology = answers.get(TOPOLOGY_SLUG)
    if topology not in VALID_RUNTIME_TOPOLOGIES:
        # Topology intent absent or not a recognized value → no signal.
        return None

    sources = [_source_entry(TOPOLOGY_SLUG, topology)]

    transport = None
    if topology in LOCAL_TOPOLOGIES:
        docker = answers.get(DOCKER_SLUG)
        transport = "docker-compose" if _is_yes(docker) else "localhost"
        sources.append(_source_entry(DOCKER_SLUG, docker))
    # full-integrated-cloud: no local transport (matches prohibited-when rule).

    derived: dict = {
        "deployment_topology": topology,
        "source_decisions": sources,
        "source_hash": canonical_json_hash({"source_decisions": sources}),
    }
    if transport is not None:
        derived["local_transport"] = transport
    return derived


def _is_yes(value) -> bool:
    """Treat 'yes'/True as docker present; everything else as absent."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"yes", "true"}


if __name__ == "__main__":
    # ponytail: one runnable check, no framework. Drives the real producer
    # against a real temp discuss registry via the discuss CRUD API.
    import sys
    import tempfile

    from aah.core.discuss import registry as _dr

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # No registry yet → None.
        assert derive_runtime_validation(root) is None

        _dr.op_init(root, project_name="demo", complexity="mvp",
                    archetype="", force=False)
        _dr.op_add_decision(
            root, slug_id=TOPOLOGY_SLUG, area="infra", question="?",
            options_presented=[], options_eliminated=[],
            response="local-cloud-ready", response_type="single", source="user",
        )
        _dr.op_add_decision(
            root, slug_id=DOCKER_SLUG, area="infra", question="?",
            options_presented=[], options_eliminated=[],
            response="yes", response_type="single", source="user",
        )
        rv = derive_runtime_validation(root)
        assert rv is not None
        assert rv["deployment_topology"] == "local-cloud-ready", rv
        assert rv["local_transport"] == "docker-compose", rv
        assert rv["source_hash"], rv

    print("runtime_decision self-check OK", file=sys.stderr)
