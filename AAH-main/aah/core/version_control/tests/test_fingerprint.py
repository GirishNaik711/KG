"""Fingerprint normalisation + round-trip stability (the core invariant)."""

from aah.core.version_control.fingerprint import fingerprint_fields, local_fp, remote_fp
from aah.core.version_control.models import WorkItem
from aah.core.version_control.providers.github import mapper


def test_excludes_nonsynced_keys():
    a = {"description": "x", "acceptance_criteria": ["a"], "knowledge_used": {"k": 1}}
    b = {"description": "x", "acceptance_criteria": ["a"], "codemap_context": "noise"}
    assert fingerprint_fields(a) == fingerprint_fields(b)


def test_order_insensitive_lists():
    a = {"acceptance_criteria": ["a", "b"]}
    b = {"acceptance_criteria": ["b", "a"]}
    assert fingerprint_fields(a) == fingerprint_fields(b)


def test_whitespace_normalised():
    a = {"description": "hello world"}
    b = {"description": "hello world   \n"}
    assert fingerprint_fields(a) == fingerprint_fields(b)


def test_real_change_moves_fingerprint():
    a = {"description": "hello"}
    b = {"description": "goodbye"}
    assert fingerprint_fields(a) != fingerprint_fields(b)


def test_test_cases_nested_inputs_order_insensitive():
    a = {
        "test_cases": [
            {
                "id": "tc-1",
                "inputs": [
                    {"name": "user_id", "value": "42"},
                    {"name": "role", "value": "admin"},
                ],
            }
        ]
    }
    b = {
        "test_cases": [
            {
                "id": "tc-1",
                "inputs": [
                    {"name": "role", "value": "admin"},
                    {"name": "user_id", "value": "42"},
                ],
            }
        ]
    }
    assert fingerprint_fields(a) == fingerprint_fields(b)


def test_workitem_test_cases_inputs_order_insensitive():
    common = dict(
        feature_id="F-TEST-99",
        title="Scratch",
        description="Scratch feature",
        acceptance_criteria=["works"],
        dependencies=[],
        layer="backend",
        status="planned",
    )
    a = WorkItem(
        test_cases=[
            {
                "id": "tc-1",
                "inputs": [
                    {"name": "role", "value": "admin"},
                    {"name": "user_id", "value": "42"},
                ],
            }
        ],
        **common,
    )
    b = WorkItem(
        test_cases=[
            {
                "id": "tc-1",
                "inputs": [
                    {"name": "user_id", "value": "42"},
                    {"name": "role", "value": "admin"},
                ],
            }
        ],
        **common,
    )
    assert local_fp(a) == remote_fp(b)


def test_test_cases_content_change_still_detected():
    a = {"test_cases": [{"id": "tc-1", "inputs": [{"name": "user_id", "value": "42"}]}]}
    b = {"test_cases": [{"id": "tc-1", "inputs": [{"name": "user_id", "value": "43"}]}]}
    assert fingerprint_fields(a) != fingerprint_fields(b)


def test_github_roundtrip_fingerprint_stable():
    wi = WorkItem(
        feature_id="F001",
        title="Init",
        description="Initialize project",
        acceptance_criteria=["has pyproject", "health 200"],
        test_cases=[{"id": "TC001", "assertions": ["200"]}],
        dependencies=["F000"],
        layer="backend",
        status="planned",
    )
    body = mapper.render_body(wi)
    issue = {
        "number": 1, "title": mapper.title_for(wi), "body": body,
        "labels": [{"name": "aah:backend"}, {"name": "aah:planned"}],
        "state": "open", "url": "u", "id": "n",
    }
    back = mapper.to_work_item("F001", issue)
    assert local_fp(wi) == remote_fp(back)
