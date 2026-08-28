"""Tests for aah.core.common.validators."""

import pytest
from pathlib import Path

from aah.core.common.validators import (
    ValidationError,
    validate_dir_exists,
    validate_field_type,
    validate_file_exists,
    validate_aah_dir_structure,
    validate_feature_contract,
    validate_required_fields,
    validate_dict_schema,
    MANIFEST_SCHEMA,
    FEATURE_SCHEMA,
)


class TestValidateRequiredFields:
    def test_all_present(self):
        data = {"name": "test", "age": 25}
        errors = validate_required_fields(data, ["name", "age"])
        assert errors == []

    def test_missing_field(self):
        data = {"name": "test"}
        errors = validate_required_fields(data, ["name", "age"])
        assert len(errors) == 1
        assert "age" in errors[0]

    def test_none_value_treated_as_missing(self):
        data = {"name": None}
        errors = validate_required_fields(data, ["name"])
        assert len(errors) == 1

    def test_context_prefix(self):
        data = {}
        errors = validate_required_fields(data, ["x"], context="myfile")
        assert errors[0].startswith("myfile:")

    def test_empty_required(self):
        errors = validate_required_fields({}, [])
        assert errors == []


class TestValidateFieldType:
    def test_correct_type(self):
        errors = validate_field_type({"name": "test"}, "name", str)
        assert errors == []

    def test_wrong_type(self):
        errors = validate_field_type({"name": 123}, "name", str)
        assert len(errors) == 1
        assert "str" in errors[0]
        assert "int" in errors[0]

    def test_missing_field_ok(self):
        errors = validate_field_type({}, "name", str)
        assert errors == []


class TestValidateFileExists:
    def test_file_exists(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert validate_file_exists(f) == []

    def test_file_missing(self, tmp_path):
        f = tmp_path / "nonexistent.txt"
        errors = validate_file_exists(f)
        assert len(errors) == 1


class TestValidateDirExists:
    def test_dir_exists(self, tmp_path):
        assert validate_dir_exists(tmp_path) == []

    def test_dir_missing(self, tmp_path):
        d = tmp_path / "nonexistent"
        errors = validate_dir_exists(d)
        assert len(errors) == 1


class TestValidateYamlSchema:
    def test_valid_manifest(self):
        data = {
            "project_name": "test",
            "project_type": "greenfield",
            "current_phase": "init",
        }
        errors = validate_dict_schema(data, MANIFEST_SCHEMA)
        assert errors == []

    def test_missing_required(self):
        data = {"project_name": "test"}
        errors = validate_dict_schema(data, MANIFEST_SCHEMA)
        assert len(errors) > 0
        assert any("project_type" in e for e in errors)

    def test_invalid_allowed_value(self):
        data = {
            "project_name": "test",
            "project_type": "invalid_type",
            "current_phase": "init",
        }
        errors = validate_dict_schema(data, MANIFEST_SCHEMA)
        assert len(errors) == 1
        assert "invalid_type" in errors[0]

    def test_wrong_type(self):
        data = {
            "project_name": 123,
            "project_type": "greenfield",
            "current_phase": "init",
        }
        errors = validate_dict_schema(data, MANIFEST_SCHEMA)
        assert len(errors) == 1
        assert "str" in errors[0]

    def test_optional_field_absent_ok(self):
        data = {
            "project_name": "test",
            "project_type": "greenfield",
            "current_phase": "init",
        }
        errors = validate_dict_schema(data, MANIFEST_SCHEMA)
        assert errors == []

    def test_optional_field_invalid_value(self):
        data = {
            "project_name": "test",
            "project_type": "greenfield",
            "current_phase": "init",
            "complexity_tier": "super_complex",
        }
        errors = validate_dict_schema(data, MANIFEST_SCHEMA)
        assert len(errors) == 1


class TestValidateFeatureSchema:
    def test_valid_feature(self):
        data = {
            "id": "F001",
            "title": "F001",
            "module_ref": "MOD-TEST",
            "spec_ref": "SPEC-001",
            "description": "Test feature",
            "layers": ["backend"],
            "file_scope": ["src/app.py"],
            "dependencies": [],
            "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1"}],
            "status": "pending",
        }
        errors = validate_dict_schema(data, FEATURE_SCHEMA)
        assert errors == []

    def test_invalid_status(self):
        data = {
            "id": "F001",
            "title": "F001",
            "module_ref": "MOD-TEST",
            "spec_ref": "SPEC-001",
            "description": "Test feature",
            "layers": ["backend"],
            "file_scope": ["src/app.py"],
            "dependencies": [],
            "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1"}],
            "status": "completed",
        }
        errors = validate_dict_schema(data, FEATURE_SCHEMA)
        assert len(errors) == 1


    def test_valid_feature_with_implementation_reasoning(self):
        """Feature with implementation_reasoning passes validation."""
        data = {
            "id": "F001",
            "title": "F001",
            "module_ref": "MOD-TEST",
            "spec_ref": "SPEC-001",
            "description": "Test feature",
            "layers": ["backend"],
            "file_scope": ["src/app.py"],
            "dependencies": [],
            "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1"}],
            "status": "pending",
            "implementation_reasoning": {
                "initial_approach": "Create service module following existing patterns...",
                "revisions": [],
            },
            "knowledge_used": {"knowledge_folder": None, "domain_brief": None},
        }
        errors = validate_dict_schema(data, FEATURE_SCHEMA)
        assert errors == []

    def test_valid_feature_without_implementation_reasoning(self):
        """Feature without implementation_reasoning still passes (optional field)."""
        data = {
            "id": "F002",
            "title": "F002",
            "module_ref": "MOD-TEST",
            "spec_ref": "SPEC-001",
            "description": "Test feature",
            "layers": ["backend"],
            "file_scope": ["src/app.py"],
            "dependencies": [],
            "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1"}],
            "status": "pending",
            "knowledge_used": {"knowledge_folder": None, "domain_brief": None},
        }
        errors = validate_dict_schema(data, FEATURE_SCHEMA)
        assert errors == []

    def test_implementation_reasoning_accepts_markdown_string(self):
        """Parsed Markdown reasoning may be a string rather than a mapping."""
        data = {
            "id": "F003",
            "title": "F003",
            "module_ref": "MOD-TEST",
            "spec_ref": "SPEC-001",
            "description": "Test feature",
            "layers": ["backend"],
            "file_scope": ["src/app.py"],
            "dependencies": [],
            "acceptance_criteria": ["AC1"],
            "test_cases": [{"id": "TC1"}],
            "status": "pending",
            "implementation_reasoning": "Implemented from the approved plan.",
            "knowledge_used": {"knowledge_folder": None, "domain_brief": None},
        }
        errors = validate_dict_schema(data, FEATURE_SCHEMA)
        assert errors == []


class TestValidateFeatureContract:
    def _valid(self):
        return {
            "id": "F001",
            "acceptance_criteria": [
                {"id": "AC1", "description": "Users can log in"},
                {"id": "AC2", "description": "Tokens expire"},
            ],
            "test_cases": [
                {"id": "TC1", "covers": ["AC1"]},
                {"id": "TC2", "covers": ["AC1", "AC2"]},
            ],
        }

    def test_valid_contract_passes(self):
        assert validate_feature_contract(self._valid()) == []

    def test_duplicate_ac_ids(self):
        f = self._valid()
        f["acceptance_criteria"] = [
            {"id": "AC1", "description": "a"},
            {"id": "AC1", "description": "b"},
        ]
        f["test_cases"] = [{"id": "TC1", "covers": ["AC1"]}]
        errors = validate_feature_contract(f)
        assert any("duplicate acceptance-criterion id 'AC1'" in e for e in errors)

    def test_duplicate_tc_ids(self):
        f = self._valid()
        f["test_cases"] = [
            {"id": "TC1", "covers": ["AC1"]},
            {"id": "TC1", "covers": ["AC2"]},
        ]
        errors = validate_feature_contract(f)
        assert any("duplicate test-case id 'TC1'" in e for e in errors)

    # v2 (lean/TDD): acceptance_criteria/test_cases are no longer part of the
    # feature spec, and there is no AC↔test coverage premise. The contract
    # validator is now a light structural check on any legacy content. The
    # following behaviours were intentionally REMOVED (they now pass):
    #   - covers referencing an unknown AC id
    #   - a test_case missing / empty `covers`
    #   - an acceptance criterion not covered by any test_case
    def test_no_fields_passes(self):
        # A new 5-section feature carries neither field → trivially valid.
        assert validate_feature_contract({"id": "F-MOD001"}) == []

    def test_covers_no_longer_enforced(self):
        f = self._valid()
        f["test_cases"] = [{"id": "TC1", "covers": ["AC99"]}]  # unknown AC
        assert validate_feature_contract(f) == []

    def test_missing_covers_no_longer_enforced(self):
        f = self._valid()
        f["test_cases"] = [{"id": "TC1"}, {"id": "TC2", "covers": []}]
        assert validate_feature_contract(f) == []

    def test_uncovered_ac_no_longer_enforced(self):
        f = self._valid()
        f["test_cases"] = [{"id": "TC1", "covers": ["AC1"]}]  # AC2 uncovered
        assert validate_feature_contract(f) == []

    def test_non_list_acceptance_criteria(self):
        f = self._valid()
        f["acceptance_criteria"] = "not a list"
        errors = validate_feature_contract(f)
        assert any("acceptance_criteria must be a list" in e for e in errors)

    def test_non_list_test_cases(self):
        f = self._valid()
        f["test_cases"] = "not a list"
        errors = validate_feature_contract(f)
        assert any("test_cases must be a list" in e for e in errors)

    def test_context_prefix_present(self):
        f = self._valid()
        f["test_cases"] = "not a list"  # a still-flagged structural error
        errors = validate_feature_contract(f, context="F001")
        assert errors
        assert all(e.startswith("F001:") for e in errors)


class TestValidateRapidsDirStructure:
    def test_complete_structure(self, tmp_path):
        from aah.core.scaffold.aah_dir import create_aah_dir
        create_aah_dir(tmp_path)
        # Need manifest too
        (tmp_path / ".aah" / "manifest.yaml").write_text("project_name: test\n")
        errors = validate_aah_dir_structure(tmp_path / ".aah")
        assert errors == []

    def test_missing_dirs(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        (aah_root / "manifest.yaml").write_text("project_name: test\n")
        errors = validate_aah_dir_structure(aah_root)
        assert len(errors) > 0
        assert any("discuss" in e for e in errors)


class TestValidateRuntimeProfile:
    def _valid(self):
        return {
            "schema_version": 1,
            "rule_version": 1,
            "deployment_topology": "full-local",
            "decision_hash": "a" * 64,
            "readiness_hash": "b" * 64,
            "profile_hash": "c" * 64,
            "required_checks": ["boot", "build"],
            "provider_anchor": {"provider": "aws"},
            "project_anchor": {"project_name": "demo"},
            "source_decisions": ["development-methodology", "docker-installed"],
            "allowed_env_keys": ["RDS_POSTGRES_HOST"],
            "observable_boundaries": ["http", "library"],
        }

    def test_valid_profile(self):
        from aah.core.common.validators import validate_runtime_profile

        assert validate_runtime_profile(self._valid()) == []

    def test_missing_required_field(self):
        from aah.core.common.validators import validate_runtime_profile

        profile = self._valid()
        del profile["decision_hash"]
        errors = validate_runtime_profile(profile)
        assert any("decision_hash" in e for e in errors)

    def test_non_hex_hash_rejected(self):
        from aah.core.common.validators import validate_runtime_profile

        profile = self._valid()
        profile["profile_hash"] = "not-hex"
        errors = validate_runtime_profile(profile)
        assert any("profile_hash" in e and "hex" in e for e in errors)

    def test_boundary_not_in_vocab_rejected(self):
        from aah.core.common.validators import validate_runtime_profile

        profile = self._valid()
        profile["observable_boundaries"] = ["http", "grpc"]
        errors = validate_runtime_profile(profile)
        assert any("observable_boundaries" in e for e in errors)

    def test_value_bearing_secret_field_rejected(self):
        from aah.core.common.validators import validate_runtime_profile

        profile = self._valid()
        profile["env_values"] = {"SECRET": "leaked"}
        errors = validate_runtime_profile(profile)
        assert any("env_values" in e and "forbidden" in e for e in errors)

    def test_allowed_env_keys_must_be_strings(self):
        from aah.core.common.validators import validate_runtime_profile

        profile = self._valid()
        profile["allowed_env_keys"] = ["OK", 123]
        errors = validate_runtime_profile(profile)
        assert any("allowed_env_keys" in e for e in errors)


class TestRejectBadSmokeAssertions:
    def test_reject_bad_smoke_assertions(self):
        """AC4: unsupported/arbitrary/secret-bearing assertions rejected before run."""
        from aah.core.common.validators import (
            validate_smoke_assertion,
            validate_smoke_assertions,
        )

        # Unsupported type.
        assert any(
            "unsupported assertion type" in e
            for e in validate_smoke_assertion({"type": "regex_match", "value": ".*"})
        )

        # Full-body snapshot (volatile) — forbidden.
        assert any(
            "full-body snapshot" in e
            for e in validate_smoke_assertion({"type": "json_equals", "path": "$", "body_equals": "x"})
        )

        # Arbitrary expression — forbidden.
        assert any(
            "arbitrary-expression" in e
            for e in validate_smoke_assertion({"type": "status_in", "values": [200], "eval": "1==1"})
        )

        # Secret-bearing assertion (header names Authorization) — forbidden.
        assert any(
            "secret target" in e
            for e in validate_smoke_assertion({"type": "header_present", "header": "Authorization"})
        )
        assert any(
            "secret target" in e
            for e in validate_smoke_assertion({"type": "header_present", "header": "X_API_TOKEN"})
        )

        # collection_length_lte requires a non-negative int bound.
        assert any(
            "collection_length_lte" in e
            for e in validate_smoke_assertion({"type": "collection_length_lte", "path": "$", "max": -1})
        )
        assert validate_smoke_assertion(
            {"type": "collection_length_lte", "path": "$.items", "max": 10}
        ) == []

        # A supported assertion validates clean.
        assert validate_smoke_assertion({"type": "status_in", "values": [200, 404]}) == []

        # Wired through a step: a non-health endpoint with only a bad assertion
        # surfaces the assertion error.
        step = {
            "request": {"path": "/orders", "method": "GET"},
            "type": "api_semantic",
            "assertions": [{"type": "eval_expr", "expr": "os.system('x')"}],
        }
        errors = validate_smoke_assertions(step)
        assert errors
