"""Status-stamping must not corrupt feature specs (weather-cli bugs #5/#14).

Both write paths — write_feature_frontmatter (qa_status) and append_section
("## Status") — must leave the spec parseable (parse_feature_frontmatter -> dict,
never None) for BOTH markdown-native and YAML-frontmatter specs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aah.core.common.feature_utils import (
    append_section,
    parse_feature_frontmatter,
    write_feature_frontmatter,
)

MARKDOWN_NATIVE = """## Id
- F-MOD000-00

## Description
- CLI skeleton

## Acceptance Criteria
- Runs
"""

YAML_FRONTMATTER = """---
id: F-MOD000-00
description: CLI skeleton
status: pending
---
## Notes
body text
"""


@pytest.mark.parametrize("spec", [MARKDOWN_NATIVE, YAML_FRONTMATTER])
def test_append_status_section_keeps_spec_parseable(tmp_path: Path, spec: str):
    path = tmp_path / "F-MOD000-00.md"
    path.write_text(spec, encoding="utf-8")

    append_section(path, "Status", ["complete"])

    data = parse_feature_frontmatter(path)
    assert data is not None, "spec became unparseable after status stamp"
    assert data["id"] == "F-MOD000-00"


@pytest.mark.parametrize("spec", [MARKDOWN_NATIVE, YAML_FRONTMATTER])
def test_write_qa_status_keeps_spec_parseable(tmp_path: Path, spec: str):
    path = tmp_path / "F-MOD000-00.md"
    path.write_text(spec, encoding="utf-8")

    data = parse_feature_frontmatter(path)
    assert data is not None
    data["qa_status"] = "passed"
    write_feature_frontmatter(data, path)

    reparsed = parse_feature_frontmatter(path)
    assert reparsed is not None, "spec became unparseable after qa_status write"
    assert reparsed.get("qa_status") == "passed"
    assert reparsed["id"] == "F-MOD000-00"


def test_repeated_status_stamps_are_idempotent_and_safe(tmp_path: Path):
    # The bug recurred ~6x because every state write re-stamped the spec.
    path = tmp_path / "F-MOD001-00.md"
    path.write_text(YAML_FRONTMATTER, encoding="utf-8")
    for _ in range(6):
        append_section(path, "Status", ["complete"])
        assert parse_feature_frontmatter(path) is not None
