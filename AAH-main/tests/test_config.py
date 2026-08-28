"""Tests for aah.core.common.config — folder=project resolution."""

from pathlib import Path

import pytest

from aah.core.common.config import (
    get_active_project_aah_path,
    require_project_path,
    resolve_framework_root,
    resolve_project_path,
)


def _make_project(root: Path) -> Path:
    """Create a minimal AAH project anchor (.aah/manifest.yaml) at root."""
    aah_root = root / ".aah"
    aah_root.mkdir(parents=True, exist_ok=True)
    (aah_root / "manifest.yaml").write_text("project_name: test\n", encoding="utf-8")
    return root


class TestResolveFrameworkRoot:
    def test_returns_aah_package_dir(self):
        root = resolve_framework_root()
        assert root is not None
        # Package-relative: the aah/ dir holding core/ and the asset dirs.
        assert (root / "core").is_dir()
        assert root.name == "aah"

    def test_ignores_config_args(self):
        # Legacy signature accepts config/config_path but must ignore them.
        assert resolve_framework_root({"framework_root": "/bogus"}, Path("/bogus")) == resolve_framework_root()


class TestResolveProjectPath:
    def test_explicit_path_with_manifest(self, tmp_path):
        _make_project(tmp_path)
        assert resolve_project_path(tmp_path) == tmp_path

    def test_explicit_path_pointing_at_aah_dir(self, tmp_path):
        _make_project(tmp_path)
        assert resolve_project_path(tmp_path / ".aah") == tmp_path

    def test_explicit_path_without_manifest_falls_through(self, tmp_path, monkeypatch):
        # No manifest at explicit path and none from cwd → None.
        monkeypatch.chdir(tmp_path)
        assert resolve_project_path(tmp_path / "nope") is None

    def test_walks_up_from_cwd(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        assert resolve_project_path() == tmp_path

    def test_resolves_at_depth_zero(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert resolve_project_path() == tmp_path

    def test_returns_none_when_no_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_project_path() is None


class TestGetActiveProjectRapidsPath:
    def test_returns_aah_dir(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert get_active_project_aah_path() == tmp_path / ".aah"

    def test_none_when_no_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_active_project_aah_path() is None


class TestRequireProjectPath:
    def test_returns_path_when_found(self, tmp_path, monkeypatch):
        _make_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert require_project_path() == tmp_path

    def test_exits_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            require_project_path()
