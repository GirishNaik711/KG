"""Functional tests for the pure 3-level order resolver."""

import pytest

from aah.core.ports import ordering


SPINE = ["init-project", "discuss", "architecture", "plan", "build", "fix", "deploy"]


def _act(aid, target="build", position="after", order=10, depends_on=None):
    return {
        "id": aid, "target": target, "position": position,
        "order": order, "depends_on": depends_on or [],
    }


class TestPeerOrder:
    def test_order_tiebreak(self):
        acts = [_act("B", order=20), _act("A", order=10)]
        result = [a["id"] for a in ordering.order_bucket(acts)]
        assert result == ["A", "B"]

    def test_stable_id_when_equal_order(self):
        acts = [_act("Z", order=10), _act("A", order=10)]
        result = [a["id"] for a in ordering.order_bucket(acts)]
        assert result == ["A", "Z"]

    def test_depends_on_forces_generation(self):
        # B depends on A → A must come first even though B has lower order.
        acts = [_act("B", order=1, depends_on=["A"]), _act("A", order=99)]
        result = [a["id"] for a in ordering.order_bucket(acts)]
        assert result == ["A", "B"]

    def test_cycle_raises(self):
        acts = [_act("A", depends_on=["B"]), _act("B", depends_on=["A"])]
        with pytest.raises(ordering.OrderingError):
            ordering.order_bucket(acts)


class TestResolveOrder:
    def test_spine_orders_across_nodes(self):
        # deploy activity should sort after an architecture activity.
        acts = [
            _act("D", target="deploy", position="after"),
            _act("A", target="architecture", position="after"),
        ]
        result = [a["id"] for a in ordering.resolve_order(acts, SPINE)]
        assert result == ["A", "D"]

    def test_empty(self):
        assert ordering.resolve_order([], SPINE) == []
