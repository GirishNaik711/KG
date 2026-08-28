#!/usr/bin/env python3
"""PreToolUse guard: BLOCK un-attested writes to verification result paths.

Issue #247, Phase 2B. Every legitimate writer
(write_qa_report, run_regression_suite, run_feature_tests,
validate_checkpoint, quality_checks, write_runtime_results) calls
``aah.core.common.attestation.write_attested``, which sets
``AAH_VERIFICATION_WRITE=1`` for the duration of the write. Any
write to a verification result path that lands here without that env
var is either a forgery or an unconverted writer — neither should
reach disk.

This guard is the **write-boundary** half of the #247 defense; the
**read-boundary** half is the orchestrator's ``attestation.verify()``
calls (Phase 2A.1) which reject unattested files at read time. Both
fire on every wave; either is sufficient on its own. Together they
make forgery require defeating cryptographic signing AND a perimeter
hook simultaneously.

The threat model: a subagent or main loop attempts to write JSON like
``{"passed": true}`` directly into ``.aah/build/test-results/``
or similar, bypassing the deterministic verifier. The guard exits 2
(block) — the harness aborts the Write/Edit/MultiEdit call and the
file never reaches disk.

Escape hatch for legitimate debugging: set ``AAH_VERIFICATION_WRITE=1``
in your shell environment before running the offending command. The
guard treats this as "the caller asserted this is intentional."

Settings.json gates this guard with an ``if:`` glob that matches the
relevant paths so the hook is invoked rarely. The guard re-checks the
path internally (``_is_verification_artifact``) so the security boundary
does not depend on the harness's glob matching.
"""

from __future__ import annotations

import json
import os
import sys

from aah.core.common.attestation import VERIFICATION_WRITE_ENV


# Path segments that mark a verification result artifact. Listed
# explicitly (instead of via a single regex) so future result-bucket
# additions are easy to grep for.
_VERIFICATION_PATH_SEGMENTS: tuple[str, ...] = (
    "/.aah/build/test-results/",
    "/.aah/build/runtime-results/",
    "/.aah/build/quality-results/",
    "/.aah/build/checkpoint-results/",
    "/.aah/build/validation-results/",
)


def _is_verification_artifact(path: str) -> bool:
    if not path:
        return False
    norm = path.replace("\\", "/")
    return any(seg in norm for seg in _VERIFICATION_PATH_SEGMENTS)


def _extract_file_path(tool_name: str, tool_input: dict) -> str:
    """Return the target file path for any of Write / Edit / MultiEdit."""
    if tool_name in ("Write", "Edit"):
        return tool_input.get("file_path", "") or ""
    if tool_name == "MultiEdit":
        # MultiEdit shape varies; common forms are file_path at the top
        # level or per-edit. We check both, conservatively.
        top_level = tool_input.get("file_path", "")
        if top_level:
            return top_level
        edits = tool_input.get("edits", []) or []
        for edit in edits:
            fp = edit.get("file_path", "") if isinstance(edit, dict) else ""
            if fp:
                return fp
    return ""


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        # Tolerate malformed/missing stdin — same precedent as other guards.
        sys.exit(0)

    # Plugin-managed projects opt out of AAH guards.
    from aah.core.guards.delegation_guard import exit_if_delegated
    exit_if_delegated()

    tool_name = hook_input.get("tool_name", "") or ""
    tool_input = hook_input.get("tool_input", {}) or {}
    file_path = _extract_file_path(tool_name, tool_input)

    from aah.core.guards._trace import trace
    if not _is_verification_artifact(file_path):
        trace("verification_artifact_guard", "noop", file_path)
        sys.exit(0)

    # Legitimate writers set this env var via attestation.write_attested.
    if os.environ.get(VERIFICATION_WRITE_ENV) == "1":
        trace("verification_artifact_guard", "allow", file_path)
        sys.exit(0)

    # Phase 2B: BLOCK. Phase 2A converted every legitimate writer to
    # call attestation.write_attested, which sets AAH_VERIFICATION_WRITE=1
    # for the duration of the write. Any path landing here without that
    # env var is either a forgery or an unconverted writer — neither
    # should reach disk.
    print(
        f"⚠ AAH verification artifact written outside attestation: {file_path}",
        file=sys.stderr,
    )
    print(
        "Verification results must be produced by aah run scripts that "
        "call aah.core.common.attestation.write_attested. If you are "
        "writing this file intentionally for debugging, set "
        "AAH_VERIFICATION_WRITE=1 in your shell to bypass this guard.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
