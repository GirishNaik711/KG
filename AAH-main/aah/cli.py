#!/usr/bin/env python3
"""
aah — ascend-agentic-harness CLI.

Single binary for the AAH delivery framework. Subcommands:

  aah run <module> [args...]     Dispatch a framework module's main() (replaces `aah-run`)
  aah install   [--platform claude] [--project] [--dev]
                                 Provision a durable `aah` (bootstrapping when launched
                                 ephemerally via uvx), then link via `setup`. The one-liner.
  aah setup     [--platform claude] [--project]
                                 Link/relink skills/agents/hooks only — no provisioning.
                                 Bare `aah setup` auto-detects scope + platform from the
                                 current install. Run it again after adding a skill/agent/hook.
  aah uninstall [--platform claude] [--project] [--purge]
  aah status                     Show what is linked where

One-liner setup (no clone needed):
  uvx --from git+<repo-url> aah install            # user, global (~/.claude)
  uvx --from git+<repo-url> aah install --project  # user, local (project venv + ./.claude)
  uvx --from git+<repo-url> aah install --dev      # developer: clone into cwd + editable install

Module-path shorthand for `run`: a bare path is prefixed with `aah.` when it does
not already start with it, so `aah run core.guards.no_mocks_guard` resolves to
`aah.core.guards.no_mocks_guard`. Fully-qualified paths still work.

Example:
  aah run core.common.manifest read --path /tmp/manifest.yaml
  aah run core.guards.no_mocks_guard
  aah run --skip-if-delegated core.guards.no_mocks_guard
"""

import importlib
import sys
import traceback

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# `aah run` — the module dispatcher (reused from the old aah-run CLI)
# ---------------------------------------------------------------------------

def _claude_retry_hint(module_name: str, error: Exception, tb: str) -> None:
    """
    Write a structured, machine-readable recovery hint to stderr.

    This is intentionally directed at Claude Code, not the user.
    The message is non-blocking (caller exits 0) so the user never sees
    a hard failure — Claude reads the hint from the tool result and
    self-corrects on the next attempt.
    """
    error_type = type(error).__name__

    if isinstance(error, ModuleNotFoundError):
        suggestion = (
            f"The module path '{module_name}' does not exist. "
            "Likely causes: hallucinated subcommand name, module renamed during refactoring, "
            "or a hyphen/underscore typo that the normaliser could not resolve. "
            "Check aah/core/ directory for the correct module path and retry "
            "with the right name. Use underscores, not hyphens."
        )
    elif isinstance(error, AttributeError) and "has no attribute 'main'" in str(error):
        suggestion = (
            f"Module '{module_name}' was imported successfully but has no main() function. "
            "You may be targeting a helper/library module instead of a CLI module. "
            "Find the correct entrypoint module and retry."
        )
    elif isinstance(error, (TypeError, AttributeError)):
        suggestion = (
            f"A TypeError or AttributeError occurred inside '{module_name}'. "
            "This is likely caused by passing the wrong argument type or calling a function "
            "that does not exist on the object. Analyse the traceback below, fix the argument "
            "or logic, and retry."
        )
    elif isinstance(error, (KeyError, IndexError)):
        suggestion = (
            f"A KeyError or IndexError occurred inside '{module_name}'. "
            "A required key or index is missing — the input data or arguments passed "
            "may be structured differently than the module expects. "
            "Analyse the traceback and adjust the call."
        )
    elif isinstance(error, ValueError) and "rejected by argparse" in str(error):
        suggestion = (
            f"Wrong subcommand or argument passed to '{module_name}'. "
            "The usage output above lists every valid subcommand and required argument. "
            "Pick the correct subcommand and retry with the exact spelling shown."
        )
    else:
        suggestion = (
            f"An unexpected error occurred inside '{module_name}'. "
            "Analyse the traceback below to understand the root cause, "
            "then fix the arguments or logic and retry."
        )

    print(
        f"\n[AAH-CLI] Auto-recoverable error — Claude Code: analyse and retry\n"
        f"  Module   : {module_name}\n"
        f"  Error    : {error_type}: {error}\n"
        f"  Suggestion: {suggestion}\n"
        f"\n"
        f"  Traceback:\n"
        + "".join(f"    {line}" for line in tb.splitlines(keepends=True)),
        file=sys.stderr,
    )


def _normalize_module(module_name: str) -> str:
    """Apply `run`'s module-path conventions.

    1. Hyphens -> underscores (so `core.intake.resolve-constraints` works).
    2. Prefix `aah.` when the path does not already start with `aah.` — this is
       the `core.<x>` shorthand for `aah.core.<x>`. Fully-qualified `aah....`
       paths pass through unchanged.
    """
    module_name = module_name.replace("-", "_")
    if not (module_name == "aah" or module_name.startswith("aah.")):
        module_name = "aah." + module_name
    return module_name


def _print_run_usage() -> None:
    print("Usage: aah run <module> [args...]")
    print()
    print("Examples:")
    print("  aah run core.common.manifest read")
    print("  aah run core.guards.no_mocks_guard")
    print("  aah run core.plan.build_dag")


def run(argv: list[str]) -> None:
    """`aah run` — import a framework module and call its main().

    `argv` is everything after `run` (i.e. sys.argv[2:]).
    """
    # Mark the environment so cloud validation scripts know they were
    # launched through the supported entry point. See common.runguard.
    import os
    os.environ["AAH_RUN"] = "1"
    os.environ["AAH_RUN"] = "1"

    if not argv:
        _print_run_usage()
        sys.exit(1)

    is_hook_call = False  # Tracks whether this is a guard/hook invocation
    if argv[0] == "--skip-if-delegated":
        is_hook_call = True
        argv = argv[1:]
        # Decide whether to skip WITHOUT letting the intentional skip get
        # swallowed. The inner try guards only the manifest lookup/parse; the
        # sys.exit(0) that implements the skip must escape (SystemExit is not
        # caught here), otherwise the hook falls through and runs the module
        # even when there is no AAH project in context (the "No AAH
        # project found." SessionStart noise in the framework root).
        skip = False
        try:
            from aah.core.common.manifest import find_manifest, load_manifest
            manifest_path = find_manifest()
            if manifest_path is None:
                # No AAH project in context — guards have nothing to check.
                skip = True
            else:
                m = load_manifest(manifest_path)
                if m.get("execution_mode") == "delegated":
                    skip = True
        except Exception:
            # Manifest missing/unreadable/malformed — treat as "nothing to
            # check" rather than erroring out of a non-blocking hook.
            skip = True
        if skip:
            sys.exit(0)

    if not argv:
        _print_run_usage()
        sys.exit(1)

    module_name = argv[0]

    if module_name in ("-h", "--help"):
        _print_run_usage()
        sys.exit(0)

    module_name = _normalize_module(module_name)

    # Present clean args to the target module: argv[0] = module name, rest = its args
    sys.argv = [module_name] + argv[1:]

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        _claude_retry_hint(module_name, e, traceback.format_exc())
        sys.exit(0)  # Non-blocking — Claude reads the hint and retries

    if not hasattr(module, "main"):
        err = AttributeError(f"module '{module_name}' has no main() function")
        _claude_retry_hint(module_name, err, "")
        sys.exit(0)

    try:
        module.main()
    except SystemExit as se:
        # Hook calls (--skip-if-delegated): always propagate — exit(2) is an intentional guard block.
        # Direct CLI calls: exit(2) from argparse means wrong args (hallucination/typo) — treat as
        # recoverable and emit a hint instead of hard-failing the user's session.
        if is_hook_call or se.code in (0, 1):
            raise
        # exit(2) on a direct CLI call = argparse rejection = wrong subcommand or args
        err = ValueError(
            f"Command rejected by argparse (exit code {se.code}). "
            f"Check the usage output above for valid subcommands and arguments."
        )
        _claude_retry_hint(module_name, err, "")
        sys.exit(0)
    except Exception as e:
        _claude_retry_hint(module_name, e, traceback.format_exc())
        sys.exit(0)  # Non-blocking — Claude reads the hint and retries


# ---------------------------------------------------------------------------
# `aah install` / `uninstall` / `status` — see aah.core.install
# ---------------------------------------------------------------------------

def _cmd_install(argv: list[str]) -> None:
    from aah.core.install.driver import install as _install
    _install(argv)


def _cmd_setup(argv: list[str]) -> None:
    from aah.core.install.driver import setup as _setup
    _setup(argv)


def _cmd_uninstall(argv: list[str]) -> None:
    from aah.core.install.driver import uninstall as _uninstall
    _uninstall(argv)


def _cmd_status(argv: list[str]) -> None:
    from aah.core.install.driver import status as _status
    _status(argv)


def _cmd_path(argv: list[str]) -> None:
    """Print the absolute path to the aah package directory."""
    from pathlib import Path
    import aah as _aah_pkg
    print(Path(_aah_pkg.__file__).parent)


def _cmd_version(argv: list[str]) -> None:
    """Report the installed aah version and, for each scope, what `aah setup`
    last linked (read from the ``_aah`` provenance marker in settings.json)."""
    import aah as _aah_pkg
    installed = _aah_pkg.__version__
    print(f"aah v{installed}  (installed)")

    try:
        from aah.core.install import registry
        from aah.core.install.formats import jsonconf
        adapter = registry.get("claude")
    except Exception:
        return  # install machinery unavailable — the version line above is enough

    linked_any = False
    for scope, project in (("global", False), ("project", True)):
        try:
            sp = adapter.config_root(project=project) / "settings.json"
            marker = jsonconf.read(sp).get(jsonconf.MARKER, {})
        except Exception:
            continue
        ver = marker.get("version")
        if not ver:
            continue
        linked_any = True
        print()
        print(f"Linked configuration ({scope})")
        print(f"  Path         {sp}")
        print(f"  AAH version  {ver}")
        print(f"  Linked at    {jsonconf.fmt_linked_at(marker.get('linked_at', ''))}")
        if ver != installed:
            print(f"  Status       out of date — installed is v{installed}; run `aah setup` to relink")
        else:
            print("  Status       up to date")

    if not linked_any:
        print()
        print("No linked configuration found — run `aah setup` to link this project or your user profile.")


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def _print_usage() -> None:
    print("Usage: aah <command> [args...]")
    print()
    print("Commands:")
    print("  run <module> [args...]   Dispatch a framework module (was `aah-run`)")
    print("  install                  Provision a durable aah + link (--platform, --project, --dev)")
    print("  setup                    Link/relink skills/agents/hooks only (auto-detects scope+platform)")
    print("  uninstall                Remove links created by install (--purge)")
    print("  status                   Show what is linked where")
    print("  path                     Print the aah package directory path")
    print("  version                  Print the installed aah version")


def main() -> None:
    if len(sys.argv) < 2:
        _print_usage()
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd in ("-h", "--help"):
        _print_usage()
        sys.exit(0)
    if cmd in ("-V", "--version"):
        _cmd_version(rest)
        sys.exit(0)
    if cmd == "run":
        run(rest)
    elif cmd == "install":
        _cmd_install(rest)
    elif cmd == "setup":
        _cmd_setup(rest)
    elif cmd == "uninstall":
        _cmd_uninstall(rest)
    elif cmd == "status":
        _cmd_status(rest)
    elif cmd == "path":
        _cmd_path(rest)
    elif cmd == "version":
        _cmd_version(rest)
    else:
        print(f"error: unknown command '{cmd}'", file=sys.stderr)
        print()
        _print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
