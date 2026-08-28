"""Readers for the three source-of-truth directories.

``aah/skills``, ``aah/agents`` and ``aah/hooks`` hold *data only* — skill dirs,
agent ``.md`` files, ``aah-hooks.yaml`` and ``settings-defaults.json``. This
module reads them; it knows nothing about any host platform. Adapters render
this raw data into their own on-disk formats.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def package_root() -> Path:
    """Directory of the installed ``aah`` package (holds skills/, agents/, hooks/)."""
    # aah/core/install/source.py -> parents[2] == aah/
    return Path(__file__).resolve().parents[2]


def skills_src() -> Path:
    return package_root() / "skills"


def agents_src() -> Path:
    return package_root() / "agents"


def hooks_dir() -> Path:
    return package_root() / "hooks"


def skill_dirs() -> list[Path]:
    """Every skill directory under ``aah/skills`` (sorted)."""
    src = skills_src()
    if not src.exists():
        return []
    return sorted(p for p in src.iterdir() if p.is_dir())


def agent_files() -> list[Path]:
    """Every agent ``.md`` under ``aah/agents`` (sorted)."""
    src = agents_src()
    if not src.exists():
        return []
    return sorted(src.glob("*.md"))


def load_hooks_spec() -> dict:
    """Load the host-agnostic hook definitions from ``aah-hooks.yaml``."""
    with (hooks_dir() / "aah-hooks.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings_defaults(platform: str) -> dict:
    """Load a platform's default env/permissions block from ``settings-defaults.json``.

    Returns ``{}`` if the file is missing/unreadable or has no block for the
    platform.
    """
    path = hooks_dir() / "settings-defaults.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    block = data.get(platform, {})
    return block if isinstance(block, dict) else {}


def load_mcp_servers() -> dict:
    """Load the ``mcpServers`` block from ``mcp-servers.json`` (source of truth).

    Host-agnostic, mirroring ``load_settings_defaults``: adapters merge the
    returned mapping into their own MCP config location. Returns ``{}`` when the
    file is missing/unreadable or carries no ``mcpServers`` object.
    """
    path = hooks_dir() / "mcp-servers.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = data.get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def hook_command(hook: dict, aah_bin: str = "aah") -> str:
    """Build the command string for one hook entry.

    Two shapes:
    * ``run: <module>``  -> ``<aah_bin> run [--skip-if-delegated] <module> [args]``
      (the common case — dispatch a framework module).
    * ``cli: <subcmd>``  -> ``<aah_bin> <subcmd> [args]`` (a top-level `aah`
      subcommand such as ``setup``; ``--skip-if-delegated`` does not apply).

    Platform-neutral: every supported host invokes hooks through the same
    ``aah`` CLI, so only the surrounding container shape differs per adapter.

    ``aah_bin`` defaults to the bare ``aah`` (found on PATH — correct for a
    global uv-tool install). For a local (venv) project install, the adapter
    passes an absolute ``<venv>/bin/aah`` so hooks work whether or not the venv
    is active (see ``venv_aah_bin``). That absolute path can contain spaces (e.g.
    a project under ``My Project/.venv/bin/aah``), so it is shell-quoted; bare
    ``aah`` is left untouched by ``shlex.quote``.
    """
    import shlex

    bin_tok = shlex.quote(aah_bin)
    if hook.get("cli"):
        parts = [bin_tok, hook["cli"]]
    else:
        parts = [bin_tok, "run"]
        if hook.get("skip_if_delegated"):
            parts.append("--skip-if-delegated")
        parts.append(hook["run"])
    cmd = " ".join(parts)
    if hook.get("args"):
        cmd += " " + hook["args"]
    return cmd


def venv_aah_bin(project_dir: Path | None = None) -> str | None:
    """Return the absolute ``aah`` path for a *project-local* venv, else None.

    The local flow (``uv venv`` + ``uv pip install aah`` + ``aah install
    --project``) puts ``aah`` at ``<project>/.venv/bin/aah`` — only on PATH while
    the venv is active — so hook commands must use the absolute path to fire
    regardless of activation.

    A *global* uv-tool install is ALSO technically a venv (its prefix !=
    base_prefix), but it lives outside the project (``~/.local/share/uv/tools``)
    and is on PATH, so bare ``aah`` is correct and portable there. The
    distinguishing test is therefore not "in a venv" but "is the venv inside the
    project dir": only then do we bake an absolute path (into a committable
    settings.json) — and that path is project-relative anyway.

    The bin dir is resolved from ``sys.prefix`` (the venv root), NOT
    ``sys.executable`` — the venv's ``python`` is often a symlink back to the
    base interpreter, so ``.resolve()`` would escape the venv and miss ``aah``.
    Returns None outside a project-local venv.
    """
    import sys

    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return None  # not in a venv at all → bare aah on PATH

    prefix = Path(sys.prefix).resolve()
    # Only treat as local mode when the venv lives under the project dir.
    if project_dir is not None:
        base = project_dir.resolve()
        if base not in prefix.parents and prefix != base:
            return None  # venv is outside the project (e.g. global uv tool) → bare aah

    for bindir in (prefix / "bin", prefix / "Scripts"):
        for name in ("aah", "aah.exe"):
            cand = bindir / name
            if cand.exists():
                return str(cand)
    return None
