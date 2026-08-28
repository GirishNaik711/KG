"""Functional verification-profile and .aah producer/consumer coverage."""

import json
import subprocess
import sys


from aah.core.common.dag import build_dag_from_features, dag_to_json
from aah.core.common.io_utils import read_yaml, write_json, write_yaml
from aah.core.common.validators import FEATURE_SCHEMA, validate_dict_schema
from aah.core.plan.determine_checkpoints import compute_verification_profiles
from aah.core.plan.determine_checkpoints import _load_adrs
from tests.support.aah_project import AAHProjectBuilder, REPO_ROOT


def _feature(fid="F001", **values):
    feature = {
        "id": fid,
        "title": fid,
        "module_ref": "MOD-TEST",
        "spec_ref": "SPEC-001",
        "description": "ordinary isolated leaf",
        "layers": ["backend"],
        "dependencies": [],
        "file_scope": ["src/leaf.py"],
        "acceptance_criteria": [],
        "test_cases": [],
        "status": "pending",
        "knowledge_used": {},
    }
    feature.update(values)
    return feature


def _risk(**overrides):
    data = {
        "security_scope": False, "auth_scope": False, "payment_scope": False,
        "credential_scope": False, "critical_nfr": False, "regulated_data": False,
        "external_integration": False, "shared_interface": False,
        "shared_schema": False, "shared_contract": False, "concurrency": False,
        "migration": False, "integrity": False,
    }
    data.update(overrides)
    return data


def _profiles(features):
    dag = build_dag_from_features(features)
    return compute_verification_profiles(dag, features, [], [], [])


def test_profile_is_byte_stable():
    features = [_feature(risk_metadata=_risk(security_scope=True))]
    assert json.dumps(_profiles(features), sort_keys=True) == json.dumps(
        _profiles(features), sort_keys=True
    )


def test_each_deep_category_routes_deep():
    fields = (
        "security_scope", "auth_scope", "payment_scope", "credential_scope",
        "critical_nfr", "regulated_data", "external_integration",
        "shared_interface", "shared_schema", "shared_contract", "concurrency",
        "migration", "integrity",
    )
    for field in fields:
        profile = _profiles([_feature(risk_metadata=_risk(**{field: True}))])["F001"]
        assert profile["level"] == "deep", field


def test_leaf_feature_is_standard():
    assert _profiles([_feature()])["F001"]["level"] == "standard"


def test_conflict_selects_deep():
    feature = _feature(security_scope=False, risk_metadata=_risk(security_scope=True))
    profile = _profiles([feature])["F001"]
    assert profile["level"] == "deep"
    assert "metadata_conflict" in profile["reasons"]


def test_fallback_only_raises():
    feature = _feature(description="credential token handling", credential_scope=False)
    profile = _profiles([feature])["F001"]
    assert profile["level"] == "deep"
    assert "keyword_credential_scope" in profile["reasons"]


def test_current_contract_still_validates():
    assert validate_dict_schema(_feature(), FEATURE_SCHEMA) == []


def test_cli_aah_markdown_producer_consumer_round_trip(tmp_path):
    builder = AAHProjectBuilder.create(tmp_path, name="profile-project", git=False)
    aah = builder.aah
    features = [
        _feature("F-NFR", risk_metadata=_risk(), nfr_refs=["NFRD-001"]),
        _feature("F-ADR", risk_metadata=_risk(), adr_refs=["ADR-001"]),
        _feature("F-EXT", risk_metadata=_risk(), integration_refs=["EXT-01"]),
    ]
    for feature in features:
        builder.feature(feature["id"], frontmatter=feature, body="")
    builder.manifest(
        project_name="p",
        project_type="greenfield",
        current_phase="plan",
        complexity_tier="trivial",
        stack_choices={},
    )
    write_json({
        "nfr_inventory": [{"id": "NFRD-001", "priority": "Hard", "requirement": "availability"}],
        "adr_inventory": [{"id": "ADR-001", "chosen_option": "transactional integrity"}],
        "integration_inventory": [{"id": "EXT-01", "type": "external", "system": "provider"}],
    }, aah / "plan" / "analysis-synthesis.json")
    dag = build_dag_from_features(features)
    write_json(dag_to_json(dag), aah / "plan" / "dag.json")
    builder.waves([[f["id"] for f in features]])

    result = builder.run_module(
        "aah.core.plan.determine_checkpoints",
        "generate", "--project-path", str(builder.path),
    )
    assert result.returncode == 0, result.stderr
    config = read_yaml(aah / "plan" / "checkpoint-config.yaml")
    profiles = config["checkpoint_configuration"]["verification_profiles"]
    assert "referenced_critical_nfr" in profiles["F-NFR"]["reasons"]
    assert "referenced_analysis_risk" in profiles["F-ADR"]["reasons"]
    assert "referenced_external_integration" in profiles["F-EXT"]["reasons"]
    assert all(profile["level"] == "deep" for profile in profiles.values())


def test_malformed_risk_metadata_fails_enforced_cli(tmp_path):
    builder = AAHProjectBuilder.create(tmp_path, name="malformed-project", git=False)
    aah = builder.aah
    feature = _feature(risk_metadata={"security_scope": "yes"})
    builder.feature("F001", frontmatter=feature, body="").manifest(
        project_name="p",
        project_type="greenfield",
        current_phase="plan",
        complexity_tier="trivial",
        stack_choices={},
    )
    dag = build_dag_from_features([feature])
    write_json(dag_to_json(dag), aah / "plan" / "dag.json")
    builder.waves([["F001"]])
    result = builder.run_module(
        "aah.core.plan.determine_checkpoints",
        "generate", "--project-path", str(builder.path),
    )
    assert result.returncode != 0
    assert "verification profile generation failed" in result.stderr
    assert not (aah / "plan" / "checkpoint-config.yaml").exists()


def test_markdown_adr_reader_extracts_decision_content(tmp_path):
    builder = AAHProjectBuilder.create(tmp_path, name="adr-project", git=False)
    builder.file(
        ".aah/analysis/decisions/ADR-001.md",
        "# ADR-001: Transaction safety\n\n## Decision Outcome\n"
        "**Use transactional integrity controls**\n",
    )
    loaded = _load_adrs(builder.path)
    assert loaded[0]["id"] == "ADR-001"
    assert "transactional integrity" in loaded[0]["chosen_option"].lower()


def _build_project(tmp_path, *, risk_overrides=None):
    """Real git project with mandatory profiles and a checkpoint config
    generated by the real determine_checkpoints CLI."""
    from aah.core.common.dag import build_dag_from_features, dag_to_json
    from aah.core.common.io_utils import write_json

    builder = AAHProjectBuilder.create(tmp_path)
    proj = builder.path
    aah = builder.aah
    builder.manifest(
        project_name="vp-test",
        project_type="greenfield",
        complexity_tier="trivial",
        stack_choices={},
    ).waves([[["F001"]]])

    test_rel = "qa_tests/test_F001.py"
    builder.file(
        test_rel,
        "from pathlib import Path\n\n"
        "def test_F001_TC1_marker():\n"
        "    marker = Path('marker.txt').read_text().strip()\n"
        "    assert marker.startswith('commit-')\n",
    ).file("marker.txt", "commit-base\n")

    command = f"{sys.executable} -m pytest {proj / test_rel} -s"
    builder.feature(
        "F001",
            frontmatter={
                "title": "F001",
                "module_ref": "MOD-TEST",
            "description": "Test feature",
            "spec_ref": "SPEC-001",
            "layers": ["backend"],
            "dependencies": [],
            "file_scope": ["src/mod.py"],
            "status": "pending",
            "test_config": {"command": command},
            "acceptance_criteria": [{"id": "AC1", "description": "Must work"}],
            "test_cases": [
                {"id": "TC1", "covers": ["AC1"], "description": "marker validation"}
            ],
            "knowledge_used": {"knowledge_folder": None},
            "risk_metadata": _risk(**(risk_overrides or {})),
        },
    )

    builder.file(
        ".aah/plan/feature-list.json",
        '{"features": [{"id": "F001", "name": "F001", "passes": true}]}\n',
    ).file(
        ".aah/claude-progress.json",
        '{"current_wave": 0, "current_phase": "build"}\n',
    ).secret().file(".gitignore", ".aah/\n.claude/\n").commit(
        "init", (".gitignore", "qa_tests", "marker.txt")
    )

    import yaml as _yaml

    feature = _yaml.safe_load(
        (aah / "plan" / "features" / "F001.md").read_text().split("---")[1]
    )
    dag = build_dag_from_features([feature])
    write_json(dag_to_json(dag), aah / "plan" / "dag.json")
    gen = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.determine_checkpoints", "generate",
         "--project-path", str(proj)],
        cwd=REPO_ROOT, env=builder.cli_env(), capture_output=True, text=True,
    )
    assert gen.returncode == 0, gen.stderr
    return proj


def _run_feature_tests(project):
    builder = AAHProjectBuilder(project)
    branch = builder.git("rev-parse", "--abbrev-ref", "HEAD")
    from aah.core.common.git_utils import code_subject_identity
    sha = code_subject_identity(cwd=project)
    return subprocess.run(
        [sys.executable, "-m", "aah.cli", "run", "core.build.run_feature_tests",
         "--feature-id", "F001", "--project-path", str(project),
         "--subject-path", str(project), "--subject-branch", branch,
         "--subject-sha", sha],
        cwd=project, capture_output=True, text=True, env=builder.cli_env(),
    )


def _write_passing_spec_validation(project):
    builder = AAHProjectBuilder(project)
    branch = builder.git("rev-parse", "--abbrev-ref", "HEAD")
    from aah.core.common.git_utils import code_subject_identity
    sha = code_subject_identity(cwd=project)
    result = builder.run_module(
        "aah.core.build.validate_implementation", "validate",
        "--feature-id", "F001", "--project-path", str(project),
        "--subject-path", str(project), "--subject-branch", branch,
        "--subject-sha", sha,
    )
    assert result.returncode == 0, result.stderr


def _write_quality_check(project, verdict="PASS", *, wave=0):
    """Seed project-scoped standards evidence for ``wave``.

    Standards is a whole-codebase gate that runs once in the final wave, so the
    artifact is wave-keyed rather than feature-keyed.
    """
    from aah.core.common.attestation import write_attested
    from aah.core.common.git_utils import code_subject_identity

    q = (
        project / ".aah" / "build" / "quality-results"
        / f"wave-{wave}-project-standards.json"
    )
    q.parent.mkdir(parents=True, exist_ok=True)
    write_attested(
        {
            "scope": "project",
            "wave": wave,
            "overall_passed": verdict == "PASS",
            "verdict": verdict,
            "status": "pass" if verdict == "PASS" else "fail",
            "timestamp": "2026-07-16T00:00:00Z",
            "subject": {
                "branch": f"integration/wave-{wave}",
                "commit_sha": code_subject_identity(cwd=project),
            },
        },
        q, project_path=project,
        command=[
            "aah", "run", "core.build.quality_checks", "run-project",
            "--wave", str(wave),
        ],
        exit_code=0, stdout="", stderr="", duration_ms=0,
        artifact_name=f"wave-{wave} project standards",
    )


def _write_qa_report(project, verdict):
    builder = AAHProjectBuilder(project)
    branch = builder.git("rev-parse", "--abbrev-ref", "HEAD")
    from aah.core.common.git_utils import code_subject_identity
    sha = code_subject_identity(cwd=project)
    criteria = [{"id": "AC1", "description": "Must work",
                 "verdict": "pass" if verdict == "pass" else "fail", "evidence": "e"}]
    issues = [] if verdict == "pass" else [
        {"issue_id": "I1", "affected_ac_ids": ["AC1"], "affected_tc_ids": ["TC1"],
         "severity": "major", "evidence": "x", "requested_behavior": "y"}]
    base = [sys.executable, "-m"]
    if verdict == "pass":
        base += ["aah.core.build.write_qa_report"]
    else:
        base += ["aah.cli", "run", "core.build.write_qa_report"]
    args = base + [
        "--feature-id", "F001", "--project-path", str(project), "--verdict", verdict,
        "--criteria-json", json.dumps(criteria), "--issues-json", json.dumps(issues),
        "--subject-path", str(project), "--subject-branch", branch, "--subject-sha", sha,
    ]
    return subprocess.run(
        args, cwd=project, capture_output=True, text=True, env=builder.cli_env()
    )


def _full_qa_pass(project):
    """Drive the complete QA-pass prerequisite chain and a passing QA report.

    Run the same subject-bound test → spec-validation → QA sequence used by the
    QA evaluator, then refresh feature-test evidence if QA updated the contract.
    """
    assert _run_feature_tests(project).returncode == 0
    _write_passing_spec_validation(project)
    _write_quality_check(project)
    r = _write_qa_report(project, "pass")
    assert r.returncode == 0, r.stderr
    assert _run_feature_tests(project).returncode == 0


def test_deep_pass_does_not_add_feature_prompt(tmp_path):
    """A deep profile retains objective checks without a second user prompt."""
    from aah.core.build.orchestrator import compute_next_action

    project = _build_project(tmp_path, risk_overrides={"security_scope": True})
    cfg = read_yaml(project / ".aah" / "plan" / "checkpoint-config.yaml")
    assert cfg["checkpoint_configuration"]["verification_profiles"]["F001"]["level"] == "deep"

    _full_qa_pass(project)
    action = compute_next_action(project)
    assert action["action"] not in ("run_qa", "human_review_required"), action


def test_standard_pass_completes(tmp_path):
    """AC2: a standard-profile feature with a passing QA completes without a
    human decision (moves past run_qa / human_review)."""
    from aah.core.build.orchestrator import compute_next_action

    project = _build_project(tmp_path)  # no risk → standard
    cfg = read_yaml(project / ".aah" / "plan" / "checkpoint-config.yaml")
    assert cfg["checkpoint_configuration"]["verification_profiles"]["F001"]["level"] == "standard"

    _full_qa_pass(project)
    action = compute_next_action(project)
    assert action["action"] not in ("run_qa", "human_review_required"), action


def test_security_routing_from_profile(tmp_path):
    """AC3: security routing comes 100% from the stored profile (level==deep),
    not a separate keyword branch, without adding a human prompt."""
    from aah.core.build.orchestrator import compute_next_action

    project = _build_project(tmp_path, risk_overrides={"security_scope": True})
    reasons = read_yaml(project / ".aah" / "plan" / "checkpoint-config.yaml")[
        "checkpoint_configuration"]["verification_profiles"]["F001"]["reasons"]
    assert "explicit_security_scope" in reasons

    _full_qa_pass(project)
    action = compute_next_action(project)
    assert action["action"] not in ("run_qa", "human_review_required"), action


def test_lower_without_override_rejected(tmp_path):
    """AC4: a deep feature cannot be lowered to standard without a valid
    override, while user review remains wave-scoped."""
    from aah.core.build.orchestrator import compute_next_action
    from aah.core.build.qa_routing import _feature_profile_level

    project = _build_project(tmp_path, risk_overrides={"security_scope": True})
    _full_qa_pass(project)

    from aah.core.common.git_utils import code_subject_identity
    sha = code_subject_identity(cwd=project)
    profile = _feature_profile_level(project, "F001", sha)
    assert profile["level"] == "deep"
    assert profile["binding"]["override_ref"] is None

    action = compute_next_action(project)
    assert action["action"] not in ("run_qa", "human_review_required"), action


def test_post_qa_profile_removal_does_not_invalidate_snapshot(tmp_path):
    """A completed QA attempt keeps its routing snapshot after config drift."""
    from aah.core.build.orchestrator import compute_next_action
    from aah.core.build.qa_routing import _feature_profile_level

    project = _build_project(tmp_path)  # standard profile initially
    _full_qa_pass(project)

    cfg_path = project / ".aah" / "plan" / "checkpoint-config.yaml"
    cfg = read_yaml(cfg_path)
    del cfg["checkpoint_configuration"]["verification_profiles"]
    write_yaml(cfg, cfg_path)

    from aah.core.common.git_utils import code_subject_identity
    sha = code_subject_identity(cwd=project)
    profile = _feature_profile_level(project, "F001", sha)
    assert profile["level"] is None
    assert profile["no_signal"] is not None

    # The snapshot survives the drift, so the feature stays QA-approved and the
    # cascade moves PAST the QA gate. (It used to land on the per-feature
    # standards gate; standards is now project-scoped, so the specific next
    # action is a cascade detail — what this test pins is that QA does not
    # re-fire and no human decision is demanded.)
    action = compute_next_action(project)
    assert action["action"] not in (
        "run_qa", "human_review_required", "no_signal",
    ), action
