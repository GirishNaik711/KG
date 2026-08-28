"""Structural tests for `.claude/skills/aah-build/SKILL.md`.

Phase 3 L6 (#247). After the L6 rewrite stripped ~538 lines of in-skill
verification choreography (orchestrator now owns it via verify.py),
these tests pin the surviving structure so the skill doesn't
silently drift back toward duplicating orchestrator behavior:

  1. Line count cap — guards against re-accumulation of choreography.
  2. Dispatch prompt template required headings — load-bearing for
     aah-feature-implementer behavior.
  3. Orchestrator-loop ABSOLUTE RULES — load-bearing doctrine; if any
     of these vanish, the skill stops enforcing the orchestrator-as-
     source-of-truth contract.
  4. Legacy-choreography term cap — `validate_checkpoint`,
     `aah-runtime-validator`, `aah-regression-tester` should appear only in
     short legacy-mode pointer notes, not as operational sections.
  5. Implementation-policy auto-injection hook present — keeps the
     wiring alive even though the generator script ships separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILL_PATH = (
    Path(__file__).parents[2]
    / ".claude" / "skills" / "aah-build" / "SKILL.md"
)


@pytest.fixture
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


class TestSkillStructure:
    def test_skill_under_600_lines(self, skill_text):
        """Pre-rewrite: 1052 lines. Post-rewrite target: ~514 lines.
        Hard cap at 600 leaves headroom for ergonomic future additions
        without inviting accumulation back to the original size."""
        line_count = len(skill_text.splitlines())
        assert line_count <= 600, (
            f"SKILL.md grew back to {line_count} lines; the L6 strip "
            f"regressed. If a new step is justified, prove it before "
            f"raising this cap."
        )

    def test_dispatch_template_intact(self, skill_text):
        """Step 9's prompt template carries the contract every
        aah-feature-implementer dispatch relies on. If any of these
        headings vanish, dispatched agents lose their structured
        context and QA failures spike."""
        for required in (
            "**Runtime Context:**",
            "**Working Environment:**",
            "## Acceptance Criteria",
            "## Test Cases",
            "## Implementation Rules",
            "## Self-QA Checklist",
        ):
            assert required in skill_text, (
                f"dispatch template lost required heading: {required!r}"
            )

    def test_orchestrator_loop_rules_intact(self, skill_text):
        """The ABSOLUTE RULES (CLAUDE.md doctrine) must persist. These
        are the rules that prevent agents from skipping gates or
        advancing waves without orchestrator authorization. Drop any
        of them and the orchestrator-as-source-of-truth model breaks."""
        for rule_marker in (
            "NEVER decide a gate",
            "NEVER advance",
            "ALWAYS call",
            "NEVER skip the expertise update",
            "NEVER run action commands without orchestrator authorization",
            "NEVER use `run_in_background` for `dispatch_sequential`",
        ):
            assert rule_marker in skill_text, (
                f"orchestrator-loop ABSOLUTE RULE lost: {rule_marker!r}"
            )

    def test_no_leftover_legacy_choreography(self, skill_text):
        """Phase 3 L1 default-flip moved verification into verify.py.
        These references should appear only in short legacy-mode
        pointer notes — not as operational sections.

        If counts grow above the cap, undeleted choreography has
        crept back in (or someone is documenting verify.py's behavior
        in the skill instead of in verify.py's docstring)."""
        for term, max_occurrences in [
            ("validate_checkpoint", 1),
            ("aah-runtime-validator", 1),
            ("aah-regression-tester", 1),
            ("run_system_checkpoint", 3),  # action-map row + 1-2 mentions in legacy notes
        ]:
            count = skill_text.count(term)
            assert count <= max_occurrences, (
                f"{term!r} appears {count} times — should be ≤{max_occurrences}. "
                f"Legacy choreography may have crept back in; verify.py is "
                f"the source of truth, not the skill."
            )

    def test_implementation_policy_hook_present(self, skill_text):
        """The auto-injection block must be present even though the
        generator script ships separately. Without this wiring, when
        the generator lands in a future PR it has nothing to plug
        into. The structural test keeps the hook alive."""
        # The Step 2c heading.
        assert "Implementation Policy Auto-Injection" in skill_text, (
            "Step 2c (auto-injection) is gone — wiring lost"
        )
        # The dispatch-template prepend point.
        assert "POLICY_TEXT_OR_EMPTY" in skill_text or "POLICY_TEXT" in skill_text, (
            "Dispatch template's policy-prepend variable is missing"
        )
        # The path that the skill reads.
        assert "implementation-policy.md" in skill_text, (
            "The .aah/buildation-policy.md path reference is missing"
        )


# ---------------------------------------------------------------------------
# aah-expertise SKILL.md — marker-write step uses deterministic writer
# ---------------------------------------------------------------------------


EXPERTISE_SKILL_PATH = (
    Path(__file__).parents[2]
    / ".claude" / "skills" / "aah-expertise" / "SKILL.md"
)


@pytest.fixture
def expertise_skill_text() -> str:
    return EXPERTISE_SKILL_PATH.read_text(encoding="utf-8")


class TestExpertiseSkillStructure:
    """Structural pin: the expertise skill's marker-write step must
    invoke the deterministic Python writer, not write the marker file
    by hand. If a future edit reverts to hand-writing the marker, this
    test fails before the regression ships."""

    def test_marker_write_step_invokes_wave_markers_cli(self, expertise_skill_text):
        """Step 6 (or wherever the wave-{N}-expertise-updated.json
        marker is written) MUST invoke
        ``aah.core.build.wave_markers``. The exact step
        number can move; what cannot move is the invocation."""
        assert "aah.core.build.wave_markers" in expertise_skill_text, (
            "aah-expertise SKILL.md no longer references the wave_markers "
            "writer module. The marker MUST be produced by the Python "
            "writer (which captures content hashes atomically), never "
            "written by hand. If you intentionally removed this "
            "reference, also update the orchestrator's always-on "
            "content-bound freshness gate — those changes ship together."
        )
        assert "write-expertise" in expertise_skill_text, (
            "aah-expertise SKILL.md no longer references the "
            "'write-expertise' subcommand. The CLI contract is broken — "
            "either the SKILL is calling the wrong command or the writer "
            "module's CLI was renamed without updating the skill."
        )

    def test_marker_write_step_warns_against_handwriting(self, expertise_skill_text):
        """The skill prose must include a 'do NOT write by hand' clause —
        otherwise a future Claude session may interpret the JSON example
        in surrounding text as licence to write the file directly,
        bypassing the writer."""
        # Match either casing — the warning's exact wording is editable
        # but the intent (do-not-hand-write) must remain.
        lower = expertise_skill_text.lower()
        assert (
            "do not write the marker file by hand" in lower
            or "do not write the marker by hand" in lower
            or "never write the marker by hand" in lower
        ), (
            "aah-expertise SKILL.md lost the warning that the marker "
            "must not be written by hand. Hand-written markers fail the "
            "content-bound freshness check; without this warning, future edits are "
            "likely to reintroduce the handwritten path."
        )
