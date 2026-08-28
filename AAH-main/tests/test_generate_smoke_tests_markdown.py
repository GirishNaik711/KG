"""Round-trip: Markdown feature -> schema-v2 smoke YAML -> live step loader.

NO MOCKS: real .md frontmatter, the real generate_smoke_tests CLI via
subprocess, and the unmodified runtime_validation._load_smoke_steps reader.
The loader lives in runtime_validation (which executes smoke steps) rather than
in verify.py, which is now a read-only evidence verifier.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from aah.core.build.runtime_validation import _load_smoke_steps
from aah.core.common.feature_utils import write_feature_frontmatter
from aah.core.common.io_utils import write_json, write_yaml
from aah.core.common.validators import validate_smoke_wave

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_manifest(aah: Path) -> None:
    write_yaml(
        {"project_name": "demo", "project_type": "greenfield", "current_phase": "plan"},
        aah / "manifest.yaml",
    )


def test_markdown_to_smoke_to_verify_round_trip(tmp_path):
    """AC6: a Markdown feature generates schema-v2 smoke YAML the live verifier reads."""
    aah = tmp_path / ".aah"
    features_dir = aah / "plan" / "features"
    features_dir.mkdir(parents=True)
    _write_manifest(aah)

    # A real feature with a health endpoint and a semantic non-health endpoint.
    write_feature_frontmatter(
        {
            "id": "F-API-001",
            "spec_ref": "SPEC-001",
            "description": "Orders API",
            "dependencies": [],
            "acceptance_criteria": ["Orders can be listed"],
            "test_cases": [{"id": "TC1", "covers": []}],
            "status": "pending",
            "knowledge_used": {},
            "endpoints": [
                {"method": "GET", "path": "/health"},
                {
                        "method": "GET",
                        "path": "/orders",
                        "headers": {"X-Smoke-Contract": "orders"},
                    "expected_status": 200,
                    "assertions": [
                        {"type": "status_in", "values": [200]},
                        {"type": "json_type_is", "path": "$", "json_type": "array"},
                    ],
                },
            ],
        },
        features_dir / "F-API-001.md",
    )

    # Real waves.json referencing the feature.
    write_json({"waves": [["F-API-001"]], "total_waves": 1}, aah / "plan" / "waves.json")

    # Generate via the real CLI.
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.generate_smoke_tests",
         "generate", "--project-path", str(tmp_path), "--wave", "0"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    smoke_path = aah / "plan" / "smoke-tests" / "wave-0.yaml"
    assert smoke_path.exists()
    smoke_def = yaml.safe_load(smoke_path.read_text())

    # Top-level schema_version 2 and passes the wave validator.
    assert smoke_def["schema_version"] == 2
    assert validate_smoke_wave(smoke_def) == []

    # Endpoint steps carry only the canonical schema-v2 request + assertions.
    ep_step = next(
        s for s in smoke_def["steps"]
        if (s.get("request") or {}).get("path") == "/orders"
    )
    assert ep_step["request"] == {
        "method": "GET",
        "path": "/orders",
        "headers": {"X-Smoke-Contract": "orders"},
    }
    assert len(ep_step["assertions"]) == 2
    assert not {"method", "path", "expected_status"}.intersection(ep_step)

    # The runtime loader returns exactly the path-bearing steps.
    steps = _load_smoke_steps(tmp_path, 0)
    paths = {s["path"] for s in steps}
    assert paths == {"/health", "/orders"}
    orders = next(s for s in steps if s["path"] == "/orders")
    assert orders["headers"] == {"X-Smoke-Contract": "orders"}
    # Every returned step is usable by the existing HTTP-probe loop (flat path).
    for s in steps:
        assert s.get("path")
        assert s.get("method")




def test_generate_fails_closed_on_unasserted_endpoint(tmp_path):
    """A declared non-health endpoint with an empty assertion list fails closed."""
    aah = tmp_path / ".aah"
    features_dir = aah / "plan" / "features"
    features_dir.mkdir(parents=True)
    _write_manifest(aah)
    write_feature_frontmatter(
        {
            "id": "F-BAD", "spec_ref": "SPEC-001", "description": "bad",
            "dependencies": [], "acceptance_criteria": [], "test_cases": [],
            "status": "pending", "knowledge_used": {},
            # Explicit empty assertions on a non-health endpoint.
            "endpoints": [{"method": "GET", "path": "/orders", "assertions": []}],
        },
        features_dir / "F-BAD.md",
    )
    write_json({"waves": [["F-BAD"]], "total_waves": 1}, aah / "plan" / "waves.json")
    result = subprocess.run(
        [sys.executable, "-m", "aah.core.plan.generate_smoke_tests",
         "generate", "--project-path", str(tmp_path), "--wave", "0"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "invalid smoke assertions" in result.stderr


def _smoke_yaml(tmp_path: Path, body: str) -> None:
    smoke_dir = tmp_path / ".aah" / "plan" / "smoke-tests"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    (smoke_dir / "wave-0.yaml").write_text(body, encoding="utf-8")
