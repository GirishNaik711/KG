"""Functional tests for catalog anchor helpers (string + dict node_anchors forms)."""

from aah.core.ports import catalog as cat


def _catalog(node_anchors):
    return {"spine_order": ["architecture", "build"], "node_anchors": node_anchors, "activities": []}


class TestAnchorNames:
    def test_string_form(self):
        c = _catalog({"build": ["per-wave", "post-slice"]})
        assert cat.anchor_names(c, "build") == ["per-wave", "post-slice"]

    def test_dict_form(self):
        c = _catalog({"architecture": [{"name": "post-module-map", "description": "after modules"}]})
        assert cat.anchor_names(c, "architecture") == ["post-module-map"]

    def test_mixed_forms(self):
        c = _catalog({"build": ["per-wave", {"name": "post-slice", "description": "after a slice"}]})
        assert cat.anchor_names(c, "build") == ["per-wave", "post-slice"]

    def test_missing_node(self):
        assert cat.anchor_names(_catalog({}), "architecture") == []


class TestAnchorDescription:
    def test_returns_description_for_dict_entry(self):
        m = {"architecture": [{"name": "post-module-map", "description": "after module-map"}]}
        assert cat.anchor_description(m, "architecture", "post-module-map") == "after module-map"

    def test_string_entry_has_no_description(self):
        m = {"build": ["per-wave"]}
        assert cat.anchor_description(m, "build", "per-wave") is None

    def test_unknown_anchor(self):
        m = {"architecture": [{"name": "post-module-map", "description": "x"}]}
        assert cat.anchor_description(m, "architecture", "nope") is None


class TestValidatorAcceptsDictAnchors:
    def test_within_anchor_validates_against_dict_form(self):
        c = {
            "schema_version": "2.0",
            "spine_order": ["architecture"],
            "node_anchors": {"architecture": [{"name": "post-module-map", "description": "x"}]},
            "activities": [{
                "id": "ACT-T", "ref": "r", "type": "skill", "target": "architecture",
                "position": "within", "anchor": "post-module-map",
                "triggered_by": {"type": "discuss", "slug_id": "s", "option": "y"},
            }],
        }
        assert cat.validate_catalog(c) == []
