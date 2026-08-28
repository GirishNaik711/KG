"""Functional subject-binding coverage.

NO MOCKS: every case uses a real Git repository, two real feature branches,
two registered worktrees, the real CLI writers, and real pytest subprocesses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.git_utils import code_subject_identity, is_clean_ignoring
from tests.support.aah_project import AAHProjectBuilder, REPO_ROOT


@pytest.fixture
def subject_project(tmp_path: Path) -> dict:
    builder = AAHProjectBuilder.create(tmp_path)
    project = builder.path
    builder.file(".gitignore", ".aah/build/\n.claude/\n").manifest(
        project_name="subject-test",
    )
    for feature_id in ("F001", "F002"):
        test_rel = f"subject_tests/test_{feature_id}.py"
        builder.feature(
            feature_id,
            frontmatter={
                "spec_ref": "SPEC-001",
                "description": f"subject evidence for {feature_id}",
                "dependencies": [],
                "status": "pending",
                "test_config": {
                    "command": f"{sys.executable} -m pytest {test_rel} -s"
                },
                "acceptance_criteria": [
                    {"id": "AC1", "description": "runs in the requested checkout"}
                ],
                "test_cases": [
                    {"id": "TC1", "covers": ["AC1"], "description": "branch marker"}
                ],
                "knowledge_used": {"knowledge_folder": None},
            },
        ).file(
            test_rel,
            "import os\n"
            "from pathlib import Path\n\n"
            f"def test_{feature_id}_TC1_branch_marker():\n"
            "    marker = Path('branch-marker.txt').read_text().strip()\n"
            "    print(f'BRANCH_MARKER={marker}')\n"
            "    if os.environ.get('BRANCH_EXPECTED'):\n"
            "        assert os.environ['BRANCH_EXPECTED'] == 'F001 quoted value'\n"
            f"    assert marker == '{feature_id}'\n",
        )
    builder.file(
        "subject_tests/mutate.py",
        "from pathlib import Path\n"
        "Path('unexpected_source.py').write_text('changed = True\\n')\n"
        "print('mutation command completed')\n",
    ).file("branch-marker.txt", "develop\n").commit(
        "test: seed subject evidence project",
        (".gitignore", ".aah", "subject_tests", "branch-marker.txt"),
    )

    worktrees: dict[str, Path] = {}
    shas: dict[str, str] = {}
    for feature_id in ("F001", "F002"):
        worktree = project / ".claude" / "worktrees" / feature_id
        builder.git(
            "worktree",
            "add",
            str(worktree),
            "-b",
            f"feature/{feature_id}",
            "develop",
        )
        (worktree / "branch-marker.txt").write_text(f"{feature_id}\n", encoding="utf-8")
        worktree_builder = AAHProjectBuilder(worktree)
        worktree_builder.git("add", "branch-marker.txt")
        worktree_builder.git("commit", "-m", f"feat({feature_id}): set branch marker")
        worktrees[feature_id] = worktree
        # Subject identity mirrors the system: .aah/.claude-excluding content
        # hash under freshness v2, matching what run_feature_tests stamps.
        shas[feature_id] = code_subject_identity(cwd=worktree)

    builder.secret()
    return {"builder": builder, "project": project, "worktrees": worktrees, "shas": shas}


def _run_feature(
    state: dict,
    feature_id: str,
    *,
    subject_id: str | None = None,
    actor: str = "qa",
    attempt_id: str = "attempt-001",
) -> subprocess.CompletedProcess:
    project = state["project"]
    subject_id = subject_id or feature_id
    subject = state["worktrees"][subject_id]
    return state["builder"].run_module(
        "aah.cli",
        "run", "core.build.run_feature_tests",
        "--feature-id", feature_id,
        "--project-path", str(project),
        "--subject-path", str(subject),
        "--subject-branch", f"feature/{subject_id}",
        "--subject-sha", state["shas"][subject_id],
        "--actor", actor,
        "--attempt-id", attempt_id,
    )


def _run_validation(
    state: dict,
    feature_id: str,
    *,
    subject_id: str | None = None,
    expected_branch: str | None = None,
) -> subprocess.CompletedProcess:
    subject_id = subject_id or feature_id
    return state["builder"].run_module(
        "aah.cli",
        "run", "core.build.validate_implementation", "validate",
        "--feature-id", feature_id,
        "--project-path", str(state["project"]),
        "--subject-path", str(state["worktrees"][subject_id]),
        "--subject-branch", expected_branch or f"feature/{subject_id}",
        "--subject-sha", state["shas"][subject_id],
    )


def _result(state: dict, feature_id: str) -> dict:
    path = state["project"] / ".aah" / "build" / "test-results" / f"{feature_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validation(state: dict, feature_id: str) -> dict:
    path = (
        state["project"]
        / ".aah"
        / "build"
        / "validation-results"
        / f"{feature_id}-spec-validation.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _feature_contract(state: dict, feature_id: str, *, root: bool = False) -> Path:
    checkout = state["project"] if root else state["worktrees"][feature_id]
    return checkout / ".aah" / "plan" / "features" / f"{feature_id}.md"


def _declared_test_command(contract: Path) -> str:
    """Read the generated command without assuming executable path casing."""
    from aah.core.common.feature_utils import parse_feature_frontmatter

    feature = parse_feature_frontmatter(contract)
    assert feature is not None
    return feature["test_config"]["command"]


def _set_declared_test_command(contract: Path, command: str | None) -> None:
    """Update a generated command through the frontmatter writer.

    YAML may fold long executable paths across physical lines, so raw text
    replacement is not a reliable way to change the semantic command.
    """
    from aah.core.common.feature_utils import (
        parse_feature_frontmatter,
        write_feature_frontmatter,
    )

    feature = parse_feature_frontmatter(contract)
    assert feature is not None
    test_config = feature.setdefault("test_config", {})
    if command is None:
        test_config.pop("command", None)
    else:
        test_config["command"] = command
    write_feature_frontmatter(feature, contract)


def test_AC1_runner_executes_subject_and_writes_project_evidence(subject_project):
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    evidence = _result(subject_project, "F001")
    assert "BRANCH_MARKER=F001" in evidence["stdout"]
    assert evidence["execution"]["cwd"] == ".claude/worktrees/F001"
    assert not (
        subject_project["worktrees"]["F001"]
        / ".aah"
        / "build"
        / "test-results"
        / "F001.json"
    ).exists()




def test_AC2_feature_and_validation_stamp_exact_branch_sha(subject_project):
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    validation = _run_validation(subject_project, "F001")
    assert validation.returncode == 0, validation.stderr
    for evidence in (_result(subject_project, "F001"), _validation(subject_project, "F001")):
        assert evidence["subject"]["branch"] == "feature/F001"
        assert evidence["subject"]["commit_sha"] == subject_project["shas"]["F001"]
        assert evidence["subject"]["clean"] is True


def test_AC3_dirty_subject_produces_no_signal(subject_project):
    subject = subject_project["worktrees"]["F001"]
    (subject / "dirty_source.py").write_text("dirty = True\n", encoding="utf-8")
    _run_feature(subject_project, "F001")
    evidence = _result(subject_project, "F001")
    assert evidence["status"] == "no_signal"
    assert evidence["passed"] is False


def test_AC4_cross_feature_and_cross_branch_evidence_is_rejected(subject_project):
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    result_dir = subject_project["project"] / ".aah" / "build" / "test-results"
    shutil.copy2(result_dir / "F001.json", result_dir / "F002.json")

    _run_validation(subject_project, "F002")
    assert _validation(subject_project, "F002")["status"] == "no_signal"

    _run_validation(
        subject_project,
        "F001",
        expected_branch="feature/F002",
    )
    assert _validation(subject_project, "F001")["status"] == "no_signal"


def test_AC5_ephemeral_outputs_pass_but_source_write_invalidates(subject_project):
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    assert _result(subject_project, "F001")["subject_unchanged"] is True
    assert AAHProjectBuilder(subject_project["worktrees"]["F001"]).git(
        "status", "--porcelain"
    ) == ""

    contract = _feature_contract(subject_project, "F001")
    old_command = _declared_test_command(contract)

    coverage_command = (
        "BRANCH_EXPECTED='F001 quoted value' "
        + old_command
        + " --cov=subject_tests --cov-report=xml --cov-report=html:custom-htmlcov"
        + " 2>&1 && printf 'operator-ok\\n' > $AAH_TEST_LOG_DIR/operator.log"
    )
    _set_declared_test_command(contract, coverage_command)
    covered = _run_feature(subject_project, "F001")
    assert covered.returncode == 0, covered.stderr
    assert _result(subject_project, "F001")["status"] == "pass"
    executed_command = _result(subject_project, "F001")["command"]
    assert "2>&1" in executed_command
    assert "&& printf" in executed_command
    assert executed_command.index("2>&1") < executed_command.index("&& printf")
    assert _result(subject_project, "F001")["summary"]["total"] == 1
    subject = subject_project["worktrees"]["F001"]
    assert not (subject / "coverage.xml").exists()
    assert not (subject / "htmlcov").exists()
    assert not (subject / "custom-htmlcov").exists()
    assert is_clean_ignoring(cwd=subject)

    new_command = f"{sys.executable} subject_tests/mutate.py"
    _set_declared_test_command(contract, new_command)

    _run_feature(subject_project, "F001")
    evidence = _result(subject_project, "F001")
    assert evidence["status"] == "no_signal"
    assert evidence["subject_unchanged"] is False
    assert "unexpected_source.py" in evidence["error"]


def test_AC6_latest_filenames_remain_readable(subject_project):
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    validated = _run_validation(subject_project, "F001")
    assert validated.returncode == 0, validated.stderr

    feature_result = _result(subject_project, "F001")
    spec_result = _validation(subject_project, "F001")
    assert feature_result["feature_id"] == "F001"
    assert feature_result["passed"] is True
    assert feature_result["summary"]["passed"] == 1
    assert spec_result["feature_id"] == "F001"
    assert spec_result["passed"] is True
    assert spec_result["criteria_results"]
    for evidence in (feature_result, spec_result):
        assert {"subject", "inputs", "execution", "status", "artifacts"} <= evidence.keys()


def test_boundary_early_failures_overwrite_latest_with_v2(subject_project):
    first = _run_feature(subject_project, "F001")
    assert first.returncode == 0, first.stderr
    passing = _result(subject_project, "F001")
    passing_signature = passing["attestation"]["signature"]

    _run_feature(subject_project, "F999", subject_id="F001")
    missing = _result(subject_project, "F999")
    assert missing["status"] == "no_signal"
    assert missing["passed"] is False
    assert missing["attestation"]["signature"]
    assert {"subject", "inputs", "execution", "artifacts"} <= missing.keys()

    contract = _feature_contract(subject_project, "F001")
    original = contract.read_text(encoding="utf-8")

    _set_declared_test_command(contract, None)
    _run_feature(subject_project, "F001")
    no_command = _result(subject_project, "F001")
    assert no_command["status"] == "no_signal"
    assert no_command["attestation"]["signature"] != passing_signature

    contract.write_text("not frontmatter\n", encoding="utf-8")
    _run_feature(subject_project, "F001")
    unparseable = _result(subject_project, "F001")
    assert unparseable["status"] == "no_signal"
    assert unparseable["attestation"]["signature"] != no_command["attestation"]["signature"]

    malformed_command = f"{sys.executable} -m pytest 'unterminated"
    contract.write_text(original, encoding="utf-8")
    _set_declared_test_command(contract, malformed_command)
    _run_feature(subject_project, "F001")
    malformed = _result(subject_project, "F001")
    assert malformed["status"] == "no_signal"
    assert malformed["passed"] is False
    assert "Unable to parse declared test command" in malformed["error"]
    assert malformed["attestation"]["signature"] != passing_signature


def test_boundary_namespace_identity_is_safely_bounded():
    from aah.core.build.run_feature_tests import _safe_namespace_token

    unsafe = "../../qa actor/" + ("x" * 200)
    token = _safe_namespace_token(unsafe, fallback="actor")
    assert len(token) <= 40
    assert "/" not in token
    assert ".." not in token
    assert token == _safe_namespace_token(unsafe, fallback="actor")


def test_boundary_noncoverage_shell_command_is_byte_exact(tmp_path):
    from aah.core.build.run_feature_tests import _redirect_coverage_reports

    command = (
        "NAME='quoted value' python -c \"print('ok')\" "
        "2>&1 && printf 'done\\n' > \"$AAH_TEST_LOG_DIR/result.log\""
    )
    assert _redirect_coverage_reports(command, tmp_path) == command


def test_boundary_validator_rejects_malformed_latest_before_consumption(subject_project):
    assert _run_feature(subject_project, "F001").returncode == 0
    assert _run_validation(subject_project, "F001").returncode == 0
    passing = _validation(subject_project, "F001")
    passing_signature = passing["attestation"]["signature"]

    test_result_path = (
        subject_project["project"] / ".aah" / "build" / "test-results" / "F001.json"
    )
    test_result_path.write_text(
        json.dumps({
            "feature_id": "F001",
            "passed": True,
            "summary": {"total": {"malformed": True}},
            "test_cases": "not-a-list",
        }),
        encoding="utf-8",
    )
    _run_validation(subject_project, "F001")
    rejected = _validation(subject_project, "F001")
    assert rejected["status"] == "no_signal"
    assert rejected["passed"] is False
    assert rejected["attestation"]["signature"] != passing_signature
    assert rejected["details"]["subject_evidence"]["reason"].startswith(
        "Feature-test attestation is invalid"
    )


def test_boundary_validator_early_failures_are_signed_v2(subject_project):
    _run_validation(subject_project, "F999", subject_id="F001")
    missing = _validation(subject_project, "F999")
    assert missing["status"] == "no_signal"
    assert missing["passed"] is False
    assert missing["attestation"]["signature"]
    assert {"subject", "inputs", "execution", "artifacts"} <= missing.keys()

    assert _run_feature(subject_project, "F001").returncode == 0
    assert _run_validation(subject_project, "F001").returncode == 0
    passing_signature = _validation(subject_project, "F001")["attestation"]["signature"]
    contract = _feature_contract(subject_project, "F001")
    contract.write_text("not frontmatter\n", encoding="utf-8")
    _run_validation(subject_project, "F001")
    unparseable = _validation(subject_project, "F001")
    assert unparseable["status"] == "no_signal"
    assert unparseable["passed"] is False
    assert unparseable["attestation"]["signature"] != passing_signature
    assert {"subject", "inputs", "execution", "artifacts"} <= unparseable.keys()


def test_finding1_pytest_not_first_segment(subject_project):
    """JUnit produced via PYTEST_ADDOPTS even when pytest isn't first shell segment."""
    contract = _feature_contract(subject_project, "F001")
    old_command = _declared_test_command(contract)
    new_command = f"cd . && {old_command}"
    _set_declared_test_command(contract, new_command)
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    evidence = _result(subject_project, "F001")
    assert evidence["status"] == "pass"
    assert evidence["summary"]["total"] == 1
    assert evidence["command"].startswith("cd .")


def test_finding1_env_assignment_quoted_pipe(subject_project):
    """Env assignment + quoted value + redirection + pipe still passes."""
    contract = _feature_contract(subject_project, "F001")
    old_command = _declared_test_command(contract)
    new_command = f"FOO='a b' {old_command} 2>&1 | cat"
    _set_declared_test_command(contract, new_command)
    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr
    evidence = _result(subject_project, "F001")
    assert evidence["status"] == "pass"
    assert evidence["summary"]["total"] == 1


def test_finding2_config_addopts_coverage_into_subject_yields_no_signal(subject_project):
    """Config-driven coverage into subject fails closed via filesystem scan."""
    subject = subject_project["worktrees"]["F001"]
    (subject / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = \"--cov=subject_tests --cov-report=xml\"\n",
        encoding="utf-8",
    )
    AAHProjectBuilder(subject).git("add", "pyproject.toml")
    AAHProjectBuilder(subject).git("commit", "-m", "test: add config coverage")
    subject_project["shas"]["F001"] = code_subject_identity(cwd=subject)

    contract = _feature_contract(subject_project, "F001")
    content = contract.read_text(encoding="utf-8")
    old_command = f"{sys.executable} -m pytest subject_tests/test_F001.py -s"
    plain_command = f"{sys.executable} -m pytest subject_tests/test_F001.py -s"
    contract.write_text(content.replace(old_command, plain_command), encoding="utf-8")

    _run_feature(subject_project, "F001")
    evidence = _result(subject_project, "F001")
    assert evidence["status"] == "no_signal"
    assert evidence["passed"] is False
    assert "coverage" in evidence["error"].lower() or "artifact" in evidence["error"].lower()


def test_finding2_gitignored_coverage_xml_still_fails_closed(subject_project):
    """Filesystem scan catches coverage.xml even if .gitignore hides it from git status."""
    subject = subject_project["worktrees"]["F001"]
    (subject / ".gitignore").write_text("coverage.xml\n.coverage\n", encoding="utf-8")
    (subject / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = \"--cov=subject_tests --cov-report=xml\"\n",
        encoding="utf-8",
    )
    AAHProjectBuilder(subject).git("add", ".gitignore", "pyproject.toml")
    AAHProjectBuilder(subject).git("commit", "-m", "test: gitignore coverage")
    subject_project["shas"]["F001"] = code_subject_identity(cwd=subject)

    contract = _feature_contract(subject_project, "F001")
    content = contract.read_text(encoding="utf-8")
    old_command = f"{sys.executable} -m pytest subject_tests/test_F001.py -s"
    plain_command = f"{sys.executable} -m pytest subject_tests/test_F001.py -s"
    contract.write_text(content.replace(old_command, plain_command), encoding="utf-8")

    _run_feature(subject_project, "F001")
    evidence = _result(subject_project, "F001")
    assert evidence["status"] == "no_signal"
    assert evidence["passed"] is False


def test_AC1_qa_report_stamps_worktree_subject(subject_project):
    """AC1: QA report captures subject descriptor when run in worktree.

    A non-passing verdict does not need the Tier 1 pass precondition because
    this case tests subject capture, not final QA approval."""
    project = subject_project["project"]
    worktree = subject_project["worktrees"]["F001"]

    completed = _run_feature(subject_project, "F001")
    assert completed.returncode == 0, completed.stderr

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aah.cli",
            "run",
            "core.build.write_qa_report",
            "--feature-id",
            "F001",
            "--verdict",
            "rework_required",  # non-pass verdict bypasses the Tier 1 gate (`fail` removed)
            "--criteria-json",
            '[{"id":"AC1","verdict":"fail","description":"test","evidence":"testing subject capture"}]',
            "--project-path",
            str(project),
            "--subject-path",
            str(worktree),
            "--subject-branch",
            "feature/F001",
            "--subject-sha",
            subject_project["shas"]["F001"],
        ],
        cwd=REPO_ROOT,
        env=subject_project["builder"].cli_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"QA report failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    assert result.returncode == 0, result.stderr

    qa_path = project / ".aah" / "build" / "qa-results" / "F001" / "attempt-001.json"
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))

    assert "subject" in qa_data
    subject = qa_data["subject"]
    assert subject["rel_path"] == ".claude/worktrees/F001"
    assert subject["commit_sha"] == subject_project["shas"]["F001"]
    assert subject["branch"] == "feature/F001"
    assert subject["clean"] is True


def test_AC2_qa_and_feature_test_share_subject_and_command(subject_project):
    """AC2: QA report subject matches feature-test subject and test command."""
    project = subject_project["project"]
    worktree = subject_project["worktrees"]["F001"]

    _run_feature(subject_project, "F001")
    feature_data = _result(subject_project, "F001")

    contract = _feature_contract(subject_project, "F001")
    content = contract.read_text(encoding="utf-8")
    test_cmd = None
    for line in content.splitlines():
        if "command:" in line:
            test_cmd = line.split("command:")[1].strip().strip('"')
            break

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aah.cli",
            "run",
            "core.build.write_qa_report",
            "--feature-id",
            "F001",
            "--verdict",
            "rework_required",  # non-pass verdict bypasses the Tier 1 gate (`fail` removed)
            "--criteria-json",
            '[{"id":"AC1","verdict":"fail","description":"test","evidence":"testing"}]',
            "--test-command",
            test_cmd,
            "--project-path",
            str(project),
            "--subject-path",
            str(worktree),
            "--subject-branch",
            "feature/F001",
            "--subject-sha",
            subject_project["shas"]["F001"],
        ],
        cwd=REPO_ROOT,
        env=subject_project["builder"].cli_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    qa_path = project / ".aah" / "build" / "qa-results" / "F001" / "attempt-001.json"
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))

    assert qa_data["subject"]["commit_sha"] == feature_data["subject"]["commit_sha"]
    assert qa_data["subject"]["branch"] == feature_data["subject"]["branch"]
    assert qa_data["test_command"] == test_cmd


def test_AC3_read_fresh_evidence_rejects_stale_evidence(subject_project):
    """AC3: fresh evidence is rejected when subject/contract/tests mutate."""
    from aah.core.build.verify import FEATURE_TEST_PREFIX, read_fresh_feature_evidence

    project = subject_project["project"]
    worktree = subject_project["worktrees"]["F001"]

    _run_feature(subject_project, "F001")
    result_path = project / ".aah" / "build" / "test-results" / "F001.json"

    evidence = read_fresh_feature_evidence(
        result_path,
        project,
        FEATURE_TEST_PREFIX,
        subject_path=worktree,
        feature_id="F001",
        expected_branch="feature/F001",
        expected_sha=subject_project["shas"]["F001"],
    )
    assert evidence is not None
    assert evidence["passed"] is True

    (worktree / "new_source.py").write_text("new = True\n", encoding="utf-8")
    evidence = read_fresh_feature_evidence(
        result_path,
        project,
        FEATURE_TEST_PREFIX,
        subject_path=worktree,
        feature_id="F001",
        expected_branch="feature/F001",
        expected_sha=subject_project["shas"]["F001"],
    )
    assert evidence is None

    (worktree / "new_source.py").unlink()

    test_file = worktree / "subject_tests" / "test_F001.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8") + "\n# comment\n",
        encoding="utf-8",
    )
    evidence = read_fresh_feature_evidence(
        result_path,
        project,
        FEATURE_TEST_PREFIX,
        subject_path=worktree,
        feature_id="F001",
        expected_branch="feature/F001",
        expected_sha=subject_project["shas"]["F001"],
    )
    assert evidence is None

    AAHProjectBuilder(worktree).git("checkout", "subject_tests/test_F001.py")

    contract = _feature_contract(subject_project, "F001")
    content = contract.read_text(encoding="utf-8")
    contract.write_text(content + "\n# modified contract\n", encoding="utf-8")
    evidence = read_fresh_feature_evidence(
        result_path,
        project,
        FEATURE_TEST_PREFIX,
        subject_path=worktree,
        feature_id="F001",
        expected_branch="feature/F001",
        expected_sha=subject_project["shas"]["F001"],
    )
    assert evidence is None


def test_AC4_regression_stamps_integration_branch_subject(subject_project):
    """AC4: Regression result includes integration/wave-N subject."""
    project = subject_project["project"]

    project_builder = subject_project["builder"]
    project_builder.git("checkout", "-b", "integration/wave-0", "develop")
    project_builder.git("commit", "--allow-empty", "-m", "test: integration commit")
    expected_sha = code_subject_identity(cwd=project)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "aah.cli",
            "run",
            "core.build.run_regression_suite",
            "--project-path",
            str(project),
            "--wave",
            "0",
        ],
        cwd=REPO_ROOT,
        env=project_builder.cli_env(),
        capture_output=True,
        text=True,
    )

    reg_path = project / ".aah" / "build" / "test-results" / "regression-latest.json"
    reg_data = json.loads(reg_path.read_text(encoding="utf-8"))

    assert "subject" in reg_data
    assert reg_data["subject"]["branch"] == "integration/wave-0"
    assert reg_data["subject"]["commit_sha"] == expected_sha

    from aah.core.build.verify import read_regression_evidence

    fresh = read_regression_evidence(project / ".aah", project, 0)
    assert fresh is not None

    (project / "regression-subject-change.txt").write_text("changed\n", encoding="utf-8")
    subject_project["builder"].git("add", "regression-subject-change.txt")
    subject_project["builder"].git("commit", "-m", "test: change regression subject")

    stale = read_regression_evidence(project / ".aah", project, 0)
    assert stale[0] is None
    assert stale[1] == "subject_sha_stale"


def test_AC6_qa_writes_attestation_to_project_not_worktree(subject_project):
    """AC6: QA report attestation written to project, NOT worktree."""
    project = subject_project["project"]
    worktree = subject_project["worktrees"]["F001"]

    _run_feature(subject_project, "F001")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aah.cli",
            "run",
            "core.build.write_qa_report",
            "--feature-id",
            "F001",
            "--verdict",
            "rework_required",  # non-pass verdict bypasses the Tier 1 gate (`fail` removed)
            "--criteria-json",
            '[{"id":"AC1","verdict":"fail","description":"test","evidence":"testing"}]',
            "--project-path",
            str(project),
            "--subject-path",
            str(worktree),
            "--subject-branch",
            "feature/F001",
            "--subject-sha",
            subject_project["shas"]["F001"],
        ],
        cwd=REPO_ROOT,
        env=subject_project["builder"].cli_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    qa_path = project / ".aah" / "build" / "qa-results" / "F001" / "attempt-001.json"
    assert qa_path.exists()
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    assert "attestation" in qa_data
    assert qa_data["attestation"]["signature"]

    assert (project / ".aah" / "build" / ".attestation-secret").exists()

    assert not (worktree / ".aah" / "build" / ".attestation-secret").exists()
    assert not (worktree / ".aah" / "build" / "test-results" / "F001-qa.json").exists()

    tracked = AAHProjectBuilder(worktree).git("ls-files")
    assert "F001-qa.json" not in tracked
