"""Security-state migration and AAH command compatibility tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, write_json
from aah.core.gates.validate_security_scan import validate_security_scan
from aah.core.security import policy, state, suppressions
from tests.support.aah_project import AAHProjectBuilder


LEGACY_FILES = {
    "policy.yaml": "scan_types:\n  sast: false\n",
    "suppressions.yaml": "suppressions: []\n",
    "semgrep-rules/custom.yaml": "rules: []\n",
    "nuclei-templates/custom.yaml": "id: custom\n",
    "raw/tool.json": "{}\n",
    "scan-results/history.json": "{}\n",
    "scan-results-latest.json": "{}\n",
    "reports/security-scan-report.md": "# report\n",
    "sbom/sbom-cyclonedx.json": "{}\n",
}


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _security_scan_project(tmp_path: Path) -> Path:
    builder = AAHProjectBuilder.create(tmp_path, git=False).secret().manifest(
        project_name="p",
        project_type="greenfield",
        current_phase="plan",
        complexity_tier="trivial",
        stack_choices={},
    )
    builder.git("init", "-q")
    builder.git("config", "user.email", "t@example.com")
    builder.git("config", "user.name", "t")
    builder.file(".gitignore", ".aah/build/\n__pycache__/\n")
    builder.git("add", "-A")
    builder.git("commit", "-q", "-m", "init")
    return builder.path


def test_migrates_complete_legacy_tree_and_is_idempotent(tmp_path):
    legacy = tmp_path / ".rapids" / "security"
    _write_tree(legacy, LEGACY_FILES)

    first = state.ensure_security_state(tmp_path)
    for relative, content in LEGACY_FILES.items():
        assert (first.security_dir / relative).read_text(encoding="utf-8") == content
    assert set(first.copied) == set(LEGACY_FILES)
    assert len(first.seeded) == 4

    second = state.ensure_security_state(tmp_path)
    assert second.copied == ()
    assert second.seeded == ()
    assert second.conflicts == ()


def test_aah_wins_conflicts_and_bundled_rules_do_not_clobber(tmp_path):
    legacy = tmp_path / ".rapids" / "security"
    _write_tree(legacy, {"policy.yaml": "legacy: true\n"})
    security = tmp_path / ".aah" / "security"
    _write_tree(
        security,
        {
            "policy.yaml": "aah: true\n",
            "semgrep-rules/sql-injection.yaml": "custom-project-rule\n",
        },
    )

    result = state.ensure_security_state(tmp_path)

    assert (security / "policy.yaml").read_text(encoding="utf-8") == "aah: true\n"
    assert (security / "semgrep-rules/sql-injection.yaml").read_text(
        encoding="utf-8"
    ) == "custom-project-rule\n"
    assert "legacy:policy.yaml" in result.conflicts
    assert "bundled:semgrep-rules/sql-injection.yaml" in result.conflicts


def test_symlinks_are_never_followed(tmp_path):
    legacy = tmp_path / ".rapids" / "security"
    legacy.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret\n", encoding="utf-8")
    link = legacy / "linked.yaml"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(state.SecurityStateMigrationError, match="legacy:linked.yaml"):
        state.ensure_security_state(tmp_path)
    assert not (tmp_path / ".aah" / "security" / "linked.yaml").exists()


def test_copy_error_fails_closed(tmp_path, monkeypatch):
    legacy = tmp_path / ".rapids" / "security"
    _write_tree(legacy, {"policy.yaml": "legacy: true\n"})

    def fail_copy(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(state.shutil, "copy2", fail_copy)
    with pytest.raises(state.SecurityStateMigrationError, match="denied"):
        state.ensure_security_state(tmp_path)


def test_missing_bundled_rules_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "resolve_framework_root", lambda: tmp_path / "missing")
    with pytest.raises(state.SecurityStateMigrationError, match="bundled security rules"):
        state.ensure_security_state(tmp_path)


def test_gate_lazily_migrates_latest_evidence(tmp_path):
    legacy_results = tmp_path / ".rapids" / "security" / "scan-results-latest.json"
    legacy_results.parent.mkdir(parents=True)
    write_json(
        {
            "scan_timestamp": datetime.now(timezone.utc).isoformat(),
            "policy_result": {"passed": True, "max_age_hours": 24},
        },
        legacy_results,
    )

    passed, issues = validate_security_scan(tmp_path)

    assert passed is True
    assert issues == []
    assert (tmp_path / ".aah" / "security" / "scan-results-latest.json").exists()


def test_pre_1_0_policy_and_suppression_keyword_api_remains_compatible(tmp_path):
    legacy_security = tmp_path / ".rapids" / "security"
    _write_tree(
        legacy_security,
        {
            "policy.yaml": "max_age_hours: 72\n",
            "suppressions.yaml": "suppressions:\n  - finding_id: test:rule\n",
        },
    )

    loaded_policy = policy.load_policy(rapids_path=tmp_path / ".rapids")
    loaded_suppressions = suppressions.load_suppressions(
        rapids_path=tmp_path / ".rapids"
    )

    assert loaded_policy["max_age_hours"] == 72
    assert loaded_suppressions == [{"finding_id": "test:rule"}]


def test_validate_patterns_cli_uses_aah_specs(tmp_path):
    aah = tmp_path / ".aah"
    (aah / "plan" / "specs").mkdir(parents=True)
    (aah / "manifest.yaml").write_text("project_name: test\n", encoding="utf-8")
    (aah / "plan" / "specs" / "SPEC-001.md").write_text(
        "# Safe specification\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aah.core.security.pre_check",
            "validate-patterns",
            "--project-path",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "No security pattern issues found" in result.stdout
    assert ".rapids" not in result.stderr


@pytest.mark.parametrize(
    ("canonical", "legacy"),
    [
        ("aah-security-scan", "rapids-security-scan"),
        ("aah-security-fix", "rapids-security-fix"),
        ("aah-dast-scan", "rapids-dast-scan"),
        ("aah-sbom-generate", "rapids-sbom-generate"),
    ],
)
def test_aah_skill_is_canonical_and_legacy_skill_is_removed(canonical, legacy):
    repo = Path(__file__).resolve().parents[1]
    canonical_text = (repo / "aah" / "skills" / canonical / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert f"name: {canonical}" in canonical_text
    assert ".rapids" not in canonical_text
    assert not (repo / "aah" / "skills" / legacy).exists()


def test_suppression_governance_preserved(tmp_path):
    """Security suppressions remain visible after the .aah migration."""
    from aah.core.security.run_security_scan import run_scan

    if not shutil.which("gitleaks"):
        pytest.skip("gitleaks not installed")

    project = _security_scan_project(tmp_path)
    (project / "config.py").write_text(
        'AWS_SECRET = "AKIAIOSFODNN7EXAMPLE"\n'
        'password = "hunter2hunter2hunter2"\n',
        encoding="utf-8",
    )
    aah = project / ".aah"
    suppressions.save_suppressions(
        aah,
        [
            {
                "finding_id": "gitleaks:aws-access-token",
                "cwe": "CWE-798",
                "file_pattern": "*.py",
            }
        ],
    )

    result = run_scan(project, ["secrets"])

    assert (aah / "security" / "suppressions.yaml").exists()
    assert "suppressed" in result["summary"]
    assert not (project / ".rapids").exists()
    assert (aah / "security" / "scan-results-latest.json").exists()


def test_scanner_aah_path_round_trip(tmp_path):
    """Security scanners and gates use only the .aah state tree."""
    from aah.core.gates.security_scan_warning import check_security_status
    from aah.core.security.run_security_scan import run_scan

    if not shutil.which("gitleaks"):
        pytest.skip("gitleaks not installed")

    project = _security_scan_project(tmp_path)
    (project / "clean.py").write_text("x = 1\n", encoding="utf-8")

    result = run_scan(project, ["secrets"])
    aah = project / ".aah"

    assert (aah / "security" / "scan-results-latest.json").exists()
    assert (aah / "security" / "reports").is_dir()
    assert not (project / ".rapids").exists()
    passed, _issues = validate_security_scan(project)
    assert isinstance(passed, bool)
    assert isinstance(check_security_status(project), list)
    assert read_json(aah / "security" / "scan-results-latest.json")[
        "summary"
    ] == result["summary"]
