"""Structural tests for the QA-routing module extracted from orchestrator.py.

The routing *behaviour* is already covered end-to-end by test_qa_synthesis.py,
test_qa_human_review.py, test_verification_profiles.py and
test_fail_closed_evidence.py — those tests drive real project fixtures and were
repointed at this module rather than duplicated here.

What this file pins is what the extraction itself could silently break and those
tests would not notice:

  - the dependency stays one-way (no import back into ``orchestrator``), which is
    the property that let the whole cluster move at all
  - the cluster is *closed* — it calls nothing that stayed behind, so a future
    edit that reaches back into the orchestrator fails here rather than at import
    time in production
  - the two ``feature-qa-*`` command strings exist in exactly one place, and carry
    exactly the flags the CLI's parsers accept
  - the rework cap has one definition, not a copy in each module
"""

from __future__ import annotations

import ast
import builtins
import inspect
import pathlib
import symtable

from aah.core.build import orchestrator, qa_routing
from aah.core.build.qa_routing import MAX_QA_REWORK_ATTEMPTS, _human_review_action

_CLUSTER = {
    "_feature_profile_level",
    "_human_review_action",
    "_feature_qa_state_action",
    "_feature_qa_final_approved",
    "_features_needing_qa",
}


def test_the_whole_qa_cluster_lives_here():
    """All five routing functions live together as a closed set (see plan §4).

    Splitting it anywhere forces a function-local ``orchestrator`` import, which
    the plan's definition of done forbids.
    """
    assert _CLUSTER <= {
        n.name
        for n in ast.parse(inspect.getsource(qa_routing)).body
        if isinstance(n, ast.FunctionDef)
    }


def test_dependency_is_one_way():
    """``orchestrator`` imports this module; this module must never import back.

    Not cosmetic: a back-import would be a cycle Python resolves only by
    accident of import order, and the guard against it is cheap.
    """
    src = inspect.getsource(qa_routing)
    assert "aah.core.build.orchestrator" not in src
    assert "import orchestrator" not in src


def test_no_function_local_imports():
    """Every routing dependency remains imported at module scope."""
    tree = ast.parse(inspect.getsource(qa_routing))
    local = [
        (fn.name, node.lineno)
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert local == [], local


def test_cluster_calls_nothing_that_stayed_in_the_orchestrator():
    """The closure property, checked directly rather than assumed.

    Every *global* name these functions reference must resolve to something this
    module defines or imports at module scope. A name that resolves to neither is
    a function still sitting in ``orchestrator.py`` — which is the failure mode
    this whole extraction was shaped to avoid.

    Scope analysis comes from ``symtable`` rather than a hand-rolled AST walk;
    locals arrive via tuple unpacking, ``for`` targets and comprehensions, and
    getting that wrong produces false positives rather than false negatives.
    """
    src = inspect.getsource(qa_routing)
    module = symtable.symtable(src, "qa_routing.py", "exec")
    at_module_scope = {
        s.get_name() for s in module.get_symbols() if s.is_assigned() or s.is_imported()
    }
    known = at_module_scope | set(dir(builtins))

    def globals_of(table):
        out = set()
        for sym in table.get_symbols():
            if sym.is_global() and sym.get_name() not in known:
                out.add(sym.get_name())
        for child in table.get_children():
            out |= globals_of(child)
        return out

    unresolved = {
        (fn.get_name(), name)
        for fn in module.get_children()
        if fn.get_name() in _CLUSTER
        for name in globals_of(fn)
    }
    assert unresolved == set(), unresolved


def test_the_two_review_commands_are_spelled_out_once():
    """§2.2's whole point: a change to the CLI's flags is one edit, not three.

    The f-strings are checked against the real source of every module under
    ``aah/core/build`` so a helpful copy elsewhere is caught too.
    """
    for stem, count in (
        ("feature-qa-approve", 1),
        ("feature-qa-request-rework", 1),
    ):
        hits = [
            p.name
            for p in pathlib.Path("aah/core/build").glob("*.py")
            if f"validate_checkpoint {stem}" in p.read_text()
        ]
        assert hits == ["qa_routing.py"], (stem, hits)
        assert inspect.getsource(qa_routing).count(f"validate_checkpoint {stem} ") == count


def test_human_review_payload_leaves_wave_for_the_caller():
    """``wave`` is a placeholder — the router fills it in.

    A payload that silently carried ``wave: 0`` would render a banner naming the
    wrong wave, which is worse than an obviously-absent one.
    """
    action = _human_review_action(
        "F007",
        {"subject_sha": "deadbeefcafebabe", "subject_branch": "feature/F007",
         "subject_path": "/w/F007"},
        {"current_attempt": 3, "attempts_total": 5, "rework_count": 3},
        trigger="rework_cap",
        reason="cap reached",
    )
    assert action["action"] == "human_review_required"
    assert action["wave"] is None
    assert action["feature_id"] == "F007"
    assert action["authoritative_attempt"] == "qa-results/F007/attempt-003.json"


def test_emitted_commands_carry_only_flags_the_cli_accepts():
    """The commands must run as printed.

    ``validate_checkpoint``'s ``feature-qa-approve`` parser accepts only
    ``--project-path`` and ``--feature-id``; ``feature-qa-request-rework`` adds
    ``--rationale``. An extra flag here — ``--subject-sha``, ``--reviewer`` —
    makes argparse exit non-zero on a command an operator was told to paste, at a
    gate where nothing else is watching. This test reads the real parsers rather
    than restating their flags, so tightening or widening the CLI shows up here.
    """
    from aah.core.build import validate_checkpoint

    src = inspect.getsource(validate_checkpoint)
    action = _human_review_action(
        "F007",
        {"subject_sha": "deadbeefcafebabe"},
        {"current_attempt": 1},
        trigger="qa_verdict",
        reason="fail",
    )

    for key in ("approve_command", "request_rework_command"):
        command = action[key]
        # The subject SHA is bound to the QA attempt, never pasted by a human.
        assert "--subject-sha" not in command, key
        assert "deadbeef" not in command, key
        assert "--reviewer" not in command, key
        flags = {tok for tok in command.split() if tok.startswith("--")}
        for flag in flags:
            assert f'"{flag}"' in src, (key, flag)

    assert "--rationale" in action["request_rework_command"]
    assert "--rationale" not in action["approve_command"]


def test_rework_cap_is_defined_once():
    """The cap moved with the router; the orchestrator must not keep a copy that
    can drift out of step with the one the router enforces."""
    assert MAX_QA_REWORK_ATTEMPTS == 3
    assert "MAX_QA_REWORK_ATTEMPTS = " not in inspect.getsource(orchestrator)
    # MAX_REWORK_ATTEMPTS is a *different* cap (current-wave rework, not QA) and
    # legitimately stays in the orchestrator. Guard against conflating them.
    assert "MAX_REWORK_ATTEMPTS = 3" in inspect.getsource(orchestrator)


def test_orchestrator_reaches_routing_through_module_scope_imports():
    """The router is imported once at the top, not re-imported inside gates."""
    tree = ast.parse(inspect.getsource(orchestrator))
    top = {
        a.name
        for n in tree.body
        if isinstance(n, ast.ImportFrom) and n.module == "aah.core.build.qa_routing"
        for a in n.names
    }
    assert top == {
        "_feature_qa_final_approved",
        "_feature_qa_state_action",
        "_features_needing_qa",
    }
