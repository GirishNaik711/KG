"""E2E tests for industry-domain intelligence (Domain Briefs + Build Playbooks).

Covers:
  - Build Playbook loader and published-flag gating
  - Back-compat shim for the legacy knowledge.domain_loader path
  - Domain Brief loader with ancestor-merge inheritance
  - Taxonomy validation
  - Offline detector ranking
  - LLM classifier fallback behavior
  - SessionStart context injection (summary + phase gating)
  - get-section on-demand retrieval
  - Manager add/reindex flow
  - Intake and manifest schema round-tripping
  - Artifact guard enforcing `## Domain Alignment` when a domain is attached

Fast tests (no Claude): run with `pytest ... -m "not e2e"`
Agent SDK tests (real Claude session): run with `pytest ... -m e2e`
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.e2e.conftest import e2e, skip_no_claude, skip_no_api_key


# ─── Helpers ──────────────────────────────────────────────────────────

def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rapids-run", *args], capture_output=True, text=True, check=check, timeout=30
    )


# ─── Group A: Build Playbook loader ───────────────────────────────────

class TestBuildPlaybookLoader:
    def test_validate_passes(self):
        """build-playbooks/ library passes validation."""
        result = _run("aah.core.playbooks.validate", check=False)
        report = json.loads(result.stdout)
        assert report["ok"], f"errors: {report['errors']}"
        # Exemplar ai-infra-platforms must be published
        assert "ai-infra-platforms" in report["published"]
        # ai-applications ships dormant
        assert "ai-applications" in report["unpublished"]

    def test_loader_respects_published_flag(self):
        """load_playbook returns None for unpublished playbooks by default."""
        from aah.core.playbooks.loader import load_playbook

        # Published — returns content
        playbook = load_playbook("ai-infra-platforms")
        assert playbook is not None
        assert playbook.get("published") is True
        assert len(playbook.get("patterns") or []) >= 10

        # Unpublished — returns None
        assert load_playbook("ai-applications") is None

        # Unpublished with override — returns content
        forced = load_playbook("ai-applications", include_unpublished=True)
        assert forced is not None
        assert forced.get("published") is False

    def test_legacy_shim_resolves_agentic_ai_to_new_playbook(self):
        """Back-compat: old `agentic-ai` key still resolves."""
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from aah.core.knowledge.domain_loader import load_domain_knowledge

            result = load_domain_knowledge("agentic-ai")
            assert result is not None
            # New playbook has 15 patterns (migrated + expanded)
            assert len(result.get("patterns") or []) >= 10
            # DeprecationWarning emitted
            assert any(
                issubclass(w.category, DeprecationWarning) for w in caught
            )


# ─── Group B: Domain Brief loader and inheritance ─────────────────────

class TestDomainBriefLoader:
    def test_validate_passes(self):
        result = _run("aah.core.domain_briefs.validate", check=False)
        report = json.loads(result.stdout)
        assert report["ok"], f"errors: {report['errors']}"
        assert report["node_count"] >= 10
        assert report["leaf_count"] >= 5

    def test_loader_merges_inheritance(self):
        """Leaf sees union of ancestor + leaf regulations and glossary."""
        from aah.core.domain_briefs.loader import load_node

        leaf = load_node(
            "financial-services/commercial-banking/commercial-client-onboarding/kyb-due-diligence"
        )
        assert leaf is not None

        # Ancestor regs (from financial-services industry node)
        reg_names = {r.get("name") for r in (leaf.get("regulations") or [])}
        assert any("BSA" in name or "AML" in name for name in reg_names if name), (
            f"expected industry-level BSA/AML regs merged into leaf; got {reg_names}"
        )

        # Leaf-specific reg (OFAC)
        assert any("OFAC" in name for name in reg_names if name), (
            f"expected leaf-level OFAC regs; got {reg_names}"
        )

        # Glossary merge — industry term KYC + leaf term SDN
        glossary_terms = {g.get("term") for g in (leaf.get("glossary") or [])}
        assert "KYC" in glossary_terms  # industry-level
        assert "SDN" in glossary_terms  # leaf-level

    def test_taxonomy_list_all_and_leaves(self):
        """list_all_ids and list_leaf_ids return coherent sets."""
        from aah.core.domain_briefs.loader import list_all_ids, list_leaf_ids

        all_ids = set(list_all_ids())
        leaves = set(list_leaf_ids())

        assert leaves.issubset(all_ids)
        # financial-services is never a leaf
        assert "financial-services" in all_ids
        assert "financial-services" not in leaves
        # KYB-due-diligence is a leaf
        assert (
            "financial-services/commercial-banking/commercial-client-onboarding/kyb-due-diligence"
            in leaves
        )


# ─── Group C: Detector ranking ────────────────────────────────────────

class TestDetector:
    def test_ranks_commercial_onboarding_top(self):
        """Problem statement with commercial-onboarding + KYB keywords
        puts commercial-client-onboarding branch at the top."""
        from aah.core.domain_briefs.detector import rank_candidates

        results = rank_candidates(
            "Build an agent that onboards commercial banking clients with KYB",
            intake_answers=["We need beneficial-owner discovery and sanctions screening"],
        )
        assert results, "expected at least one candidate"
        top_id = results[0]["id"]
        assert "commercial-client-onboarding" in top_id or "commercial-banking" in top_id, (
            f"expected top candidate under commercial-banking; got {top_id}"
        )

    def test_returns_empty_for_unrelated(self):
        from aah.core.domain_briefs.detector import rank_candidates

        results = rank_candidates(
            "Build a travel planning agent that finds flights and hotels",
            intake_answers=["Use Amadeus APIs"],
        )
        # Zero or at most weak hits
        assert all(r["score"] <= 3 for r in results), (
            f"travel-planner shouldn't strongly match any fin-svc node; got {results}"
        )

    def test_classifier_fallback_when_no_api_key(self, monkeypatch):
        """Without ANTHROPIC_API_KEY, classifier returns detector top with
        source=fallback_detector."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from aah.core.domain_briefs.classifier import classify

        result = classify(
            "Build an agent for commercial client onboarding with KYB",
            intake_answers=[],
        )
        assert result["source"] == "fallback_detector"
        assert result["confidence"] == "low"
        assert result["chosen_id"] is not None
        assert "commercial" in result["chosen_id"] or "financial" in result["chosen_id"]


# ─── Group D: Context rendering ───────────────────────────────────────

class TestContextRendering:
    def test_summary_requires_research_or_analysis_phase(
        self, project_with_commercial_onboarding_intake
    ):
        """Context summary is empty during implement/plan phases."""
        from aah.core.domain_briefs.context import build_domain_context_summary
        import yaml as pyyaml

        project_path = project_with_commercial_onboarding_intake
        # Currently set to 'research' — should produce content
        summary = build_domain_context_summary(project_path)
        assert "## Domain Intelligence" in summary
        assert "KYB" in summary
        assert "BSA" in summary or "AML" in summary

        # Flip to implement — should return empty
        manifest_path = project_path / ".rapids" / "manifest.yaml"
        manifest = pyyaml.safe_load(manifest_path.read_text())
        manifest["current_phase"] = "implement"
        manifest_path.write_text(pyyaml.dump(manifest, sort_keys=False))

        empty = build_domain_context_summary(project_path)
        assert empty == "", f"expected empty summary in implement phase; got: {empty!r}"

    def test_summary_budget(self, project_with_commercial_onboarding_intake):
        """Summary stays within the ~2 KB budget."""
        from aah.core.domain_briefs.context import build_domain_context_summary

        summary = build_domain_context_summary(project_with_commercial_onboarding_intake)
        assert 0 < len(summary) < 3000, (
            f"summary length {len(summary)} outside expected range"
        )

    def test_get_section_processes_returns_merged_content(
        self, project_with_commercial_onboarding_intake
    ):
        from aah.core.domain_briefs.context import get_section

        out = get_section(project_with_commercial_onboarding_intake, "processes")
        assert "## Processes" in out
        # Leaf processes
        assert "Entity Verification" in out or "KYB" in out

    def test_get_section_glossary(self, project_with_commercial_onboarding_intake):
        from aah.core.domain_briefs.context import get_section

        out = get_section(project_with_commercial_onboarding_intake, "glossary")
        assert "KYB" in out
        assert "UBO" in out


# ─── Group E: Manager CRUD ────────────────────────────────────────────

class TestManager:
    def test_add_and_remove(self, tmp_path):
        """Add a node programmatically; it becomes visible; remove cleans up.

        The manager writes to the real domain-briefs/ tree (it's repo-root
        aware), so this test preserves and restores _taxonomy.yaml exactly
        as it was — the YAML-roundtrip through the manager reformats the
        file even if the logical content is unchanged.
        """
        from aah.core.domain_briefs import manager
        from aah.core.domain_briefs.loader import find_briefs_root, list_all_ids

        root = find_briefs_root()
        assert root is not None
        taxonomy_path = root / "_taxonomy.yaml"
        taxonomy_backup = taxonomy_path.read_text()

        test_id = "financial-services/commercial-banking/test-node-for-e2e"
        try:
            manager.add_node(
                node_id=test_id,
                name="Test Node For E2E",
                description="Transient test node created by test_industry_domains_e2e.",
                keywords_strong=["test-e2e-marker-unique"],
                keywords_moderate=["ephemeral"],
            )
            assert test_id in list_all_ids()
        finally:
            try:
                manager.remove_node(test_id, force=True)
            except Exception:
                pass
            # Restore taxonomy file verbatim (manager's YAML roundtrip
            # reformats even when logical content matches).
            taxonomy_path.write_text(taxonomy_backup)
            # Confirm removal
            assert test_id not in list_all_ids()


# ─── Group F: Intake + manifest schema ────────────────────────────────

class TestSchemaRoundTrip:
    def test_intake_persists_industry_domain(self, scaffolded_project):
        """intake.json accepts and retains the industry_domain field."""
        rapids = scaffolded_project / ".rapids"
        intake_path = rapids / "intake.json"
        if not intake_path.exists():
            # Scaffold doesn't auto-create intake.json; seed a default.
            from aah.core.intake.intake import get_default_intake
            intake_path.write_text(json.dumps(get_default_intake(), indent=2))
        intake = json.loads(intake_path.read_text())
        intake["industry_domain"] = {
            "path": "financial-services/commercial-banking/commercial-client-onboarding",
            "confidence": "high",
            "source": "classifier",
            "rationale": "test rationale",
            "classified_at": "2026-04-20T00:00:00+00:00",
        }
        intake_path.write_text(json.dumps(intake, indent=2))

        reloaded = json.loads(intake_path.read_text())
        assert reloaded["industry_domain"]["path"] == intake["industry_domain"]["path"]

    def test_manifest_industry_domain_path_roundtrips(self, scaffolded_project):
        rapids = scaffolded_project / ".rapids"
        manifest_path = rapids / "manifest.yaml"

        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["industry_domain_path"] = (
            "financial-services/commercial-banking/commercial-client-onboarding"
        )
        manifest_path.write_text(yaml.dump(manifest, sort_keys=False))

        # Load via aah.core.common.manifest
        from aah.core.common.manifest import load_manifest

        reloaded = load_manifest(manifest_path)
        assert reloaded["industry_domain_path"] == (
            "financial-services/commercial-banking/commercial-client-onboarding"
        )

    def test_legacy_project_type_shim(self, scaffolded_project):
        """Legacy project_type=ai-agents translates to ai-infra-platforms."""
        import warnings

        rapids = scaffolded_project / ".rapids"
        manifest_path = rapids / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["project_type"] = "ai-agents"  # legacy value
        manifest_path.write_text(yaml.dump(manifest, sort_keys=False))

        from aah.core.common.manifest import load_manifest

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            reloaded = load_manifest(manifest_path)
        assert reloaded["project_type"] == "ai-infra-platforms"
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


# ─── Group G: Artifact guard enforces Domain Alignment ────────────────

class TestArtifactGuard:
    def test_guard_blocks_artifact_without_domain_alignment(
        self, project_with_commercial_onboarding_intake
    ):
        """Research artifact missing ## Domain Alignment fails the guard
        when an industry domain is attached."""
        from aah.core.guards.validate_artifact_template import (
            validate_artifact_content,
            validate_domain_alignment,
            get_artifact_type,
        )

        rapids = project_with_commercial_onboarding_intake / ".rapids"
        research_dir = rapids / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = research_dir / "prior-art-analysis.md"

        # Write a research artifact with the 4 required section headers but
        # no Domain Alignment section
        missing_alignment = """# Stakeholder Analysis

## Problem Framing
We're building a thing.

## Findings
Users want the thing.

## Trade-offs
Build vs buy.

## Recommendation
Build it.
"""
        artifact_path.write_text(missing_alignment)
        artifact_type = get_artifact_type(str(artifact_path))
        assert artifact_type == "research"

        # Base template validation passes
        assert validate_artifact_content(missing_alignment, artifact_type) == []

        # Domain alignment check fires
        errors = validate_domain_alignment(
            missing_alignment, str(artifact_path), artifact_type
        )
        assert errors, "expected Domain Alignment guard to fire"
        assert any("Domain Alignment" in e for e in errors)

    def test_guard_passes_with_domain_alignment_and_references(
        self, project_with_commercial_onboarding_intake
    ):
        from aah.core.guards.validate_artifact_template import (
            validate_domain_alignment,
            get_artifact_type,
        )

        rapids = project_with_commercial_onboarding_intake / ".rapids"
        research_dir = rapids / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = research_dir / "prior-art-analysis.md"

        compliant = """# Stakeholder Analysis

## Problem Framing
Onboarding commercial banking clients is slow.

## Findings
KYB and beneficial-owner discovery are the bottlenecks.

## Trade-offs
Automate KYB vs hire more analysts.

## Recommendation
Automate KYB with human-in-the-loop approval for high-risk cases.

## Domain Alignment
This artifact directly addresses KYB Due Diligence and UBO Discovery
process areas. It references the BSA/AML regulatory regime and the
LegalEntity data entity from the Domain Brief.
"""
        artifact_path.write_text(compliant)
        artifact_type = get_artifact_type(str(artifact_path))

        errors = validate_domain_alignment(compliant, str(artifact_path), artifact_type)
        assert errors == [], f"expected no errors; got {errors}"


# ─── Group H: Agent SDK E2E tests (real Claude session) ───────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentSDKIndustryDomains:
    @pytest.mark.slow
    async def test_session_context_injects_domain_intelligence(
        self, project_with_commercial_onboarding_intake, framework_root
    ):
        """An agent session run against a project with a domain attached
        and in the research phase receives the Domain Intelligence block."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        project_path = project_with_commercial_onboarding_intake
        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.implement.load_impl_context "
                f"--project-path {project_path}\n"
                "Show me the status_summary field. Does it contain a "
                "'## Domain Intelligence' section?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lowered = result_text.lower()
        assert "domain intelligence" in lowered or "kyb" in lowered, (
            f"expected Domain Intelligence section or KYB mention; got: {result_text[:500]}"
        )

    @pytest.mark.slow
    async def test_status_command_reports_attached_domain(
        self, project_with_commercial_onboarding_intake, framework_root
    ):
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        project_path = project_with_commercial_onboarding_intake
        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.domain_briefs.status show "
                f"--project-path {project_path}\n"
                "What industry domain is attached to this project?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert "commercial-client-onboarding" in result_text.lower() or (
            "commercial" in result_text.lower() and "onboarding" in result_text.lower()
        )
