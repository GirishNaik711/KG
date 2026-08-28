"""Tests for aah.core.intake.resolve_constraints"""
import json
import tempfile
from pathlib import Path

import pytest
import yaml

from aah.core.intake.resolve_constraints import (
    flatten_intake_text,
    detect_triggered_regimes,
    collect_regime_mandates,
    flatten_eliminated_options,
    extract_client_constraints,
    resolve,
    APPLIES_WHEN,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

HIPAA_CATALOG = {
    "regime": "hipaa",
    "trigger_signals": ["hipaa", "phi", "protected health"],
    "mandates": [
        {
            "mandate_id": "CC-HIPAA-001",
            "statement": "PHI must not be transmitted to a model provider without a signed BAA.",
            "closes_options": [
                {"ddr_id": "DDR-L3-001", "eliminates": ["providers without signed BAA"]},
            ],
        },
        {
            "mandate_id": "CC-HIPAA-004",
            "statement": "Audit logs required.",
            "closes_options": [],
        },
    ],
}

GDPR_CATALOG = {
    "regime": "gdpr",
    "trigger_signals": ["gdpr", "eu data", "data subject"],
    "mandates": [
        {
            "mandate_id": "CC-GDPR-001",
            "statement": "EU data must stay in adequate jurisdictions.",
            "closes_options": [
                {"ddr_id": "DDR-L3-001", "eliminates": ["providers without EU data residency or SCCs"]},
                {"ddr_id": "DDR-SHARED-017", "eliminates": ["compute regions outside GDPR-adequate countries without SCCs"]},
            ],
        },
    ],
}

ONPREM_CATALOG = {
    "regime": "on-premises",
    "trigger_signals": ["on-premises", "on-prem", "no cloud"],
    "mandates": [
        {
            "mandate_id": "CC-INFRA-ONPREM-001",
            "statement": "Model inference must run on-premises.",
            "closes_options": [
                {"ddr_id": "DDR-L3-001", "eliminates": ["best-fit-for-domain", "cost-optimized"]},
            ],
        },
    ],
}

ALL_CATALOGS = [HIPAA_CATALOG, GDPR_CATALOG, ONPREM_CATALOG]


# ── flatten_intake_text ───────────────────────────────────────────────────────

def test_flatten_includes_problem_statement():
    intake = {"problem_statement": "Build a HIPAA-compliant portal", "rounds": []}
    text = flatten_intake_text(intake)
    assert "hipaa" in text


def test_flatten_includes_qa_answers():
    intake = {
        "problem_statement": "",
        "rounds": [{"questions": [{"question": "q", "answer": "we are subject to GDPR"}]}],
    }
    text = flatten_intake_text(intake)
    assert "gdpr" in text


def test_flatten_is_lowercase():
    intake = {"problem_statement": "HIPAA PHI Healthcare", "rounds": []}
    text = flatten_intake_text(intake)
    assert text == text.lower()


def test_flatten_empty_intake():
    text = flatten_intake_text({})
    assert text == ""


# ── detect_triggered_regimes ─────────────────────────────────────────────────

def test_hipaa_detected():
    text = "build a hipaa-compliant patient portal"
    regimes = detect_triggered_regimes(text, ALL_CATALOGS)
    assert "hipaa" in regimes


def test_gdpr_detected_from_answer():
    text = "we process eu data and have data subjects in germany"
    regimes = detect_triggered_regimes(text, ALL_CATALOGS)
    assert "gdpr" in regimes


def test_onprem_detected():
    text = "all data must stay on-premises, no cloud allowed"
    regimes = detect_triggered_regimes(text, ALL_CATALOGS)
    assert "on-premises" in regimes


def test_no_signals_returns_empty():
    text = "internal admin dashboard for our ops team"
    regimes = detect_triggered_regimes(text, ALL_CATALOGS)
    assert regimes == []


def test_multiple_regimes_detected():
    text = "hipaa-compliant system, eu data residency required"
    regimes = detect_triggered_regimes(text, ALL_CATALOGS)
    assert "hipaa" in regimes
    assert "gdpr" in regimes


# ── collect_regime_mandates ───────────────────────────────────────────────────

def test_collects_mandates_for_triggered_regime():
    mandates = collect_regime_mandates(ALL_CATALOGS, ["hipaa"])
    ids = [m["mandate_id"] for m in mandates]
    assert "CC-HIPAA-001" in ids
    assert "CC-HIPAA-004" in ids


def test_does_not_collect_untriggered_regimes():
    mandates = collect_regime_mandates(ALL_CATALOGS, ["hipaa"])
    ids = [m["mandate_id"] for m in mandates]
    assert "CC-GDPR-001" not in ids


def test_empty_triggered_returns_empty():
    mandates = collect_regime_mandates(ALL_CATALOGS, [])
    assert mandates == []


# ── flatten_eliminated_options ────────────────────────────────────────────────

def test_eliminates_extracted_correctly():
    mandates = collect_regime_mandates([HIPAA_CATALOG], ["hipaa"])
    eliminated = flatten_eliminated_options(mandates, "by_mandate", "mandate_id")
    assert any(
        e["ddr_id"] == "DDR-L3-001"
        and e["option"] == "providers without signed BAA"
        and e["by_mandate"] == "CC-HIPAA-001"
        for e in eliminated
    )


def test_empty_closes_options_not_included():
    mandates = collect_regime_mandates([HIPAA_CATALOG], ["hipaa"])
    eliminated = flatten_eliminated_options(mandates, "by_mandate", "mandate_id")
    # CC-HIPAA-004 has empty closes_options — should produce no eliminated entries
    assert not any(e["by_mandate"] == "CC-HIPAA-004" for e in eliminated)


def test_multiple_options_per_mandate_all_included():
    mandates = collect_regime_mandates([ONPREM_CATALOG], ["on-premises"])
    eliminated = flatten_eliminated_options(mandates, "by_mandate", "mandate_id")
    options = [e["option"] for e in eliminated if e["by_mandate"] == "CC-INFRA-ONPREM-001"]
    assert "best-fit-for-domain" in options
    assert "cost-optimized" in options


# ── APPLIES_WHEN evaluators ───────────────────────────────────────────────────

def test_always_evaluates_true():
    assert APPLIES_WHEN["always"]("anything") is True


def test_if_rag_detects_retrieval():
    assert APPLIES_WHEN["if_rag"]("build a rag pipeline over documents")
    assert not APPLIES_WHEN["if_rag"]("simple chatbot with no search capability")


def test_if_multi_agent_detects():
    assert APPLIES_WHEN["if_multi_agent"]("multi-agent system with specialist agents")
    assert not APPLIES_WHEN["if_multi_agent"]("single agent chatbot")


# ── resolve (integration) ─────────────────────────────────────────────────────

def _write_catalog_files(base: Path):
    """Helper: write minimal catalog files for integration tests."""
    compliance = base / "compliance"
    compliance.mkdir(parents=True)
    (compliance / "hipaa.yaml").write_text(yaml.dump(HIPAA_CATALOG))
    (compliance / "gdpr.yaml").write_text(yaml.dump(GDPR_CATALOG))
    infra = base / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "on-premises.yaml").write_text(yaml.dump(ONPREM_CATALOG))


def test_resolve_hipaa_project(tmp_path):
    intake = {"problem_statement": "Build a hipaa-compliant patient portal", "rounds": []}
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake))

    catalog_dir = tmp_path / "constraints"
    _write_catalog_files(catalog_dir)

    output = tmp_path / "constraints-resolved.yaml"
    result = resolve(intake_path, catalog_dir, tmp_path / "nonexistent-archetype-catalog.yaml")

    assert "hipaa" in result["triggered_regimes"]
    assert any(e["ddr_id"] == "DDR-L3-001" for e in result["eliminated_options"])
    assert result["client_specific_constraints"] == []


def test_resolve_no_regime_project(tmp_path):
    intake = {"problem_statement": "Internal admin dashboard", "rounds": []}
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake))

    catalog_dir = tmp_path / "constraints"
    _write_catalog_files(catalog_dir)

    result = resolve(intake_path, catalog_dir, tmp_path / "nonexistent.yaml")

    assert result["triggered_regimes"] == []
    assert result["eliminated_options"] == []


def test_resolve_output_has_required_keys(tmp_path):
    intake = {"problem_statement": "test", "rounds": []}
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake))

    catalog_dir = tmp_path / "constraints"
    _write_catalog_files(catalog_dir)

    result = resolve(intake_path, catalog_dir, tmp_path / "nonexistent.yaml")

    assert "schema_version" in result
    assert "triggered_regimes" in result
    assert "applied_mandates" in result
    assert "eliminated_options" in result
    assert "client_specific_constraints" in result


def test_resolve_graceful_when_catalog_dir_missing(tmp_path):
    intake = {"problem_statement": "hipaa project", "rounds": []}
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake))

    # catalog_dir does not exist
    result = resolve(intake_path, tmp_path / "nonexistent_dir", tmp_path / "nonexistent.yaml")

    # Should not raise; returns empty
    assert result["triggered_regimes"] == []
    assert result["eliminated_options"] == []


# ── BSA/AML regime detection ────────────────────────────────────────────────

BSA_AML_CATALOG = {
    "regime": "bsa-aml",
    "trigger_signals": [
        "bsa", "kyc", "aml", "bank secrecy", "anti-money laundering",
        "know your customer", "ofac", "sanctions", "cdd", "edd",
        "fincen", "customer identification", "beneficial ownership",
        "sar", "suspicious activity",
    ],
    "mandates": [
        {
            "mandate_id": "CC-BSA-001",
            "statement": "Audit trail retention for 5 years.",
            "closes_options": [
                {"ddr_id": "DDR-L9-002", "eliminates": ["environment-variables"]},
            ],
        },
        {
            "mandate_id": "CC-BSA-002",
            "statement": "OFAC screening must be synchronous.",
            "closes_options": [],
        },
        {
            "mandate_id": "CC-BSA-004",
            "statement": "SAR data must not be disclosed.",
            "closes_options": [
                {"ddr_id": "DDR-L3-001", "eliminates": ["providers that log prompts by default without opt-out"]},
            ],
        },
    ],
}


def test_bsa_detected_from_kyc_signal():
    text = "standard kyc/aml (bsa compliance) for banking customers"
    regimes = detect_triggered_regimes(text, [BSA_AML_CATALOG])
    assert "bsa-aml" in regimes


def test_bsa_detected_from_ofac_signal():
    text = "we need ofac sanctions screening integrated"
    regimes = detect_triggered_regimes(text, [BSA_AML_CATALOG])
    assert "bsa-aml" in regimes


def test_bsa_detected_from_bank_secrecy():
    text = "must comply with bank secrecy act requirements"
    regimes = detect_triggered_regimes(text, [BSA_AML_CATALOG])
    assert "bsa-aml" in regimes


def test_bsa_detected_from_cdd():
    text = "cdd/edd processes for customer onboarding"
    regimes = detect_triggered_regimes(text, [BSA_AML_CATALOG])
    assert "bsa-aml" in regimes


def test_bsa_not_detected_irrelevant_text():
    text = "internal admin dashboard for marketing team"
    regimes = detect_triggered_regimes(text, [BSA_AML_CATALOG])
    assert regimes == []


def test_bsa_mandates_collected():
    mandates = collect_regime_mandates([BSA_AML_CATALOG], ["bsa-aml"])
    ids = [m["mandate_id"] for m in mandates]
    assert "CC-BSA-001" in ids
    assert "CC-BSA-002" in ids
    assert "CC-BSA-004" in ids


def test_bsa_eliminates_environment_variables():
    mandates = collect_regime_mandates([BSA_AML_CATALOG], ["bsa-aml"])
    eliminated = flatten_eliminated_options(mandates, "by_mandate", "mandate_id")
    assert any(
        e["ddr_id"] == "DDR-L9-002"
        and e["option"] == "environment-variables"
        and e["by_mandate"] == "CC-BSA-001"
        for e in eliminated
    )


# ── extract_client_constraints — expanded patterns ───────────────────────────

def test_budget_matches_monthly_cap():
    intake = {
        "problem_statement": "",
        "rounds": [{
            "questions": [{
                "question": "What is your budget cap?",
                "answer": "$5k/month",
            }],
        }],
    }
    constraints = extract_client_constraints(intake)
    assert len(constraints) >= 1
    assert any("/month" in c["statement"] or "$5k" in c["statement"] for c in constraints)


def test_stack_matches_google_cloud():
    intake = {
        "problem_statement": "",
        "rounds": [{
            "questions": [{
                "question": "What is your planned or existing technology stack?",
                "answer": "Google Cloud (Vertex AI) — GCP-native",
            }],
        }],
    }
    constraints = extract_client_constraints(intake)
    assert len(constraints) >= 1
    assert any("Google Cloud" in c["statement"] for c in constraints)


def test_scale_matches_low_concurrency():
    intake = {
        "problem_statement": "",
        "rounds": [{
            "questions": [{
                "question": "How many concurrent users do you expect?",
                "answer": "Small (< 100 concurrent)",
            }],
        }],
    }
    constraints = extract_client_constraints(intake)
    assert len(constraints) >= 1
    assert any("< 100" in c["statement"] for c in constraints)


def test_scope_matches_standalone():
    intake = {
        "problem_statement": "",
        "rounds": [{
            "questions": [{
                "question": "Does this integrate with existing systems?",
                "answer": "Standalone MVP — no integration with legacy systems",
            }],
        }],
    }
    constraints = extract_client_constraints(intake)
    assert len(constraints) >= 1
    assert any("Standalone" in c["statement"] for c in constraints)


def test_multiple_constraints_extracted():
    intake = {
        "problem_statement": "KYC/AML compliance automation",
        "rounds": [{
            "questions": [
                {"question": "What is your budget cap?", "answer": "$5k/month"},
                {"question": "What is your technology stack?", "answer": "Google Cloud (Vertex AI)"},
                {"question": "How many concurrent users?", "answer": "Small (< 100 concurrent)"},
                {"question": "Existing systems?", "answer": "Standalone MVP"},
            ],
        }],
    }
    constraints = extract_client_constraints(intake)
    assert len(constraints) >= 3


# ── resolve integration: BSA/AML project ─────────────────────────────────────

def _write_catalog_files_with_bsa(base: Path):
    """Helper: write all catalog files including BSA/AML for integration tests."""
    compliance = base / "compliance"
    compliance.mkdir(parents=True, exist_ok=True)
    (compliance / "hipaa.yaml").write_text(yaml.dump(HIPAA_CATALOG))
    (compliance / "gdpr.yaml").write_text(yaml.dump(GDPR_CATALOG))
    (compliance / "bsa-aml.yaml").write_text(yaml.dump(BSA_AML_CATALOG))
    infra = base / "infrastructure"
    infra.mkdir(parents=True, exist_ok=True)
    (infra / "on-premises.yaml").write_text(yaml.dump(ONPREM_CATALOG))


def test_resolve_bsa_aml_project(tmp_path):
    intake = {
        "problem_statement": "Standard KYC/AML (BSA compliance) automation",
        "rounds": [{
            "questions": [
                {"question": "What is your budget cap?", "answer": "$5k/month"},
                {"question": "What is your technology stack?", "answer": "Google Cloud (Vertex AI)"},
            ],
        }],
    }
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake))

    catalog_dir = tmp_path / "constraints"
    _write_catalog_files_with_bsa(catalog_dir)

    result = resolve(intake_path, catalog_dir, tmp_path / "nonexistent.yaml")

    assert "bsa-aml" in result["triggered_regimes"]
    assert any(m["mandate_id"] == "CC-BSA-001" for m in result["applied_mandates"])
    assert any(
        e["ddr_id"] == "DDR-L9-002" and e["by_mandate"] == "CC-BSA-001"
        for e in result["eliminated_options"]
    )
    assert len(result["client_specific_constraints"]) >= 2
