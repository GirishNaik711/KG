"""Concurrent-write smoke test for the attestation library.

The library does not introduce locking; it relies on the existing
per-file write isolation (each feature/wave writes its own file).
This test guards against accidental shared-state regressions in the
library — e.g., a global cache of canonical-JSON serialization that
isn't thread-safe.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path

from aah.core.common import attestation
from aah.core.common.attestation import SECRET_REL_PATH
from aah.core.common.io_utils import read_json


def _seed_secret(project_path: Path) -> None:
    secret_path = project_path / SECRET_REL_PATH
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_bytes(secrets.token_bytes(32))


def test_two_threads_distinct_paths(rapids_project):
    _seed_secret(rapids_project)
    out_dir = rapids_project / ".aah" / "build" / "test-results"
    errors: list[BaseException] = []

    def write_one(idx: int) -> None:
        try:
            out = out_dir / f"F{idx:03d}.json"
            attestation.write_attested(
                {"feature_id": f"F{idx:03d}", "verdict": "pass"},
                out,
                project_path=rapids_project,
                command=["aah-run", f"writer-{idx}"],
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=idx,
            )
        except BaseException as exc:  # pragma: no cover — surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=write_one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"unexpected errors: {errors}"

    # All 8 files exist and verify.
    for i in range(8):
        out = out_dir / f"F{i:03d}.json"
        loaded = read_json(out)
        ok, reason = attestation.verify(loaded, project_path=rapids_project)
        assert ok, f"F{i:03d} failed verify: {reason}"
