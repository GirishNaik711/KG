#!/usr/bin/env python3
"""SessionStart hook: ensure the project-local attestation secret exists.

Issue #247, Phase 1. The secret is consumed by
``aah.core.common.attestation`` to sign verification result files
so an AI agent cannot forge them. The key is durable for the project so a
normal session restart or resume does not invalidate otherwise-fresh evidence.

Behaviour:
  * Skip silently if the project is delegated (plugin-managed).
  * Skip silently if there's no resolvable project (pre-scaffold).
  * If missing, create 32 random bytes at
    ``$PROJECT/.aah/build/.attestation-secret`` without overwriting a
    concurrent creator, then set mode 0600 (best-effort on Windows).
  * If present, validate and preserve the existing key unchanged.
  * Refuse to replace malformed, unreadable, symlinked, or non-regular keys.

Exit codes:
  0  always — this hook never blocks SessionStart.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

from aah.core.common.attestation import (
    SECRET_LENGTH_BYTES,
    attestation_secret_path,
    get_or_create_session_secret,
)


def ensure_attestation_secret(project_path: Path) -> Path:
    """Ensure a valid project-local secret exists and return its path.

    This operation is idempotent: an existing valid key is only read and is
    never rewritten.
    ``O_EXCL`` ensures concurrent SessionStart processes cannot overwrite one
    another. A losing creator briefly retries validation while the winning
    process completes its single 32-byte write.
    """
    secret_path = attestation_secret_path(project_path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        get_or_create_session_secret(project_path)
        return secret_path
    except FileNotFoundError:
        pass

    # O_BINARY: write the 32 raw random bytes verbatim. Without it, Windows text
    # mode would translate \n bytes on write, corrupting the secret. Absent on
    # POSIX (getattr default 0), where writes are already raw.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(secret_path, flags, 0o600)
    except FileExistsError:
        # Another startup may have won after our missing-file check. Its file
        # becomes visible before the write completes, so retry only this race;
        # a persistently malformed winner still fails closed.
        last_error: OSError | None = None
        for _ in range(20):
            try:
                get_or_create_session_secret(project_path)
                return secret_path
            except FileNotFoundError as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
            time.sleep(0.005)
        assert last_error is not None
        raise last_error

    try:
        secret = secrets.token_bytes(SECRET_LENGTH_BYTES)
        written = 0
        while written < len(secret):
            count = os.write(fd, secret[written:])
            if count <= 0:
                raise OSError("could not write complete attestation secret")
            written += count
        os.fsync(fd)
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            # Best-effort on platforms without POSIX permissions.
            pass
    finally:
        os.close(fd)

    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        # Best-effort on platforms without POSIX permissions (Windows, FAT).
        # Git exclusion and the Read deny rule remain the access boundary on
        # filesystems that do not implement POSIX permission bits.
        pass

    # Read through the same fail-closed path used by writers/readers before
    # reporting success.
    get_or_create_session_secret(project_path)
    return secret_path


def main() -> None:
    # Plugin-managed projects opt out of AAH guards entirely.
    from aah.core.guards.delegation_guard import exit_if_delegated
    exit_if_delegated()

    # No project resolved → pre-scaffold, no .aah/ to write into.
    from aah.core.common.config import resolve_project_path
    project_path = resolve_project_path()
    if project_path is None:
        sys.exit(0)

    try:
        ensure_attestation_secret(project_path)
    except OSError as exc:
        # Don't block the whole SessionStart hook chain. Official evidence
        # writers/readers still fail closed until the key problem is fixed.
        print(
            f"⚠ aah_root: attestation key is unavailable or invalid: {exc}",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
