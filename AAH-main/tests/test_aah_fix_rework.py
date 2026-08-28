"""Functional tests (NO MOCKS) for the aah-fix current-wave rework model.

Covers the revamp plan:
  - Prereq #0: compute-blast-radius operates on the YAML master log.
  - Rework entry: built-feature feedback → <F>-rework-NN with supersedes,
    edges from spec, placed in the CURRENT wave; existing waves unchanged.
  - Current-wave placement: a rework lands in the current wave (before promote),
    never a future wave — this is the fix for the defer-past-promote deadlock.
  - New work (Case 4): future-wave placement remains available for brand-new
    features (current_wave_rework=False).
  - Regression exclusion: a live <F>-rework-NN with passing tests drops <F>.
  - Retry cap: re-triggering the same feedback bumps the attempt count.
  - Revert removed: rework_revert.py is gone; no revert_rework action fires.
  - Convergence: a two-level dependency chain places reworks in acyclic order.
"""

import json
from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.feature_utils import load_feature_data
from aah.core.build.feedback_capture import compute_blast_radius
from aah.core.build.rework import trigger_rework, get_rework_status, complete_rework
from aah.core.build.run_regression_suite import _supersede_exclusions
from aah.core.plan.build_dag import build_and_validate_dag
from aah.core.plan.compute_waves import place_rework_entry, flatten_waves
from aah.core.plan.create_rework_entry import create_rework_entry, next_rework_id


def _write_feature(features_dir: Path, fid: str, module: str, deps: list[str],
                   file_scope: list[str], acceptance: list[str]) -> None:
    acceptance_ids = [f"AC{index:03d}" for index in range(1, len(acceptance) + 1)]
    lines = [
        "## Id", f"- {fid}", "",
        "## Module Ref", f"- {module}", "",
        "## Description", f"{fid} description", "",
        "## Layers", "- backend", "",
    ]
    if deps:
        lines += ["## Dependencies", *[f"- {d}" for d in deps], ""]
    lines += ["## File Scope", *[f"- {f}" for f in file_scope], ""]
    lines += ["## Acceptance Criteria"]
    for ac_id, description in zip(acceptance_ids, acceptance):
        lines += [f"### {ac_id}: {description}", ""]
    lines += [
        "## Test Cases", "", "### TC001: basic", "- Type: functional",
        "- Covers:", *[f"  - {ac_id}" for ac_id in acceptance_ids], "",
    ]
    (features_dir / f"{fid}.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    """A minimal AAH project: two features F1 (auth) and F2 (auth, dep F1)."""
    aah = tmp_path / ".aah"
    features_dir = aah / "plan" / "features"
    features_dir.mkdir(parents=True)

    _write_feature(features_dir, "F1", "auth", [], ["src/auth/core.py"], ["core works"])
    _write_feature(features_dir, "F2", "auth", ["F1"], ["src/auth/login.py"],
                   ["login works"])

    write_json({"features": [
        {"id": "F1", "module_ref": "auth", "file_scope": ["src/auth/core.py"], "passes": True},
        {"id": "F2", "module_ref": "auth", "file_scope": ["src/auth/login.py"],
         "dependencies": ["F1"], "passes": True},
    ]}, aah / "feature-list.json")

    write_json({"waves": [[["F1"]], [["F2"]]], "total_waves": 2}, aah / "plan" / "waves.json")
    write_json(build_and_validate_dag(features_dir, None), aah / "plan" / "dag.json")

    write_yaml({"current_iteration": 1}, aah / "manifest.yaml")
    return tmp_path


# ---------------------------------------------------------------------------
# Prereq #0 — YAML master log blast radius
# ---------------------------------------------------------------------------


def test_compute_blast_radius_from_cli_modules(project):
    aah = project / ".aah"
    feedback_dir = aah / "build" / "feedback"
    feedback_dir.mkdir(parents=True)
    master = feedback_dir / "UF-01.yaml"
    # Modules come from the CLI (Option B); the plan step need not pre-declare
    # them — the script is the authoritative writer.
    write_yaml({
        "feedback_id": "UF-01",
        "wave": 1,
        "steps": [{"phase": "plan", "input": "fix login"}],
        "status": "in_progress",
    }, master)

    result = compute_blast_radius(project, str(master), ["auth"])

    # auth module → F1, F2 (by module_ref); F2 depends on F1 so downstream of F1
    # is F2 (already included). Both features are in the blast radius.
    assert set(result["blast_radius_features"]) == {"F1", "F2"}
    assert result["affected_modules"] == ["auth"]

    # Script stamps BOTH affected_modules and blast_radius_features into the step.
    from aah.core.common.io_utils import read_yaml
    ml = read_yaml(master)
    plan_step = next(s for s in ml["steps"] if s.get("phase") == "plan")
    assert plan_step["affected_modules"] == ["auth"]
    assert set(plan_step["blast_radius_features"]) == {"F1", "F2"}


def test_compute_blast_radius_creates_plan_step_when_absent(project):
    """Option B: no read-before-write dependency — the script creates a plan
    step if none exists and stamps the fields into it."""
    aah = project / ".aah"
    feedback_dir = aah / "build" / "feedback"
    feedback_dir.mkdir(parents=True)
    master = feedback_dir / "UF-03.yaml"
    write_yaml({"feedback_id": "UF-03", "steps": [], "status": "in_progress"}, master)

    result = compute_blast_radius(project, str(master), ["auth"])
    assert set(result["blast_radius_features"]) == {"F1", "F2"}

    from aah.core.common.io_utils import read_yaml
    ml = read_yaml(master)
    assert any(s.get("phase") == "plan" for s in ml["steps"])


def test_compute_blast_radius_empty_modules_writes_empty(project):
    aah = project / ".aah"
    feedback_dir = aah / "build" / "feedback"
    feedback_dir.mkdir(parents=True)
    master = feedback_dir / "UF-02.yaml"
    write_yaml({
        "feedback_id": "UF-02",
        "steps": [{"phase": "plan", "input": "x"}],
        "status": "in_progress",
    }, master)

    result = compute_blast_radius(project, str(master), [])
    assert result["blast_radius_features"] == []


# ---------------------------------------------------------------------------
# Rework entry creation (Case A — implemented feature)
# ---------------------------------------------------------------------------


def test_rework_entry_has_supersedes_and_edges_from_spec(project):
    result = create_rework_entry(project, "F2", current_wave=1)

    assert result["rework_id"] == "F2-rework-01"
    assert result["supersedes"] == "F2"
    assert result["spec_file"] == "F2.md"

    aah = project / ".aah"
    md = (aah / "plan" / "features" / "F2-rework-01.md").read_text()
    assert "## Supersedes\n- F2" in md
    assert "## Spec File\n- F2.md" in md

    # feature-list carries the rework entry with passes:false + supersedes.
    fl = read_json(aah / "feature-list.json")
    entry = next(f for f in fl["features"] if f["id"] == "F2-rework-01")
    assert entry["passes"] is False
    # supersedes parsed from the bullet (list or scalar both acceptable)
    sup = entry.get("supersedes")
    assert sup == "F2" or sup == ["F2"]

    # DAG edge derived from the updated spec: F1 → F2-rework-01.
    dag = read_json(aah / "plan" / "dag.json")
    edges = {(e["from"], e["to"]) for e in dag["edges"]}
    assert ("F1", "F2-rework-01") in edges


def test_rework_entry_is_pointer_stub_no_body(project):
    """Option Z: the rework .md is a metadata-only stub — no duplicated spec."""
    create_rework_entry(project, "F2", current_wave=1)
    aah = project / ".aah"
    md = (aah / "plan" / "features" / "F2-rework-01.md").read_text()

    # Only identity/pointer sections — body sections live in F2.md, not here.
    assert "## Id" in md
    assert "## Supersedes" in md
    assert "## Spec File" in md
    assert "## File Scope" not in md
    assert "## Acceptance Criteria" not in md
    assert "## Test Cases" not in md
    # Stub is tiny.
    assert len([l for l in md.splitlines() if l.strip()]) <= 8


def test_pointer_resolves_base_spec_under_rework_id(project):
    """parse/load follows spec_file and merges F2's body under the rework id."""
    create_rework_entry(project, "F2", current_wave=1)
    features_dir = project / ".aah" / "plan" / "features"

    resolved = load_feature_data(features_dir, "F2-rework-01")
    # Identity from the stub...
    assert resolved["id"] == "F2-rework-01"
    assert resolved.get("spec_file") == "F2.md"
    sup = resolved.get("supersedes")
    assert sup == "F2" or sup == ["F2"]
    # ...body from F2.md (the single source of truth).
    assert resolved["dependencies"] == ["F1"]
    assert resolved["file_scope"] == ["src/auth/login.py"]
    assert resolved["acceptance_criteria"] == [
        {"id": "AC001", "description": "login works"}
    ]


def test_base_spec_update_flows_through_pointer(project):
    """Editing F2.md is reflected when reading F2-rework-01 (no duplication)."""
    features_dir = project / ".aah" / "plan" / "features"
    create_rework_entry(project, "F2", current_wave=1)

    # Update the single source of truth AFTER the stub exists.
    f2 = (features_dir / "F2.md").read_text()
    f2 = f2.replace(
        "### AC001: login works",
        "### AC001: login works\n\n### AC002: MFA supported",
    ).replace("  - AC001", "  - AC001\n  - AC002")
    (features_dir / "F2.md").write_text(f2)

    resolved = load_feature_data(features_dir, "F2-rework-01")
    descriptions = {item["description"] for item in resolved["acceptance_criteria"]}
    assert "MFA supported" in descriptions


def test_rework_entry_placed_in_current_wave(project):
    """Current-wave rework model: a rework of a built feature lands in the
    CURRENT wave (before promote), appended as a new tier — NOT a future wave.
    This is the fix for the defer-past-promote deadlock."""
    aah = project / ".aah"
    before = read_json(aah / "plan" / "waves.json")["waves"]

    create_rework_entry(project, "F2", current_wave=1)

    after = read_json(aah / "plan" / "waves.json")["waves"]
    # Wave 0 is untouched; no NEW wave is appended (still 2 waves).
    assert after[0] == before[0]
    assert len(after) == 2
    # The rework entry is appended as a new tier of the CURRENT wave (index 1).
    assert after[1] == [["F2"], ["F2-rework-01"]]
    assert "F2-rework-01" in [f for w in flatten_waves({"waves": after}) for f in w]


def test_rework_in_last_wave_needs_no_future_wave(project):
    """Even when the current wave is the LAST wave, the rework lands in it —
    no future wave need be invented."""
    aah = project / ".aah"
    # current_wave points at the last wave (index 1).
    create_rework_entry(project, "F2", current_wave=1)
    after = read_json(aah / "plan" / "waves.json")["waves"]
    assert len(after) == 2  # no new wave appended
    assert ["F2-rework-01"] in after[1]


def test_next_rework_id_increments(project):
    aah = project / ".aah"
    create_rework_entry(project, "F2", current_wave=1)
    # A second rework of F2 should be -rework-02.
    assert next_rework_id(aah / "plan" / "features", "F2") == "F2-rework-02"


def test_original_feature_stays_passing(project):
    """Case A: the original F2 keeps passes:true; only the rework entry builds."""
    create_rework_entry(project, "F2", current_wave=1)
    fl = read_json(project / ".aah" / "feature-list.json")
    f2 = next(f for f in fl["features"] if f["id"] == "F2")
    assert f2["passes"] is True


# ---------------------------------------------------------------------------
# place_rework_entry — DAG-valid placement
# ---------------------------------------------------------------------------


def test_place_rework_lands_in_current_wave():
    # current_wave=1 → rework appended as a new tier of wave 1 (before promote).
    wd = {"waves": [[["X1"]], [["X2"], ["X3"]]], "total_waves": 2}
    place_rework_entry(wd, "R-rework-01", ["X3"], 1)
    assert wd["waves"][1] == [["X2"], ["X3"], ["R-rework-01"]]
    assert len(wd["waves"]) == 2  # no new wave appended


def test_place_rework_current_wave_past_end_clamps():
    # current_wave points past the list → append a new wave holding the entry.
    wd = {"waves": [[["X1"]]], "total_waves": 1}
    place_rework_entry(wd, "R-rework-01", [], 5)
    assert "R-rework-01" in [f for w in flatten_waves(wd) for f in w]


def test_place_new_work_lands_in_future_wave():
    """Case 4 (brand-new feature): current_wave_rework=False → earliest DAG-valid
    FUTURE wave, no earlier than current_wave + 1 and after the last prereq."""
    wd = {"waves": [[["X1"]], [["X2"], ["X3"]]], "total_waves": 2}
    place_rework_entry(wd, "N-01", ["X3"], 0, current_wave_rework=False)
    # X3 in wave 1 → future placement is wave 2.
    assert wd["waves"][2] == [["N-01"]]


def test_place_new_work_never_before_current_wave_plus_one():
    wd = {"waves": [[["X1"]], [["X2"]], [["X3"]]], "total_waves": 3}
    place_rework_entry(wd, "N-01", [], 1, current_wave_rework=False)
    assert "N-01" in [f for w in flatten_waves(wd) for f in w]
    assert "N-01" not in wd["waves"][0][0]
    assert "N-01" not in [f for b in wd["waves"][1] for f in b]


# ---------------------------------------------------------------------------
# Regression supersede exclusion
# ---------------------------------------------------------------------------


def test_regression_excludes_superseded_only_when_rework_passes(project):
    aah = project / ".aah"
    create_rework_entry(project, "F2", current_wave=1)
    fl = read_json(aah / "feature-list.json")

    # No rework test result yet → F2 stays in the regression set.
    assert _supersede_exclusions(aah, fl) == set()

    # Passing rework test result → F2 dropped.
    tr = aah / "build" / "test-results"
    tr.mkdir(parents=True)
    write_json({"passed": True}, tr / "F2-rework-01.json")
    assert _supersede_exclusions(aah, fl) == {"F2"}

    # Failing rework → no exclusion (coverage never silently drops).
    write_json({"passed": False}, tr / "F2-rework-01.json")
    assert _supersede_exclusions(aah, fl) == set()


# ---------------------------------------------------------------------------
# rework.py trigger — forward model (no backward reset)
# ---------------------------------------------------------------------------


def test_trigger_resets_only_named_features_and_bumps_iteration(project):
    aah = project / ".aah"
    create_rework_entry(project, "F2", current_wave=1)

    result = trigger_rework(project, "UF-01", affected_features_input=["F2-rework-01"])
    assert result["affected_features"] == ["F2-rework-01"]

    # Rework entry stays passes:false; original F2 untouched (still true).
    fl = read_json(aah / "feature-list.json")
    assert next(f for f in fl["features"] if f["id"] == "F2")["passes"] is True

    # Iteration bumped.
    from aah.core.common.io_utils import read_yaml
    assert read_yaml(aah / "manifest.yaml")["current_iteration"] == 2

    # Active rework recorded; retiring works via complete.
    status = get_rework_status(project)
    assert status["active_reworks"] == 1
    complete_rework(project, "UF-01")
    assert get_rework_status(project)["active_reworks"] == 0


def test_trigger_bumps_attempts_on_retrigger_same_feedback(project):
    """Enabler 2: re-triggering the same feedback (a failed verify looping back)
    bumps the attempt count instead of appending a duplicate rework entry."""
    aah = project / ".aah"
    create_rework_entry(project, "F2", current_wave=1)

    trigger_rework(project, "UF-06-147", affected_features_input=["F2-rework-01"])
    r2 = trigger_rework(project, "UF-06-147", affected_features_input=["F2-rework-01"])
    assert r2["attempts"] == 2

    state = read_json(aah / "build" / "rework-state.json")
    in_progress = [r for r in state["active_reworks"]
                   if r["feedback_id"] == "UF-06-147" and r["status"] == "in_progress"]
    assert len(in_progress) == 1  # no duplicate entry
    assert in_progress[0]["attempts"] == 2


def test_trigger_does_not_reset_current_wave_backwards(project):
    """Forward model: trigger must never rewind progress.current_wave."""
    aah = project / ".aah"
    write_json({"current_wave": 1, "current_tier": 0}, aah / "claude-progress.json")
    create_rework_entry(project, "F2", current_wave=1)

    trigger_rework(project, "UF-01", affected_features_input=["F2-rework-01"])

    progress = read_json(aah / "claude-progress.json")
    assert progress["current_wave"] == 1  # unchanged


# ---------------------------------------------------------------------------
# Revert removed
# ---------------------------------------------------------------------------


def test_rework_revert_module_absent():
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("aah.core.git_ops.rework_revert")


def test_orchestrator_has_no_revert_or_sequential_action():
    src = Path("aah/core/build/orchestrator.py").read_text()
    assert '"action": "revert_rework"' not in src
    assert '"action": "dispatch_sequential"' not in src
    assert '"action": "plan_reentry"' not in src


# ---------------------------------------------------------------------------
# Convergence — two-level dependency chain places reworks acyclically
# ---------------------------------------------------------------------------


def test_two_level_rework_chain_converges(project):
    """F2 reworked, then its downstream (a new F7 depending on F2) also
    reworked — both land in the current wave as new tiers, DAG stays acyclic."""
    aah = project / ".aah"
    features_dir = aah / "plan" / "features"

    # Add F7 depending on F2, implemented + in wave 1.
    _write_feature(features_dir, "F7", "auth", ["F2"], ["src/auth/session.py"],
                   ["session works"])
    fl = read_json(aah / "feature-list.json")
    fl["features"].append({"id": "F7", "module_ref": "auth",
                           "file_scope": ["src/auth/session.py"],
                           "dependencies": ["F2"], "passes": True})
    write_json(fl, aah / "feature-list.json")
    waves = read_json(aah / "plan" / "waves.json")
    waves["waves"][1].append(["F7"])
    write_json(waves, aah / "plan" / "waves.json")
    write_json(build_and_validate_dag(features_dir, None), aah / "plan" / "dag.json")

    # First rework: F2.
    r1 = create_rework_entry(project, "F2", current_wave=1)
    # Second rework: F7 (downstream), after F2's rework exists.
    r2 = create_rework_entry(project, "F7", current_wave=1)

    # DAG must remain acyclic and include both rework nodes.
    import networkx as nx
    from aah.core.common.dag import dag_from_json
    G = dag_from_json(read_json(aah / "plan" / "dag.json"))
    assert nx.is_directed_acyclic_graph(G)
    assert "F2-rework-01" in G
    assert "F7-rework-01" in G
    assert r1["rework_id"] == "F2-rework-01"
    assert r2["rework_id"] == "F7-rework-01"
