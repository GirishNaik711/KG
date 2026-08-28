"""Deterministic fan-out coverage ratchet.

Functional, NO MOCKS. Builds a real DAG, runs the real
generate_checkpoint_config writer, and reads the floor back through the real
_get_coverage_threshold / run_coverage_check reader against real tmp projects.
"""

from aah.core.build.quality_checks import (
    _get_coverage_threshold,
    run_coverage_check,
)
from aah.core.common.dag import build_dag_from_features
from aah.core.common.io_utils import write_yaml
from aah.core.plan.determine_checkpoints import (
    generate_checkpoint_config,
)

# F_HUB has 4 direct dependents (fan-out=4 > K=3) → flagged.
# F_IFACE touches an api/schema path → flagged by the interface branch.
# F_LEAF has no dependents and a plain file_scope → NOT flagged.
_FEATURES = [
    {"id": "F_HUB", "dependencies": [], "file_scope": ["src/core/hub.py"]},
    {"id": "F_LEAF", "dependencies": [], "file_scope": ["src/util/leaf.py"]},
    {"id": "F_IFACE", "dependencies": [], "file_scope": ["src/api/schema.py"]},
    {"id": "F1", "dependencies": ["F_HUB"], "file_scope": ["src/a.py"]},
    {"id": "F2", "dependencies": ["F_HUB"], "file_scope": ["src/b.py"]},
    {"id": "F3", "dependencies": ["F_HUB"], "file_scope": ["src/c.py"]},
    {"id": "F4", "dependencies": ["F_HUB"], "file_scope": ["src/d.py"]},
]






def _write_config(tmp_path):
    """Run the real writer, persist the config to the standard path."""
    dag = build_dag_from_features(_FEATURES)
    waves = [["F_HUB", "F_LEAF", "F_IFACE"], ["F1", "F2", "F3", "F4"]]
    manifest = {"project_name": "t", "complexity_tier": "trivial", "stack_choices": {}}
    config = generate_checkpoint_config(
        manifest=manifest, dag=dag, waves=waves, adrs=[], nfrs=[]
    )
    write_yaml(config, tmp_path / ".aah" / "plan" / "checkpoint-config.yaml")
    return config


def test_ac1_floors_differ_read_through_reader(tmp_path):
    """High-fan-out and interface features receive the raised floor."""
    _write_config(tmp_path)

    assert _get_coverage_threshold(tmp_path, "F_HUB") == 80.0
    assert _get_coverage_threshold(tmp_path, "F_IFACE") == 80.0
    assert _get_coverage_threshold(tmp_path, "F_LEAF") == 60.0
    assert _get_coverage_threshold(tmp_path, "F1") == 60.0


def test_ac2_ratchet_enforced_by_run_coverage_check(tmp_path):
    """A 70% fixture passes the leaf floor but blocks the raised hub floor."""
    _write_config(tmp_path)

    # Real Node project whose test:coverage prints a 70% istanbul summary.
    (tmp_path / "package.json").write_text(
        '{"name":"t","version":"1.0.0","scripts":'
        '{"test:coverage":"echo \\"All files | 70 | 70 | 70 | 70 |\\""}}'
    )

    leaf = run_coverage_check(tmp_path, "F_LEAF")
    hub = run_coverage_check(tmp_path, "F_HUB")

    assert leaf["details"].get("coverage_percent") == 70.0
    assert leaf["details"].get("threshold") == 60.0
    assert leaf["passed"] is True

    assert hub["details"].get("threshold") == 80.0
    assert hub["passed"] is False


def test_ac3_no_lenient_gate_added(tmp_path):
    """The ratchet does not add a lenient replacement gate."""
    config = _write_config(tmp_path)
    cc = config["checkpoint_configuration"]

    floors = cc["standards"]["per_feature_coverage_floors"]
    assert floors == {"F_HUB": 80.0, "F_IFACE": 80.0}
    per_feature = cc["system_checkpoints"]["per_feature_checks"]
    assert per_feature["enabled"] is False
    assert per_feature["checks_by_feature"] == {}


def test_fanout_floor_still_derived(tmp_path):
    """The stored profile remains the floor source."""
    config = _write_config(tmp_path)["checkpoint_configuration"]
    assert config["verification_profiles"]["F_HUB"]["level"] == "deep"
    assert config["standards"]["per_feature_coverage_floors"]["F_HUB"] == 80.0


def test_adr_and_nfr_inputs_raise_checkpoint_risk():
    """Merge regression: the writer must consume ADR/NFR risk inputs."""
    dag = build_dag_from_features(_FEATURES)
    waves = [["F_HUB", "F_LEAF", "F_IFACE"], ["F1", "F2", "F3", "F4"]]
    manifest = {"project_name": "t", "complexity_tier": "trivial", "stack_choices": {}}

    baseline = generate_checkpoint_config(
        manifest=manifest, dag=dag, waves=waves, adrs=[], nfrs=[]
    )["checkpoint_configuration"]
    raised = generate_checkpoint_config(
        manifest=manifest,
        dag=dag,
        waves=waves,
        adrs=[{"id": f"ADR-{i}"} for i in range(3)],
        nfrs=[{"id": f"NFR-{i}", "priority": "Hard"} for i in range(3)],
    )["checkpoint_configuration"]

    assert raised["complexity_score"] == baseline["complexity_score"] + 23
    assert "critical_nfrs=3 >= 3" in raised["upgrade_reasons"]
