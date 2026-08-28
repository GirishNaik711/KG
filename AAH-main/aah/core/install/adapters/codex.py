"""Codex host adapter.

Codex's config model differs from Claude's, but the two concerns we wire here
map cleanly and need no TOML writing:

  * skills  — a skill is a ``SKILL.md`` directory, same shape as Claude's.
              Codex discovers skills under ``.agents/skills`` (user: ~/.agents/skills,
              project: <repo>/.agents/skills) — NOT under ~/.codex. We symlink there.
  * hooks   — Codex supports the same lifecycle events + matcher/command schema
              as Claude, and loads them from a standalone ``~/.codex/hooks.json``
              (JSON, same event structure as the inline config). We write that
              file outright and own it — no merge into the user's config.toml.
              Path-conditional ``if:`` hooks have no Codex equivalent and are
              dropped with a warning (guard scripts re-check paths internally).

  * agents  — DEFERRED. Claude agents are .md; Codex agents are standalone .toml
              in ~/.codex/agents/. Needs a field mapping (model value map,
              tools -> sandbox_mode/mcp_servers) before it can be turned on.
  * perms   — DEFERRED. Codex uses named [permissions.<name>] profiles +
              default_permissions, not Claude's allow/deny arrays.

``capabilities()`` advertises only what is actually wired ({"skills","hooks"}),
so ``status`` and reports never misrepresent the deferred parts.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import linkutil, source
from ..adapter import InstallCtx, PlatformAdapter, Report, StatusRow
from ..formats import jsonconf

_CONFIG_DIRNAME = ".codex"
_HOOKS_FILE = "hooks.json"
_HOOKS_MARKER_FILE = ".aah-hooks-managed"   # sidecar: proves hooks.json is ours
_ENV_OVERRIDE = "CODEX_HOME"                 # user-scope root override

# Codex discovers skills under .agents/skills, separate from the .codex config root.
_SKILLS_DIRNAME = "skills"
_AGENTS_SKILLS_PARENT = ".agents"


class CodexAdapter(PlatformAdapter):
    name = "codex"

    # -- resolution --------------------------------------------------------

    def config_root(self, *, project: bool, project_dir: Path | None = None) -> Path:
        if project:
            return (project_dir or Path(".")).resolve() / _CONFIG_DIRNAME
        if os.environ.get(_ENV_OVERRIDE):
            return Path(os.environ[_ENV_OVERRIDE]).resolve()
        return Path.home() / _CONFIG_DIRNAME

    def _skills_root(self, ctx: InstallCtx) -> Path:
        """Codex skills discovery dir: <repo>/.agents/skills or ~/.agents/skills."""
        if ctx.project:
            base = (ctx.project_dir or Path(".")).resolve()
        else:
            base = Path.home()
        return base / _AGENTS_SKILLS_PARENT / _SKILLS_DIRNAME

    def capabilities(self) -> set[str]:
        # Deferred: "agents", "permissions". See module docstring.
        return {"skills", "hooks"}

    def project_ignore_lines(self, project_dir: Path) -> list[str]:
        # Project-scope Codex artifacts: skill symlinks under .agents/skills, and
        # the managed hooks.json + sidecar marker under .codex. Relative to
        # project_dir, forward slashes.
        lines = [
            f"{_AGENTS_SKILLS_PARENT}/{_SKILLS_DIRNAME}/{skill.name}/"
            for skill in source.skill_dirs()
        ]
        lines.append(f"{_CONFIG_DIRNAME}/{_HOOKS_FILE}")
        lines.append(f"{_CONFIG_DIRNAME}/{_HOOKS_MARKER_FILE}")
        return lines

    # -- hook rendering (Codex hooks.json schema) --------------------------

    def render_hooks(self, spec: dict) -> tuple[dict, list[str]]:
        """Render Codex's ``hooks.json`` object from the spec.

        Returns ``(hooks_obj, dropped)`` where ``dropped`` lists hook ``run``
        targets whose path-conditional ``if:`` could not be represented.
        """
        out: dict[str, list] = {}
        dropped: list[str] = []
        for event, groups in spec.get("hooks", {}).items():
            rendered_groups = []
            for group in groups:
                g: dict = {}
                if "matcher" in group:
                    # Codex matchers are regexes; anchor the tool-name alternation.
                    g["matcher"] = f"^({group['matcher']})$"
                hooks_list = []
                for hook in group.get("run_hooks", []):
                    if "if" in hook:
                        # No Codex equivalent for path-conditional hooks.
                        dropped.append(hook.get("run", "?"))
                        continue
                    entry = {"type": "command", "command": source.hook_command(hook)}
                    if "timeout" in hook:
                        entry["timeout"] = hook["timeout"]
                    if "statusMessage" in hook:
                        entry["statusMessage"] = hook["statusMessage"]
                    hooks_list.append(entry)
                g["hooks"] = hooks_list
                rendered_groups.append(g)
            out[event] = rendered_groups
        return out, dropped

    # -- install -----------------------------------------------------------

    def install(self, ctx: InstallCtx) -> list[Report]:
        reports: list[Report] = []

        # Skills — symlink each skill dir into Codex's .agents/skills discovery dir.
        skills_dst = self._skills_root(ctx)
        ok = link = 0
        for skill in source.skill_dirs():
            status = linkutil.make_link(skill, skills_dst / skill.name, is_dir=True)
            ok += status == "ok"
            link += status != "ok"
        reports.append(Report("skills", linked=link, current=ok, note=str(skills_dst)))

        # Hooks — write a standalone hooks.json that we own. Refuse to clobber a
        # user's own hooks.json (one without our sidecar marker).
        root = self.config_root(project=ctx.project, project_dir=ctx.project_dir)
        hp = root / _HOOKS_FILE
        marker = root / _HOOKS_MARKER_FILE
        if hp.exists() and not marker.exists():
            reports.append(Report(
                "hooks",
                note=f"SKIPPED: {hp} exists and is not managed by aah (remove it to let aah manage hooks)",
            ))
        else:
            hooks_obj, dropped = self.render_hooks(source.load_hooks_spec())
            jsonconf.write(hp, hooks_obj)
            marker.write_text("aah-managed\n", encoding="utf-8")
            note = str(hp)
            if dropped:
                note += (f"  (dropped {len(dropped)} path-conditional hooks: "
                         f"{', '.join(sorted(set(dropped)))})")
            reports.append(Report("hooks", linked=len(hooks_obj), note=note))

        # Deferred concerns — reported, not silently skipped.
        reports.append(Report("agents", note="deferred: .md->.toml transform (needs model/tools mapping)"))
        reports.append(Report("permissions", note="deferred: allow/deny -> [permissions.*] profile"))
        return reports

    # -- uninstall ---------------------------------------------------------

    def uninstall(self, ctx: InstallCtx) -> list[Report]:
        reports: list[Report] = []

        removed = 0
        skills_dst = self._skills_root(ctx)
        for skill in source.skill_dirs():
            removed += linkutil.unlink_if_ours(skills_dst / skill.name, skill)
        reports.append(Report("links", linked=removed, note="removed"))

        # Hooks — remove only the hooks.json we wrote (marker present).
        root = self.config_root(project=ctx.project, project_dir=ctx.project_dir)
        hp = root / _HOOKS_FILE
        marker = root / _HOOKS_MARKER_FILE
        if marker.exists():
            hp.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            reports.append(Report("settings", note=f"removed managed hooks file {hp}"))
        return reports

    # -- status ------------------------------------------------------------

    def status(self, ctx: InstallCtx) -> StatusRow:
        scope = "project" if ctx.project else "user-global"
        root = self.config_root(project=ctx.project, project_dir=ctx.project_dir)
        skills_dst = self._skills_root(ctx)
        linked = 0
        if skills_dst.exists():
            for skill in source.skill_dirs():
                if linkutil.is_our_link(skills_dst / skill.name, skill):
                    linked += 1
        managed = "managed" if (root / _HOOKS_MARKER_FILE).exists() else "-"
        return StatusRow(self.name, scope, root, skills_linked=linked, managed=managed,
                         extra="agents+perms deferred")
