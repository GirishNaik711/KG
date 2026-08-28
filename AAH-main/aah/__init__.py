"""aah — ascend-agentic-harness.

The umbrella package for the AAH delivery framework. The deterministic
Python modules live under :mod:`aah.core`, skills under :mod:`aah.skills`,
agents under ``aah/agents``, and host-agnostic hook definitions under
:mod:`aah.hooks`. The ``aah`` CLI (``aah run`` / ``install`` / ``uninstall`` /
``status``) is defined in :mod:`aah.cli`.
"""

def _resolve_version() -> str:
    """Resolve the aah version.

    Single source of truth: ``[project] version`` in ``pyproject.toml``.

    - Editable install / running from the repo checkout: a ``pyproject.toml``
      sits next to the package source, so we read it live — the version tracks
      a bump immediately, no reinstall needed. The ``name == "aah"`` guard means
      a stray ``pyproject.toml`` from an unrelated project can never be picked up.
    - Real wheel install (site-packages, no adjacent pyproject): fall back to the
      package metadata that hatchling stamped from pyproject.toml at build time.
    """
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project", {})
            if project.get("name") == "aah":
                return project["version"]
        except Exception:
            pass  # fall through to installed metadata

    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        return _pkg_version("aah")
    except PackageNotFoundError:  # no install and no readable pyproject
        return "0.0.0+unknown"


__version__ = _resolve_version()
