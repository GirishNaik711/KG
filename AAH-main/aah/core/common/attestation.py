#!/usr/bin/env python3
"""Tamper-evident result-file attestation (AAH issue #247, Phase 1).

Phase 1 ships this module as a pure library — nothing in the existing
verification flow calls it yet. Phase 2 will convert each result-file
writer (write_qa_report, run_regression_suite, run_feature_tests,
validate_checkpoint, quality_checks) to call ``write_attested`` and add
``verify`` calls to the orchestrator readers.

Threat model: an AI agent can write JSON result files like
``{"passed": true}`` directly and the orchestrator advances because
nothing re-runs the work. This module addresses that by embedding an
HMAC signature, keyed by a project-local secret, so result files written
outside the official subprocess writers fail the signature check at
read time. The secret lives at ``.aah/build/.attestation-secret``,
is created once on first use (see ``regenerate_attestation_secret``),
mode 0600 where supported, and gitignored. Session restarts do not rotate
the key; evidence freshness is enforced separately by each consumer's
subject and input/configuration checks.

Linked worktrees resolve this path through Git's common directory, so every
checkout of the repository shares one key. The module is stdlib-only
(``hmac`` + ``hashlib``).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aah.core.common.git_utils import repo_common_root
from aah.core.common.io_utils import _sanitise_for_json, write_json_verified


# Env var that legitimate writers set so the perimeter guard
# (verification_artifact_guard) recognises an attested write.
VERIFICATION_WRITE_ENV = "AAH_VERIFICATION_WRITE"

# Serializes the env-var save/restore inside ``write_attested`` so
# concurrent in-process callers don't race on os.environ. In production
# the orchestrator runs writers sequentially per gate, but tests
# exercise concurrent writes and the lock keeps the env-var contract
# honest under that load.
_ENV_LOCK = threading.Lock()

# Where the repository-level HMAC key lives, relative to its main checkout.
SECRET_REL_PATH = Path(".aah") / "build" / ".attestation-secret"
SECRET_LENGTH_BYTES = 32


# ---------------------------------------------------------------------------
# Secret management
# ---------------------------------------------------------------------------


def attestation_secret_path(project_path: Path) -> Path:
    """Return the one repository-level key path shared by all worktrees."""
    project_path = Path(project_path).resolve()
    return (repo_common_root(project_path) or project_path) / SECRET_REL_PATH


def get_or_create_session_secret(project_path: Path) -> bytes:
    """Read and validate the project-local attestation secret.

    The legacy function name is retained for API compatibility. Despite the
    name, this function never creates or rotates a key. SessionStart creation
    is the responsibility of ``aah.core.build.regenerate_attestation_secret``.

    Raises ``FileNotFoundError`` if the secret hasn't been generated yet.
    Raises ``OSError`` if the path is a symlink or non-regular file, cannot be
    read, or does not contain exactly 32 bytes. This function deliberately has
    no side effects on import or call so signing and verification fail closed.
    """
    path = attestation_secret_path(project_path)
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Attestation secret not found at {path}. "
            "Ensure the SessionStart hook regenerate_attestation_secret has run."
        ) from None

    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError(f"Attestation secret must not be a symlink: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise OSError(f"Attestation secret must be a regular file: {path}")

    # O_BINARY: the secret is 32 raw random bytes. On Windows, os.open defaults
    # to text mode, so \r\n<->\n translation corrupts (and shortens) the read.
    # O_BINARY is absent on POSIX (getattr default 0), where reads are already raw.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(path, flags)
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError(f"Attestation secret must be a regular file: {path}")
        # Ensure the path inspected above is the file that was opened. This
        # closes the lstat/open substitution window on platforms without
        # O_NOFOLLOW and also detects concurrent replacement.
        if not os.path.samestat(path_stat, opened_stat):
            raise OSError(f"Attestation secret changed while being opened: {path}")
        current_stat = os.lstat(path)
        if not stat.S_ISREG(current_stat.st_mode) or not os.path.samestat(
            current_stat, opened_stat
        ):
            raise OSError(f"Attestation secret changed while being opened: {path}")

        chunks: list[bytes] = []
        remaining = SECRET_LENGTH_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        secret = b"".join(chunks)
    finally:
        os.close(fd)

    if len(secret) != SECRET_LENGTH_BYTES:
        raise OSError(
            f"Attestation secret at {path} must contain exactly "
            f"{SECRET_LENGTH_BYTES} bytes (found {len(secret)})"
        )
    return secret


def _secret_fingerprint(secret: bytes) -> str:
    """SHA-256 hex digest of the key. Used as a tag in result files so
    we can detect a key mismatch without revealing the key.
    """
    return hashlib.sha256(secret).hexdigest()


# ---------------------------------------------------------------------------
# Signing primitives
# ---------------------------------------------------------------------------


def _canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding for signing.

    sort_keys=True and tight separators give the same bytes regardless of
    insertion order or whitespace, so the signature roundtrips even after
    json.dump → json.load.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _compute_signature(
    secret: bytes,
    payload_without_signature: dict,
) -> str:
    """HMAC-SHA256 over the canonical-JSON of (payload + attestation-minus-signature).

    The "signature" field is excluded from its own input, but every other
    attestation field IS included — so tampering with command, exit_code,
    stdout_sha256, etc. invalidates the signature.
    """
    return hmac.new(
        secret,
        _canonical_json(payload_without_signature),
        hashlib.sha256,
    ).hexdigest()


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Public API: write_attested
# ---------------------------------------------------------------------------


def write_attested(
    payload: dict,
    path: Path,
    *,
    project_path: Path,
    command: list[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_ms: int,
    artifact_name: str = "",
) -> dict:
    """Embed an attestation block on ``payload`` and write it to ``path``.

    The payload dict is mutated in place: a top-level ``attestation`` key
    is added before writing. Reuses ``write_json_verified`` so we
    inherit its post-write existence + JSON-validity check.

    Sets ``AAH_VERIFICATION_WRITE=1`` in the environment for the
    duration of the write so a PreToolUse guard can distinguish a
    legitimate write from a forgery. The env var is restored after the
    write returns.

    The ``command`` items are coerced to ``str`` via ``str()`` so the
    on-disk attestation is unambiguously ``list[str]``. This means
    ``Path`` and other str-able types are accepted ergonomically while
    ``verify(payload, expected_command_prefix=[...])`` always compares
    string-to-string.

    Returns the (mutated) payload, mostly for ergonomics in tests.

    Raises:
        FileNotFoundError: project-local secret not yet generated.
        RuntimeError: propagated from ``write_json_verified`` if the
            file does not exist or is unreadable post-write.
    """
    secret = get_or_create_session_secret(project_path)

    # Build the attestation block in two steps: first all fields except
    # the signature, then compute the signature over the whole payload
    # (with the partial attestation block embedded), then attach the
    # signature.
    attestation: dict[str, Any] = {
        "method": "subprocess",
        "command": [str(c) for c in command],
        "exit_code": int(exit_code),
        "stdout_sha256": _sha256_hex(stdout or ""),
        "stderr_sha256": _sha256_hex(stderr or ""),
        "duration_ms": int(duration_ms),
        "host_pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_secret_sha256": _secret_fingerprint(secret),
        "schema_version": 1,
    }
    payload["attestation"] = attestation

    # Sanitise the payload BEFORE signing so the on-disk form
    # (post write_json_verified → _sanitise_for_json) matches the signed
    # form exactly.  Without this, ANSI escape codes in stdout/stderr
    # get stripped on write but are present at sign-time → mismatch.
    sanitised_payload = _sanitise_for_json(payload)
    # Mutate payload in-place to the sanitised version so downstream
    # write_json_verified writes the same bytes we signed.
    payload.clear()
    payload.update(sanitised_payload)
    attestation = payload["attestation"]

    # Sign the payload (excluding the not-yet-present signature field).
    signature = _compute_signature(secret, payload)
    attestation["signature"] = signature

    with _ENV_LOCK:
        prev_env = os.environ.get(VERIFICATION_WRITE_ENV)
        os.environ[VERIFICATION_WRITE_ENV] = "1"
        try:
            write_json_verified(payload, path, artifact_name=artifact_name)
        finally:
            if prev_env is None:
                os.environ.pop(VERIFICATION_WRITE_ENV, None)
            else:
                os.environ[VERIFICATION_WRITE_ENV] = prev_env

    return payload


# ---------------------------------------------------------------------------
# Public API: verify
# ---------------------------------------------------------------------------


# Reason codes returned by verify(). Keep stable — orchestrator readers
# in Phase 2 will dispatch on these.
REASON_NO_ATTESTATION = "no_attestation"
REASON_MISSING_SECRET = "missing_secret"
REASON_UNREADABLE_SECRET = "unreadable_secret"
REASON_STALE_SECRET = "stale_secret"
REASON_SIGNATURE_MISMATCH = "signature_mismatch"
REASON_COMMAND_MISMATCH = "command_mismatch"
REASON_MALFORMED = "malformed_attestation"


_REQUIRED_ATTESTATION_FIELDS: tuple[tuple[str, type], ...] = (
    ("method", str),
    ("command", list),
    ("exit_code", int),
    ("stdout_sha256", str),
    ("stderr_sha256", str),
    ("duration_ms", int),
    ("host_pid", int),
    ("timestamp", str),
    ("session_secret_sha256", str),
    ("signature", str),
)


def verify(
    payload: dict,
    *,
    project_path: Path,
    expected_command_prefix: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate the attestation block on a loaded payload.

    Returns ``(True, "")`` when:
      1. Payload contains an ``attestation`` block with all required fields.
      2. ``session_secret_sha256`` matches the current project-local secret.
      3. The recomputed HMAC signature matches the stored signature.
      4. (If ``expected_command_prefix`` given) the recorded ``command``
         starts with that prefix.

    Returns ``(False, reason)`` otherwise. ``reason`` is one of the
    REASON_* constants above so callers can branch deterministically.
    """
    if not isinstance(payload, dict):
        return False, REASON_MALFORMED

    attestation = payload.get("attestation")
    if not isinstance(attestation, dict):
        return False, REASON_NO_ATTESTATION

    # Field-presence/type check.
    for field, expected_type in _REQUIRED_ATTESTATION_FIELDS:
        value = attestation.get(field)
        # bool is a subclass of int — exclude it explicitly for int fields.
        if expected_type is int and isinstance(value, bool):
            return False, REASON_MALFORMED
        if not isinstance(value, expected_type):
            return False, REASON_MALFORMED

    # Secret check. FileNotFoundError → missing; any other OSError
    # (PermissionError, partial write, FS corruption) → unreadable.
    # FileNotFoundError is a subclass of OSError, so its except clause
    # must come first.
    try:
        secret = get_or_create_session_secret(project_path)
    except FileNotFoundError:
        return False, REASON_MISSING_SECRET
    except OSError:
        return False, REASON_UNREADABLE_SECRET

    if attestation["session_secret_sha256"] != _secret_fingerprint(secret):
        return False, REASON_STALE_SECRET

    # Signature check. Strip the signature, recompute, compare in
    # constant time.
    stored_sig = attestation["signature"]
    payload_for_sig = _strip_signature(payload)
    expected_sig = _compute_signature(secret, payload_for_sig)
    if not hmac.compare_digest(stored_sig, expected_sig):
        return False, REASON_SIGNATURE_MISMATCH

    # Command-prefix check (optional).
    if expected_command_prefix is not None:
        recorded = attestation["command"]
        if len(recorded) < len(expected_command_prefix) or list(
            recorded[: len(expected_command_prefix)]
        ) != list(expected_command_prefix):
            return False, REASON_COMMAND_MISMATCH

    return True, ""


def _strip_signature(payload: dict) -> dict:
    """Return a clone of ``payload`` with the signature removed.

    Deep-copies the attestation block so callers (or future readers
    that render/log the attestation) can mutate the result without
    aliasing into the original payload's nested ``command`` list. The
    non-attestation portion of the payload is left as a shallow
    reference because canonical-JSON serialization doesn't mutate it.
    """
    cloned = dict(payload)
    cloned["attestation"] = copy.deepcopy(payload["attestation"])
    cloned["attestation"].pop("signature", None)
    return cloned
