"""Tests for aah.core.guards modules."""

import json
from pathlib import Path

import pytest

from aah.core.guards.no_mocks_guard import check_command
from aah.core.guards.validate_aah_path import validate_path
from aah.core.guards.validate_artifact_template import (
    get_artifact_phase,
    validate_artifact_content,
)


def _artifact_spec(*sections: str) -> dict:
    """Build the activity-driven artifact contract used by the live guard."""
    return {"sections": list(sections)}


class TestNoMocksGuard:
    def test_allows_normal_commands(self):
        assert check_command("pip install flask") is None
        assert check_command("npm install express") is None
        assert check_command("python -m pytest tests/") is None
        assert check_command("ls -la") is None

    def test_blocks_python_mock_install(self):
        result = check_command("pip install pytest-mock")
        assert result is not None
        assert "mock" in result.lower()

    def test_blocks_npm_mock_install(self):
        assert check_command("npm install sinon") is not None
        assert check_command("npm install nock") is not None
        assert check_command("yarn add testdouble") is not None

    def test_blocks_uv_mock_install(self):
        assert check_command("uv pip install pytest-mock") is not None
        assert check_command("uv add responses") is not None

    def test_blocks_mock_usage_patterns(self):
        assert check_command("python -c 'from unittest.mock import patch'") is not None
        assert check_command("python -c 'jest.mock(\"./module\")'") is not None

    def test_allows_unrelated_mock_string(self):
        # "mock" in a comment or variable name shouldn't trigger
        assert check_command("echo 'this is a mockup design'") is None


class TestValidateRapidsPath:
    def test_non_aah_path_allowed(self):
        assert validate_path("/home/user/project/src/main.py") is None

    def test_root_files_allowed(self):
        assert validate_path("/project/.aah/manifest.yaml") is None
        assert validate_path("/project/.aah/claude-progress.json") is None
        assert validate_path("/project/.aah/feature-list.json") is None

    def test_audit_always_writable(self):
        assert validate_path("/project/.aah/audit/log.json", "discuss") is None
        assert validate_path("/project/.aah/audit/log.json", "build") is None

    def test_codebase_intel_always_writable(self):
        assert validate_path("/project/.aah/codebase-intel/structure.md", "plan") is None

    def test_correct_phase_allowed(self):
        assert validate_path("/project/.aah/discuss/tech.md", "discuss") is None
        assert validate_path("/project/.aah/architecture/design.md", "architecture") is None
        assert validate_path("/project/.aah/plan/specs/s1.md", "plan") is None
        assert validate_path("/project/.aah/build/state.json", "build") is None

    def test_wrong_phase_blocked(self):
        result = validate_path("/project/.aah/build/test.json", "discuss")
        assert result is not None
        assert "blocked" in result.lower()

    def test_no_phase_allows_all(self):
        assert validate_path("/project/.aah/discuss/doc.md", None) is None

    def test_init_phase_allows_all(self):
        assert validate_path("/project/.aah/discuss/doc.md", "init") is None


class TestValidateArtifactTemplate:
    def test_research_artifact_type(self):
        assert get_artifact_phase("/p/.aah/discuss/tech-comparison.md") == "discuss"

    def test_adr_artifact_type(self):
        assert get_artifact_phase("/p/.aah/architecture/decisions/adr-001.md") == "architecture"

    def test_non_rapids_returns_none(self):
        assert get_artifact_phase("/p/src/main.py") is None

    def test_non_markdown_returns_none(self):
        assert get_artifact_phase("/p/.aah/discuss/data.json") is None

    def test_valid_research_content(self):
        content = """# Tech Comparison
## Problem Framing
The problem is...
## Findings
We found...
## Trade-offs
Pros and cons...
## Recommendation
We recommend...
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Problem Framing", "Findings", "Trade-offs", "Recommendation"),
        )
        assert missing == []

    def test_missing_research_sections(self):
        content = """# Tech Comparison
## Problem Framing
The problem is...
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Problem Framing", "Findings", "Trade-offs", "Recommendation"),
        )
        assert len(missing) == 3  # Missing Findings, Trade-offs, Recommendation

    def test_valid_adr_content(self):
        content = """# ADR-001
## Context
Background...
## Decision
We decided...
## Rationale
Because...
## Consequences
Impact...
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Context", "Decision", "Rationale", "Consequences"),
        )
        assert missing == []

    def test_analysis_artifact_type(self):
        assert get_artifact_phase("/p/.aah/architecture/solution-architecture.md") == "architecture"

    def test_valid_analysis_with_overview_and_architecture(self):
        content = """# Solution Architecture
## Overview
High-level description
## Architecture
Component diagram and details
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Overview", "Architecture"),
        )
        assert missing == []

    def test_valid_analysis_with_design_and_components(self):
        content = """# Data Model
## Design
ERD and schema
## Components
Service breakdown
## Integration
API contracts
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Design", "Components", "Integration"),
        )
        assert missing == []

    def test_invalid_analysis_only_one_section(self):
        content = """# Architecture
## Overview
Just an overview, nothing else structured
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Overview", "Architecture"),
        )
        assert len(missing) == 1  # Need at least 2
        assert missing == ["Architecture"]

    def test_valid_spec_content(self):
        content = """# Spec
## Overview
About...
## Inputs
Input data...
## Outputs
Output data...
## Behavior
How it works...
## Constraints
Limits...
"""
        missing = validate_artifact_content(
            content,
            _artifact_spec("Overview", "Inputs", "Outputs", "Behavior", "Constraints"),
        )
        assert missing == []
