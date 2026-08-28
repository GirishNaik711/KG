#!/usr/bin/env python3
"""Deterministic order resolution for port activities (design §5).

Pure functions — NO I/O. Given a list of activity dicts, produce the order in
which they must fire. Three levels, applied in this precedence:

  1. Position order — enforced by CALL SITE, not here. The executor asks for one
     position bucket at a time (`before` returns before+within; `after` returns
     after). This module orders WITHIN whatever list it is given.
  2. Spine order (macro) — index into ``spine_order`` on the activity's
     ``target`` node. Lets activities bound to different nodes be compared on one
     timeline. Only matters when a caller mixes nodes; within a single node it is
     constant.
  3. Peer order (within one (target, position) bucket) —
     ``topological_generations(depends_on)`` → integer ``order`` tiebreak →
     stable ``id``.

``depends_on`` is restricted to peers sharing the same (target, position)
coordinate (validated in catalog.py); a cycle raises ``OrderingError``.
"""

from __future__ import annotations

import networkx as nx


class OrderingError(ValueError):
    """Raised when activities cannot be ordered (e.g. a dependency cycle)."""


def _peer_sort_key(activity: dict, generation_index: int) -> tuple:
    """Sort key WITHIN a coordinate bucket: generation → order → id."""
    return (
        generation_index,
        activity.get("order", 100),
        activity.get("id", ""),
    )


def order_bucket(bucket: list[dict]) -> list[dict]:
    """Order activities that share one (target, position) coordinate.

    Level 3 of the resolver: topological generations over ``depends_on``, then
    integer ``order``, then stable ``id``.
    """
    if not bucket:
        return []

    by_id = {a["id"]: a for a in bucket if "id" in a}

    g = nx.DiGraph()
    for a in bucket:
        aid = a.get("id")
        if aid is None:
            continue
        g.add_node(aid)
        for dep in a.get("depends_on", []) or []:
            # Only honor deps that point at a peer in this same bucket.
            if dep in by_id:
                g.add_edge(dep, aid)  # dep must come first

    if not nx.is_directed_acyclic_graph(g):
        cycles = list(nx.simple_cycles(g))
        raise OrderingError(f"dependency cycle among peers: {cycles}")

    # Map each id to its topological generation index.
    gen_of: dict[str, int] = {}
    for idx, generation in enumerate(nx.topological_generations(g)):
        for aid in generation:
            gen_of[aid] = idx

    return sorted(
        bucket,
        key=lambda a: _peer_sort_key(a, gen_of.get(a.get("id", ""), 0)),
    )


def resolve_order(
    acts: list[dict],
    spine_order: list[str] | None = None,
) -> list[dict]:
    """Fully order a list of activities (levels 2 + 3).

    Groups by (spine-index-of-target, position), orders each peer bucket via
    ``order_bucket``, then concatenates buckets by spine index. ``position`` is
    NOT reordered here — the caller passes a single position class (level 1).
    """
    spine_order = spine_order or []
    spine_index = {node: i for i, node in enumerate(spine_order)}

    def coord(a: dict) -> tuple:
        return (
            spine_index.get(a.get("target"), len(spine_order)),
            a.get("position", ""),
        )

    buckets: dict[tuple, list[dict]] = {}
    for a in acts:
        buckets.setdefault(coord(a), []).append(a)

    ordered: list[dict] = []
    for key in sorted(buckets.keys(), key=lambda k: (k[0], k[1])):
        ordered.extend(order_bucket(buckets[key]))
    return ordered
