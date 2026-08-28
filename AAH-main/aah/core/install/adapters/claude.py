"""Claude host adapter.

Installs into ``.claude/``:
  * skills  — one symlink per skill directory  -> .claude/skills/
  * agents  — one symlink per agent .md file    -> .claude/agents/
  * hooks   — aah-hooks.yaml rendered into the ``hooks`` object of settings.json
  * env / permissions — additively merged from settings-defaults.json["claude"]

Owns its own hook renderer (``render_hooks``): the Claude ``settings.json``
schema is a Claude concern, so the rendering knowledge lives here, not in a
shared transpiler.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import linkutil, source
from ..adapter import InstallCtx, PlatformAdapter, Report, StatusRow
from ..formats import jsonconf

_CONFIG_DIRNAME = ".claude"
_SETTINGS_FILE = "settings.json"
_ENV_OVERRIDE = "CLAUDE_CONFIG_DIR"  # user-scope root override
_MCP_PROJECT_FILE = ".mcp.json"      # project-scope MCP config (project root)
_MCP_GLOBAL_FILE = ".claude.json"    # user-scope MCP config (alongside ~/.claude)


class ClaudeAdapter(PlatformAdapter):
    name = "claude"

    # -- resolution --------------------------------------------------------

    def config_root(self, *, project: bool, project_dir: Path | None = None) -> Path:
        if project:
            return (project_dir or Path(".")).resolve() / _CONFIG_DIRNAME
        if os.environ.get(_ENV_OVERRIDE):
            return Path(os.environ[_ENV_OVERRIDE]).resolve()
        return Path.home() / _CONFIG_DIRNAME

    def mcp_config_path(self, *, project: bool, project_dir: Path | None = None) -> Path:
        """Where Claude Code reads MCP servers from — NOT inside ``.claude/``.

        Project scope: ``<project>/.mcp.json`` (the project root — the parent of
        the ``.claude`` config dir). Global scope: ``~/.claude.json`` (or
        ``$CLAUDE_CONFIG_DIR/.claude.json`` when the root override is set).
        """
        root = self.config_root(project=project, project_dir=project_dir)
        if project:
            return root.parent / _MCP_PROJECT_FILE
        # Global: the user config file sits alongside the .claude dir.
        return root.parent / _MCP_GLOBAL_FILE

    def capabilities(self) -> set[str]:
        return {"skills", "agents", "hooks", "permissions", "mcp"}

    def project_ignore_lines(self, project_dir: Path) -> list[str]:
        # Machine-specific symlinks a project install writes into <project>/.claude,
        # plus the local settings file. Relative to project_dir, forward slashes.
        lines = [f"{_CONFIG_DIRNAME}/skills/{skill.name}/" for skill in source.skill_dirs()]
        lines += [f"{_CONFIG_DIRNAME}/agents/{agent.name}" for agent in source.agent_files()]
        lines.append(f"{_CONFIG_DIRNAME}/settings.local.json")
        return lines

    # -- hook rendering (Claude settings.json schema) ----------------------

    def render_hooks(self, spec: dict, aah_bin: str = "aah") -> dict:
        """Render the ``hooks`` object for Claude's settings.json from the spec.

        ``aah_bin`` is the command used to invoke hooks — bare ``aah`` for a
        global install, or an absolute ``<venv>/bin/aah`` for a local project
        install so hooks fire without the venv being active.
        """
        out: dict[str, list] = {}
        for event, groups in spec.get("hooks", {}).items():
            rendered_groups = []
            for group in groups:
                g: dict = {}
                if "matcher" in group:
                    g["matcher"] = group["matcher"]
                hooks_list = []
                for hook in group.get("run_hooks", []):
                    entry = {"type": "command", "command": source.hook_command(hook, aah_bin)}
                    if "timeout" in hook:
                        entry["timeout"] = hook["timeout"]
                    if "statusMessage" in hook:
                        entry["statusMessage"] = hook["statusMessage"]
                    if "if" in hook:
                        entry["if"] = hook["if"]
                    hooks_list.append(entry)
                g["hooks"] = hooks_list
                rendered_groups.append(g)
            out[event] = rendered_groups
        return out

    # -- install -----------------------------------------------------------

    def install(self, ctx: InstallCtx) -> list[Report]:
        root = self.config_root(project=ctx.project, project_dir=ctx.project_dir)
        reports: list[Report] = []

        # Skills — one symlink per skill directory.
        skills_dst = root / "skills"
        ok = link = 0
        for skill in source.skill_dirs():
            status = linkutil.make_link(skill, skills_dst / skill.name, is_dir=True)
            ok += status == "ok"
            link += status != "ok"
        reports.append(Report("skills", linked=link, current=ok, note=str(skills_dst)))

        # Agents — one symlink per agent .md file.
        agents_dst = root / "agents"
        ok = link = 0
        for agent in source.agent_files():
            status = linkutil.make_link(agent, agents_dst / agent.name, is_dir=False)
            ok += status == "ok"
            link += status != "ok"
        reports.append(Report("agents", linked=link, current=ok, note=str(agents_dst)))

        # Hooks + env/permissions — merged into settings.json (single read/write).
        # Local (venv) project installs render an absolute <venv>/bin/aah so
        # hooks fire without the venv being active; global installs use bare aah.
        aah_bin = "aah"
        if ctx.project:
            # root is <project>/.claude; the project dir is its parent. Only a
            # project-local venv (./.venv) yields an absolute path — a global uv
            # tool install stays bare `aah`.
            venv_bin = source.venv_aah_bin(root.parent)
            if venv_bin:
                aah_bin = venv_bin
        sp = root / _SETTINGS_FILE
        data = jsonconf.read(sp)
        jsonconf.merge_hooks(data, self.render_hooks(source.load_hooks_spec(), aah_bin))
        defaults = source.load_settings_defaults(self.name)
        env_added = jsonconf.merge_env(data, defaults.get("env", {}))
        allow_added, deny_added = jsonconf.merge_permissions(data, defaults.get("permissions", {}))
        # Provenance: which aah version linked this host, and when (local time).
        import aah
        from datetime import datetime
        jsonconf.stamp_provenance(
            data, aah.__version__, datetime.now().astimezone().isoformat(timespec="seconds")
        )
        jsonconf.write(sp, data)

        n_events = len(data["hooks"])
        reports.append(Report("hooks", linked=n_events, note=str(sp)))
        reports.append(Report(
            "permissions",
            linked=len(env_added) + len(allow_added) + len(deny_added),
            note=f"+{len(env_added)} env, +{len(allow_added)} allow, +{len(deny_added)} deny",
        ))

        # MCP servers — merged into the MCP config (a DIFFERENT file from
        # settings.json: <project>/.mcp.json or ~/.claude.json, never inside
        # .claude/). Add-if-absent + `_aah` marker, so the user's own servers are
        # never touched and uninstall removes exactly ours.
        mcp_servers = source.load_mcp_servers()
        if mcp_servers:
            mp = self.mcp_config_path(project=ctx.project, project_dir=ctx.project_dir)
            mcp_data = jsonconf.read(mp)
            mcp_added = jsonconf.merge_mcp_servers(mcp_data, mcp_servers)
            jsonconf.write(mp, mcp_data)
            reports.append(Report("mcp", linked=len(mcp_added), note=str(mp)))

        # NOTE: install never writes git artifacts. The machine-specific
        # .claude/* symlinks a project install creates are excluded from git by
        # the scaffolder's .gitignore (written at /rapids-new-project, which
        # always follows install). Just remind the user in project scope.
        if ctx.project:
            reports.append(Report(
                "gitignore",
                note="linked into ./.claude — /rapids-new-project will exclude these from git",
            ))
        return reports

    # -- uninstall ---------------------------------------------------------

    def uninstall(self, ctx: InstallCtx) -> list[Report]:
        root = self.config_root(project=ctx.project, project_dir=ctx.project_dir)
        reports: list[Report] = []

        removed = 0
        skills_dst = root / "skills"
        for skill in source.skill_dirs():
            removed += linkutil.unlink_if_ours(skills_dst / skill.name, skill)
        agents_dst = root / "agents"
        for agent in source.agent_files():
            removed += linkutil.unlink_if_ours(agents_dst / agent.name, agent)
        reports.append(Report("links", linked=removed, note="removed"))

        sp = root / _SETTINGS_FILE
        if sp.exists():
            data = jsonconf.read(sp)
            if jsonconf.unmerge(data):
                jsonconf.write(sp, data)
                reports.append(Report("settings", note=f"reverted managed additions in {sp}"))

        # MCP servers — remove exactly what we recorded under `_aah.mcp_servers`
        # from the MCP config file; leave the user's own servers untouched.
        mp = self.mcp_config_path(project=ctx.project, project_dir=ctx.project_dir)
        if mp.exists():
            mcp_data = jsonconf.read(mp)
            if jsonconf.unmerge_mcp_servers(mcp_data):
                jsonconf.write(mp, mcp_data)
                reports.append(Report("mcp", note=f"reverted managed servers in {mp}"))

        # uninstall does not touch the project .gitignore — the aah-managed
        # .claude ignore lines are harmless and the user's file stays theirs.
        return reports

    # -- status ------------------------------------------------------------

    def status(self, ctx: InstallCtx) -> StatusRow:
        root = self.config_root(project=ctx.project, project_dir=ctx.project_dir)
        scope = "project" if ctx.project else "user-global"
        skills_dst = root / "skills"
        linked = 0
        if skills_dst.exists():
            for skill in source.skill_dirs():
                if linkutil.is_our_link(skills_dst / skill.name, skill):
                    linked += 1
        agents_dst = root / "agents"
        agents_linked = 0
        if agents_dst.exists():
            for agent in source.agent_files():
                if linkutil.is_our_link(agents_dst / agent.name, agent):
                    agents_linked += 1

        managed = "-"
        version = linked_at = ""
        sp = root / _SETTINGS_FILE
        if sp.exists():
            try:
                marker = jsonconf.read(sp).get(jsonconf.MARKER, {})
                managed = "managed" if marker.get(jsonconf.HOOKS_MARKER) else "unmanaged"
                version = marker.get("version", "")
                linked_at = marker.get("linked_at", "")
            except Exception:
                managed = "?"

        # Flag links stranded by a deleted venv (local-mode). Re-run
        # `aah install --project` after rebuilding the venv to re-link.
        dead = linkutil.count_dead_links(skills_dst) + linkutil.count_dead_links(agents_dst)
        extra = ""
        if dead:
            extra = f"{dead} DEAD links (target missing — re-run `aah install --project`)"
        return StatusRow(self.name, scope, root, skills_linked=linked, managed=managed,
                         extra=extra, agents_linked=agents_linked, version=version,
                         linked_at=linked_at, dead=dead)
