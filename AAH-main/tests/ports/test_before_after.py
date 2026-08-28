"""Functional tests for before/after node filtering + update + check-guarantees.

Activation is id-based (activates[] carries catalog ids). Both ACT-UX and
ACT-ACCESS are `available` and fire as `pending`. ACT-UX is architecture/within
@post-module-map; ACT-ACCESS is architecture/before. op_before returns before +
within, ordered by (position, order) — "before" sorts ahead of "within", so
ACT-ACCESS precedes ACT-UX in that list.
"""

from aah.core.ports import executor

from tests.ports.conftest import write_discuss_registry


def _compiled(project):
    executor.op_init(project)
    write_discuss_registry(project, [
        {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},        # architecture/within @post-module-map
        {"slug_id": "cloud-vs-local", "response": "cloud", "activates": ["ACT-ACCESS"]},      # architecture/before
    ])
    executor.op_compile(project)


class TestBeforeAfter:
    def test_before_returns_bound_only(self, project):
        _compiled(project)
        # op_before returns before + within: ACT-ACCESS (before) then ACT-UX
        # (within@post-module-map) — "before" sorts ahead of "within".
        acts = executor.op_before(project, "architecture")["activities"]
        ids = [a["id"] for a in acts]
        assert ids == ["ACT-ACCESS", "ACT-UX"]

    def test_within_carries_anchor_location(self, project):
        # A within port must surface anchor_location (from node_anchors' description)
        # so the core skill can self-localize without an inline marker.
        _compiled(project)
        acts = executor.op_before(project, "architecture")["activities"]
        ux = next(a for a in acts if a["id"] == "ACT-UX")
        assert ux["anchor"] == "post-module-map"
        assert ux.get("anchor_location")            # non-empty description present
        assert "module-map" in ux["anchor_location"].lower()

    def test_within_subcommand_carries_location(self, project):
        _compiled(project)
        acts = executor.op_within(project, "architecture", "post-module-map")["activities"]
        assert [a["id"] for a in acts] == ["ACT-UX"]
        assert acts[0].get("anchor_location")

    def test_after_returns_bound_only(self, project):
        _compiled(project)
        # No port binds to architecture/after — ACT-ACCESS is `before`, ACT-UX is
        # `within`. The after bucket for architecture is empty.
        acts = executor.op_after(project, "architecture")["activities"]
        assert [a["id"] for a in acts] == []

    def test_discuss_before_returns_default(self, project):
        _compiled(project)
        acts = executor.op_before(project, "discuss")["activities"]
        assert [a["id"] for a in acts] == ["ACT-INTENT-MEDIATOR"]

    def test_empty_node_is_inert(self, project):
        _compiled(project)
        assert executor.op_before(project, "plan")["activities"] == []
        assert executor.op_after(project, "deploy")["activities"] == []

    def test_unactivated_activity_not_returned(self, project):
        # ACT-ACCESS not named in any activates[] → skipped → not surfaced at after.
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
        ])
        executor.op_compile(project)
        assert executor.op_after(project, "architecture")["activities"] == []


class TestUpdate:
    def test_update_records_status_and_artifact(self, project):
        _compiled(project)
        r = executor.op_update(project, "ACT-UX", "completed",
                               artifacts=[".aah/architecture/applications_wireframes/README.md"])
        assert r["to"] == "completed"
        assert ".aah/architecture/applications_wireframes/README.md" in r["artifacts_produced"]

    def test_update_unknown_activity(self, project):
        _compiled(project)
        r = executor.op_update(project, "ACT-NOPE", "completed")
        assert "error" in r

    def test_invalid_status(self, project):
        _compiled(project)
        r = executor.op_update(project, "ACT-UX", "bogus")
        assert "error" in r


class TestCheckGuarantees:
    def test_missing_consumes_reported(self, project):
        _compiled(project)
        # ACT-UX is `available` → pending → its consumes ARE checked.
        # op_before covers before+within, so check "before" reaches the within port.
        r = executor.op_check_guarantees(project, "architecture", "before")
        # ACT-UX consumes .aah/discuss/project-intent.yaml + .aah/architecture/module-map.yaml — absent here.
        assert r["ok"] is False
        assert r["missing"]

    def test_after_coordinate_has_no_guarantees(self, project):
        # No port binds to architecture/after, so check-guarantees at that
        # coordinate has nothing to verify and passes vacuously.
        _compiled(project)
        r = executor.op_check_guarantees(project, "architecture", "after")
        assert r["ok"] is True

    def test_consumes_present_ok(self, project):
        _compiled(project)
        # ACT-UX consumes subdir-qualified paths — create them at their real locations.
        for rel in (".aah/discuss/project-intent.yaml", ".aah/architecture/module-map.yaml"):
            p = project / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        r = executor.op_check_guarantees(project, "architecture", "before")
        assert r["ok"] is True
