"""Tests for aah.core.build.evidence — evidence-v2 primitives.

NO MOCKS. Every test uses a real temporary git repository (the `git_repo`
fixture from conftest.py), real `git worktree add`, real file mutations, and
the real `run_git` subprocess wrapper. No git helper is monkeypatched.
"""

import os
from pathlib import Path

import pytest

from aah.core.common.git_utils import run_git
from aah.core.build.evidence import (
    EvidenceError,
    EVIDENCE_SCHEMA_VERSION,
    assert_subject_unchanged,
    capture_subject,
    evidence_is_fresh,
    hash_feature_contract,
    hash_test_inputs,
)


# Mirror the git_env autouse fixture from tests/test_git_ops.py so commits
# succeed without relying on the developer's global git identity.
GIT_ENV_KEYS = ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]


@pytest.fixture(autouse=True)
def git_env():
    os.environ["GIT_AUTHOR_NAME"] = "Test"
    os.environ["GIT_AUTHOR_EMAIL"] = "test@test.com"
    os.environ["GIT_COMMITTER_NAME"] = "Test"
    os.environ["GIT_COMMITTER_EMAIL"] = "test@test.com"
    yield
    for key in GIT_ENV_KEYS:
        os.environ.pop(key, None)


def _default_branch(repo: Path) -> str:
    """Detect the repo's actual default branch (main vs master)."""
    res = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return res.stdout.strip()


@pytest.fixture
def feature_md(git_repo):
    """Seed a feature contract .md (frontmatter + body) and commit it."""
    p = git_repo / "F001.feature.md"
    p.write_text(
        "---\n"
        "id: F001\n"
        "description: User authentication\n"
        "---\n"
        "\n"
        "# Feature F001\n"
        "\n"
        "Users can log in and log out.\n"
    )
    run_git(["add", "-A"], cwd=git_repo)
    run_git(["commit", "-m", "chore: add feature md"], cwd=git_repo)
    return p


@pytest.fixture
def test_file(git_repo):
    """Seed a tracked test file and commit it."""
    p = git_repo / "test_seed.py"
    p.write_text("def test_seed():\n    assert True\n")
    run_git(["add", "-A"], cwd=git_repo)
    run_git(["commit", "-m", "chore: add seed test"], cwd=git_repo)
    return p


# --------------------------------------------------------------------------
# AC1 — capture_subject: relative identity + trust boundary rejection
# --------------------------------------------------------------------------
def test_ac1_capture_and_reject(git_repo):
    # Capture on project root (before any worktree churns the working tree).
    subj = capture_subject(git_repo, git_repo)
    assert subj["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert subj["rel_path"] == "."
    assert subj["branch"] == _default_branch(git_repo)
    assert len(subj["head_sha"]) == 40  # real HEAD SHA preserved for audit
    int(subj["head_sha"], 16)  # valid hex
    int(subj["commit_sha"], 16)  # freshness identity is valid hex too
    assert subj["clean"] is True
    # NO absolute path anywhere in the dict.
    for v in subj.values():
        assert str(git_repo) not in str(v)

    # Create a REGISTERED worktree and capture on it.
    default = _default_branch(git_repo)
    registered_worktree = git_repo / ".claude" / "worktrees" / "F001"
    run_git(["worktree", "add", str(registered_worktree), "-b", "feature/F001", default], cwd=git_repo)
    wt_subj = capture_subject(registered_worktree, git_repo)
    assert wt_subj["rel_path"] == ".claude/worktrees/F001"
    assert wt_subj["branch"] == "feature/F001"

    # Reject an external path (outside the project root entirely).
    external = git_repo.parent / "outside-project"
    external.mkdir()
    with pytest.raises(EvidenceError):
        capture_subject(external, git_repo)

    # Reject a traversal path.
    with pytest.raises(EvidenceError):
        capture_subject(git_repo / ".." / "elsewhere", git_repo)

    # Reject an UNREGISTERED .claude/worktrees/ directory (proves the
    # registration check, not just the containment check).
    bogus = git_repo / ".claude" / "worktrees" / "bogus"
    bogus.mkdir(parents=True)
    with pytest.raises(EvidenceError):
        capture_subject(bogus, git_repo)

    # Reject a symlink escape: a link inside worktrees/ pointing outside root.
    escape_target = git_repo.parent / "symlink-target"
    escape_target.mkdir()
    link = git_repo / ".claude" / "worktrees" / "evil"
    link.symlink_to(escape_target, target_is_directory=True)
    with pytest.raises(EvidenceError):
        capture_subject(link, git_repo)


# --------------------------------------------------------------------------
# AC2 — hashing determinism / sensitivity
# --------------------------------------------------------------------------
def test_ac2_contract_hash_deterministic(feature_md):
    h1 = hash_feature_contract(feature_md)
    h2 = hash_feature_contract(feature_md)
    assert h1 == h2
    assert len(h1) == 64


def test_ac2_contract_hash_stable_across_line_endings(git_repo):
    lf = git_repo / "lf.md"
    crlf = git_repo / "crlf.md"
    lf.write_text("---\nid: X\n---\n\nBody line.\n", newline="")
    crlf.write_bytes(b"---\r\nid: X\r\n---\r\n\r\nBody line.\r\n")
    assert hash_feature_contract(lf) == hash_feature_contract(crlf)


def test_ac2_contract_hash_frontmatter_change(git_repo):
    a = git_repo / "a.md"
    b = git_repo / "b.md"
    a.write_text("---\nid: F001\n---\n\nSame body.\n")
    b.write_text("---\nid: F002\n---\n\nSame body.\n")
    assert hash_feature_contract(a) != hash_feature_contract(b)


def test_ac2_contract_hash_body_change(git_repo):
    a = git_repo / "a.md"
    b = git_repo / "b.md"
    a.write_text("---\nid: F001\n---\n\nBody version one.\n")
    b.write_text("---\nid: F001\n---\n\nBody version two.\n")
    assert hash_feature_contract(a) != hash_feature_contract(b)


def test_ac2_contract_hash_missing_file_raises(git_repo):
    with pytest.raises(EvidenceError):
        hash_feature_contract(git_repo / "does_not_exist.md")


def test_ac2_test_inputs_order_independent_and_sensitive(git_repo):
    t1 = git_repo / "test_one.py"
    t2 = git_repo / "test_two.py"
    t1.write_text("def test_one():\n    assert 1\n")
    t2.write_text("def test_two():\n    assert 2\n")

    h_order_a = hash_test_inputs([t1, t2], git_repo)
    h_order_b = hash_test_inputs([t2, t1], git_repo)
    assert h_order_a == h_order_b  # order-independent

    # Editing a file changes the hash.
    t1.write_text("def test_one():\n    assert 999\n")
    assert hash_test_inputs([t1, t2], git_repo) != h_order_a


def test_ac2_test_inputs_escape_and_missing_raise(git_repo):
    outside = git_repo.parent / "outside.py"
    outside.write_text("x = 1\n")
    with pytest.raises(EvidenceError):
        hash_test_inputs([outside], git_repo)
    with pytest.raises(EvidenceError):
        hash_test_inputs([git_repo / "nope.py"], git_repo)


# --------------------------------------------------------------------------
# AC3 — assert_subject_unchanged
# --------------------------------------------------------------------------
def test_ac3_passes_when_untouched(git_repo, test_file):
    before = capture_subject(git_repo, git_repo)
    assert assert_subject_unchanged(before, git_repo, git_repo) is None


def test_ac3_detects_new_untracked_source(git_repo, test_file):
    before = capture_subject(git_repo, git_repo)
    (git_repo / "newsrc.py").write_text("x = 1\n")
    with pytest.raises(EvidenceError):
        assert_subject_unchanged(before, git_repo, git_repo)


def test_ac3_detects_tracked_test_modification(git_repo, test_file):
    before = capture_subject(git_repo, git_repo)
    test_file.write_text("def test_seed():\n    assert False\n")
    with pytest.raises(EvidenceError):
        assert_subject_unchanged(before, git_repo, git_repo)


def test_ac3_tolerates_ephemeral(git_repo, test_file):
    before = capture_subject(git_repo, git_repo)
    (git_repo / ".pytest_cache").mkdir()
    (git_repo / ".pytest_cache" / "CACHEDIR.TAG").write_text("x\n")
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "mod.pyc").write_bytes(b"\x00\x01")
    (git_repo / "htmlcov").mkdir()
    (git_repo / "htmlcov" / "index.html").write_text("<html></html>\n")
    (git_repo / ".aah").mkdir(exist_ok=True)
    (git_repo / ".aah" / "state.json").write_text("{}\n")
    # None of these are source/test writes → must not raise.
    assert assert_subject_unchanged(before, git_repo, git_repo) is None


def test_ac3_tolerates_uv_lock(git_repo, test_file):
    # `uv run` writes uv.lock at the project root when a rewritten test command
    # resolves deps; that byproduct must not read as a source edit.
    before = capture_subject(git_repo, git_repo)
    (git_repo / "uv.lock").write_text("# generated by uv\nversion = 1\n")
    assert assert_subject_unchanged(before, git_repo, git_repo) is None


def test_ac3_detects_commit_between_before_after(git_repo, test_file):
    before = capture_subject(git_repo, git_repo)
    (git_repo / "another.py").write_text("y = 2\n")
    run_git(["add", "-A"], cwd=git_repo)
    run_git(["commit", "-m", "feat: land a commit"], cwd=git_repo)
    # Working tree is now clean, but the SHA changed → must raise.
    with pytest.raises(EvidenceError):
        assert_subject_unchanged(before, git_repo, git_repo)


# --------------------------------------------------------------------------
# AC4 — evidence_is_fresh
# --------------------------------------------------------------------------
def _stored(sha="a" * 40, contract="c1", test="t1"):
    return {"commit_sha": sha, "contract_hash": contract, "test_input_hash": test}


def _subject(sha="a" * 40):
    return {"schema_version": 2, "rel_path": ".", "branch": "main", "commit_sha": sha, "clean": True}


def test_ac4_fresh_when_all_match():
    assert evidence_is_fresh(_stored(), _subject(), "c1", "t1") is True


def test_ac4_stale_on_sha_diff():
    assert evidence_is_fresh(_stored(), _subject(sha="b" * 40), "c1", "t1") is False


def test_ac4_stale_on_contract_diff():
    assert evidence_is_fresh(_stored(), _subject(), "c2", "t1") is False


def test_ac4_stale_on_test_input_diff():
    assert evidence_is_fresh(_stored(), _subject(), "c1", "t2") is False


def test_ac4_stale_on_missing_key():
    incomplete = {"contract_hash": "c1", "test_input_hash": "t1"}  # no commit_sha
    assert evidence_is_fresh(incomplete, _subject(), "c1", "t1") is False


# --------------------------------------------------------------------------
# AC5 — no secrets / no absolute paths leak into evidence
# --------------------------------------------------------------------------
def _walk_strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk_strings(item)
    else:
        yield str(obj)


def test_ac5_no_secrets_or_absolute_paths(git_repo, feature_md, test_file):
    subject = capture_subject(git_repo, git_repo)
    evidence = {
        "subject": subject,
        "contract_hash": hash_feature_contract(feature_md),
        "test_input_hash": hash_test_inputs([test_file], git_repo),
    }
    strings = list(_walk_strings(evidence))
    for s in strings:
        assert str(git_repo) not in s, f"absolute project path leaked: {s}"
        assert ".attestation-secret" not in s
    lowered = " ".join(strings).lower()
    for forbidden in ("signature", "hmac", "secret", "attestation"):
        assert forbidden not in lowered, f"forbidden material present: {forbidden}"
