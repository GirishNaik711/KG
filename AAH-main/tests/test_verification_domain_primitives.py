from __future__ import annotations

import hashlib
import json
from datetime import timezone

import yaml

from aah.core.build import qa_evidence
from aah.core.build.verification_profiles import (
    catalog_hash,
    load_profile_catalog,
    profile_binding_for_feature,
    validate_profile_catalog,
)
from aah.core.common.audit import append_gate_decision, read_gate_decisions
from aah.core.common.feature_utils import load_wave_feature_ids
from aah.core.common.hashing import canonical_json_bytes, canonical_json_hash
from aah.core.common.manifest import RolloutMode, rollout_mode, rollout_value
from aah.core.common.readiness import resolve_runtime_port
from aah.core.common.time_utils import parse_legacy_rfc3339, parse_rfc3339


def _aah(tmp_path):
    aah = tmp_path / ".aah"
    (aah / "plan").mkdir(parents=True)
    return aah

def test_wave_reader_supports_flat_dict_and_nested_tiers(tmp_path):
    from aah.core.plan.compute_waves import flatten_waves
    aah = _aah(tmp_path)
    (aah / "plan" / "waves.json").write_text(
        json.dumps(
            {
                "waves": [
                    ["F001", "F002"],
                    {"features": ["F003"]},
                    [["F004", "F005"], ["F006"]],
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_wave_feature_ids(tmp_path, 0) == ["F001", "F002"]
    assert load_wave_feature_ids(aah, 1) == ["F003"]
    assert load_wave_feature_ids(tmp_path, 2) == ["F004", "F005", "F006"]
    assert flatten_waves(json.loads((aah / "plan" / "waves.json").read_text())) == [
        ["F001", "F002"], ["F003"], ["F004", "F005", "F006"]
    ]
    assert load_wave_feature_ids(tmp_path, -1) == []
    assert load_wave_feature_ids(tmp_path, 99) == []
    (aah / "plan" / "waves.json").write_text("not-json", encoding="utf-8")
    assert load_wave_feature_ids(tmp_path, 0) == []

def test_runtime_port_precedence_and_malformed_fallback(tmp_path):
    aah = _aah(tmp_path)
    (aah / "manifest.yaml").write_text(
        yaml.safe_dump({"stack_choices": {"port": "9001", "primary_port": 9002}}),
        encoding="utf-8",
    )
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump(
            {"checkpoint_configuration": {"system_checkpoints": {"port": "9000"}}}
        ),
        encoding="utf-8",
    )
    assert resolve_runtime_port(tmp_path) == 9000
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump(
            {"checkpoint_configuration": {"system_checkpoints": {"port": "bad"}}}
        ),
        encoding="utf-8",
    )
    assert resolve_runtime_port(aah) == 9001
    (aah / "manifest.yaml").write_text(
        yaml.safe_dump({"stack_choices": {"port": 70000, "primary_port": "9002"}}),
        encoding="utf-8",
    )
    assert resolve_runtime_port(tmp_path) == 9002
    (aah / "manifest.yaml").write_text("stack_choices: malformed", encoding="utf-8")
    assert resolve_runtime_port(tmp_path) == 8000

def test_rollout_defaults_modes_fail_closed_and_ignore_environment(
    tmp_path, monkeypatch
):
    assert rollout_value({}, "runtime_profile_v1") == "report_only"
    assert (
        rollout_mode(
            {"features": {"runtime_profile_v1": False}},
            "runtime_profile_v1",
        )
        is RolloutMode.OFF
    )
    assert (
        rollout_mode(
            {"features": {"runtime_profile_v1": True}},
            "runtime_profile_v1",
        )
        is RolloutMode.ENFORCE
    )
    assert (
        rollout_mode(
            {"features": {"runtime_profile_v1": "report_only"}},
            "runtime_profile_v1",
        )
        is RolloutMode.REPORT_ONLY
    )
    assert (
        rollout_mode(
            {"features": {"runtime_profile_v1": "invalid"}},
            "runtime_profile_v1",
        )
        is RolloutMode.ENFORCE
    )
    assert (
        rollout_mode({"features": []}, "runtime_profile_v1")
        is RolloutMode.ENFORCE
    )
    aah = _aah(tmp_path)
    (aah / "manifest.yaml").write_text(
        "features:\n  runtime_profile_v1: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("RUNTIME_PROFILE_V1", "false")
    assert rollout_mode(tmp_path, "runtime_profile_v1") is RolloutMode.ENFORCE

def test_canonical_json_bytes_preserve_existing_hash_contract():
    value = {"unicode": "café", "nested": {"b": 2, "a": 1}}
    historical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert canonical_json_bytes(value) == historical
    assert canonical_json_hash(value) == hashlib.sha256(historical).hexdigest()

def test_rfc3339_strict_and_legacy_modes():
    strict = parse_rfc3339("2026-07-17T10:00:00+05:30")
    assert strict is not None
    assert strict.tzinfo is timezone.utc
    assert strict.isoformat() == "2026-07-17T04:30:00+00:00"
    assert parse_rfc3339("2026-07-17T10:00:00") is None
    legacy = parse_legacy_rfc3339("2026-07-17T10:00:00")
    assert legacy is not None and legacy.tzinfo is timezone.utc
    assert parse_rfc3339("2026-07-17T10:00:00Z") is not None
    assert parse_rfc3339("not-a-time") is None

def test_audit_appends_versioned_events_and_normalizes_both_legacy_shapes(tmp_path):
    aah = _aah(tmp_path)
    assert append_gate_decision(
        aah,
        gate="wave",
        wave=2,
        decision="refire",
        reason="stale evidence",
        timestamp="2026-07-17T00:00:00+00:00",
    )
    path = aah / "audit" / "gate-decisions.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": "legacy-wave",
                    "gate": "wave",
                    "wave": 1,
                    "decision": "pass",
                    "reason": "ok",
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "timestamp": "legacy-human",
                    "gate": "feature_qa_approve",
                    "feature_id": "F001",
                    "decision": "approve",
                    "reviewer": "owner",
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps({"gate": "legacy-no-time", "decision": "pass", "reason": "ok"})
            + "\n"
        )
    events = read_gate_decisions(tmp_path)
    assert [event["decision"] for event in events] == [
        "refire",
        "pass",
        "approve",
        "pass",
    ]
    assert events[0]["schema_version"] == 1
    assert events[1]["schema_version"] == 0
    assert events[2]["ts"] == "legacy-human"
    assert events[2]["feature_id"] == "F001"
    assert "ts" not in events[3]

def test_profile_catalog_hash_binding_remains_compatible(tmp_path):
    aah = _aah(tmp_path)
    profiles = {
        "F001": {
            "level": "deep",
            "rule_version": "1",
            "reasons": ["explicit_integrity"],
            "override": None,
        }
    }
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump(
            {"checkpoint_configuration": {"verification_profiles": profiles}}
        ),
        encoding="utf-8",
    )
    loaded, reason = load_profile_catalog(tmp_path)
    assert reason == "" and loaded == profiles
    assert validate_profile_catalog(loaded) == []
    assert catalog_hash(profiles) == qa_evidence._canonical_config_hash(profiles)
    assert profile_binding_for_feature(
        tmp_path, "F001"
    ) == qa_evidence.profile_binding_for_feature(tmp_path, "F001")

def test_profile_catalog_fails_closed_with_stable_reasons(tmp_path):
    aah = _aah(tmp_path)
    assert load_profile_catalog(tmp_path) == (None, "missing_profile")
    from aah.core.build import verify

    collector = verify._Collector(tmp_path, 0)
    verify._check_profile_catalog(collector)
    assert collector.failures[0].code == "verification_profiles_missing"
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        "[not, a, mapping]", encoding="utf-8"
    )
    # read_yaml historically normalizes non-mappings to {}, so preserve the
    # established reason rather than changing persisted consumer vocabulary.
    assert load_profile_catalog(tmp_path) == (None, "missing_verification_profiles")
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        "checkpoint_configuration: {}\n", encoding="utf-8"
    )
    assert load_profile_catalog(tmp_path) == (None, "missing_verification_profiles")
    assert profile_binding_for_feature(tmp_path, "F001")["no_signal"] == (
        "missing_verification_profiles"
    )
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump(
            {
                "checkpoint_configuration": {
                    "verification_profiles": {
                        "F001": {"level": "deep", "reasons": "invalid"}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_profile_catalog(tmp_path) == (None, "malformed_verification_profiles")
    collector = verify._Collector(tmp_path, 0)
    verify._check_profile_catalog(collector)
    assert collector.failures[0].code == "verification_profiles_missing"
    (aah / "plan" / "checkpoint-config.yaml").write_text(
        yaml.safe_dump(
            {
                "checkpoint_configuration": {
                    "verification_profiles": {
                        "F001": {
                            "reasons": [],
                            "rule_version": "1",
                            "level": "invalid",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_profile_catalog(tmp_path) == (None, "malformed_verification_profiles")
