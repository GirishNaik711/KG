"""Tests for the back-compat shim at aah.core.knowledge.domain_loader.

The old implementation detected tech domains from intake keywords via a flat
keyword map and loaded patterns.yaml files from activity-library/_domains/.
The current implementation delegates to aah.core.playbooks — the shim
reads manifest.project_type for detect_domains, and load_domain_knowledge
returns the migrated Build Playbook content.

Comprehensive coverage of the new taxonomy/classifier/context flow lives in
tests/e2e/test_industry_domains_e2e.py. This file focuses on verifying the
shim's back-compat contract.
"""

import warnings
from pathlib import Path

import pytest
import yaml

from aah.core.common.manifest import save_manifest, get_default_manifest
from aah.core.knowledge.domain_loader import (
    build_domain_context,
    detect_domains,
    load_domain_knowledge,
)


def _make_project_with_manifest(tmp_path: Path, project_type: str) -> Path:
    """Create a minimal scaffolded project with a manifest declaring project_type."""
    rapids = tmp_path / ".rapids"
    rapids.mkdir(parents=True, exist_ok=True)
    manifest = get_default_manifest("test-project", project_type="greenfield")
    # Overwrite the 'project_type' field with a technology-domain value.
    # Note: in the new schema, manifest.project_type is the lifecycle type
    # ('greenfield' or 'brownfield'); the shim treats any other value as a
    # tech archetype for back-compat.
    manifest["project_type"] = project_type
    save_manifest(manifest, rapids / "manifest.yaml")
    return tmp_path


class TestDetectDomainsShim:
    def test_no_manifest_returns_empty(self, tmp_path):
        # No .rapids/manifest.yaml
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert detect_domains(tmp_path) == []

    def test_returns_project_type_from_manifest(self, tmp_path):
        project = _make_project_with_manifest(tmp_path, "ai-infra-platforms")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            domains = detect_domains(project)
        assert "ai-infra-platforms" in domains

    def test_lifecycle_project_type_not_returned(self, tmp_path):
        """Pure 'greenfield' (lifecycle) project_type produces no tech-domain detection."""
        project = _make_project_with_manifest(tmp_path, "greenfield")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            domains = detect_domains(project)
        assert domains == []

    def test_emits_deprecation_warning(self, tmp_path):
        # Reset the shim's one-time emission flag so the warning fires for this test.
        import aah.core.knowledge.domain_loader as shim
        shim._DEPRECATION_EMITTED = False

        project = _make_project_with_manifest(tmp_path, "ai-infra-platforms")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            detect_domains(project)
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


class TestLoadDomainKnowledgeShim:
    def test_legacy_agentic_ai_resolves_to_new_playbook(self):
        """Legacy 'agentic-ai' key now resolves to the migrated ai-infra-platforms playbook."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            knowledge = load_domain_knowledge("agentic-ai")
        assert knowledge is not None
        # New playbook has a technology_domain field, not a generic 'domain' field.
        # The shim returns the raw playbook YAML.
        assert knowledge.get("technology_domain") == "ai-infra-platforms"
        assert len(knowledge.get("patterns") or []) > 0
        assert any(
            p.get("name") == "Tool Use Abstraction" for p in knowledge["patterns"]
        )

    def test_new_tech_domain_key(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            knowledge = load_domain_knowledge("ai-infra-platforms")
        assert knowledge is not None
        assert knowledge.get("technology_domain") == "ai-infra-platforms"

    def test_nonexistent(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert load_domain_knowledge("nonexistent-domain") is None


class TestBuildDomainContextShim:
    def test_no_manifest_empty_context(self, tmp_path):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert build_domain_context(tmp_path) == ""

    def test_populated_context_from_published_playbook(self, tmp_path):
        project = _make_project_with_manifest(tmp_path, "ai-infra-platforms")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = build_domain_context(project)
        # The shim renders the Build Playbook under the new heading.
        assert "Build Playbook" in ctx
        assert "Tool Use Abstraction" in ctx

    def test_unpublished_playbook_returns_empty(self, tmp_path):
        """ai-applications playbook ships dormant — shim returns empty."""
        project = _make_project_with_manifest(tmp_path, "ai-applications")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert build_domain_context(project) == ""
