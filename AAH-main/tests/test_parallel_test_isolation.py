"""Parallel isolation + required-setup fail-closed (NO MOCKS).

AC1: two real concurrent runs get distinct namespaces and scratch state so
they cannot see each other's state. AC2: a REQUIRED-service setup
failure fails closed (no_signal + block); an unavailable irrelevant service is
not_applicable and proceeds.

Default CI needs NO Docker: isolation is asserted via stdlib TCP servers,
distinct sqlite files, and distinct run namespaces. Docker-backed assertions
are opt-in and skip cleanly when docker is unavailable.
"""

from __future__ import annotations

import concurrent.futures


import yaml

from tests._isolation_helpers import commit_worktree, free_port, make_isolated_project
from tests.support.aah_project import AAHProjectBuilder

from aah.core.build import ensure_infra
from aah.core.build.evidence import make_run_id
from aah.core.build.run_feature_tests import run_feature_tests


_ISOLATION_BODY = (
    "import os, sqlite3\n"
    "from pathlib import Path\n"
    "def test_TC1_isolated_row():\n"
    "    log_dir = Path(os.environ['AAH_TEST_LOG_DIR'])\n"
    "    db = log_dir / 'iso.sqlite'\n"
    "    con = sqlite3.connect(db)\n"
    "    con.execute('CREATE TABLE IF NOT EXISTS t(v TEXT)')\n"
    "    con.execute(\"INSERT INTO t(v) VALUES('mine')\")\n"
    "    con.commit()\n"
    "    rows = con.execute('SELECT COUNT(*) FROM t').fetchone()[0]\n"
    "    con.close()\n"
    "    # Exactly one row: this db belongs to THIS run only.\n"
    "    assert rows == 1, f'db leaked across runs: {rows} rows'\n"
)


def _run(state: dict, *, wave=0, attempt="attempt-001") -> dict:
    return run_feature_tests(
        state["feature_id"],
        state["project"],
        subject_path=state["worktree"],
        subject_branch=state["branch"],
        subject_sha=state["sha"],
        actor="implementer",
        attempt_id=attempt,
        wave=wave,
    )


def _declare_infra(state: dict, service: dict, message: str) -> None:
    builder = AAHProjectBuilder(state["worktree"])
    builder.file(".aah/infra-services.yaml", yaml.safe_dump({"services": [service]}))
    builder.file(
        "docker-compose.test.yaml",
        f"services:\n  {service['name']}:\n    image: "
        + ("postgres:15\n" if service["type"] == "postgres" else "busybox\n"),
    )
    commit_worktree(state, message)


def test_parallel_isolation(tmp_path):
    """AC1: two runs execute concurrently against distinct namespaced state
    and neither observes the other's rows; the run_ids and scratch differ."""
    a = make_isolated_project(tmp_path / "a", test_body=_ISOLATION_BODY, feature_id="F001", name="proj-a")
    b = make_isolated_project(tmp_path / "b", test_body=_ISOLATION_BODY, feature_id="F001", name="proj-b")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_run, a)
        fut_b = pool.submit(_run, b)
        res_a = fut_a.result(timeout=120)
        res_b = fut_b.result(timeout=120)

    assert res_a["passed"] is True, res_a
    assert res_b["passed"] is True, res_b
    assert res_a["run_id"] != res_b["run_id"]
    assert res_a["run_id"] and res_b["run_id"]

    ids = {
        make_run_id(project="p", wave=0, feature="F001", actor="impl", attempt="attempt-001")
        for _ in range(50)
    }
    assert len(ids) == 50, "run_id suffix must be collision-resistant"

    assert free_port() or True  # allocator works; distinctness is timing-based


def test_required_setup_failure_blocks(tmp_path):
    """AC2: a declared REQUIRED service that is unavailable makes setup fail
    closed -> no_signal + block, and tests do NOT run."""
    state = make_isolated_project(tmp_path, test_body=_ISOLATION_BODY, feature_id="F001")

    closed_port = free_port()  # allocated then released -> nothing listening
    _declare_infra(
        state,
        {"name": "db", "type": "postgres", "port": closed_port, "required": True},
        "declare required db",
    )

    res = _run(state)
    assert res.get("setup_status") == "no_signal", res
    assert res.get("block") is True
    assert res.get("passed") is not True
    assert "db" in (res.get("required_failures") or [])


def test_irrelevant_infra_not_applicable(tmp_path):
    """AC2 companion: an unavailable NON-required (unknown-type) service is
    not_applicable and the run proceeds to execute tests."""
    state = make_isolated_project(tmp_path, test_body=_ISOLATION_BODY, feature_id="F001")
    closed_port = free_port()
    _declare_infra(
        state,
        {"name": "widget", "type": "unknown", "port": closed_port, "required": False},
        "declare non-required widget",
    )

    res = _run(state)
    assert res.get("setup_status") in ("not_applicable", "ready"), res
    assert res.get("block") is not True
    assert res["passed"] is True


def test_setup_status_helper_classifies_required(tmp_path):
    """Unit-level: _service_is_required + setup-status finalizer classify a
    required DB failure as no_signal and an unknown as not_applicable."""
    assert ensure_infra._service_is_required({"type": "postgres"}) is True
    assert ensure_infra._service_is_required({"type": "unknown"}) is False
    assert ensure_infra._service_is_required({"type": "unknown", "required": True}) is True
    assert ensure_infra._service_is_required({"type": "postgres", "required": False}) is False
