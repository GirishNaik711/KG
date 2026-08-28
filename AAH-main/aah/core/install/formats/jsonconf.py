"""JSON settings-file merge/unmerge with an ``_aah`` provenance marker.

Used by adapters whose host config is JSON (e.g. Claude's ``settings.json``).
Every addition is recorded under the file's ``_aah`` marker so uninstall can
remove *exactly* what install added and never disturb keys the user set
themselves.

All merges are additive and idempotent:
  * env — a key is added ONLY if absent; a value the user set is kept.
  * permissions.allow / deny — unioned; existing entries untouched.
  * hooks — the whole block is (re)written and marked, so reinstall replaces
    exactly that region and leaves non-hook keys (env, permissions, …) intact.
"""

from __future__ import annotations

import json
from pathlib import Path

MARKER = "_aah"
HOOKS_MARKER = "_aah_managed"  # sentinel under _aah marking the generated hooks block


def read(path: Path) -> dict:
    """Load the JSON settings file, or ``{}`` if it does not exist."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write(path: Path, data: dict) -> None:
    """Write ``data`` as pretty JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def merge_env(data: dict, env_defaults: dict) -> list[str]:
    """Add missing env keys only. Records + returns the keys actually added."""
    added: list[str] = []
    if not env_defaults:
        return added
    env = data.setdefault("env", {})
    for k, v in env_defaults.items():
        if k not in env:
            env[k] = v
            added.append(k)
    if added:
        marker = data.setdefault(MARKER, {})
        marker["settings_env"] = sorted(set(marker.get("settings_env", [])) | set(added))
    return added


def merge_permissions(data: dict, perms_defaults: dict) -> tuple[list[str], list[str]]:
    """Reconcile permissions.allow / deny against the defaults.

    The defaults file is the single source of truth for what aah manages:
      * additions — every default entry not already present is appended and
        recorded under the ``_aah`` marker.
      * prunes    — any entry aah previously added (recorded in the marker) that
        is no longer in the defaults is removed. This retires stale rules (e.g.
        the redundant ``Write(<secret>)`` deny entries) on the next install/
        setup, without an explicit migration step.

    Entries the user added independently are never recorded and never pruned,
    even when they happen to coincide with a default. Returns the entries
    actually added this call — (allow_added, deny_added)."""
    allow_added: list[str] = []
    deny_added: list[str] = []
    if not perms_defaults:
        return (allow_added, deny_added)
    perms = data.setdefault("permissions", {})
    marker = data.setdefault(MARKER, {})
    for field, mkey, tracker in (
        ("allow", "settings_allow", allow_added),
        ("deny", "settings_deny", deny_added),
    ):
        want = perms_defaults.get(field, [])
        recorded = set(marker.get(mkey, []))
        # Prune entries aah added before but no longer wants. Only touch what we
        # recorded — user-added entries (never recorded) are left in place.
        stale = recorded - set(want)
        if stale and field in perms:
            perms[field] = [e for e in perms[field] if e not in stale]
        recorded -= stale
        if not want:
            # Nothing to add; still persist any prune to the marker below.
            if field in perms and not perms[field]:
                del perms[field]
        else:
            cur = perms.setdefault(field, [])
            existing = set(cur)
            for entry in want:
                if entry not in existing:
                    cur.append(entry)
                    existing.add(entry)
                    tracker.append(entry)
        # Marker now reflects exactly the entries aah manages: what it added
        # before (minus prunes) plus what it added this call.
        recorded |= set(tracker)
        if recorded:
            marker[mkey] = sorted(recorded)
        elif mkey in marker:
            del marker[mkey]
    return (allow_added, deny_added)


def merge_hooks(data: dict, hooks_obj: dict) -> None:
    """(Re)write the generated hooks block and mark it under ``_aah``."""
    data["hooks"] = hooks_obj
    data.setdefault(MARKER, {})[HOOKS_MARKER] = True


def fmt_linked_at(raw: str) -> str:
    """Render an ISO ``linked_at`` timestamp as a readable string, e.g.
    ``2026-08-19 11:20:49 (UTC+00:00)``. Returns the raw value if unparseable."""
    if not raw:
        return "unknown"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(raw)
        off = dt.strftime("%z")               # e.g. "+0000" / "-0400"
        tz = f" (UTC{off[:3]}:{off[3:]})" if off else ""
        return dt.strftime("%Y-%m-%d %H:%M:%S") + tz
    except Exception:
        return raw


def stamp_provenance(data: dict, version: str, linked_at: str) -> None:
    """Record which aah version, and when (local time), last linked this host.

    Written under the ``_aah`` marker so it lives beside the other managed keys
    and is removed by ``unmerge`` on uninstall. Overwritten on every relink, so
    it always reflects the most recent ``aah install`` / ``aah setup``.
    """
    marker = data.setdefault(MARKER, {})
    marker["version"] = version
    marker["linked_at"] = linked_at


def merge_mcp_servers(data: dict, servers: dict) -> list[str]:
    """Add missing MCP servers only. Records + returns the names actually added.

    Operates on an MCP config dict (``.mcp.json`` or ``~/.claude.json``), whose
    servers live under a top-level ``mcpServers`` object. Mirrors ``merge_env``:
    a server is added ONLY if its name is absent, so a server the user defined
    themselves is never overwritten. Every add is recorded under the ``_aah``
    marker (``mcp_servers``) so ``unmerge_mcp_servers`` can remove exactly what
    aah added and leave the user's own servers intact.
    """
    added: list[str] = []
    if not servers:
        return added
    dst = data.setdefault("mcpServers", {})
    for name, spec in servers.items():
        if name not in dst:
            dst[name] = spec
            added.append(name)
    if added:
        marker = data.setdefault(MARKER, {})
        marker["mcp_servers"] = sorted(set(marker.get("mcp_servers", [])) | set(added))
    return added


def unmerge_mcp_servers(data: dict) -> bool:
    """Reverse exactly the MCP servers recorded under ``_aah.mcp_servers``.

    Mutates ``data`` in place; returns True if anything changed. Servers the user
    added independently (never recorded) are left untouched; the empty
    ``mcpServers`` container and marker key are cleaned up when emptied.
    """
    marker = data.get(MARKER, {})
    recorded = marker.get("mcp_servers", [])
    if not recorded:
        return False
    changed = False
    servers = data.get("mcpServers", {})
    for name in recorded:
        if name in servers:
            del servers[name]
            changed = True
    if "mcpServers" in data and not data["mcpServers"]:
        del data["mcpServers"]
    if "mcp_servers" in marker:
        del marker["mcp_servers"]
        changed = True
    if MARKER in data and not data[MARKER]:
        del data[MARKER]
    return changed


def unmerge(data: dict) -> bool:
    """Reverse exactly the env/permissions/hooks additions recorded under ``_aah``.

    Mutates ``data`` in place. Returns True if anything changed. Entries the user
    added independently (not in our record) are left untouched; empty containers
    we created are cleaned up; the marker is dropped once it carries nothing.
    """
    marker = data.get(MARKER, {})
    changed = False

    # env
    for key in marker.get("settings_env", []):
        if key in data.get("env", {}):
            del data["env"][key]
            changed = True
    if "env" in data and not data["env"]:
        del data["env"]

    # permissions
    perms = data.get("permissions", {})
    for field, mkey in (("allow", "settings_allow"), ("deny", "settings_deny")):
        recorded = set(marker.get(mkey, []))
        if recorded and field in perms:
            kept = [e for e in perms[field] if e not in recorded]
            if len(kept) != len(perms[field]):
                changed = True
            if kept:
                perms[field] = kept
            else:
                del perms[field]
    if "permissions" in data and not data["permissions"]:
        del data["permissions"]

    # hooks
    if marker.get(HOOKS_MARKER):
        data.pop("hooks", None)
        marker.pop(HOOKS_MARKER, None)
        changed = True

    # marker bookkeeping
    for mkey in ("settings_env", "settings_allow", "settings_deny"):
        if mkey in marker:
            del marker[mkey]
            changed = True
    # provenance keys — dropped so uninstall leaves the marker empty and removable.
    for mkey in ("version", "linked_at"):
        if marker.pop(mkey, None) is not None:
            changed = True
    if MARKER in data and not data[MARKER]:
        del data[MARKER]
    return changed
