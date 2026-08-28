"""Shared fixtures for the ports test suite. NO MOCKS — real temp projects."""

from pathlib import Path

import pytest

from aah.core.common.io_utils import write_yaml


def _manifest() -> dict:
    return {
        "schema_version": "1.0",
        "project_name": "port-test",
        "current_phase": "research",
        "project_type": "greenfield",
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal real project: a folder with .aah/manifest.yaml."""
    aah = tmp_path / ".aah"
    aah.mkdir(parents=True)
    write_yaml(_manifest(), aah / "manifest.yaml")
    return tmp_path


def write_discuss_registry(project: Path, decisions: list[dict],
                           pre_resolved: list[dict] | None = None) -> None:
    """Write a real slug decision-registry.yaml the executor's compile reads."""
    reg = {
        "schema_version": "1.0",
        "project": "port-test",
        "pre_resolved": pre_resolved or [],
        "decisions": decisions,
    }
    write_yaml(reg, project / ".aah" / "discuss" / "decision-registry.yaml")
