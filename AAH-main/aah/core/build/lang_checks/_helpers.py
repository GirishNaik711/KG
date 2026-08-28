"""Small shared helpers for the lang_checks adapter pack."""

from __future__ import annotations

import os
import re
from pathlib import Path


_VENV_COMMAND_RE = re.compile(
    r"^(?:\./)?\.?venv[\\/](?:bin|Scripts)[\\/](.+?)(?:\.exe)?$",
    re.IGNORECASE,
)
# Bare interpreters and test tools that must resolve through uv, not the
# ambient PATH (which may be a system Python with none of the project's deps).
_INTERPRETERS = frozenset({"python", "python3", "py"})
_TEST_TOOLS = frozenset({"pytest"})


def _uv_python() -> str:
    """Interpreter uv should resolve for a rewritten command.

    Mirrors ``PythonAdapter._uv_run`` (``python.py``): pin a version that ships
    wheels so uv doesn't pick the newest installed CPython (3.14/3.15), for
    which some pinned deps have no wheels and fail an in-place source build.
    """
    return os.environ.get("AAH_UV_PYTHON", "3.12")


def rewrite_bare_venv_command(command: str) -> str:
    """Route a test command through ``uv run`` so its interpreter and deps
    resolve via uv instead of the ambient PATH.

    Rewrites the command when its first token is:
      * a ``.venv/bin|Scripts/<tool>`` path (worktrees have no ``.venv``), or
      * a bare interpreter (``python``, ``python3``, ``py``; optional ``.exe``), or
      * a bare test tool (``pytest``).

    The uv interpreter is pinned via ``--python`` (see :func:`_uv_python`), and
    ``--with pytest`` is added when the command runs pytest (``pytest`` tool or
    ``-m pytest``). The interpreter itself is supplied by uv, so a ``python``
    token never becomes ``--with python``. Any other first token — ``npm test``,
    ``go test``, an env-var-prefixed command — is returned unchanged.
    """
    first, _, rest = command.partition(" ")
    match = _VENV_COMMAND_RE.match(first)
    if match:
        tool = match.group(1)
    else:
        tool = first
    # Normalize a trailing ``.exe`` on a bare token (e.g. ``python.exe``).
    tool_name = tool[:-4] if tool.lower().endswith(".exe") else tool
    lowered = tool_name.lower()

    is_interpreter = lowered in _INTERPRETERS
    is_test_tool = lowered in _TEST_TOOLS
    if not match and not is_interpreter and not is_test_tool:
        return command

    argv = ["uv", "run", "--python", _uv_python()]
    # pytest must be present in the ephemeral env; the interpreter comes from
    # uv, so never ``--with python``.
    if is_test_tool or "-m pytest" in rest:
        argv += ["--with", "pytest"]
    argv.append(tool_name)
    prefix = " ".join(argv)
    return f"{prefix} {rest}" if rest else prefix


def path_to_python_module(file_path: str | Path) -> str:
    """Convert a Python file path to a module path.

    Examples:
        src/db/connection.py -> src.db.connection
        scripts/__init__.py -> scripts (package itself)
        a/b.py -> a.b

    Mirrors the convention used by aah-runtime-validator.md step 2 prose
    so the migrated module-validation_command produces the same import
    statements the agent used to compose by hand.
    """
    p = Path(file_path)
    parts = list(p.parts)
    # Drop trailing ``__init__.py`` -> import the package itself.
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def manifest_stack_primary(manifest: dict) -> str:
    """Lowercased ``stack_choices.primary`` or empty string if unset."""
    return str(
        (manifest or {}).get("stack_choices", {}).get("primary", "") or ""
    ).lower()


def manifest_stack_field(manifest: dict, field: str) -> str | None:
    """Return ``stack_choices.<field>`` from the manifest, or None.

    Used for optional overrides like ``start_command``, ``test_command``
    that adapters consult before falling back to defaults.
    """
    val = (manifest or {}).get("stack_choices", {}).get(field)
    if isinstance(val, str) and val.strip():
        return val
    return None
