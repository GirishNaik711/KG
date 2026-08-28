"""Filesystem link helpers — platform-agnostic, shared by every install adapter.

The installed ``aah`` package is the single source of truth. Adapters install
*symlinks* (never copies) so edits to ``aah/skills`` and ``aah/agents`` are live
in every host they were installed into. Windows without symlink privilege falls
back to a directory junction (dirs) or a copy (files).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def is_our_link(link: Path, target: Path) -> bool:
    """True if ``link`` is one of our links to ``target``.

    Covers a symlink pointing at ``target`` as well as (on Windows) a file
    hard link or directory junction that shares ``target``'s underlying file.
    A plain copy is *not* considered our link — its inode differs from
    ``target`` so ``samefile`` returns False.
    """
    try:
        if link.is_symlink():
            return link.resolve() == target.resolve()
        # Not a symlink: a hard link (file) or junction (dir) references the
        # same underlying file as ``target`` and ``samefile`` proves it. A
        # broken/missing path or a distinct copy falls through to False.
        if not link.exists():
            return False
        return link.samefile(target)
    except OSError:
        return False


def _is_replaceable_link(link: Path) -> bool:
    """True if ``link`` is a stale hard link we may safely replace.

    Only meaningful on Windows, where our file installs are hard links rather
    than symlinks. A hard link has more than one name (``st_nlink > 1``); a
    standalone real file the user owns has exactly one, so we never clobber it.
    """
    if sys.platform != "win32":
        return False
    try:
        return not link.is_dir() and link.stat().st_nlink > 1
    except OSError:
        return False


def _is_junction(link: Path) -> bool:
    """True if ``link`` is a Windows directory junction (mount-point reparse point).

    Python reports a junction as a *directory*, not a symlink (``is_symlink()``
    is False), so it must be identified by its reparse tag. ``st_reparse_tag`` is
    populated on Windows lstat results (Python 3.8+); it is absent elsewhere.
    """
    if sys.platform != "win32":
        return False
    try:
        tag = getattr(link.lstat(), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0)


def _is_link_node(link: Path) -> bool:
    """True if ``link`` is any link we may safely replace: a symlink (any target),
    a Windows junction, or a stale hard link we created. A standalone real
    file/dir returns False so we never clobber genuine content."""
    return link.is_symlink() or _is_junction(link) or _is_replaceable_link(link)


def _remove_link_node(link: Path) -> None:
    """Remove a link node (symlink / junction / hard link) without touching its
    target. Directory links (junctions and dir symlinks) need ``rmdir``; file
    links need ``unlink``. ``rmdir`` unlinks the reparse point only — it never
    recurses into or deletes the target's contents."""
    try:
        os.rmdir(link)  # junctions and directory symlinks
    except OSError:
        link.unlink()  # file symlink / hard link


def make_link(src: Path, dst: Path, *, is_dir: bool) -> str:
    """Create ``dst`` -> ``src``. Returns a short status word for reporting.

    Idempotent: an existing correct link is left as-is (``"ok"``). A stale or
    foreign symlink, or a stale hard link we created, is replaced. Refuses to
    clobber a real (non-link) file/dir.

    Returns one of: ``"ok"``, ``"linked"``, ``"hardlinked"``, ``"junction"``,
    ``"copied"``.
    """
    if is_our_link(dst, src):
        return "ok"
    if dst.exists() or dst.is_symlink():
        if _is_link_node(dst):
            # stale/foreign symlink, stale junction, or stale hard link -> replace
            _remove_link_node(dst)
        elif dst.is_file():
            # A plain regular file at a path we own — a copy-fallback artifact
            # (Windows/cross-volume, where neither symlink nor hard link is
            # possible) or a drifted older copy. Refreshing a single file in
            # place is the intended reinstall/upgrade behavior. Directories are
            # never blindly removed below (they may hold unrelated user content).
            dst.unlink()
        else:
            raise FileExistsError(
                f"refusing to overwrite existing non-link path: {dst}"
            )
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.symlink_to(src, target_is_directory=is_dir)
        return "linked"
    except (OSError, NotImplementedError):
        # Windows without symlink privilege: junction for dirs; hard link for
        # files (a junction cannot target a file). Both keep edits live.
        if sys.platform == "win32" and is_dir:
            import subprocess
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                check=True, capture_output=True,
            )
            return "junction"
        try:
            os.link(src, dst)  # NTFS hard link — no privilege required
            return "hardlinked"
        except (OSError, NotImplementedError):
            # Cross-volume or unsupported filesystem: last-resort copy.
            import shutil
            shutil.copy2(src, dst)
            return "copied"


def unlink_if_ours(link: Path, target: Path) -> bool:
    """Remove ``link`` iff it is our symlink to ``target``. Returns True if removed."""
    if is_our_link(link, target):
        _remove_link_node(link)  # junction-safe: dir junctions need rmdir, not unlink
        return True
    return False


def count_dead_links(directory: Path) -> int:
    """Count symlinks in ``directory`` whose target no longer exists.

    A local (venv) install links ``.claude/skills`` and ``.claude/agents`` into
    ``<venv>/.../aah``. Deleting ``.venv`` strands those links. This surfaces the
    strand so ``aah status`` can flag it (re-run ``aah install --project`` after
    rebuilding the venv to re-link).
    """
    if not directory.exists():
        return 0
    dead = 0
    for entry in directory.iterdir():
        # exists() follows the link; a symlink or junction whose target is gone
        # reports False -> it is a stranded link.
        if (entry.is_symlink() or _is_junction(entry)) and not entry.exists():
            dead += 1
    return dead
