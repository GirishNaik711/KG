"""`aah install` / `uninstall` / `status` — the platform-agnostic driver.

Parses args, resolves the adapter from the registry, runs it, and renders the
returned reports. Contains NO host-specific logic — every platform detail lives
in its adapter (``adapters/*.py``). The three source dirs (``aah/skills``,
``aah/agents``, ``aah/hooks``) are the single source of truth; adapters render
them into each host's format.
"""

from __future__ import annotations

import sys

from . import registry
from .adapter import InstallCtx


# ---------------------------------------------------------------------------
# Argument parsing (shared by install/uninstall)
# ---------------------------------------------------------------------------

class _Args:
    """Parsed args. ``platform is None`` / ``project_explicit is False`` mean the
    value was NOT given on the command line — ``setup`` then auto-detects it."""

    def __init__(self, platform: str | None, project: bool, dev: bool,
                 project_explicit: bool, quiet: bool = False):
        self.platform = platform              # None => not given (setup detects)
        self.project = project                # requested project scope
        self.dev = dev                        # install-only: editable clone bootstrap
        self.project_explicit = project_explicit
        self.quiet = quiet                    # setup: suppress per-line output (hook use)


def _parse_args(argv: list[str], *, allow_dev: bool) -> _Args:
    """Parse install/setup/uninstall args.

    Accepts ``--platform X`` / ``--platform=X`` / a bare positional platform,
    plus ``--project``. ``--dev`` is only valid where ``allow_dev`` (install).
    The platform is left as ``None`` when not given so ``setup`` can auto-detect.
    """
    platform: str | None = None
    project = False
    dev = False
    quiet = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            _print_usage()
            sys.exit(0)
        elif arg == "--project":
            project = True
            i += 1
        elif arg == "--quiet":
            quiet = True
            i += 1
        elif arg == "--dev":
            if not allow_dev:
                print("error: --dev is only valid for `aah install`", file=sys.stderr)
                sys.exit(1)
            dev = True
            i += 1
        elif arg.startswith("--platform="):
            platform = arg.split("=", 1)[1]
            i += 1
        elif arg == "--platform":
            if i + 1 >= len(argv):
                print("error: --platform requires a value", file=sys.stderr)
                sys.exit(1)
            platform = argv[i + 1]
            i += 2
        elif arg.startswith("-"):
            print(f"error: unknown option '{arg}'", file=sys.stderr)
            sys.exit(1)
        else:
            platform = arg
            i += 1
    if platform is not None and platform not in registry.names():
        print(
            f"error: unsupported platform '{platform}'. "
            f"supported: {', '.join(registry.names())}",
            file=sys.stderr,
        )
        sys.exit(1)
    return _Args(platform, project, dev, project_explicit=project, quiet=quiet)


def _print_usage() -> None:
    plats = "|".join(registry.names())
    print(f"Usage: aah install [--platform {plats}] [--project] [--dev]")
    print(f"       aah setup   [--platform {plats}] [--project] [--quiet]")
    print(f"       aah uninstall [--platform {plats}] [--project] [--purge]")
    print( "       aah status")
    print()
    print("  install    Provision a durable `aah`, then link (the one-liner).")
    print("             --dev = clone the harness into cwd + editable install.")
    print("  setup      Link/relink skills/agents/hooks only. Bare `aah setup`")
    print("             auto-detects scope + platform from the current install.")
    print("  uninstall  Remove links (--purge also removes the aah uv tool).")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _render_reports(adapter, ctx: InstallCtx, quiet: bool = False) -> int:
    """Run one adapter's install (linking). Render report lines unless ``quiet``.

    Returns the number of newly-linked items (0 when everything was already
    current) so a quiet caller can decide whether to say anything at all.
    """
    if not quiet:
        root = adapter.config_root(project=ctx.project, project_dir=ctx.project_dir)
        scope = "project" if ctx.project else "user-global"
        print(f"==> linking  (platform={adapter.name}, scope={scope})")
        print(f"    host config: {root}")
    newly_linked = 0
    for rep in adapter.install(ctx):
        if rep.kind in ("skills", "agents"):
            newly_linked += rep.linked
        if quiet:
            continue
        if rep.note.startswith("deferred") or rep.note.startswith("SKIPPED"):
            print(f"    {rep.kind}: {rep.note}")
        elif rep.kind in ("skills", "agents"):
            print(f"    {rep.kind}: {rep.linked} linked, {rep.current} already current -> {rep.note}")
        elif rep.kind == "hooks":
            print(f"    hooks: {rep.linked} events -> {rep.note}")
        elif rep.kind == "permissions":
            print(f"    permissions: {rep.note}")
        elif rep.kind == "mcp":
            print(f"    mcp: {rep.linked} server(s) -> {rep.note}")
        else:
            print(f"    {rep.kind}: {rep.note}")
    return newly_linked


def install(argv: list[str]) -> None:
    """Provision a durable ``aah``, then link via ``setup`` (the one-liner)."""
    args = _parse_args(argv, allow_dev=True)

    # Self-persisting bootstrap: when launched ephemerally (uvx --from …) or with
    # --dev, first create a DURABLE install, then re-exec `aah setup` on that
    # durable binary to link — otherwise links would point into uv's ephemeral
    # cache and rot. A durable, non-dev invocation falls through and links here.
    from . import bootstrap

    platform = args.platform or registry.DEFAULT
    try:
        if bootstrap.maybe_bootstrap(project=args.project, dev=args.dev, platform=platform):
            return  # bootstrap provisioned durably and ran `aah setup`
    except Exception as e:
        print(f"error: bootstrap failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Already durable and no --dev: provisioning is a no-op, so just link.
    _render_reports(registry.get(platform), InstallCtx(project=args.project))
    print("==> done.")


def setup(argv: list[str]) -> None:
    """Link/relink skills/agents/hooks. No provisioning.

    Bare ``aah setup`` auto-detects both axes from the current install:
      * scope   — project when the running ``aah`` is a project-local venv
                  (its prefix is under CWD), else global.
      * platform — every host that already has aah-managed links in that scope;
                  or ``claude`` when none are linked yet.
    Explicit ``--project`` / ``--platform`` override the respective detection.
    """
    args = _parse_args(argv, allow_dev=False)

    # Guard: linking from an ephemeral uvx cache would create dangling symlinks.
    # In --quiet (hook) mode, stay silent and exit 0 — a hook must never make an
    # ephemeral session fail; there is simply nothing durable to relink.
    from . import bootstrap
    if bootstrap.running_from_cache():
        if args.quiet:
            return
        print(
            "error: `aah setup` cannot run from an ephemeral uvx cache — the links\n"
            "       would dangle when the cache is pruned. Run `aah install` to\n"
            "       bootstrap a durable install (it links for you), or run `aah\n"
            "       setup` from a durable install.",
            file=sys.stderr,
        )
        sys.exit(1)

    project = args.project if args.project_explicit else _detect_project_scope()
    platforms = ([args.platform] if args.platform
                 else _detect_linked_platforms(project) or [registry.DEFAULT])

    total_new = 0
    for name in platforms:
        total_new += _render_reports(registry.get(name), InstallCtx(project=project),
                                     quiet=args.quiet)
    if args.quiet:
        # Hook mode: one terse line only when something actually changed.
        if total_new:
            print(f"aah setup: linked {total_new} new item(s) "
                  f"({', '.join(platforms)}, {'project' if project else 'global'})")
    else:
        print("==> done.")


def _detect_project_scope() -> bool:
    """True (project) when the running ``aah`` is a project-local venv under CWD."""
    from .source import venv_aah_bin
    from pathlib import Path
    return venv_aah_bin(Path.cwd()) is not None


def _detect_linked_platforms(project: bool) -> list[str]:
    """Registered platforms that already have aah-managed links in this scope."""
    linked: list[str] = []
    for name in registry.names():
        adapter = registry.get(name)
        row = adapter.status(InstallCtx(project=project))
        if row.skills_linked > 0 or row.managed == "managed":
            linked.append(name)
    return linked


def uninstall(argv: list[str]) -> None:
    # --purge additionally removes the `aah` uv tool itself. Handled here (not in
    # _parse_args) so `install` still rejects it as unknown. Supported teardown
    # order: unlink first (from the still-present binary), THEN remove the tool —
    # a bare `uv tool uninstall aah` runs no pre-remove hooks and would strand the
    # links + a hooks block calling a now-missing binary.
    purge = "--purge" in argv
    argv = [a for a in argv if a != "--purge"]

    args = _parse_args(argv, allow_dev=False)
    platform, project = args.platform or registry.DEFAULT, args.project
    adapter = registry.get(platform)
    ctx = InstallCtx(project=project)
    print(f"==> aah uninstall  (platform={platform}{', purge' if purge else ''})")
    for rep in adapter.uninstall(ctx):
        if rep.kind == "links":
            print(f"    removed {rep.linked} links")
        else:
            print(f"    {rep.note}")

    if purge:
        import shutil
        import subprocess
        if shutil.which("uv"):
            print("    purging the aah uv tool (uv tool uninstall aah)")
            result = subprocess.run(["uv", "tool", "uninstall", "aah"], check=False)
            # On Windows, uv cannot delete the running aah.exe's own directory
            # (the executable image is locked while this process runs), so the
            # uninstall returns non-zero after removing everything else. This is
            # expected, not a failure: the residual file is released the moment
            # this shell exits and is overwritten by the next `aah install`.
            # We do NOT spawn a detached self-delete helper — that pattern trips
            # EDR/AV heuristics (self-deletion, hidden shell spawn). Unix unlinks
            # a running binary fine, so this branch only ever matters on Windows.
            if result.returncode != 0 and sys.platform == "win32":
                print("    note: the aah tool is removed; its last file is locked while")
                print("          this shell runs and clears when you close it (or on the")
                print("          next `aah install`). No further action needed.")
        else:
            print("    warning: uv not on PATH — remove the aah tool manually: uv tool uninstall aah",
                  file=sys.stderr)
    print("==> done.")


def status(argv: list[str]) -> None:
    """Report the installed aah version and, per platform+scope, what is linked."""
    import aah as _aah_pkg
    from .source import package_root
    from .formats import jsonconf

    installed = _aah_pkg.__version__
    print(f"aah v{installed}  (installed)")
    print(f"  source   {package_root()}")

    linked_rows = []
    for platform in registry.names():
        for project in (False, True):
            row = registry.get(platform).status(InstallCtx(project=project))
            if row.skills_linked or row.agents_linked or row.managed == "managed":
                linked_rows.append(row)

    if not linked_rows:
        print()
        print("Not linked yet — run `aah setup` to link AAH into this project or your user profile.")
        return

    for row in linked_rows:
        print()
        print(f"{row.platform} · {row.scope}")
        print(f"  Config       {row.root}")
        print(f"  Skills       {row.skills_linked} linked")
        print(f"  Agents       {row.agents_linked} linked")
        print(f"  Hooks        {row.managed}")
        if row.version:
            when = jsonconf.fmt_linked_at(row.linked_at)
            drift = "" if row.version == installed else f"  — out of date (installed v{installed}, run `aah setup`)"
            print(f"  Version      v{row.version}   linked {when}{drift}")
        if row.dead:
            print(f"  Warning      {row.extra}")
