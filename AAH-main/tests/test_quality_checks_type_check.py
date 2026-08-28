"""Static type-check gate.

Functional, NO MOCKS — real tmp projects, real result files, real subprocess.
Covers run_type_check's config-gated skip, the missing-binary block, the
type-error block, and the run-all wiring.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.build.quality_checks import (
    get_quality_summary,
    run_type_check,
)


def _seed_mypy_project(tmp_path: Path, body: str) -> Path:
    """A Python project that opted into mypy, with a source file."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.mypy]\nstrict = true\n"
    )
    (tmp_path / "m.py").write_text(body)
    return tmp_path


def test_skip_when_no_type_config(tmp_path):
    # Bare Python project — no [tool.mypy], no tsconfig → clean skip (AC2).
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    result = run_type_check(tmp_path, "F001")
    assert result["passed"] is True
    assert result["details"]["skip_reason"] == "no_type_config"


def test_type_error_blocks(tmp_path):
    # mypy config present + a real type error. If mypy is installed it
    # returns non-zero (block); if it's absent the tool-missing branch also
    # blocks. Either way the gate must NOT silently pass (AC1).
    _seed_mypy_project(tmp_path, "x: int = 'not an int'\n")
    result = run_type_check(tmp_path, "F001")
    assert result["passed"] is False
    assert result["details"].get("skip_reason") is None


def test_run_all_surfaces_type_check(tmp_path):
    # run-all over a skip-case project → summary carries a type-check file
    # and the skip does not flip overall_passed. Exercised via the summary
    # reader (which lists type-check among check_types).
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    quality_dir = tmp_path / ".aah" / "build" / "quality-results"
    quality_dir.mkdir(parents=True)

    from aah.core.common.io_utils import write_json
    tc = run_type_check(tmp_path, "F001")
    write_json(tc, quality_dir / "F001-type-check.json")

    summary = get_quality_summary(tmp_path, "F001")
    assert "type-check" in summary["checks"]
    assert summary["checks"]["type-check"]["passed"] is True
    assert summary["overall_passed"] is True
