"""Tests for the install link helpers (symlink / junction handling).

The junction-specific branches only execute on Windows; the symlink replace and
refuse behavior is exercised on every platform since it now routes through the
same ``_is_link_node`` / ``_remove_link_node`` helpers.
"""

import os
import sys

import pytest

from aah.core.install import linkutil


class TestMakeLink:
    def test_creates_symlink_to_target(self, tmp_path):
        src = tmp_path / "pkg" / "aah"
        src.mkdir(parents=True)
        dst = tmp_path / "home" / "skills" / "aah"

        status = linkutil.make_link(src, dst, is_dir=True)

        assert status in {"linked", "junction", "copied"}
        assert dst.exists()

    def test_idempotent_existing_correct_link_is_ok(self, tmp_path):
        src = tmp_path / "pkg" / "aah"
        src.mkdir(parents=True)
        dst = tmp_path / "home" / "skills" / "aah"
        first = linkutil.make_link(src, dst, is_dir=True)

        second = linkutil.make_link(src, dst, is_dir=True)

        # A real symlink/junction is recognised as ours and left alone. A copy
        # fallback (no link support) is re-copied rather than reporting "ok".
        if first == "copied":
            assert second == "copied"
        else:
            assert second == "ok"

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="symlinks unsupported on this platform"
    )
    def test_replaces_foreign_symlink(self, tmp_path):
        src = tmp_path / "pkg" / "aah"
        src.mkdir(parents=True)
        other = tmp_path / "somewhere_else"
        other.mkdir()
        dst = tmp_path / "home" / "skills" / "aah"
        dst.parent.mkdir(parents=True)
        try:
            os.symlink(other, dst, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("no symlink privilege on this host")

        status = linkutil.make_link(src, dst, is_dir=True)

        assert status in {"linked", "junction", "copied"}
        assert linkutil.is_our_link(dst, src)

    def test_refreshes_stale_file_copy_not_refused(self, tmp_path):
        # Simulates the Windows/cross-volume copy fallback: dst is a plain file
        # (st_nlink == 1), not a link. Reinstalling an updated version must
        # refresh it, not raise FileExistsError.
        src = tmp_path / "pkg" / "aah-feature-implementer.md"
        src.parent.mkdir(parents=True)
        src.write_text("NEW agent definition")
        dst = tmp_path / "home" / "agents" / "aah-feature-implementer.md"
        dst.parent.mkdir(parents=True)
        dst.write_text("OLD drifted copy")

        status = linkutil.make_link(src, dst, is_dir=False)

        assert status in {"linked", "hardlinked", "copied"}
        assert dst.read_text() == "NEW agent definition"

    def test_refuses_to_overwrite_real_directory(self, tmp_path):
        src = tmp_path / "pkg" / "aah"
        src.mkdir(parents=True)
        dst = tmp_path / "home" / "skills" / "aah"
        dst.mkdir(parents=True)
        (dst / "real_user_file.txt").write_text("keep me")

        with pytest.raises(FileExistsError):
            linkutil.make_link(src, dst, is_dir=True)

        # The guard must not have touched the real content.
        assert (dst / "real_user_file.txt").read_text() == "keep me"


class TestJunctionHelpers:
    def test_is_junction_false_off_windows(self, tmp_path):
        if sys.platform == "win32":
            pytest.skip("non-Windows behavior only")
        d = tmp_path / "d"
        d.mkdir()
        assert linkutil._is_junction(d) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
    def test_stale_junction_is_replaced_not_refused(self, tmp_path):
        import subprocess

        src = tmp_path / "pkg" / "aah"
        src.mkdir(parents=True)
        old_target = tmp_path / "old_pkg"
        old_target.mkdir()
        dst = tmp_path / "home" / "skills" / "aah"
        dst.parent.mkdir(parents=True)
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(old_target)],
            check=True, capture_output=True,
        )
        assert linkutil._is_junction(dst)

        # A stale junction (points at old_target, not src) must be re-pointed,
        # never raise FileExistsError.
        linkutil.make_link(src, dst, is_dir=True)

        assert linkutil.is_our_link(dst, src)
        # Removing/replacing the junction must not delete the old target's files.
        assert old_target.exists()
