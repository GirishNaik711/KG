from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from aah.core.common.sequenced_store import claim_sequenced_path, sequenced_paths

def _concurrent_claim_worker(
    directory: str,
    start: Any,
    results: Any,
) -> None:
    if not start.wait(timeout=15):
        results.put(("error", "start timeout"))
        return
    try:
        sequence, path = claim_sequenced_path(Path(directory), "attempt-")
    except Exception as exc:  # pragma: no cover - reported in the parent assertion
        results.put(("error", repr(exc)))
        return
    results.put(("ok", sequence, path.name))

def test_claim_preserves_existing_files_and_uses_next_number(tmp_path: Path) -> None:
    directory = tmp_path / "attempts"
    directory.mkdir()
    first = directory / "attempt-001.json"
    first.write_text("do not overwrite", encoding="utf-8")
    (directory / "attempt-001-tests.json").write_text("unrelated", encoding="utf-8")
    (directory / "attempt-not-a-number.json").touch()
    sequence, claimed = claim_sequenced_path(directory, "attempt-")
    assert sequence == 2
    assert claimed == directory / "attempt-002.json"
    assert claimed.exists()
    assert claimed.read_bytes() == b""
    assert first.read_text(encoding="utf-8") == "do not overwrite"

def test_claim_keeps_existing_max_plus_one_numbering(tmp_path: Path) -> None:
    directory = tmp_path / "decisions"
    directory.mkdir()
    (directory / "decision-001.json").touch()
    (directory / "decision-003.json").touch()
    sequence, claimed = claim_sequenced_path(directory, "decision-")
    assert sequence == 4
    assert claimed.name == "decision-004.json"
    (directory / "decision-999.json").touch()
    sequence, claimed = claim_sequenced_path(directory, "decision-")
    assert (sequence, claimed.name) == (1000, "decision-1000.json")
    assert claim_sequenced_path(directory, "decision-")[0] == 1001

@pytest.mark.parametrize("prefix", ["attempt-", "override-", "decision-", "human-"])
def test_consumer_ordering_remains_numeric_after_rollover(
    tmp_path: Path, prefix: str
) -> None:
    directory = tmp_path / prefix.rstrip("-")
    directory.mkdir()
    for number in (998, 1000, 999):
        (directory / f"{prefix}{number:03d}.json").touch()
    (directory / f"{prefix}099.json.extra").touch()
    assert [number for number, _ in sequenced_paths(directory, prefix)] == [
        998,
        999,
        1000,
    ]

def test_custom_width_suffix_and_mode_are_honored(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        sequence, claimed = claim_sequenced_path(
            tmp_path / "lessons",
            "lesson-",
            width=5,
            suffix=".yaml",
            mode=0o640,
        )
    finally:
        os.umask(previous_umask)
    assert sequence == 1
    assert claimed.name == "lesson-00001.yaml"
    assert stat.S_IMODE(claimed.stat().st_mode) == 0o640

def test_real_processes_claim_unique_gap_free_paths(tmp_path: Path) -> None:
    process_count = 8
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    directory = tmp_path / "concurrent-attempts"
    processes = [
        context.Process(
            target=_concurrent_claim_worker,
            args=(str(directory), start, results),
        )
        for _ in range(process_count)
    ]
    for process in processes:
        process.start()
    start.set()
    messages = []
    try:
        for _ in processes:
            messages.append(results.get(timeout=30))
    except Empty:
        pytest.fail("a claim process did not report a result")
    finally:
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    assert all(message[0] == "ok" for message in messages), messages
    sequences = sorted(message[1] for message in messages)
    names = {message[2] for message in messages}
    assert sequences == list(range(1, process_count + 1))
    assert names == {f"attempt-{number:03d}.json" for number in sequences}
    assert all((directory / name).read_bytes() == b"" for name in names)

@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"width": 0}, "width"), ({"max_attempts": 0}, "max_attempts")],
)
def test_invalid_limits_are_rejected(
    tmp_path: Path, kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        claim_sequenced_path(tmp_path, "attempt-", **kwargs)
