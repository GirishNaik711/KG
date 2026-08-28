"""Atomic allocation for append-only, sequentially named artifacts."""

from __future__ import annotations

import os
import re
from pathlib import Path


def sequence_number(
    path: Path,
    prefix: str,
    width: int = 3,
    suffix: str = ".json",
) -> int | None:
    """Parse a sequence whose configured width is only a minimum."""
    match = re.match(
        rf"^{re.escape(prefix)}(\d{{{width},}}){re.escape(suffix)}$",
        Path(path).name,
    )
    return int(match.group(1)) if match else None


def sequenced_paths(
    directory: Path,
    prefix: str,
    width: int = 3,
    suffix: str = ".json",
) -> list[tuple[int, Path]]:
    """Return matching paths in numeric sequence order."""
    if not Path(directory).is_dir():
        return []
    parsed = (
        (number, path)
        for path in Path(directory).glob(f"{prefix}*{suffix}")
        if (number := sequence_number(path, prefix, width, suffix)) is not None
    )
    return sorted(parsed, key=lambda item: item[0])


def claim_sequenced_path(
    directory: Path,
    prefix: str,
    width: int = 3,
    suffix: str = ".json",
    mode: int = 0o644,
    max_attempts: int = 100,
) -> tuple[int, Path]:
    """Atomically claim an empty ``prefixNNNsuffix`` path without overwriting."""
    if width < 1:
        raise ValueError("width must be at least 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    for _ in range(max_attempts):
        existing: list[int] = []
        for candidate in directory.iterdir():
            number = sequence_number(candidate, prefix, width, suffix)
            if number is not None:
                existing.append(number)

        sequence = (max(existing) if existing else 0) + 1
        claimed_path = directory / f"{prefix}{sequence:0{width}d}{suffix}"
        try:
            descriptor = os.open(claimed_path, flags, mode)
        except FileExistsError:
            continue
        os.close(descriptor)
        return sequence, claimed_path

    raise RuntimeError(
        f"Failed to claim a sequenced path with prefix {prefix!r} "
        f"after {max_attempts} attempts"
    )
