#!/usr/bin/env python3
"""version_control CLI — the surface every AAH skill calls.

    aah run core.version_control.cli <subcommand> [args]

Subcommands: init | sync | delta | apply | conflicts | resolve | status
All subcommands exit 0 (non-blocking) and emit machine-readable output for the
LLM, following the cli.py _claude_retry_hint convention.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.version_control.config import load_vc_config
from aah.core.version_control.conflict_store import ConflictStore
from aah.core.version_control.engine import DeltaEngine
from aah.core.version_control.feature_codec import FeatureCodec
from aah.core.version_control.ledger import SyncLedger
from aah.core.version_control import reporter


def _state_dir(args) -> Path | None:
    """Resolve the project's ``.aah/`` state directory.

    ``.aah/`` is the sole source of truth: feature .md files, the sync ledger, and
    the conflict store all live under it. Returns None when no project is found.
    """
    explicit = Path(args.project_path) if getattr(args, "project_path", None) else None
    project = resolve_project_path(explicit)
    if project is None:
        return None
    return project / ".aah"


def _wire(args):
    """Build (config, provider, ledger, codec, conflicts) for the active project.

    Returns None on any soft failure (no project, disabled, bad provider),
    having already printed an explanatory [AAH-VC] line. Non-blocking.
    """
    state_dir = _state_dir(args)
    if state_dir is None:
        print("[AAH-VC] No active AAH project found — nothing to sync.")
        return None

    # Read config from THIS project's manifest, not whatever is above cwd.
    config = load_vc_config(state_dir / "manifest.yaml")
    if not config.enabled and args.command != "status":
        print("[AAH-VC] version_control is disabled in manifest.yaml "
              "(set version_control.enabled: true to use it).")
        return None

    provider = None
    try:
        from aah.core.version_control.providers.registry import get_provider
        provider = get_provider(config)
    except Exception as e:  # noqa: BLE001
        # status only inspects local state, so it can run without a provider.
        if args.command != "status":
            print(f"[AAH-VC] Could not load provider '{config.provider}': {e}")
            return None

    ledger = SyncLedger(state_dir)
    codec = FeatureCodec(state_dir)
    conflicts = ConflictStore(state_dir)
    return config, provider, ledger, codec, conflicts


def _scope_feature_ids(args, codec: FeatureCodec, ledger: SyncLedger) -> list[str] | None:
    if getattr(args, "feature", None):
        return list(args.feature)
    if getattr(args, "full", False):
        return None  # engine unions feature.md + ledger
    # default: all feature.md features
    return codec.list_features()


# --- subcommands ------------------------------------------------------------
def cmd_init(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    ledger.init(config.provider, config.repo)
    conflicts.save()
    ok, msg = provider.validate_auth()
    print(f"[AAH-VC] init complete — provider={config.provider} repo={config.repo}")
    print(f"[AAH-VC] auth: {'OK' if ok else 'FAILED'} — {msg}")
    return 0


def cmd_labels(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    if not hasattr(provider, "provision_labels"):
        print(f"[AAH-VC] provider '{config.provider}' does not support label provisioning.")
        return 0
    try:
        created = provider.provision_labels()
    except Exception as e:  # noqa: BLE001
        print(f"[AAH-VC] label provisioning failed: {e}")
        return 0
    print(f"[AAH-VC] provisioned {len(created)} predefined label(s) on {config.repo}:")
    for name in created:
        print(f"  - {name}")
    return 0


def _brief_issue(issue: dict) -> dict:
    """Condense a raw gh issue dict into the briefer-facing shape."""
    author = issue.get("author") or {}
    comments = []
    for c in issue.get("comments") or []:
        c_author = c.get("author") or {}
        comments.append({
            "author": c_author.get("login", "") if isinstance(c_author, dict) else str(c_author),
            "created_at": c.get("createdAt", ""),
            "body": c.get("body", ""),
        })
    return {
        "number": issue.get("number"),
        "title": issue.get("title", ""),
        "state": issue.get("state", ""),
        "labels": [l.get("name", "") if isinstance(l, dict) else str(l)
                   for l in (issue.get("labels") or [])],
        "author": author.get("login", "") if isinstance(author, dict) else str(author),
        "created_at": issue.get("createdAt", ""),
        "updated_at": issue.get("updatedAt", ""),
        "body": issue.get("body", ""),
        "url": issue.get("url", ""),
        "comment_count": len(comments),
        "comments": comments,
    }


def cmd_issues(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    try:
        if args.unlabeled:
            raw = provider.unlabeled_issues()
        elif args.phase:
            raw = provider.phase_issues(args.phase)
        else:
            print("[AAH-VC] issues: pass --phase <name> or --unlabeled.")
            return 0
    except Exception as e:  # noqa: BLE001 — non-blocking, degrade to a message
        # Keep it to the first line; gh echoes the full command otherwise.
        msg = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        print(f"[AAH-VC] issues: could not fetch ({msg}).")
        return 0

    briefed = [_brief_issue(i) for i in raw]
    print(json.dumps({
        "phase": None if args.unlabeled else args.phase,
        "unlabeled": bool(args.unlabeled),
        "count": len(briefed),
        "issues": briefed,
    }, indent=2))
    return 0


def cmd_create(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body is not None:
        body = args.body
    try:
        issue = provider.create_phase_issue(args.phase, args.title, body, kind=args.kind)
    except Exception as e:  # noqa: BLE001
        print(f"[AAH-VC] create: failed ({e}).")
        return 0
    print(f"[AAH-VC] created issue #{issue.get('number')} "
          f"[aah:{args.phase}, aah:{args.kind}] — {issue.get('url', '')}")
    return 0


def cmd_comment(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body is not None:
        body = args.body
    if not body.strip():
        print("[AAH-VC] comment: empty body — nothing posted.")
        return 0
    try:
        provider.comment(args.issue, body)
    except Exception as e:  # noqa: BLE001
        print(f"[AAH-VC] comment: failed ({e}).")
        return 0
    print(f"[AAH-VC] commented on issue #{args.issue}.")
    return 0


def cmd_delta(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    engine = DeltaEngine(provider, ledger, codec)
    feature_ids = _scope_feature_ids(args, codec, ledger)
    delta_set = engine.compute(feature_ids)
    print(json.dumps(delta_set.to_dict(), indent=2))
    return 0


def _delta_and_apply(provider, ledger, codec, conflicts, feature_ids, label, auto_fold_structural=False):
    """Compute a DeltaSet for feature_ids and apply it. Shared by cmd_apply()
    and sync_one_feature() so there is exactly one delta/apply implementation."""
    from aah.core.version_control.applier import Applier

    engine = DeltaEngine(provider, ledger, codec)
    delta_set = engine.compute(feature_ids)

    applier = Applier(
        provider, ledger, codec, conflicts,
        detected_at=label,
        auto_fold_structural_inbound=auto_fold_structural,
    )
    return applier.apply(delta_set)


def cmd_apply(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    feature_ids = _scope_feature_ids(args, codec, ledger)

    report = _delta_and_apply(
        provider, ledger, codec, conflicts, feature_ids,
        label=args.label or "apply",
        auto_fold_structural=args.auto_fold_structural,
    )
    reporter.print_apply_summary(report)
    open_conflicts = conflicts.open()
    if open_conflicts:
        print()
        reporter.print_conflicts(open_conflicts)
    return 0


def cmd_sync(args) -> int:
    # Convenience: delta + apply in one call.
    return cmd_apply(args)


def sync_one_feature(project_path, feature_id: str, label: str = "lifecycle-update"):
    """Push one feature's current state (incl. its aah:* label) to the tracker.

    Non-blocking: returns the apply report dict on success, None on any soft
    failure (no project, disabled, bad provider, network error) — mirrors the
    printed-warning-then-return-0 convention the rest of this CLI follows,
    instead of raising. Intended for direct import from other Python modules
    (e.g. update-lifecycle), not from argparse.
    """
    try:
        wired = _wire(argparse.Namespace(project_path=str(project_path), command="apply"))
        if wired is None:
            return None
        config, provider, ledger, codec, conflicts = wired
        return _delta_and_apply(provider, ledger, codec, conflicts, [feature_id], label=label)
    except Exception:
        return None


def cmd_conflicts(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    records = conflicts.open() if args.open else conflicts.all()
    reporter.print_conflicts(records)
    return 0


def cmd_resolve(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    from aah.core.version_control.applier import Applier

    value = None
    if args.value_file:
        value = json.loads(Path(args.value_file).read_text(encoding="utf-8"))
    elif args.value is not None:
        value = args.value

    applier = Applier(provider, ledger, codec, conflicts, detected_at="resolve")
    result = applier.resolve(args.conflict_id, args.resolution, value)
    print(f"[AAH-VC] resolve: {json.dumps(result)}")
    if result.get("rework_staged"):
        print("[AAH-VC] structural change staged — run aah-plan re-entry "
              "to recompute DAG/waves.")
    return 0


def cmd_status(args) -> int:
    wired = _wire(args)
    if wired is None:
        return 0
    config, provider, ledger, codec, conflicts = wired
    open_conflicts = conflicts.open()
    print("[AAH-VC] status")
    print(f"  enabled       : {config.enabled}")
    print(f"  provider      : {config.provider}")
    print(f"  repo          : {config.repo}")
    print(f"  tracked feats : {len(ledger.all_feature_ids())}")
    print(f"  md feats      : {len(codec.list_features())}")
    print(f"  open conflicts: {len(open_conflicts)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="version_control", description="AAH issue-tracker sync")
    p.add_argument("--project-path", type=str, default=None,
                   help="Explicit project path (defaults to active AAH project)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_scope(sp):
        sp.add_argument("--feature", action="append", help="Limit to feature id(s)")
        sp.add_argument("--full", action="store_true",
                        help="All features in feature.md + ledger (catches orphans)")

    sp = sub.add_parser("init", help="Create ledger + conflict store; validate auth")

    sp = sub.add_parser("labels", help="Provision the static predefined label set in the repo (run once)")

    sp = sub.add_parser("delta", help="Compute the delta set (mutates nothing)")
    add_scope(sp)

    sp = sub.add_parser("apply", help="Apply safe deltas; record + print conflicts")
    add_scope(sp)
    sp.add_argument("--label", default="apply", help="detected_at label for conflicts")
    sp.add_argument("--auto-fold-structural", action="store_true",
                    help="Auto-fold remote-only structural changes into feature.md "
                         "(default: record for LLM review)")

    sp = sub.add_parser("sync", help="delta + apply in one call (--from-plan supported)")
    add_scope(sp)
    sp.add_argument("--from-plan", action="store_true",
                    help="Sync all plan features (create/update issues)")
    sp.add_argument("--label", default="sync")
    sp.add_argument("--auto-fold-structural", action="store_true")

    sp = sub.add_parser("conflicts", help="List recorded conflicts for the LLM")
    sp.add_argument("--open", action="store_true", help="Only open conflicts")

    sp = sub.add_parser("resolve", help="Apply an LLM/human conflict resolution")
    sp.add_argument("--conflict-id", required=True)
    sp.add_argument("--resolution", choices=["merged", "local", "remote"], default="merged")
    sp.add_argument("--value", default=None, help="Merged value for scalar fields")
    sp.add_argument("--value-file", default=None, help="JSON file with merged value for list fields")

    sp = sub.add_parser("status", help="Show sync health + open-conflict count")

    sp = sub.add_parser("issues", help="List phase or untriaged issues as JSON (for the briefer)")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--phase", help="Opaque phase name → label aah:<phase>")
    grp.add_argument("--unlabeled", action="store_true",
                     help="Open issues carrying no aah:* label (untriaged)")
    # Output is always JSON; --json is accepted (and default) so the documented
    # `issues --phase X --json` invocation is valid rather than an arg error.
    sp.add_argument("--json", action="store_true", default=True,
                    help="Emit JSON (default; accepted for explicit callers)")

    sp = sub.add_parser("create", help="Open a phase-tagged issue")
    sp.add_argument("--phase", required=True, help="Opaque phase name → label aah:<phase>")
    sp.add_argument("--title", required=True)
    sp.add_argument("--kind", default="feedback", choices=["feedback", "feature"])
    sp.add_argument("--body", default=None, help="Inline body text")
    sp.add_argument("--body-file", default=None, help="Read body from a file")

    sp = sub.add_parser("comment", help="Post a comment on an issue")
    sp.add_argument("--issue", type=int, required=True)
    sp.add_argument("--body", default=None, help="Inline body text")
    sp.add_argument("--body-file", default=None, help="Read body from a file")
    return p


_DISPATCH = {
    "init": cmd_init,
    "labels": cmd_labels,
    "delta": cmd_delta,
    "apply": cmd_apply,
    "sync": cmd_sync,
    "conflicts": cmd_conflicts,
    "resolve": cmd_resolve,
    "status": cmd_status,
    "issues": cmd_issues,
    "create": cmd_create,
    "comment": cmd_comment,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    # --from-plan is just sync over all plan features.
    if getattr(args, "from_plan", False):
        args.full = False
        args.feature = None
    handler = _DISPATCH[args.command]
    rc = handler(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
