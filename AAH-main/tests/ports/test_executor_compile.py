"""Functional tests for `ports.executor compile` — the discuss Step 13 merge.

A discuss-triggered port fires on EITHER signal:
  1. PRIMARY  — its catalog id appears in some decision's activates[].
  2. FALLBACK — its catalog triggered_by rule matches a recorded answer, even
                when activates[] is empty (guards against a missing --activates-json
                during the walk). Fire-only: the discuss registry is not modified;
                the gap is surfaced via fired_via_fallback + missing_activates.
"""

from aah.core.common.io_utils import read_yaml
from aah.core.ports import executor

from tests.ports.conftest import write_discuss_registry


def _by_id(project):
    reg = read_yaml(executor.registry_path(project))
    return {a["id"]: a for a in reg["activities"]}, reg


class TestCompile:
    def test_fired_and_skipped(self, project):
        executor.op_init(project)
        # activates:[ACT-UX] fires the UI port; ACT-ACCESS is never named → skipped.
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
            {"slug_id": "cloud-vs-local", "response": "local", "activates": []},
        ])
        result = executor.op_compile(project)

        assert "ACT-UX" in result["fired"]
        assert "ACT-ACCESS" in result["skipped"]

        acts, reg = _by_id(project)
        assert reg["compiled_at"]
        # ACT-UX is available (aah-ux exists) → pending, ready to run.
        assert acts["ACT-UX"]["status"] == "pending"
        assert "activates[]" in acts["ACT-UX"]["trigger_reason"]
        assert acts["ACT-ACCESS"]["status"] == "skipped"

    def test_defaults_untouched(self, project):
        executor.op_init(project)
        executor.op_update(project, "ACT-INTENT-MEDIATOR", "completed")
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
        ])
        executor.op_compile(project)

        acts, _ = _by_id(project)
        # The default retained its completed status through compile.
        assert acts["ACT-INTENT-MEDIATOR"]["status"] == "completed"

    def test_activates_fires_access(self, project):
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "cloud-vs-local", "response": "cloud", "activates": ["ACT-ACCESS"]},
        ])
        result = executor.op_compile(project)
        assert "ACT-ACCESS" in result["fired"]

    def test_answer_fires_via_trigger_fallback(self, project):
        # The answer matches the catalog triggered_by option but activates[] is
        # empty (the walk forgot --activates-json). The triggered_by FALLBACK
        # must fire the port anyway, and surface the missing link.
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": []},
        ])
        result = executor.op_compile(project)

        assert "ACT-UX" in result["fired"]
        assert "ACT-UX" not in result["skipped"]
        assert "ACT-UX" in result["fired_via_fallback"]
        miss = {m["activity_id"]: m for m in result["missing_activates"]}
        assert miss["ACT-UX"]["slug_id"] == "custom-ui-required"
        assert miss["ACT-UX"]["option"] == "yes"

        acts, _ = _by_id(project)
        assert acts["ACT-UX"]["status"] == "pending"
        assert "triggered_by fallback" in acts["ACT-UX"]["trigger_reason"]

    def test_activates_and_trigger_both_present_fires_once(self, project):
        # Both the explicit activates[] link AND the triggered_by answer match are
        # present → the port fires once (no duplicate row) via the PRIMARY path,
        # so it is NOT reported as a fallback / missing-activates gap.
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
        ])
        result = executor.op_compile(project)

        assert result["fired"].count("ACT-UX") == 1
        assert "ACT-UX" not in result["fired_via_fallback"]
        assert all(m["activity_id"] != "ACT-UX" for m in result["missing_activates"])

        acts, reg = _by_id(project)
        assert [a["id"] for a in reg["activities"]].count("ACT-UX") == 1
        assert "activates[]" in acts["ACT-UX"]["trigger_reason"]

    def test_wrong_answer_no_activates_still_skipped(self, project):
        # Answer does NOT match the catalog triggered_by option and activates[] is
        # empty → neither signal fires. The fallback must not over-fire.
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "no", "activates": []},
            {"slug_id": "cloud-vs-local", "response": "local", "activates": []},
        ])
        result = executor.op_compile(project)

        assert "ACT-UX" in result["skipped"]
        assert "ACT-ACCESS" in result["skipped"]
        assert result["fired_via_fallback"] == []
        assert result["missing_activates"] == []

    def test_trigger_fallback_from_pre_resolved(self, project):
        # An inferred answer written to pre_resolved[] (not decisions[]) with no
        # activates still fires its port via the triggered_by fallback.
        executor.op_init(project)
        write_discuss_registry(
            project,
            decisions=[],
            pre_resolved=[
                {"slug_id": "cloud-vs-local", "value": "cloud", "activates": []},
            ],
        )
        result = executor.op_compile(project)

        assert "ACT-ACCESS" in result["fired"]
        assert "ACT-ACCESS" in result["fired_via_fallback"]

    def test_multiple_activates(self, project):
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
            {"slug_id": "cloud-vs-local", "response": "cloud", "activates": ["ACT-ACCESS"]},
        ])
        result = executor.op_compile(project)
        assert "ACT-UX" in result["fired"]
        assert "ACT-ACCESS" in result["fired"]
