#!/usr/bin/env python3
"""Guidance-file loader for /aah-discuss.

Reads aah/_resources/_references/discuss/decisions_guidance.yaml and answers the
following queries the walk (Step 8) and validator (Step 10a) need:

    * `mandatory-questions` — filtered by `applies_when` given resolved answers
    * `follow-ups`          — sub-slugs opened when a parent option is chosen
    * `whitelists`          — inline whitelists per slug (Check C)
    * `forbidden-options`   — global blacklist (Check B)
    * `service-scope`       — approved / forbidden service inventory (Check D)
    * `active-references`   — union of chosen-option `references[]` and any
                              `always_references[]` whose `applies_when` holds
                              (used by Check E)

The `applies_when` mini-DSL supports:
    always
    <slug> == <value>           # single equality
    <slug> != <value>           # single inequality
    <slug> in [v1, v2, ...]     # membership
    <slug> contains <v>         # multi-select membership

Anything more complex should be pushed back into structured YAML rather than
grown here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_yaml


# ─── Path resolution ─────────────────────────────────────────────────────

def find_framework_root() -> Path:
    """Locate the repo root (contains aah/_resources/_references/discuss/decisions_guidance.yaml).

    Walks up from this file's location; fails loudly if not found so we don't
    silently read a stale guidance file. Works for both editable installs
    (source tree) and non-editable wheel installs (guidance data is shipped
    as package artifacts under aah/_resources/).
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "aah" / "_resources" / "_references" / "discuss" / "decisions_guidance.yaml"
        if candidate.exists():
            return parent
    raise FileNotFoundError(
        "Could not locate aah/_resources/_references/discuss/decisions_guidance.yaml — "
        "expected to walk up from " + str(here)
    )


def guidance_path(root: Path | None = None) -> Path:
    root = root or find_framework_root()
    return root / "aah" / "_resources" / "_references" / "discuss" / "decisions_guidance.yaml"


def load_guidance(path: Path | None = None) -> dict:
    """Parse the guidance file. Callers should treat the return value as read-only."""
    p = path or guidance_path()
    if not p.exists():
        raise FileNotFoundError(f"decisions_guidance.yaml not found at {p}")
    return read_yaml(p)


# ─── applies_when evaluator ──────────────────────────────────────────────

def _tokenize(expr: str) -> list[str]:
    """Very small tokenizer — splits on whitespace but keeps [bracketed, lists] intact."""
    # Normalise brackets + commas: `[a, b]` should tokenize as `[`, `a`, `,`, `b`, `]`.
    # Both sides of each bracket/comma need whitespace, otherwise values like
    # `local-cloud-ready]` don't split into separate tokens.
    normalized = expr.replace("[", " [ ").replace("]", " ] ").replace(",", " , ")
    return [t for t in normalized.split() if t]


def eval_applies_when(expr: str | bool | None, answers: dict[str, Any]) -> bool:
    """Evaluate an `applies_when` expression against a resolved answers dict.

    `answers` maps slug_id → response value (str for single-select, list for
    multi-select). Missing slugs evaluate as `None`, which never satisfies
    equality — callers may still want to treat unresolved conditions
    optimistically; that decision lives in the caller.
    """
    if expr is None or expr is True or expr == "always":
        return True
    if expr is False:
        return False

    tokens = _tokenize(str(expr))
    if not tokens:
        return True

    # slug OP value
    # slug == value
    if len(tokens) == 3 and tokens[1] in ("==", "!="):
        lhs, op, rhs = tokens
        actual = answers.get(lhs)
        rhs_val = _coerce(rhs)
        if op == "==":
            return actual == rhs_val
        return actual != rhs_val

    # slug in [a, b, c]  → tokens: [slug, 'in', '[', 'a', ',', 'b', ',', 'c', ']']
    if len(tokens) >= 4 and tokens[1] == "in" and tokens[2] == "[" and tokens[-1] == "]":
        lhs = tokens[0]
        rhs_list = [_coerce(t) for t in tokens[3:-1] if t != ","]
        actual = answers.get(lhs)
        return actual in rhs_list

    # slug contains value  → single-token membership check for multi-select
    if len(tokens) == 3 and tokens[1] == "contains":
        lhs, _, rhs = tokens
        actual = answers.get(lhs)
        rhs_val = _coerce(rhs)
        if isinstance(actual, list):
            return rhs_val in actual
        return actual == rhs_val

    # Unknown shape — refuse quietly rather than throw; callers may see extra
    # questions or references, but we'll never silently drop mandatory floors.
    return True


def _coerce(token: str) -> Any:
    """Trim quotes and coerce YAML-style scalars."""
    t = token.strip().strip("'").strip('"')
    if t.lower() == "true":
        return True
    if t.lower() == "false":
        return False
    return t


# ─── Query helpers ───────────────────────────────────────────────────────

def collect_answers(registry: dict) -> dict[str, Any]:
    """Build a slug → value map from pre_resolved + decisions in a registry."""
    answers: dict[str, Any] = {}
    for pr in registry.get("pre_resolved", []) or []:
        answers[pr["slug_id"]] = pr.get("value")
    for d in registry.get("decisions", []) or []:
        answers[d["slug_id"]] = d.get("response")
    return answers


def mandatory_questions(guidance: dict, answers: dict[str, Any]) -> list[dict]:
    """Return the mandatory question list, filtered to entries whose applies_when holds."""
    out = []
    for q in guidance.get("mandatory_questions", []) or []:
        if eval_applies_when(q.get("applies_when"), answers):
            out.append(q)
    return out


def follow_ups_for(guidance: dict, parent_slug: str, parent_value: Any) -> list[dict]:
    """Return the follow-up questions opened by a parent (slug, value) pair."""
    out = []
    for q in guidance.get("follow_up_questions", []) or []:
        if q.get("parent_slug") == parent_slug and q.get("parent_value") == parent_value:
            out.append(q)
    return out


def whitelist_for(guidance: dict, slug_id: str) -> list[Any] | None:
    return (guidance.get("inline_whitelists") or {}).get(slug_id)


def forbidden_options(guidance: dict) -> list[Any]:
    return list(guidance.get("forbidden_options") or [])


def service_scope(guidance: dict) -> dict[str, list[Any]]:
    scope = guidance.get("service_scope") or {}
    return {
        "approved": list(scope.get("approved") or []),
        "forbidden": list(scope.get("forbidden") or []),
    }


def _entry_source(entry) -> str | None:
    """Extract the reference identifier from a provenance entry.

    The canonical field is `source`, but real agent responses have been seen
    to emit `path` (or `url`) instead. This helper tolerates all three so
    validators don't misreport schema-drift as missing coverage.
    """
    if not isinstance(entry, dict):
        return str(entry) if entry else None
    for key in ("source", "path", "url"):
        val = entry.get(key)
        if val:
            return str(val)
    return None


def validate_response_provenance(
    expected: list[str],
    response_provenance: dict,
) -> dict:
    """Compare the expected active-reference set against a response's provenance.

    `expected` is the string form active-references returns (URLs verbatim,
    paths relative to framework root).

    Reads `provenance.active_references_at_prep_time[]` (success list) and
    `provenance.reference_load_warnings[]` (attempted-but-failed list). Both
    count as "covered" — the spec requires each expected ref to appear in ONE
    of them, never silently absent.

    Each entry's identifier is read via `_entry_source`, which tolerates
    `source | path | url` field names (agent responses have drifted between them).
    Callers that need strict spec compliance should also inspect
    `field_shape_drift` for a per-entry list of which key was actually used.
    """
    covered_sources: set[str] = set()
    followed_children: list[str] = []
    field_shape_drift: list[dict] = []

    def collect(entries, block: str):
        for entry in entries or []:
            # Child pages reached by navigating a wired URL (depth-limited) are
            # marked with `via` (their wired parent). They are additive context,
            # NOT part of the expected wired set — exclude them from coverage so
            # they never register as `extra[]` and fail the gate.
            if isinstance(entry, dict) and entry.get("via"):
                child_src = _entry_source(entry)
                if child_src:
                    followed_children.append(child_src)
                continue
            src = _entry_source(entry)
            if src:
                covered_sources.add(src)
                if isinstance(entry, dict) and "source" not in entry:
                    used = next((k for k in ("path", "url") if k in entry), "?")
                    field_shape_drift.append({"block": block, "used_key": used, "value": src})

    collect(response_provenance.get("active_references_at_prep_time"), "active_references_at_prep_time")
    collect(response_provenance.get("reference_load_warnings"), "reference_load_warnings")

    expected_set = {str(r) for r in expected}
    missing = sorted(expected_set - covered_sources)
    extra = sorted(covered_sources - expected_set)
    return {
        "covered": sorted(covered_sources & expected_set),
        "missing": missing,
        "extra": extra,
        "followed_children": sorted(followed_children),
        "ok": not missing and not extra,
        "field_shape_drift": field_shape_drift,
    }


def _is_url(ref: str) -> bool:
    return isinstance(ref, str) and (ref.startswith("http://") or ref.startswith("https://"))


def active_reference_paths(guidance: dict, answers: dict[str, Any], framework_root: Path) -> list[Path | str]:
    """Resolve the union of active mandate catalogs / URLs.

    Includes:
      * `always_references[]` entries whose `applies_when` holds
      * `references[]` from any chosen option in mandatory_questions / follow_up_questions

    Return elements:
      * `Path` for filesystem references that exist
      * `str` for URLs (http://, https://) — returned verbatim; consumers use
        WebFetch, no filesystem check applies

    Path resolution: filesystem entries are resolved relative to the guidance
    file's own directory (aah/_resources/_references/discuss/); absolute paths
    are used as-is; missing files are dropped silently. URLs are never resolved
    against the filesystem.
    """
    # References are stored package-relative (base = the aah/ package dir),
    # e.g. "build-playbooks/constraints/compliance/gdpr.yaml" — the same base as
    # overlays.py playbook/domain-brief paths. Resolve against aah/, NOT the
    # guidance file's own dir, so every reference family shares one base.
    package_dir = framework_root / "aah"

    def resolve(ref: str) -> Path | str | None:
        if _is_url(ref):
            return ref
        p = Path(ref)
        candidate = p if p.is_absolute() else package_dir / p
        return candidate if candidate.exists() else None

    out: list[Path | str] = []
    seen: set[str] = set()

    def add(ref):
        resolved = resolve(ref)
        if resolved is None:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        out.append(resolved)

    for entry in guidance.get("always_references", []) or []:
        if eval_applies_when(entry.get("applies_when"), answers):
            add(entry["path"])

    def add_from_option_list(qs: list[dict]):
        for q in qs:
            chosen = answers.get(q["slug_id"])
            if chosen is None:
                continue
            for opt in q.get("inline_options", []) or []:
                if _matches(opt.get("value"), chosen):
                    for ref in opt.get("references", []) or []:
                        add(ref)

    add_from_option_list(guidance.get("mandatory_questions", []) or [])
    add_from_option_list(guidance.get("follow_up_questions", []) or [])

    return out


def active_references_detailed(
    guidance: dict, answers: dict[str, Any], framework_root: Path
) -> list[dict]:
    """Provenance-aware variant of active_reference_paths().

    Returns one dict per active reference (deduped by resolved source), each:
        {
          "source":       "<package-relative path (under aah/) | verbatim URL>",
          "kind":         "file" | "url",
          "exists":       <bool>,          # True for URLs (not checked) and present files
          "activated_by": [{"slug_id": ..., "value": ...} | {"slug_id": "always", ...}],
        }

    Same activation logic as active_reference_paths (chosen-option references[] +
    applicable always_references[]) but annotated with WHICH decision activated
    each ref, so the set is auditable. `active_reference_paths` is left untouched
    for its existing callers.
    """
    # References are stored package-relative (base = the aah/ package dir); see
    # active_reference_paths() above and the guidance file's comment.
    package_dir = framework_root / "aah"

    # resolved-key -> entry dict (dedupe while merging activated_by provenance)
    by_key: dict[str, dict] = {}
    order: list[str] = []

    def record(ref: str, activated_by: dict) -> None:
        is_url = _is_url(ref)
        if is_url:
            source, exists = ref, True
        else:
            p = Path(ref)
            candidate = p if p.is_absolute() else package_dir / p
            exists = candidate.exists()
            # Package-relative (relative to the aah/ package dir, e.g.
            # "_resources/_references/discuss/compliance/gdpr.yaml") — NOT repo-relative.
            # Matches the skill's canonical `$(aah path)/<path>` convention and the
            # form emitted by overlays.py / domain_briefs.loader, so every consumer
            # resolves ALL reference paths the same way. Always forward-slash: paths
            # are matched as strings against response provenance, so the separator
            # must be canonical cross-platform.
            try:
                source = candidate.relative_to(package_dir).as_posix()
            except ValueError:
                source = candidate.as_posix()
        key = source
        if key not in by_key:
            by_key[key] = {
                "source": source,
                "kind": "url" if is_url else "file",
                "exists": exists,
                "activated_by": [activated_by],
            }
            order.append(key)
        elif activated_by not in by_key[key]["activated_by"]:
            by_key[key]["activated_by"].append(activated_by)

    for entry in guidance.get("always_references", []) or []:
        if eval_applies_when(entry.get("applies_when"), answers):
            record(entry["path"], {"slug_id": "always", "value": entry.get("applies_when", "always")})

    def scan(qs: list[dict]) -> None:
        for q in qs:
            chosen = answers.get(q["slug_id"])
            if chosen is None:
                continue
            for opt in q.get("inline_options", []) or []:
                if _matches(opt.get("value"), chosen):
                    for ref in opt.get("references", []) or []:
                        record(ref, {"slug_id": q["slug_id"], "value": opt.get("value")})

    scan(guidance.get("mandatory_questions", []) or [])
    scan(guidance.get("follow_up_questions", []) or [])

    return [by_key[k] for k in order]


def _matches(option_value: Any, chosen: Any) -> bool:
    if isinstance(chosen, list):
        return option_value in chosen
    return option_value == chosen


# ─── CLI ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Discuss guidance-file loader")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="Dump the guidance file as JSON")
    p_load.add_argument("--path", help="Optional explicit path to decisions_guidance.yaml")

    p_mand = sub.add_parser("mandatory-questions",
                            help="List mandatory questions applicable given answers")
    p_mand.add_argument("--answers-json", default="{}",
                        help="JSON dict slug->value; used to evaluate applies_when")

    p_fu = sub.add_parser("follow-ups",
                          help="List follow-up questions for a chosen (parent_slug, parent_value)")
    p_fu.add_argument("--parent-slug", required=True)
    p_fu.add_argument("--parent-value", required=True)

    p_refs = sub.add_parser("active-references",
                            help="List active mandate catalog files given answers")
    p_refs.add_argument("--answers-json", default="{}")

    p_val = sub.add_parser(
        "validate-response-provenance",
        help="Compare active-references(answers) to a response's provenance block",
    )
    p_val.add_argument("--project-path", default=None,
                       help="Read the expected set from the registry's active_references "
                            "block (the single source of truth). Falls back to --answers-json "
                            "if omitted or the block is unavailable.")
    p_val.add_argument("--answers-json", default="{}",
                       help="Fallback: recompute the expected reference set from this "
                            "answers dict when --project-path is not given.")
    p_val.add_argument("--response-json", required=True,
                       help="The researcher-agent response as JSON (top-level dict)")

    for p in (sub.add_parser("whitelists"),
              sub.add_parser("forbidden-options"),
              sub.add_parser("service-scope")):
        pass  # no args

    args = parser.parse_args()

    if args.command == "load":
        path = Path(args.path).resolve() if getattr(args, "path", None) else None
        print(json.dumps(load_guidance(path), indent=2))
        return

    guidance = load_guidance()

    if args.command == "mandatory-questions":
        answers = json.loads(args.answers_json)
        print(json.dumps(mandatory_questions(guidance, answers), indent=2))
    elif args.command == "follow-ups":
        print(json.dumps(follow_ups_for(guidance, args.parent_slug, args.parent_value), indent=2))
    elif args.command == "active-references":
        answers = json.loads(args.answers_json)
        root = find_framework_root()
        refs = []
        for entry in active_reference_paths(guidance, answers, root):
            if isinstance(entry, str):        # URL — return verbatim
                refs.append(entry)
            else:                              # Path — relative to the aah/ package dir
                refs.append((entry.relative_to(root / "aah")).as_posix())
        print(json.dumps(refs, indent=2))
    elif args.command == "validate-response-provenance":
        response = json.loads(args.response_json)
        root = find_framework_root()
        expected: list[str] = []
        used_block = False
        # Preferred: read the SAME block the agent reads — the registry's
        # active_references.guidance_references[]. This keeps the coverage check
        # and the agent's fetch list in lockstep (single source of truth).
        if args.project_path:
            try:
                from aah.core.discuss import registry as _reg
                reg_data = _reg._load(Path(args.project_path))
                block = (reg_data.get("active_references") or {})
                grefs = block.get("guidance_references")
                if grefs is not None:
                    expected = [e["source"] for e in grefs if e.get("source")]
                    used_block = True
            except Exception:
                used_block = False
        # Fallback: recompute from an answers dict (block unavailable / no path).
        if not used_block:
            answers = json.loads(args.answers_json)
            for entry in active_reference_paths(guidance, answers, root):
                expected.append(entry if isinstance(entry, str) else entry.relative_to(root / "aah").as_posix())
        result = validate_response_provenance(
            expected,
            (response.get("provenance") or {}),
        )
        result["expected_source"] = "registry_block" if used_block else "recomputed_answers"
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            # aah CLI wrapper propagates exit(0/1) but converts exit(2) into a
            # retry-hint (see aah/cli.py:207), so use 1 for "validation failed".
            sys.exit(1)
    elif args.command == "whitelists":
        print(json.dumps(guidance.get("inline_whitelists") or {}, indent=2))
    elif args.command == "forbidden-options":
        print(json.dumps(forbidden_options(guidance), indent=2))
    elif args.command == "service-scope":
        print(json.dumps(service_scope(guidance), indent=2))
    else:
        parser.error("unknown command")


if __name__ == "__main__":
    main()
