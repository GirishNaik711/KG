#!/usr/bin/env python3
"""
Run-mode guard for cloud validation scripts.

Cloud gates (validate_cloud_readiness, validate_data_readiness, registry_extractor,
identity) MUST be launched via `aah run`. This guarantees the uv-managed
environment is active, so boto3 / google-cloud-* / azure-* SDKs
are available — otherwise the gate fails with cryptic ImportError messages
that look like real cloud failures.

How it works:
  - `aah run` sets RAPIDS_RUN=1 in the environment before invoking main()
  - Cloud gates call require_rapids_run() at the top of their main()
  - Direct invocation (python -m, python script.py) is refused with a clear message
  - Pytest is allowed through (so unit tests can exercise main() directly)

This is enforced ONLY for cloud validation scripts. Other scripts in the
repo can still be invoked any way the caller prefers.
"""

import os
import sys


_ENV_FLAGS = ("AAH_RUN", "RAPIDS_RUN")
_PYTEST_FLAG = "PYTEST_CURRENT_TEST"


def is_rapids_run() -> bool:
    """Return True if the current process was launched via `aah run`.

    Accepts either AAH_RUN (post-rename, set by aah/cli.py) or the legacy
    RAPIDS_RUN name for backward compatibility.
    """
    return any(os.environ.get(flag) == "1" for flag in _ENV_FLAGS)


def require_rapids_run(script_label: str = "this script") -> None:
    """
    Refuse to run unless launched via `aah run`. Allows pytest.

    Call this as the first statement in main(). Refusal exits with code 2
    and a message pointing the caller at the correct invocation.
    """
    if is_rapids_run():
        return
    if os.environ.get(_PYTEST_FLAG):
        return

    invoked_as = " ".join(sys.argv) if sys.argv else "<module>"
    msg = (
        f"\n"
        f"ERROR: {script_label} must be launched via `aah run`.\n"
        f"\n"
        f"You ran:\n"
        f"  python {invoked_as}\n"
        f"\n"
        f"Use instead:\n"
        f"  aah run <module> [args...]\n"
        f"\n"
        f"Why:\n"
        f"  Cloud validation depends on boto3 / google-cloud-* / azure-* SDKs\n"
        f"  that are pinned in pyproject.toml. `aah run` activates the\n"
        f"  uv-managed environment so those imports succeed reliably. Direct\n"
        f"  invocation can pick up the wrong interpreter or miss SDKs entirely.\n"
        f"\n"
        f"If `aah` is not on PATH:\n"
        f"  uv tool install . --force\n"
    )
    print(msg, file=sys.stderr)
    sys.exit(2)
