"""Tests for ``discover_package_roots`` — the one discovery function that
drives BOTH lint-config placement and standards-gate scope.

NO MOCKS: real directory trees under pytest ``tmp_path``.
"""

from __future__ import annotations

from aah.core.build.lang_checks import discover_package_roots


def _names(tmp_path, roots):
    return [
        ("." if root == tmp_path.resolve() else root.relative_to(tmp_path.resolve()).as_posix(), stack)
        for root, stack in roots
    ]


class TestFullstackLayout:
    def test_backend_and_frontend_each_get_their_own_adapter(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='b'\n")
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text("{}")

        assert _names(tmp_path, discover_package_roots(tmp_path)) == [
            ("backend", "python"),
            ("frontend", "node"),
        ]

    def test_order_is_deterministic(self, tmp_path):
        for name, fname, body in (
            ("zeta", "package.json", "{}"),
            ("alpha", "pyproject.toml", "[project]\n"),
            ("mid", "go.mod", "module m\n"),
        ):
            (tmp_path / name).mkdir()
            (tmp_path / name / fname).write_text(body)

        first = discover_package_roots(tmp_path)
        assert _names(tmp_path, first) == [
            ("alpha", "python"), ("mid", "go"), ("zeta", "node"),
        ]
        assert first == discover_package_roots(tmp_path)


class TestSkipRules:
    def test_node_modules_is_never_descended(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        dep = tmp_path / "node_modules" / "left-pad"
        dep.mkdir(parents=True)
        (dep / "package.json").write_text("{}")

        assert _names(tmp_path, discover_package_roots(tmp_path)) == [(".", "node")]

    def test_venv_dist_and_dot_dirs_are_skipped(self, tmp_path):
        for name in (".venv", "dist", ".git", ".aah", "build", "__pycache__"):
            d = tmp_path / name
            d.mkdir()
            (d / "pyproject.toml").write_text("[project]\n")

        assert discover_package_roots(tmp_path) == []


class TestAncestorShadowing:
    def test_workspace_root_shadows_same_language_members(self, tmp_path):
        (tmp_path / "package.json").write_text('{"workspaces": ["packages/*"]}')
        member = tmp_path / "packages" / "web"
        member.mkdir(parents=True)
        (member / "package.json").write_text("{}")

        assert _names(tmp_path, discover_package_roots(tmp_path)) == [(".", "node")]

    def test_different_language_under_an_accepted_root_is_kept(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        api = tmp_path / "api"
        api.mkdir()
        (api / "pyproject.toml").write_text("[project]\n")

        assert _names(tmp_path, discover_package_roots(tmp_path)) == [
            (".", "node"), ("api", "python"),
        ]


class TestDepth:
    def test_depth_two_is_reached(self, tmp_path):
        pkg = tmp_path / "apps" / "web"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text("{}")

        assert _names(tmp_path, discover_package_roots(tmp_path)) == [("apps/web", "node")]

    def test_depth_three_is_not_reached(self, tmp_path):
        pkg = tmp_path / "a" / "b" / "c"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text("{}")

        assert discover_package_roots(tmp_path) == []


class TestEmptyTree:
    def test_empty_tree_returns_empty_list(self, tmp_path):
        assert discover_package_roots(tmp_path) == []

    def test_missing_path_returns_empty_list(self, tmp_path):
        assert discover_package_roots(tmp_path / "nope") == []
