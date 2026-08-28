#!/usr/bin/env python3
"""Constraint validation gate for /aah-discuss (HTML §Step-10a).

Runs Checks A-F against the slug registry using the guidance file plus the
active mandate catalogs (see reference-file constraint model in the HTML
"Deterministic Backbone" section).

Checks:
    A · Mandatory coverage       Every mandatory slug (given applies_when) has
                                 a non-empty response. Both `user` and
                                 `inferred` sources count.
    B · Forbidden options        No decision presents or picks an option
                                 listed in guidance.forbidden_options[].
    C · Whitelist adherence      For slugs in inline_whitelists, the recorded
                                 response must be in the whitelist.
    D · Service scope            Any chosen option value that appears in
                                 service_scope.forbidden[] is a violation;
                                 approved-list is informational.
    E · Mandated-option          For every active mandate file, if a chosen
                                 option value is in closes_options[].eliminates[]
                                 for that slug, it's a **blocking violation**
                                 (chosen == eliminated). If the eliminated
                                 option was merely *presented* but not chosen,
                                 it's a **flag** — WARN, not FAIL.
    F · Provenance               For each eliminated option in
                                 decision.options_eliminated[], the reason
                                 must cite a constraint_id (mandate_id). WARN
                                 only — not a blocking check.

Exit codes:
    0  All checks pass (F warnings may be present).
    2  One or more blocking violations. Writes constraint_audit.passed = false
       to the registry and prints the violation table to stderr so the calling
       skill can drive the surgical re-float loop.

The constraint_audit block is always written back to the registry.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from aah.core.common.config import require_project_path
from aah.core.common.io_utils import read_yaml
from aah.core.discuss import guidance as g
from aah.core.discuss import registry as reg


# ─── helpers ─────────────────────────────────────────────────────────────

def _values_in(response: Any) -> list:
    """Coerce a response into an iterable of chosen values."""
    if response is None:
        return []
    if isinstance(response, list):
        return list(response)
    return [response]


def _load_mandate_file(path: Path) -> dict:
    return read_yaml(path)


def _collect_active_eliminations(active_paths: list) -> list[dict]:
    """Flatten `closes_options[]` across all active mandate files.

    URLs (str entries) are skipped — Check E is filesystem-only. The researcher
    agent handles URL-based guidance separately via WebFetch during option
    generation; Check E's job here is deterministic set-membership over
    slug-native closes_options[], which URLs don't participate in.

    Returns a list of dicts:
        {mandate_id, slug_id | ddr_id, eliminates: [...], source_file: str}
    """
    out: list[dict] = []
    for path in active_paths:
        if isinstance(path, str):     # URL — Check E doesn't consume these
            continue
        data = _load_mandate_file(path)
        for mandate in data.get("mandates", []) or []:
            mandate_id = mandate.get("mandate_id", "<unknown>")
            for close in mandate.get("closes_options", []) or []:
                out.append({
                    "mandate_id": mandate_id,
                    "slug_id": close.get("slug_id"),
                    "ddr_id": close.get("ddr_id"),
                    "eliminates": list(close.get("eliminates", []) or []),
                    "source_file": str(path.name),
                })
    return out


# ─── individual checks ───────────────────────────────────────────────────

def check_a_coverage(guidance: dict, registry: dict) -> tuple[str, list[dict]]:
    """Every mandatory slug (given applies_when) must have a non-empty response.

    A `pre_resolved` entry counts as answered. `source: user | inferred` both count.
    """
    answers = g.collect_answers(registry)
    required = g.mandatory_questions(guidance, answers)
    violations: list[dict] = []
    for q in required:
        slug = q["slug_id"]
        val = answers.get(slug)
        if val is None or val == "" or (isinstance(val, list) and len(val) == 0):
            violations.append({
                "check": "A",
                "slug_id": slug,
                "detail": f"mandatory slug {slug!r} has no response",
                "severity": "blocking",
            })
    return ("pass" if not violations else "fail", violations)


def check_b_forbidden(guidance: dict, registry: dict) -> tuple[str, list[dict]]:
    forbidden = set(g.forbidden_options(guidance))
    if not forbidden:
        return ("pass", [])
    violations: list[dict] = []
    for d in registry.get("decisions", []) or []:
        for opt in d.get("options_presented", []) or []:
            val = opt.get("value") if isinstance(opt, dict) else opt
            if val in forbidden:
                violations.append({
                    "check": "B",
                    "slug_id": d["slug_id"],
                    "detail": f"forbidden option {val!r} was presented",
                    "severity": "blocking",
                })
        for v in _values_in(d.get("response")):
            if v in forbidden:
                violations.append({
                    "check": "B",
                    "slug_id": d["slug_id"],
                    "detail": f"forbidden option {v!r} was chosen",
                    "severity": "blocking",
                })
    return ("pass" if not violations else "fail", violations)


def check_c_whitelist(guidance: dict, registry: dict) -> tuple[str, list[dict]]:
    whitelists: dict = guidance.get("inline_whitelists") or {}
    if not whitelists:
        return ("pass", [])
    answers = g.collect_answers(registry)
    violations: list[dict] = []
    for slug, allowed in whitelists.items():
        chosen = answers.get(slug)
        if chosen is None:
            # slug not answered → coverage is Check A's problem, not C's
            continue
        allowed_set = set(allowed)
        for v in _values_in(chosen):
            if v not in allowed_set:
                violations.append({
                    "check": "C",
                    "slug_id": slug,
                    "detail": f"response {v!r} not in whitelist {list(allowed_set)}",
                    "severity": "blocking",
                })
    return ("pass" if not violations else "fail", violations)


def check_d_service_scope(guidance: dict, registry: dict) -> tuple[str, list[dict]]:
    scope = g.service_scope(guidance)
    if not scope["forbidden"]:
        return ("pass", [])
    forbidden_set = set(scope["forbidden"])
    violations: list[dict] = []
    for d in registry.get("decisions", []) or []:
        for v in _values_in(d.get("response")):
            if v in forbidden_set:
                violations.append({
                    "check": "D",
                    "slug_id": d["slug_id"],
                    "detail": f"chosen service {v!r} is in service_scope.forbidden",
                    "severity": "blocking",
                })
    return ("pass" if not violations else "fail", violations)


def check_e_mandated_options(guidance: dict, registry: dict,
                             framework_root: Path) -> tuple[str, list[dict]]:
    """Chosen ∈ eliminates[] → block; presented-but-not-chosen ∈ eliminates[] → flag.

    Ref: HTML §"Blocking vs flagging distinction".
    """
    answers = g.collect_answers(registry)
    active_paths = g.active_reference_paths(guidance, answers, framework_root)
    eliminations = _collect_active_eliminations(active_paths)
    if not eliminations:
        return ("pass", [])

    findings: list[dict] = []
    for elim in eliminations:
        slug = elim["slug_id"]
        if not slug:
            # DDR-keyed eliminations (legacy) — skip for slug-native validation
            continue
        forbidden_vals = set(elim["eliminates"])
        chosen = answers.get(slug)

        # Blocking — chosen ∈ eliminates
        for v in _values_in(chosen):
            if v in forbidden_vals:
                findings.append({
                    "check": "E",
                    "slug_id": slug,
                    "detail": (
                        f"chosen option {v!r} is eliminated by "
                        f"{elim['mandate_id']} in {elim['source_file']}"
                    ),
                    "severity": "blocking",
                    "mandate_id": elim["mandate_id"],
                })

        # Flagging — option presented but not chosen, and it's eliminated
        for d in registry.get("decisions", []) or []:
            if d.get("slug_id") != slug:
                continue
            chosen_vals = set(_values_in(d.get("response")))
            for opt in d.get("options_presented", []) or []:
                val = opt.get("value") if isinstance(opt, dict) else opt
                if val in forbidden_vals and val not in chosen_vals:
                    findings.append({
                        "check": "E",
                        "slug_id": slug,
                        "detail": (
                            f"option {val!r} was presented but is eliminated by "
                            f"{elim['mandate_id']} — reviewer should have filtered "
                            f"it out at generation"
                        ),
                        "severity": "flag",
                        "mandate_id": elim["mandate_id"],
                    })

    blocking = [f for f in findings if f["severity"] == "blocking"]
    return ("pass" if not blocking else "fail", findings)


def check_f_provenance(registry: dict) -> tuple[str, list[dict]]:
    """Every eliminated option must cite a constraint_id / mandate_id. WARN only."""
    warnings: list[dict] = []
    for d in registry.get("decisions", []) or []:
        for elim in d.get("options_eliminated", []) or []:
            if isinstance(elim, dict) and not (elim.get("constraint_id") or elim.get("mandate_id")):
                warnings.append({
                    "check": "F",
                    "slug_id": d["slug_id"],
                    "detail": (
                        f"eliminated option {elim.get('option')!r} missing "
                        "constraint_id/mandate_id provenance"
                    ),
                    "severity": "warn",
                })
    return ("pass" if not warnings else "warn", warnings)


# ─── orchestration ───────────────────────────────────────────────────────

def run_all(project_root: Path) -> dict:
    guidance = g.load_guidance()
    registry = reg.op_read(project_root)
    framework_root = g.find_framework_root()

    checks = {}
    all_findings: list[dict] = []

    for name, fn in [
        ("A_mandatory_coverage",  lambda: check_a_coverage(guidance, registry)),
        ("B_forbidden_options",   lambda: check_b_forbidden(guidance, registry)),
        ("C_whitelist",           lambda: check_c_whitelist(guidance, registry)),
        ("D_service_scope",       lambda: check_d_service_scope(guidance, registry)),
        ("E_mandated_options",    lambda: check_e_mandated_options(guidance, registry, framework_root)),
    ]:
        status, findings = fn()
        detail = _summarize_findings(findings)
        checks[name] = {"result": status, "detail": detail}
        all_findings.extend(findings)

    f_status, f_findings = check_f_provenance(registry)
    checks["F_provenance"] = {"result": f_status,
                              "detail": _summarize_findings(f_findings) or "no eliminations recorded"}
    all_findings.extend(f_findings)

    blocking = [f for f in all_findings if f["severity"] == "blocking"]
    flags = [f for f in all_findings if f["severity"] == "flag"]
    warns = [f for f in all_findings if f["severity"] == "warn"]

    audit = {
        "passed": len(blocking) == 0,
        "ran_at": date.today().isoformat(),
        "checks": checks,
        "violations": blocking,
        "flags": flags,
        "warnings": warns,
    }
    reg.op_set_constraint_audit(project_root, json.dumps(audit))
    return audit


def _summarize_findings(findings: list[dict]) -> str:
    if not findings:
        return "clean"
    parts = []
    for f in findings:
        parts.append(f"[{f['severity']}] {f.get('slug_id','-')}: {f['detail']}")
    return " · ".join(parts)


# ─── CLI ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Discuss constraint validation gate (Checks A-F)")
    parser.add_argument("--project-path", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of human summary")
    args = parser.parse_args()

    project_root = require_project_path(args.project_path)
    audit = run_all(project_root)

    if args.json:
        print(json.dumps(audit, indent=2, default=str))
    else:
        _print_human(audit)

    sys.exit(0 if audit["passed"] else 2)


def _print_human(audit: dict) -> None:
    print(f"Constraint gate: {'PASS' if audit['passed'] else 'FAIL'}")
    print(f"Ran at: {audit['ran_at']}")
    print()
    print("Check results:")
    for name, info in audit["checks"].items():
        print(f"  {name:<28} {info['result'].upper():<5}  {info['detail']}")
    if audit["violations"]:
        print("\nBLOCKING violations:")
        for v in audit["violations"]:
            print(f"  · [{v['check']}] {v.get('slug_id','-')}: {v['detail']}")
    if audit.get("flags"):
        print("\nFlags (non-blocking):")
        for f in audit["flags"]:
            print(f"  · [{f['check']}] {f.get('slug_id','-')}: {f['detail']}")
    if audit.get("warnings"):
        print("\nWarnings:")
        for w in audit["warnings"]:
            print(f"  · [{w['check']}] {w.get('slug_id','-')}: {w['detail']}")


if __name__ == "__main__":
    main()
