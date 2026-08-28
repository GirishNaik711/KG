"""Contract tests for the read-only wave evidence verifier.

The old execution-pipeline suite (step runner, Docker lifecycle, boot, smoke
probes, cloud readiness, fix_category mapping) was deleted with the pipeline it
covered. What remains here is the new contract: identity derivation, complete
failure accumulation, deterministic ordering, typed exceptions, the race guard,
and the action payloads the orchestrator dispatches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.aah_project import AAHProjectBuilder

from aah.core.build import verify as verify_mod
from aah.core.build.verification_contracts import (
    ACTION_BLOCKED,
    ACTION_FIX_REGRESSION,
    ACTION_NO_SIGNAL,
    ACTION_RUN_REGRESSION,
    ACTION_RUN_SYSTEM_CHECKPOINT,
    _PRIORITY,
    VerificationFailure,
    VerificationReport,
    VerificationSystemError,
    WaveVerificationFailed,
    action_from_verification_failure,
)
from aah.core.build.verification_identity import (
    runtime_criteria_identity,
    subject_identity,
    verification_report_is_current,
)
from aah.core.build.verify import (
    verify_wave_evidence,
)


WAVE = 0
BRANCH = f"integration/wave-{WAVE}"


@pytest.fixture
def project(tmp_path):
    """A real git-backed project on integration/wave-0 with plan config."""
    builder = (
        AAHProjectBuilder.create(tmp_path, name="verify-proj", branch=BRANCH)
        .dirs(
            "build/test-results",
            "build/runtime-results",
            "build/quality-results",
            "build/checkpoint-results",
            "build/validation-results",
            "build/wave-summaries",
            "plan/smoke-tests",
        )
        .secret()
        .manifest(project_name="verify-proj", stack_choices={"primary": "python", "port": 8000})
        .waves([["F001"]])
        .feature("F001")
        .file("src/app.py", "print('hi')\n")
    )
    write_checkpoint_config(builder)
    builder.commit()
    return builder.path


def write_checkpoint_config(builder: AAHProjectBuilder, **extra) -> None:
    import yaml

    payload = {
        "checkpoint_configuration": {
            "system_checkpoints": {"startup_command": "python src/app.py"},
            "verification_profiles": {
                "F001": {
                    "level": "standard",
                    "reasons": [],
                    "required_checks": {},
                }
            },
            **extra,
        }
    }
    (builder.aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8"
    )


def failing_report(project: Path) -> WaveVerificationFailed:
    """Run the verifier on a project with no evidence and return the exception."""
    with pytest.raises(WaveVerificationFailed) as excinfo:
        verify_wave_evidence(project, WAVE)
    return excinfo.value


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Verification behaviour
# ---------------------------------------------------------------------------
