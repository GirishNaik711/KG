"""Tests for aah.core.build modules."""

from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, write_json, write_yaml
from aah.core.common.git_utils import code_subject_identity, run_git
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.build.load_impl_context import (
    build_status_summary,
    compute_recommended_action,
    load_impl_context,
)
from aah.core.build.orchestrator import compute_next_action
from aah.core.build.generate_session_summary import generate_session_summary
from aah.core.build.run_init_check import run_init_check
from aah.core.build.update_impl_state import update_impl_state


@pytest.fixture
def impl_project(git_repo):
    """Create a project set up for implementation testing."""
    import secrets as _secrets

    tmp_path = git_repo
    run_git(["branch", "develop"], cwd=tmp_path)
    aah_root = tmp_path / ".aah"
    for d in ["plan/features", "plan/sprint-contracts", "build/test-results", "brownfield"]:
        (aah_root / d).mkdir(parents=True)

    # Manifest
    manifest = get_default_manifest("test-proj")
    manifest["current_phase"] = "build"
    manifest["complexity_tier"] = "moderate"
    save_manifest(manifest, aah_root / "manifest.yaml")

    # Progress
    progress = get_default_progress()
    progress["current_phase"] = "build"
    progress["current_wave"] = 0
    progress["environment_state"] = "healthy"
    save_progress(progress, aah_root / "claude-progress.json")

    # Feature list
    write_json({
        "features": [
            {"id": "F001", "spec_ref": "S1", "description": "Auth", "dependencies": [], "passes": True},
            {"id": "F002", "spec_ref": "S1", "description": "Reg", "dependencies": [], "passes": False},
            {"id": "F003", "spec_ref": "S2", "description": "Dashboard", "dependencies": ["F001"], "passes": False},
        ]
    }, aah_root / "feature-list.json")

    # Waves
    write_json({"waves": [["F001", "F002"], ["F003"]], "total_waves": 2}, aah_root / "plan" / "waves.json")

    # Attestation secret: converted writers use write_attested,
    # which requires the project-local key to exist. SessionStart's
    # regenerate_attestation_secret hook seeds this in production; tests
    # seed it directly so subprocess writers don't crash.
    (aah_root / "build").mkdir(parents=True, exist_ok=True)
    (aah_root / "build" / ".attestation-secret").write_bytes(_secrets.token_bytes(32))

    return tmp_path


class TestLoadImplContext:
    def test_loads_context(self, impl_project):
        ctx = load_impl_context(impl_project)
        assert "progress" in ctx
        assert "feature_summary" in ctx
        assert "status_summary" in ctx
        assert "recommended_action" in ctx

    def test_feature_summary(self, impl_project):
        ctx = load_impl_context(impl_project)
        fs = ctx["feature_summary"]
        assert fs["total"] == 3
        assert fs["passing"] == 1
        assert fs["failing"] == 2

    def test_current_wave(self, impl_project):
        ctx = load_impl_context(impl_project)
        assert ctx.get("current_wave") is not None
        assert ctx["current_wave"]["index"] == 0

    def test_status_summary_text(self, impl_project):
        ctx = load_impl_context(impl_project)
        summary = ctx["status_summary"]
        assert "test-proj" in summary
        assert "build" in summary


class TestComputeRecommendedAction:
    def test_unhealthy_env(self):
        progress = {"environment_state": "unhealthy", "known_issues": [], "in_progress_features": [], "current_phase": "build"}
        action = compute_recommended_action(progress, {})
        assert "fix" in action.lower()

    def test_known_issues(self):
        progress = {"environment_state": "healthy", "known_issues": ["Test flake"], "in_progress_features": [], "current_phase": "build"}
        action = compute_recommended_action(progress, {})
        assert "issue" in action.lower()

    def test_in_progress(self):
        progress = {"environment_state": "healthy", "known_issues": [], "in_progress_features": ["F002"], "current_phase": "build"}
        action = compute_recommended_action(progress, {})
        assert "F002" in action

    def test_init_phase(self):
        progress = {"environment_state": "unknown", "known_issues": [], "in_progress_features": [], "current_phase": "init"}
        action = compute_recommended_action(progress, {})
        assert "/aah-discuss" in action.lower()


class TestRunInitCheck:
    def test_no_init_sh(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        result = run_init_check(tmp_path)
        assert result["healthy"] is True

    def test_passing_init_sh(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        init_sh = aah_root / "init.sh"
        init_sh.write_text("#!/bin/bash\necho 'OK'\nexit 0\n")
        init_sh.chmod(0o755)
        result = run_init_check(tmp_path)
        assert result["healthy"] is True

    def test_failing_init_sh_first_wave_is_ok(self, tmp_path):
        """First wave: init.sh failure is expected (infra not built yet)."""
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        init_sh = aah_root / "init.sh"
        init_sh.write_text("#!/bin/bash\necho 'FAIL' >&2\nexit 1\n")
        init_sh.chmod(0o755)
        result = run_init_check(tmp_path)
        assert result["healthy"] is True  # Tolerated on first wave
        assert result.get("first_wave") is True

    def test_failing_init_sh_after_features_is_unhealthy(self, tmp_path):
        """After features pass: init.sh failure is a real problem."""
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        init_sh = aah_root / "init.sh"
        init_sh.write_text("#!/bin/bash\necho 'FAIL' >&2\nexit 1\n")
        init_sh.chmod(0o755)
        # Create feature-list with a passing feature
        from aah.core.common.io_utils import write_json
        write_json({"features": [{"id": "F001", "passes": True}]}, aah_root / "feature-list.json")
        result = run_init_check(tmp_path)
        assert result["healthy"] is False


class TestComputeNextAction:
    def test_dispatch_sets_in_progress_features(self, impl_project):
        """After compute_next_action dispatches, progress contains dispatched feature IDs."""
        aah_root = impl_project / ".aah"

        # F001 passes, F002 pending — wave 0 should dispatch F002
        result = compute_next_action(impl_project)
        assert result["action"].startswith("dispatch_")

        # Read back progress and verify in_progress_features was set
        progress = read_json(aah_root / "claude-progress.json")
        dispatched_ids = [f["id"] for f in result.get("features", [])]
        assert progress["in_progress_features"] == dispatched_ids
        assert len(dispatched_ids) > 0

    def test_dispatch_includes_integration_branch(self, impl_project):
        """Dispatch response must include integration_branch field."""
        result = compute_next_action(impl_project)
        assert result["action"].startswith("dispatch_")
        assert "integration_branch" in result
        assert result["integration_branch"] == "integration/wave-0"

    def test_strategy_parallel_for_multiple_features(self, impl_project):
        """Strategy is 'parallel' (not 'team') when 3+ features are available."""
        aah_root = impl_project / ".aah"
        # Set all features to wave 0 with none passing
        write_json({
            "features": [
                {"id": "F001", "spec_ref": "S1", "description": "A", "dependencies": [], "passes": False},
                {"id": "F002", "spec_ref": "S1", "description": "B", "dependencies": [], "passes": False},
                {"id": "F003", "spec_ref": "S2", "description": "C", "dependencies": [], "passes": False},
            ]
        }, aah_root / "feature-list.json")
        write_json({"waves": [["F001", "F002", "F003"]], "total_waves": 1}, aah_root / "plan" / "waves.json")

        result = compute_next_action(impl_project)
        assert result["strategy"] == "parallel"
        assert result["action"] == "dispatch_parallel"


class TestGenerateSessionSummary:
    def test_session_summary_includes_feature_list_passing(self, tmp_path):
        """Feature with passed:false in test result but passes:true in feature-list.json
        appears in features_completed."""
        aah_root = tmp_path / ".aah"
        (aah_root / "build" / "test-results").mkdir(parents=True)

        # Progress with no session history
        progress = get_default_progress()
        progress["current_phase"] = "build"
        save_progress(progress, aah_root / "claude-progress.json")

        # Feature list: F001 passes=True
        write_json({
            "features": [
                {"id": "F001", "spec_ref": "S1", "description": "Auth", "dependencies": [], "passes": True},
            ]
        }, aah_root / "feature-list.json")

        # Test result: F001 passed=False (stale/inconsistent)
        write_json({
            "feature_id": "F001",
            "passed": False,
            "timestamp": "2099-01-01T00:00:00Z",
        }, aah_root / "build" / "test-results" / "F001.json")

        summary = generate_session_summary(tmp_path)
        assert "F001" in summary["features_completed"]


def _seed_tier1_pass(impl_project, feature_id="F002"):
    """Seed the inputs Tier 1 (validate_implementation) needs to report
    passed=True for ``feature_id``: a feature YAML at
    ``.aah/plan/features/{fid}.yaml`` plus an attested passing
    test-result at ``.aah/build/test-results/{fid}.json``.

    The QA workflow writes an attested Tier 1 artifact before
    write_qa_report accepts a pass. Tests that exercise the pass-verdict path
    must seed that artifact first.
    """
    import subprocess
    import sys

    from aah.core.common.feature_utils import write_feature_frontmatter
    aah_root = impl_project / ".aah"
    test_rel = f"qa_tests/test_{feature_id}.py"
    test_path = impl_project / test_rel
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        f"def test_{feature_id}_TC1_works():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    run_git(["add", test_rel], cwd=impl_project)
    run_git(["commit", "-m", f"test: add {feature_id} QA subject test"], cwd=impl_project)
    write_feature_frontmatter(
        {"id": feature_id, "description": "x", "dependencies": [],
         "test_config": {
             "command": f"{sys.executable} -m pytest {test_rel} -s"
         },
         "acceptance_criteria": [{"id": "AC1", "description": "Works"}],
         "test_cases": [{"id": "TC1", "covers": ["AC1"],
                         "description": "Works"}]},
        aah_root / "plan" / "features" / f"{feature_id}.md",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "aah.cli", "run",
            "core.build.run_feature_tests",
            "--feature-id", feature_id,
            "--project-path", str(impl_project),
            *_qa_subject_args(impl_project),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [
            sys.executable, "-m", "aah.cli", "run",
            "core.build.validate_implementation", "validate",
            "--feature-id", feature_id,
            "--project-path", str(impl_project),
            *_qa_subject_args(impl_project),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _qa_subject_args(project: Path) -> list[str]:
    branch = run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=project
    ).stdout.strip()
    return [
        "--subject-path", str(project),
        "--subject-branch", branch,
        "--subject-sha", code_subject_identity(cwd=project),
    ]


class TestWriteQaReport:
    def test_qa_pass_marks_feature_passing(self, impl_project):
        """When QA verdict is 'pass' AND Tier 1 agrees, feature-list.json
        gets passes=True and progress is updated."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"

        # Verify F002 starts as not passing
        fl_data = read_json(aah_root / "feature-list.json")
        f002 = next(f for f in fl_data["features"] if f["id"] == "F002")
        assert f002["passes"] is False

        # Pass-verdict requires Tier 1 to agree.
        _seed_tier1_pass(impl_project, "F002")

        # Run write_qa_report with verdict=pass for F002
        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "pass",
                "--criteria-json", '[{"id":"AC1","description":"Works","verdict":"pass","evidence":"tested"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"write_qa_report failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        # Verify feature-list.json now has F002 passes=True
        fl_data = read_json(aah_root / "feature-list.json")
        f002 = next(f for f in fl_data["features"] if f["id"] == "F002")
        assert f002["passes"] is True

        # Verify progress was updated
        progress = read_json(aah_root / "claude-progress.json")
        assert progress["last_completed_feature"] == "F002"

    def test_qa_fail_does_not_mark_feature(self, impl_project):
        """When QA verdict is non-passing (rework_required), feature-list.json
        stays unchanged. `fail` is no longer a valid authoritative verdict."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"

        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "rework_required",
                "--criteria-json", '[{"id":"AC1","description":"Works","verdict":"fail","evidence":"broken"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        # Feature should still be failing
        fl_data = read_json(aah_root / "feature-list.json")
        f002 = next(f for f in fl_data["features"] if f["id"] == "F002")
        assert f002["passes"] is False

    def test_qa_report_has_attestation_block(self, impl_project):
        """write_qa_report must produce an attested file
        whose attestation block verifies under the project's secret."""
        import subprocess
        import sys
        from aah.core.common import attestation

        aah_root = impl_project / ".aah"

        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "rework_required",
                "--criteria-json", '[{"id":"AC1","description":"Works","verdict":"fail","evidence":"broken"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        loaded = read_json(
            aah_root / "build" / "qa-results" / "F002" / "attempt-001.json"
        )
        assert "attestation" in loaded
        assert loaded["attestation"]["command"][:3] == [
            "aah", "run", "core.build.write_qa_report"
        ]
        ok, reason = attestation.verify(
            loaded,
            project_path=impl_project,
            expected_command_prefix=["aah", "run", "core.build.write_qa_report"],
        )
        assert ok, f"attestation verify failed: {reason}"

    # write_qa_report verifies the fresh attested Tier 1 artifact before
    # honoring --verdict pass. This closes the bypass where a
    # aah-qa-evaluator agent hand-writes a passing verdict on a feature
    # whose tests don't actually cover all acceptance criteria.

    def test_pass_verdict_refused_when_tier1_fails(self, impl_project):
        """Missing Tier 1 evidence requests revalidation and cannot pass QA."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"
        # Deliberately do NOT seed Tier 1 inputs: no feature YAML, no test result.
        # Tier 1 will return passed=False (feature YAML not found).
        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "pass",
                "--criteria-json", '[{"id":"AC1","description":"Works","verdict":"pass","evidence":"claimed"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 3, (
            f"Missing Tier 1 evidence must request revalidation; got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )
        assert "REVERIFY_REQUIRED" in result.stderr
        # feature-list.json must NOT mark F002 passing.
        fl_data = read_json(aah_root / "feature-list.json")
        f002 = next(f for f in fl_data["features"] if f["id"] == "F002")
        assert f002["passes"] is False
        # No QA file should have been written either — the refusal is total.
        attempts_path = aah_root / "build" / "qa-results" / "F002"
        assert not attempts_path.exists(), (
            "L4 hardening must refuse BEFORE writing the attested file; "
            "otherwise audit could see a contradictory pass."
        )

    def test_pass_verdict_honored_when_tier1_passes(self, impl_project):
        """Happy path: Tier 1 inputs seeded → verdict=pass succeeds and
        marks the feature passing. Equivalent to test_qa_pass_marks_feature_passing
        but stated explicitly to pin the post-hardening contract."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"
        _seed_tier1_pass(impl_project, "F002")

        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "pass",
                "--criteria-json", '[{"id":"AC1","description":"Works","verdict":"pass","evidence":"tested"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"write_qa_report failed: stderr={result.stderr!r}"
        )
        fl_data = read_json(aah_root / "feature-list.json")
        f002 = next(f for f in fl_data["features"] if f["id"] == "F002")
        assert f002["passes"] is True

    def test_fail_verdict_skips_tier1_check(self, impl_project):
        """A non-passing verdict (rework_required) must NOT gate on
        Tier 1 — there's no point. `fail` is no longer a valid verdict."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"
        # No Tier 1 inputs seeded — Tier 1 would say passed=False.
        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "rework_required",
                "--criteria-json", '[{"id":"AC1","description":"Works","verdict":"fail","evidence":"broken"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"non-passing verdict must succeed regardless of Tier 1 state; "
            f"stderr={result.stderr!r}"
        )

    def test_pass_verdict_refused_message_includes_gaps(self, impl_project):
        """Refused-message must list the specific Tier 1 gaps so the
        agent / user can act on them. Without gap text, the refusal is
        opaque and recurrent."""
        import subprocess
        import sys
        from aah.core.common.feature_utils import write_feature_frontmatter

        aah_root = impl_project / ".aah"
        test_rel = "qa_tests/test_F002.py"
        test_path = impl_project / test_rel
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            "def test_F002_TC1_circuit_breaker():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        run_git(["add", test_rel], cwd=impl_project)
        run_git(["commit", "-m", "test: add partial F002 coverage"], cwd=impl_project)
        # Seed TWO ACs but map the one collected test only to AC1. Tier 1
        # therefore has fresh subject evidence and a genuine AC2 gap.
        write_feature_frontmatter(
            {"id": "F002", "description": "x", "dependencies": [],
             "test_config": {
                 "command": f"{sys.executable} -m pytest {test_rel} -s"
             },
             "acceptance_criteria": [
                 {"id": "AC1", "description": "AC for circuit breaker"},
                 {"id": "AC2", "description": "AC for retry policy"},
             ],
             "test_cases": [
                 {"id": "TC1", "covers": ["AC1"],
                  "description": "circuit breaker"}
             ]},
            aah_root / "plan" / "features" / "F002.md",
        )
        feature_run = subprocess.run(
            [
                sys.executable, "-m", "aah.cli", "run",
                "core.build.run_feature_tests",
                "--feature-id", "F002",
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert feature_run.returncode == 0, feature_run.stderr
        validation = subprocess.run(
            [
                sys.executable, "-m", "aah.cli", "run",
                "core.build.validate_implementation", "validate",
                "--feature-id", "F002",
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert (
            aah_root / "build" / "validation-results"
            / "F002-spec-validation.json"
        ).exists(), validation.stderr

        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "pass",
                "--criteria-json", '[{"id":"AC1","description":"x","verdict":"pass"}]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        # Must name at least one criterion ID and a reason.
        assert "AC2" in result.stderr, (
            f"stderr should name a criterion ID; got {result.stderr!r}"
        )
        assert "No covering test case" in result.stderr, (
            f"stderr should explain why; got {result.stderr!r}"
        )

    def test_pass_verdict_refused_when_no_test_results(self, impl_project):
        """Tier 1 fails because tests didn't run (no test-result file).
        The refusal must still fire — partial Tier 1 evidence is not
        enough to honor a pass verdict."""
        import subprocess
        import sys
        from aah.core.common.io_utils import write_yaml

        aah_root = impl_project / ".aah"
        # Seed feature YAML but NOT a test-result file.
        write_yaml(
            {"id": "F002", "description": "x", "dependencies": [],
             "acceptance_criteria": ["AC1"],
             "test_cases": [{"id": "TC1", "covers": ["AC1"]}]},
            aah_root / "plan" / "features" / "F002.yaml",
        )
        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "pass",
                "--criteria-json", '[]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 3
        assert "REVERIFY_REQUIRED" in result.stderr

    def test_pass_verdict_refused_when_validate_implementation_unavailable(
        self, impl_project, monkeypatch,
    ):
        """Defensive: if Tier 1 itself raises (e.g., feature YAML
        unreadable, internal bug), refuse with a clear message — don't
        silently let the verdict through."""
        import subprocess
        import sys
        # Monkeypatching subprocess won't reach the child process. Instead,
        # corrupt the feature YAML so Tier 1's _load_feature_yaml raises.
        aah_root = impl_project / ".aah"
        fy_path = aah_root / "plan" / "features" / "F002.yaml"
        fy_path.parent.mkdir(parents=True, exist_ok=True)
        fy_path.write_text("not: [valid yaml: with: nested unclosed brackets")

        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.write_qa_report",
                "--feature-id", "F002",
                "--verdict", "pass",
                "--criteria-json", '[]',
                "--project-path", str(impl_project),
                *_qa_subject_args(impl_project),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 3, (
            f"Missing valid Tier 1 evidence must request revalidation; "
            f"got {result.returncode}, stderr={result.stderr!r}"
        )
        assert "REVERIFY_REQUIRED" in result.stderr


class TestRunRegressionSuite:
    """regression-latest.json must verify under REGRESSION_PREFIX so
    read_regression_evidence() accepts it. Pre-fix, the
    writer recorded the inner pytest argv as the attestation `command`,
    causing a `command_mismatch` rejection that would have re-fired
    `run_regression` indefinitely in legacy mode (AAH_VERIFY_INTERNAL=0).
    Identical bug-shape to the one on run_feature_tests.py — fix lands the
    same canonical-prefix pattern."""

    def test_regression_attestation_uses_canonical_prefix(self, impl_project):
        """The flagship regression test. After the writer runs, the attested
        file's command starts with the canonical run_regression_suite
        prefix — not the inner pytest argv."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"
        # No-op early-return path: feature-list shows nothing passing,
        # but the writer still produces an attested file. This is the
        # cheap path to exercise the canonical-prefix contract without
        # actually running tests. (The fix applies to both paths
        # uniformly — both call write_attested with the new command kwarg.)
        # impl_project's fixture seeds F001 as passes=True but with no
        # corresponding test-results file; clear feature-list passes
        # to force the no-op path.
        fl_path = aah_root / "feature-list.json"
        fl_data = read_json(fl_path)
        for f in fl_data["features"]:
            f["passes"] = False
        write_json(fl_data, fl_path)

        # --wave is required: the run must prove it happened on
        # integration/wave-N, and the branch check is the proof. This fixture is
        # NOT on that branch, so the runner refuses — and per the non-refiring
        # producer invariant it MUST still write attested evidence, which is
        # exactly what this test reads.
        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.run_regression_suite",
                "--project-path", str(impl_project), "--wave", "0",
            ],
            capture_output=True,
            text=True,
        )
        # 0 (no-op / refusal) or 2 (real regression fail). Every path writes
        # the attested file — a path that writes nothing would make the
        # orchestrator read MISSING and re-dispatch regression forever.
        assert result.returncode in (0, 2), (
            f"unexpected exit code: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        regression_path = aah_root / "build" / "test-results" / "regression-latest.json"
        assert regression_path.exists(), "writer did not produce regression-latest.json"

        d = read_json(regression_path)
        assert "attestation" in d, "regression-latest.json has no attestation block"
        prefix = d["attestation"]["command"][:3]
        expected = ["aah", "run", "core.build.run_regression_suite"]
        assert prefix == expected, (
            f"F4 regression: attestation command should start with the canonical "
            f"writer prefix {expected!r}, got {prefix!r}"
        )

    def test_regression_attested_file_passes_orchestrator_reader(self, impl_project):
        """End-to-end: read_regression_evidence must
        accept the writer's file. This is the load-bearing assertion —
        if it fails, the legacy AI-dispatched flow loops forever
        (orchestrator re-fires `run_regression` because attestation
        verification fails).

        Regression evidence is bound to the integration/wave-N HEAD, so
        read_regression_evidence takes (aah_path, project_path, current_wave) and
        the subject SHA must match the integration branch HEAD. This test now
        stands up a real integration/wave-0 checkout and runs the writer with
        --wave 0 so the reader has a real subject to verify against."""
        import subprocess
        import sys

        aah_root = impl_project / ".aah"
        # Same no-op setup as above.
        fl_path = aah_root / "feature-list.json"
        fl_data = read_json(fl_path)
        for f in fl_data["features"]:
            f["passes"] = False
        write_json(fl_data, fl_path)

        # Subject binding: the reader compares the regression evidence's
        # subject.commit_sha to the integration/wave-0 HEAD, so we need a real
        # git repo on that branch.
        def _git(*args):
            subprocess.run(["git", *args], cwd=impl_project, check=True,
                           capture_output=True, text=True)
        _git("init", "-b", "develop")
        _git("config", "user.email", "test@example.com")
        _git("config", "user.name", "Test")
        _git("add", "-A")
        _git("commit", "-m", "init")
        _git("checkout", "-b", "integration/wave-0")
        _git("commit", "--allow-empty", "-m", "integration commit")

        result = subprocess.run(
            [
                sys.executable, "-m", "aah.core.build.run_regression_suite",
                "--project-path", str(impl_project),
                "--wave", "0",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 2)

        # The load-bearing check: the canonical reader accepts it (3-arg
        # signature, bound to integration/wave-0 HEAD).
        from aah.core.build.verify import read_regression_evidence
        reader_result, refuse_reason = read_regression_evidence(
            aah_root, impl_project, 0
        )
        assert reader_result is not None, (
            "F4 regression: read_regression_evidence rejected the writer's "
            "attested file (likely command_mismatch or subject mismatch). In legacy "
            f"mode this would re-fire run_regression indefinitely. reason={refuse_reason!r}"
        )

    def test_no_passing_features_returns_no_signal_not_green(self, impl_project):
        """With ZERO passing features to regress, the result must be
        a DISTINCT `no_signal` status and must NOT read as a satisfied green
        gate (passed True). A vacuously-green regression must never gate a
        merge; but no_signal must also NOT hard-FAIL a legitimate early
        wave-0. Calls the real function against a real project — no mocks."""
        from aah.core.build.run_regression_suite import run_regression_suite

        rapids = impl_project / ".aah"
        # Force the no-passing-features path: clear feature-list passes and
        # ensure there are no passing test-results files.
        fl_path = rapids / "feature-list.json"
        fl_data = read_json(fl_path)
        for f in fl_data["features"]:
            f["passes"] = False
        write_json(fl_data, fl_path)
        for leftover in (rapids / "build" / "test-results").glob("F*.json"):
            leftover.unlink()

        result = run_regression_suite(impl_project, wave=None)

        assert result.get("status") == "no_signal", (
            f"no-feature path must emit status='no_signal', got {result.get('status')!r}"
        )
        # The core protection: it must NOT read as a satisfied gate.
        assert result.get("passed") is not True, (
            "no_signal must NOT set passed=True — a vacuously-green regression "
            "would otherwise gate a merge"
        )


class TestQualityChecks:
    """Fail-closed quality gates.

    All functional, no mocks: tests either drive the real code path against
    a real tmp project (npm is present, so Node's `npm run lint` script is a
    controllable real linter) or feed REAL captured tool output to the pure
    parser/classifier functions.
    """

    # ── 1.1 Missing tool → block ──────────────────────────────────────

    def _node_proj(self, tmp_path, lint_script):
        (tmp_path / "package.json").write_text(
            '{"name":"t","version":"1.0.0","scripts":{"lint":"%s"}}' % lint_script
        )
        return tmp_path

    def test_missing_tool_blocks(self, tmp_path):
        """AC1: an applicable linter that is not installed → block, not pass.

        The lint script invokes a binary that does not exist; the real shell
        emits a 'not found' marker → _tool_missing → block.
        """
        from aah.core.build.quality_checks import run_linting

        proj = self._node_proj(tmp_path, "this-binary-does-not-exist-xyz .")
        result = run_linting(proj, "F001")
        assert result["passed"] is False
        assert result["details"].get("blocking_reason") == "tool_not_installed"
        assert "not installed" in result["details"]["message"]

    def test_na_stack_skips_not_blocks(self, tmp_path):
        """AC2: a stack with no coverage tool (adapter returns None) → skip
        clean, does not block. Node with no test:coverage script → None."""
        from aah.core.build.quality_checks import run_coverage_check

        proj = self._node_proj(tmp_path, "echo ok")  # has lint, NOT test:coverage
        result = run_coverage_check(proj, "F001")
        assert result["passed"] is True
        assert result["details"].get("skip_reason") == "no_coverage_tool"
        assert result["details"].get("blocking_reason") is None

    def test_violation_routes_to_severity_logic_not_missing_branch(self, tmp_path):
        """AC3: a tool that RUNS and reports a violation (exit 1, clean
        stderr) goes through the existing severity logic — the tool-missing
        branch does not swallow it."""
        from aah.core.build.quality_checks import run_linting

        # Real linter that exits 1 with a parseable ERROR line on stdout.
        proj = self._node_proj(tmp_path, "echo file.js:1:1: error E100; exit 1")
        result = run_linting(proj, "F001")
        # NOT classified as tool-missing…
        assert result["details"].get("blocking_reason") != "tool_not_installed"
        # …and the violation was parsed by the severity path.
        assert result["violations"], "expected parsed violations from severity logic"

    def test_polyglot_limitation_logged(self, tmp_path, capsys):
        """AC4: a repo with BOTH python and node fingerprints emits the
        single-stack limitation line and gates exactly one stack."""
        from aah.core.build.quality_checks import _log_polyglot_limitation
        from aah.core.build.lang_checks import detect

        (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
        (tmp_path / "package.json").write_text('{"name":"t","version":"1.0.0"}')
        gated = detect(tmp_path).name
        _log_polyglot_limitation(tmp_path, gated)
        err = capsys.readouterr().err
        assert "polyglot" in err.lower()
        assert gated in err

    # ── 1.2 Unparseable coverage → block (harden parser first) ────────

    def test_coverage_parser_shapes(self):
        """AC1 (precondition): parser extracts the percentage from real
        pytest / jest-istanbul / vitest / c8 / go-cover output shapes."""
        from aah.core.build.quality_checks import _extract_coverage_percentage

        pytest_out = "Name    Stmts   Miss  Cover\nTOTAL     100     15   85%\n"
        # istanbul table shared by jest / vitest / c8:
        istanbul = (
            "----------|---------|----------|---------|---------|\n"
            "File      | % Stmts | % Branch | % Funcs | % Lines |\n"
            "----------|---------|----------|---------|---------|\n"
            "All files |   85.7  |    72    |   90    |  85.7   |\n"
        )
        go_out = "ok  example/pkg  0.012s  coverage: 83.4% of statements\n"
        assert _extract_coverage_percentage(pytest_out) == 85.0
        assert _extract_coverage_percentage(istanbul) == 85.7
        assert _extract_coverage_percentage(go_out) == 83.4

    def test_unparseable_coverage_blocks(self, tmp_path):
        """AC2: coverage output matching no known shape → block, not pass.

        Node with a test:coverage script that prints an unparseable line.
        """
        from aah.core.build.quality_checks import run_coverage_check

        (tmp_path / "package.json").write_text(
            '{"name":"t","version":"1.0.0",'
            '"scripts":{"test:coverage":"echo build succeeded"}}'
        )
        result = run_coverage_check(tmp_path, "F001")
        assert result["passed"] is False
        assert result["details"].get("blocking_reason") == "coverage_unparseable"

    # ── 1.3 Security high → block (fix classifier first) ──────────────

    def test_sa_json_preserves_high_severity(self):
        """AC1 (precondition, JSON path): a real bandit JSON with mixed
        severities keeps each severity — no high→critical promotion,
        no dropped highs."""
        import json
        from aah.core.build.quality_checks import _parse_sa_output

        bandit_json = json.dumps({
            "results": [
                {"issue_text": "hardcoded pwd", "issue_severity": "HIGH",
                 "filename": "a.py", "line_number": 3},
                {"issue_text": "assert used", "issue_severity": "LOW",
                 "filename": "b.py", "line_number": 7},
                {"issue_text": "eval used", "issue_severity": "MEDIUM",
                 "filename": "c.py", "line_number": 9},
            ]
        })
        vios = _parse_sa_output(bandit_json, "")
        sev = sorted(v["severity"] for v in vios)
        assert sev == ["high", "low", "medium"]  # high preserved, none promoted

    def test_sa_text_fallback_classifies_distinctly(self):
        """AC1 (precondition, text path): the line-based fallback maps high
        to `high` (not `critical`) and keeps critical distinct."""
        from aah.core.build.quality_checks import _parse_sa_output

        text = (
            "issue: SQL injection risk severity: high\n"
            "issue: use of md5 severity: medium\n"
            "vulnerability: RCE severity: critical\n"
        )
        vios = _parse_sa_output("", text)
        by_sev = {v["severity"] for v in vios}
        assert "high" in by_sev
        assert "critical" in by_sev
        assert "medium" in by_sev
        # the high line must NOT have been promoted to critical
        high_lines = [v for v in vios if v["severity"] == "high"]
        assert any("injection" in v["message"] for v in high_lines)

    def test_high_finding_blocks_gate(self, tmp_path):
        """AC2: a genuine `high` finding blocks static analysis.

        Runs the real analyzer (bandit) over a fixture with a known
        high-severity issue. Skips cleanly if bandit isn't installed —
        a legitimate skip (no mock, no fabricated result)."""
        import shutil
        import subprocess
        from aah.core.build.quality_checks import run_static_analysis

        # bandit runs under `uv run bandit`; skip if unavailable in this env.
        probe = subprocess.run(
            "uv run bandit --version", shell=True, cwd=str(tmp_path),
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            pytest.skip("bandit not installed in this environment")

        # B602: subprocess with shell=True is a HIGH severity bandit finding.
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"bandit-fixture\"\nversion = \"0.0.0\"\n"
        )
        (tmp_path / "vuln.py").write_text(
            "import subprocess\n"
            "def run(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n"
        )
        result = run_static_analysis(tmp_path, "F001")
        assert result["passed"] is False
        assert any(v["severity"] in ("critical", "high") for v in result["violations"])


class TestUpdateImplState:
    def test_computes_state(self, impl_project):
        from aah.core.common.dag import build_dag_from_features, dag_to_json
        aah_root = impl_project / ".aah"
        # Need a DAG
        features = [
            {"id": "F001", "dependencies": []},
            {"id": "F002", "dependencies": []},
            {"id": "F003", "dependencies": ["F001"]},
        ]
        G = build_dag_from_features(features)
        write_json(dag_to_json(G), aah_root / "plan" / "dag.json")

        state = update_impl_state(impl_project)
        assert "F001" in state["completed"]
        assert "F003" in state["available"]  # F001 is done, F003 can proceed
        assert state["total"] == 3


# ─── Regression Guards ────────────────────────────────────────────────


class TestHumanReviewWorkflowConstants:
    """AC test (a): Assert MAX_QA_REWORK_ATTEMPTS == 3."""

    def test_max_qa_rework_attempts_is_three(self):
        from aah.core.build.qa_routing import MAX_QA_REWORK_ATTEMPTS

        assert MAX_QA_REWORK_ATTEMPTS == 3, "Rework cap must be exactly 3 (P0.3-4 AC3)"


class TestFeatureQaFinalApproved:
    """Final QA approval always requires current-subject evidence."""
