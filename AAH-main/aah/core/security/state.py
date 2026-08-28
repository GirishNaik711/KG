#!/usr/bin/env python3
"""Canonical security-state initialization and legacy migration.

Security state lives under ``.aah/security``.  During the pre-1.0 retirement
window this module non-destructively copies state from ``.rapids/security`` and
seeds the framework's bundled Semgrep and Nuclei rules.  Existing ``.aah``
files always win.
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import ensure_dir


class SecurityStateMigrationError(RuntimeError):
    """Raised when canonical security state cannot be initialized safely."""


@dataclass(frozen=True)
class SecurityStateResult:
    """Result of an idempotent security-state initialization."""

    security_dir: Path
    copied: tuple[str, ...] = ()
    seeded: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def _has_symlink_parent(path: Path, root: Path) -> bool:
    """Return whether ``path`` or one of its parents below ``root`` is a symlink."""
    current = path
    while current != root:
        if current.is_symlink():
            return True
        current = current.parent
    return root.is_symlink()


def _copy_missing_tree(
    source: Path,
    destination: Path,
    *,
    prefix: str,
) -> tuple[list[str], list[str], list[str]]:
    """Copy regular files missing from ``destination`` without following links."""
    copied: list[str] = []
    conflicts: list[str] = []
    skipped_symlinks: list[str] = []

    if source.is_symlink():
        skipped_symlinks.append(prefix.rstrip("/"))
        return copied, conflicts, skipped_symlinks
    if not source.exists():
        return copied, conflicts, skipped_symlinks
    if not source.is_dir():
        skipped_symlinks.append(prefix.rstrip("/"))
        return copied, conflicts, skipped_symlinks

    items = sorted(source.rglob("*"))
    for item in items:
        if item.is_symlink():
            relative = item.relative_to(source)
            skipped_symlinks.append(f"{prefix}{relative.as_posix()}")
    if skipped_symlinks:
        return copied, conflicts, skipped_symlinks

    for item in items:
        relative = item.relative_to(source)
        display = f"{prefix}{relative.as_posix()}"

        if item.is_dir():
            continue
        if not item.is_file():
            continue

        target = destination / relative
        if _has_symlink_parent(target, destination):
            raise SecurityStateMigrationError(
                f"refusing security-state destination through symlink: {display}"
            )

        if target.exists():
            if not target.is_file() or not filecmp.cmp(item, target, shallow=False):
                conflicts.append(display)
            continue

        try:
            ensure_dir(target.parent)
            shutil.copy2(item, target)
        except OSError as exc:
            raise SecurityStateMigrationError(
                f"could not copy security state {display}: {exc}"
            ) from exc
        copied.append(relative.as_posix())

    return copied, conflicts, skipped_symlinks


def ensure_security_state(project_path: Path, *, notify: bool = False) -> SecurityStateResult:
    """Initialize ``.aah/security`` and copy all missing legacy state.

    The operation is idempotent and non-destructive.  Differing destination
    files are retained and reported as conflicts.  Symlinks are never followed.
    Any copy/initialization error raises :class:`SecurityStateMigrationError`
    so callers cannot silently continue with incomplete policy or evidence.
    """
    project_path = project_path.resolve()
    aah_dir = project_path / ".aah"
    if aah_dir.is_symlink():
        raise SecurityStateMigrationError(f"refusing symlinked AAH state root: {aah_dir}")
    legacy_dir = project_path / ".rapids"
    if legacy_dir.is_symlink():
        raise SecurityStateMigrationError(f"refusing symlinked legacy state root: {legacy_dir}")
    security_dir = aah_dir / "security"

    try:
        ensure_dir(security_dir)
    except OSError as exc:
        raise SecurityStateMigrationError(
            f"could not initialize {security_dir}: {exc}"
        ) from exc

    copied, conflicts, skipped = _copy_missing_tree(
        legacy_dir / "security",
        security_dir,
        prefix="legacy:",
    )

    framework_root = resolve_framework_root()
    if framework_root is None:
        raise SecurityStateMigrationError("could not resolve bundled security rules")
    bundled_root = framework_root / "_resources" / "config" / "security"
    if not bundled_root.is_dir():
        raise SecurityStateMigrationError(
            f"bundled security rules not found at {bundled_root}"
        )

    seeded, seed_conflicts, seed_skipped = _copy_missing_tree(
        bundled_root,
        security_dir,
        prefix="bundled:",
    )
    conflicts.extend(seed_conflicts)
    skipped.extend(seed_skipped)

    if skipped:
        raise SecurityStateMigrationError(
            "refusing unsafe or symlinked security state: " + ", ".join(skipped)
        )

    result = SecurityStateResult(
        security_dir=security_dir,
        copied=tuple(copied),
        seeded=tuple(seeded),
        conflicts=tuple(conflicts),
    )

    if notify and (result.copied or result.seeded or result.conflicts):
        print(
            "AAH security state: "
            f"migrated={len(result.copied)}, seeded={len(result.seeded)}, "
            f"conflicts={len(result.conflicts)}",
            file=sys.stderr,
        )
        for conflict in result.conflicts:
            print(f"  kept existing .aah file ({conflict})", file=sys.stderr)

    return result
