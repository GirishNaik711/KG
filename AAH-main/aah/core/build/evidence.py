#!/usr/bin/env python3
"""Evidence-v2 building blocks.

This module is a STANDALONE, pure library for the evidence-v2 subsystem.

Design contract:
  * Importing it is side-effect free.
  * It writes NO artifacts and touches NO orchestrator state.
  * It provides the low-level primitives that a future consumer will compose
    into evidence records: capturing the checkout subject identity, hashing
    feature contracts and test inputs, asserting the subject was not mutated
    during evaluation, and deciding whether stored evidence is still fresh.

Security / correctness posture — FAIL CLOSED:
  * Path resolution is a TRUST BOUNDARY. Every path is realpath-collapsed and
    validated for containment BEFORE it is used for any git call or file read.
    Only the project root itself or a REGISTERED `.claude/worktrees/<x>` entry
    is an acceptable subject; traversal, arbitrary external paths, and symlink
    escapes are rejected because realpath collapses them and containment fails.
  * Every ambiguous, missing, or rejected condition raises EvidenceError.
    Nothing silently passes.
  * NO secret / attestation material is ever produced or embedded here, and
    absolute filesystem paths are minimized (subjects are stored relative,
    test-input identities are stored relative-to-root POSIX paths).
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import os
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.execution import CommandSpec, run_bounded_command
from aah.core.common.git_utils import code_subject_identity, list_worktrees, run_git
from aah.core.common.redaction import sanitize_output

EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_STATUSES = frozenset({"pass", "fail", "no_signal", "not_applicable"})


def write_attested_result(
    result: dict,
    output: Path,
    *,
    project_path: Path,
    command: list[str],
    artifact_name: str,
) -> None:
    """Persist a producer result using its private execution metadata."""
    from aah.core.common.attestation import write_attested

    meta = result.pop(
        "_attestation_meta",
        {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 0},
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_attested(
        result,
        output,
        project_path=project_path,
        command=command,
        exit_code=meta["exit_code"],
        stdout=meta["stdout"],
        stderr=meta["stderr"],
        duration_ms=meta["duration_ms"],
        artifact_name=artifact_name,
    )

# Default ceiling on the number of distinct log streams retained in one
# bundle. Beyond this the extra streams are dropped and the drop is recorded
# in the reproduction manifest — bounded-artifact count.
_DEFAULT_ARTIFACTS_CAP = 64

# Conservative retention policy used whenever no verified policy exists.
# local-only, 30-day cap on sanitized logs, append-only metadata through the
# milestone, and NO automatic destructive cleanup (a real policy AND a
# namespace-safe target are BOTH required before anything is deleted).
CONSERVATIVE_RETENTION_DEFAULT = {
    "schema_version": 1,
    "owner": None,
    "local_log_days": 30,
    "metadata_retention": "milestone",
    "allow_destructive_cleanup": False,
    "source": "fixed",
}

# Paths that are expected to churn during evaluation and therefore must NOT be
# treated as evidence-invalidating source/test writes. `.aah/` is included
# because AAH framework state (progress, audit logs, test results) is updated
# in-place during sessions and is routinely auto-committed — it is never part
# of the evaluated subject's source or test surface.
#
# Coverage/JUnit artifacts are NOT tolerated here — they are scanned separately
# via capture_coverage_artifacts / assert_no_coverage_artifacts and fail closed.
DEFAULT_EPHEMERAL_GLOBS = (
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".aah/",
    ".claude/",
    "htmlcov/",  # coverage HTML output — a test byproduct, gated separately by assert_no_coverage_artifacts
    ".playwright-mcp/",  # browser scratch output — raw screenshots before persistence
    "uv.lock",  # written by `uv run` when a rewritten test command resolves deps — a run byproduct, not a source edit
)

# Test-run OUTPUT files (coverage databases, JUnit reports). Distinct from the
# ephemeral globs above: presence in the subject fails closed via
# assert_no_coverage_artifacts, so these are never tolerated as drift — but they
# are also never test INPUTS, so input-identity hashing must skip them.
COVERAGE_ARTIFACT_FILES = frozenset({
    "coverage.xml",
    "coverage.json",
    "coverage.lcov",
    "coverage.md",
    ".coverage",
    "junit.xml",
    "test-results.xml",
})
COVERAGE_ARTIFACT_DIRS = frozenset({"htmlcov"})
COVERAGE_ARTIFACT_PREFIXES = (".coverage.",)


def is_coverage_artifact_name(fname: str) -> bool:
    """Whether a bare filename is a known coverage/JUnit output artifact."""
    return fname in COVERAGE_ARTIFACT_FILES or fname.startswith(COVERAGE_ARTIFACT_PREFIXES)


class EvidenceError(Exception):
    """Raised on any fail-closed condition in the evidence library."""

    pass


def _porcelain_clean_ignoring(porcelain_text: str, globs) -> bool:
    """True if no non-ephemeral porcelain entry remains (see _is_ephemeral)."""
    for line in porcelain_text.splitlines():
        if not line.strip():
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        rel_path = entry.strip().strip('"')
        if _is_ephemeral(rel_path, globs):
            continue
        return False
    return True


def build_evidence_v2_record(
    *,
    feature_id: str,
    producer: str,
    subject: dict,
    contract_hash: str,
    test_input_hash: str,
    test_paths: list[str],
    execution: dict,
    status: str,
    artifacts: dict,
) -> dict:
    """Build the common additive evidence-v2 envelope.

    The builder is intentionally pure.  Writers retain their legacy top-level
    compatibility fields and merge this returned mapping into the artifact.
    """
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise EvidenceError("Evidence feature_id must be a non-empty string")
    if not isinstance(producer, str) or not producer.strip():
        raise EvidenceError("Evidence producer must be a non-empty string")
    if status not in EVIDENCE_STATUSES:
        raise EvidenceError(f"Unsupported evidence status: {status!r}")
    if not isinstance(subject, dict):
        raise EvidenceError("Evidence subject must be a mapping")
    if not isinstance(execution, dict):
        raise EvidenceError("Evidence execution must be a mapping")
    if not isinstance(artifacts, dict):
        raise EvidenceError("Evidence artifacts must be a mapping")

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "producer": producer,
        "feature_id": feature_id,
        "subject": dict(subject),
        "inputs": {
            "contract_hash": contract_hash,
            "test_input_hash": test_input_hash,
            "test_paths": list(test_paths),
        },
        "execution": dict(execution),
        "status": status,
        "artifacts": dict(artifacts),
    }


def subject_matches_binding(
    subject: dict,
    *,
    expected_branch: str,
    expected_sha: str,
) -> bool:
    """Check an exact branch/SHA/clean-subject binding.

    Subject descriptors are embedded by several artifact schemas. Their
    binding semantics do not depend on the containing artifact's schema
    version.
    """
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (expected_branch, expected_sha)
    ):
        return False
    if not isinstance(subject, dict):
        return False
    return bool(
        subject.get("branch") == expected_branch
        and subject.get("commit_sha") == expected_sha
        and subject.get("clean") is True
    )


def evidence_matches_binding(
    evidence: dict,
    *,
    feature_id: str,
    expected_branch: str,
    expected_sha: str,
) -> bool:
    """Check an exact feature/branch/SHA/clean-subject binding.

    Missing or malformed data never matches.  Cleanliness is part of the
    binding gate even though it intentionally remains outside
    :func:`evidence_is_fresh`'s three-axis freshness semantics.
    """
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (feature_id, expected_branch, expected_sha)
    ):
        return False
    if not isinstance(evidence, dict):
        return False
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        return False
    if evidence.get("feature_id") != feature_id:
        return False
    return subject_matches_binding(
        evidence.get("subject"),
        expected_branch=expected_branch,
        expected_sha=expected_sha,
    )


def _normalize_text_bytes(text: str) -> bytes:
    """Normalize text for cross-platform-deterministic hashing.

    Collapses CRLF/CR line endings to LF, right-strips each line (to ignore
    trailing-whitespace churn), enforces a single trailing newline, and
    encodes as UTF-8. Used by BOTH hash functions so a hash computed on
    Windows matches one computed on Linux for logically identical content.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    # split() on trailing "\n" yields a final "" element; joining and adding a
    # single "\n" gives exactly one trailing newline regardless of input.
    while lines and lines[-1] == "":
        lines.pop()
    normalized = "\n".join(lines) + "\n"
    return normalized.encode("utf-8")


def _is_ephemeral(rel_path: str, globs) -> bool:
    """Return True if rel_path matches an ephemeral glob.

    Globs ending in "/" are treated as directory prefixes (match the dir
    itself or anything beneath it, at any depth). Others use fnmatch on the
    full relative path AND on the basename, so bare-name patterns like
    "*.pyc" match nested files too.
    """
    posix = rel_path.replace("\\", "/")
    basename = posix.rsplit("/", 1)[-1]
    for glob in globs:
        if glob.endswith("/"):
            prefix = glob.rstrip("/")
            if posix == prefix or posix.startswith(prefix + "/") or ("/" + prefix + "/") in ("/" + posix):
                return True
        else:
            if fnmatch.fnmatch(posix, glob) or fnmatch.fnmatch(basename, glob):
                return True
    return False


def _registered_worktree_realpaths(project_root: Path) -> list[str]:
    """Realpath of every git-registered worktree, as seen from project_root."""
    out: list[str] = []
    for wt in list_worktrees(cwd=project_root):
        wt_path = wt.get("path")
        if wt_path:
            out.append(os.path.realpath(wt_path))
    return out


def _validate_subject_path(path: Path, project_root: Path) -> tuple[str, str]:
    """Trust boundary: resolve and authorize a subject path.

    Returns (subject_realpath, root_realpath). Raises EvidenceError if the
    path is not the project root itself nor a registered worktree beneath
    `<root>/.claude/worktrees/`.
    """
    subj_real = os.path.realpath(path)
    root_real = os.path.realpath(project_root)

    if subj_real == root_real:
        return subj_real, root_real

    subj_p = Path(subj_real)
    worktrees_base = Path(root_real) / ".claude" / "worktrees"
    if subj_p.is_relative_to(worktrees_base):
        registered = _registered_worktree_realpaths(project_root)
        if subj_real in registered:
            return subj_real, root_real
        raise EvidenceError(
            f"Subject path is under .claude/worktrees/ but is not a registered "
            f"git worktree: {subj_real}"
        )

    raise EvidenceError(
        f"Subject path is neither the project root nor a registered worktree: "
        f"{subj_real} (root={root_real})"
    )


def capture_subject(path: Path, project_root: Path) -> dict:
    """Capture the normalized, RELATIVE checkout identity of a subject.

    The subject must be either the project root or a REGISTERED worktree under
    `<root>/.claude/worktrees/`. Path resolution is validated as a trust
    boundary BEFORE any git command runs.

    Returns a dict with NO absolute paths and NO secrets::

        {
          "schema_version": 2,
          "rel_path": "." | ".claude/worktrees/<x>",   # POSIX, relative to root
          "branch": <str>,
          "commit_sha": <40-hex str>,
          "clean": <bool>,
        }

    Raises EvidenceError on any traversal / external / unregistered / symlink
    escape, or if git fails to resolve the checkout.
    """
    subj_real, root_real = _validate_subject_path(path, project_root)

    # Relative POSIX identity — never an absolute path.
    if subj_real == root_real:
        rel_path = "."
    else:
        rel_path = Path(subj_real).relative_to(root_real).as_posix()

    branch_res = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=Path(subj_real), check=False)
    if branch_res.returncode != 0:
        raise EvidenceError(
            f"Failed to resolve branch for subject {rel_path}: {branch_res.stderr.strip()}"
        )
    branch = branch_res.stdout.strip()

    sha_res = run_git(["rev-parse", "HEAD"], cwd=Path(subj_real), check=False)
    if sha_res.returncode != 0:
        raise EvidenceError(
            f"Failed to resolve commit SHA for subject {rel_path}: {sha_res.stderr.strip()}"
        )
    head_sha = sha_res.stdout.strip()

    # Freshness identity always ignores .aah/.claude bookkeeping commits so a
    # passing attempt survives framework auto-commits. Never downgrade to the
    # raw HEAD SHA: producers and consumers must use one identity contract.
    commit_sha = code_subject_identity(cwd=Path(subj_real))
    if commit_sha is None:
        raise EvidenceError(
            f"Failed to compute code subject identity for {rel_path}"
        )

    status_res = run_git(["status", "--porcelain"], cwd=Path(subj_real), check=False)
    if status_res.returncode != 0:
        raise EvidenceError(
            f"Failed to read git status for subject {rel_path}: {status_res.stderr.strip()}"
        )
    clean = _porcelain_clean_ignoring(status_res.stdout, DEFAULT_EPHEMERAL_GLOBS)

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "rel_path": rel_path,
        "branch": branch,
        "commit_sha": commit_sha,
        "head_sha": head_sha,
        "clean": clean,
    }


def hash_feature_contract(feature_md_path: Path) -> str:
    """Deterministically hash a feature contract .md (frontmatter + body).

    The ENTIRE markdown file is hashed — both frontmatter AND body — so that
    changing either the frontmatter or the prose body changes the hash. Do NOT
    route this through a frontmatter parser, which would drop the body.

    Raises EvidenceError if the file is missing.
    """
    p = Path(feature_md_path)
    if not p.is_file():
        raise EvidenceError(f"Feature contract file not found: {p}")
    raw = p.read_text(encoding="utf-8")
    return hashlib.sha256(_normalize_text_bytes(raw)).hexdigest()


def hash_test_inputs(test_paths: list[Path], project_root: Path) -> str:
    """Deterministically hash a set of test input files.

    Each path is realpath-collapsed and required to live inside project_root
    (else EvidenceError — trust boundary + no absolute paths leak into the
    hash identity). Per-file content is normalized and sha256'd; (rel, digest)
    tuples are SORTED by relative POSIX path and folded into a rolling sha256.

    The result is order-independent (sorting) but rename-sensitive (the
    relative path is part of the folded identity). Raises EvidenceError if any
    path is missing or escapes project_root.
    """
    root_real = os.path.realpath(project_root)
    pairs: list[tuple[str, str]] = []

    for tp in test_paths:
        tp_real = os.path.realpath(tp)
        tp_p = Path(tp_real)
        if not tp_p.is_relative_to(root_real):
            raise EvidenceError(
                f"Test input path escapes project root: {tp_real} (root={root_real})"
            )
        if not tp_p.is_file():
            raise EvidenceError(f"Test input file not found: {tp_real}")
        rel = tp_p.relative_to(root_real).as_posix()
        raw = tp_p.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary inputs (fixtures, sqlite, images) get a raw-bytes digest.
            # Text normalization exists to make line endings platform-agnostic;
            # applying it to bytes would be meaningless, and failing outright
            # would make any binary fixture unhashable.
            digest = hashlib.sha256(raw).hexdigest()
        else:
            digest = hashlib.sha256(_normalize_text_bytes(content)).hexdigest()
        pairs.append((rel, digest))

    pairs.sort(key=lambda pair: pair[0])
    rolling = hashlib.sha256()
    for rel, digest in pairs:
        rolling.update(f"{rel}\n{digest}\n".encode("utf-8"))
    return rolling.hexdigest()


def capture_coverage_artifacts(path: Path, project_root: Path) -> set[str]:
    """Return the set of coverage/JUnit artifacts currently present in the subject.

    Walks the working tree (excluding .git/, .aah/, .claude/) and returns relative
    POSIX paths for known artifact patterns: coverage.xml, coverage.json, .coverage,
    htmlcov/, junit.xml, etc. Raises EvidenceError if the subject path is invalid.
    """
    subj_real, _ = _validate_subject_path(path, project_root)
    subject_path = Path(subj_real)
    artifacts = set()
    known_dirs = COVERAGE_ARTIFACT_DIRS
    exclude_parts = {".git", ".aah", ".claude"}

    for root, dirs, files in os.walk(subject_path):
        # Filter out excluded directories (modifies in-place)
        dirs[:] = [d for d in dirs if d not in exclude_parts]
        rel_root = Path(root).relative_to(subject_path).as_posix()
        if rel_root == ".":
            rel_root = ""
        for fname in files:
            if is_coverage_artifact_name(fname):
                rel = (Path(rel_root) / fname).as_posix() if rel_root else fname
                artifacts.add(rel)
        for dname in dirs:
            if dname in known_dirs:
                rel = (Path(rel_root) / dname).as_posix() if rel_root else dname
                artifacts.add(rel + "/")
    return artifacts


def assert_no_coverage_artifacts(before: set[str], path: Path, project_root: Path) -> None:
    """Assert no coverage/JUnit artifacts were written into the subject during evaluation.

    Captures current artifacts and compares to the before-set. New artifacts
    (present after but not before) fail closed. Artifacts present before AND after
    (e.g., committed coverage.xml) are NOT flagged. Raises EvidenceError if new
    artifacts are detected, independent of .gitignore.
    """
    current = capture_coverage_artifacts(path, project_root)
    new = current - before
    if new:
        sorted_new = sorted(new)
        raise EvidenceError(
            "Coverage/JUnit artifact(s) written into subject checkout: " + ", ".join(sorted_new)
        )


def assert_subject_unchanged(
    before: dict,
    path: Path,
    project_root: Path,
    ephemeral_globs=DEFAULT_EPHEMERAL_GLOBS,
) -> None:
    """Assert the subject checkout was not mutated (source/test writes) since `before`.

    Recaptures the subject. Raises EvidenceError if the commit SHA changed
    (a commit landed between before/after) or if `git status --porcelain`
    reports any non-ephemeral modified/added/deleted path. Redirected caches
    and declared ephemeral outputs (see DEFAULT_EPHEMERAL_GLOBS) are tolerated.

    Coverage/JUnit artifacts are NO LONGER tolerated here — they are scanned
    separately via assert_no_coverage_artifacts and fail closed.
    """
    after = capture_subject(path, project_root)

    if before.get("commit_sha") != after.get("commit_sha"):
        raise EvidenceError(
            f"Subject commit SHA changed during evaluation: "
            f"{before.get('commit_sha')} -> {after.get('commit_sha')}"
        )

    subj_real, _ = _validate_subject_path(path, project_root)
    status_res = run_git(["status", "--porcelain"], cwd=Path(subj_real), check=False)
    if status_res.returncode != 0:
        raise EvidenceError(f"Failed to read git status: {status_res.stderr.strip()}")

    offenders: list[str] = []
    for line in status_res.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1: XY<space>path  (path starts at column 3).
        entry = line[3:]
        # Renames/copies are reported as "old -> new"; the destination is what
        # actually exists on disk, so evaluate the destination path.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        # Porcelain may quote paths containing special chars.
        rel_path = entry.strip().strip('"')
        if _is_ephemeral(rel_path, ephemeral_globs):
            continue
        offenders.append(rel_path)

    if offenders:
        raise EvidenceError(
            "Subject was modified during evaluation (non-ephemeral changes): "
            + ", ".join(sorted(offenders))
        )


def evidence_is_fresh(
    stored_evidence: dict,
    current_subject: dict,
    current_contract_hash: str,
    current_test_input_hash: str,
) -> bool:
    """Decide whether stored evidence is still valid for the current state.

    Freshness compares EXACTLY three axes: the commit SHA, the feature
    contract hash, and the test-input hash. If any of the three differs (or a
    required key is missing — missing compares unequal, i.e. fail closed), the
    evidence is stale and this returns False. Only when all three match does it
    return True.

    Branch name and working-tree cleanliness are INTENTIONALLY excluded from
    the freshness decision: the same commit evaluated under a different branch
    label is still the same evaluated content, and cleanliness is enforced
    separately via assert_subject_unchanged rather than by freshness.
    """
    stored_sha = stored_evidence.get("commit_sha", None)
    current_sha = current_subject.get("commit_sha", "__missing_current_sha__")
    if stored_sha is None or stored_sha != current_sha:
        return False

    stored_contract = stored_evidence.get("contract_hash", None)
    if stored_contract is None or stored_contract != current_contract_hash:
        return False

    stored_test = stored_evidence.get("test_input_hash", None)
    if stored_test is None or stored_test != current_test_input_hash:
        return False

    return True


# ===========================================================================
# run-id namespacing, output sanitization, failure bundles, retention
# ===========================================================================


def _safe_namespace_token(value: str, *, fallback: str) -> str:
    """Return a bounded filesystem-safe token without losing record identity.

    Moved here from run_feature_tests.py so the runner and the run-id builder
    share ONE sanitizer. Non-[A-Za-z0-9_.-] runs collapse to a single '-';
    leading/trailing '.'/'-' are stripped; an empty result falls back. Tokens
    longer than 40 chars are hash-collapsed so identity is preserved without
    unbounded growth.
    """
    raw = value if isinstance(value, str) and value else fallback
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-") or fallback
    if len(normalized) <= 40:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{normalized[:29]}-{digest}"


def _docker_safe_token(value: str, *, fallback: str) -> str:
    """Lowercase [a-z0-9-] token suitable for a Compose project name.

    Compose project names / container names / network names accept only
    ``[a-z0-9][a-z0-9_-]*`` — no dots, no uppercase. This is stricter than
    :func:`_safe_namespace_token`: it lowercases, maps every disallowed run
    (including '.' and '_') to '-', and strips leading/trailing '-'.
    """
    raw = value if isinstance(value, str) and value else fallback
    normalized = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or fallback
    return normalized


def make_run_id(
    *,
    project: str,
    wave: str | int | None,
    feature: str,
    actor: str,
    attempt: str,
) -> str:
    """Build a collision-resistant, filesystem+docker-safe run namespace.

    Each component is sanitized to lowercase ``[a-z0-9-]`` (Compose
    project-name rules), joined by '-', and suffixed with
    ``secrets.token_hex(4)`` so two runs sharing the same
    project/wave/feature/actor/attempt still get distinct namespaces —
    parallel runs cannot collide. The deterministic prefix is bounded: if it
    would exceed ~80 chars it is hash-collapsed, so the total id stays within
    Docker/​filesystem limits regardless of input length.
    """
    wave_token = "w?" if wave is None or str(wave) == "" else f"w{wave}"
    parts = [
        _docker_safe_token(project, fallback="project"),
        _docker_safe_token(wave_token, fallback="w"),
        _docker_safe_token(feature, fallback="feature"),
        _docker_safe_token(actor, fallback="actor"),
        _docker_safe_token(attempt, fallback="attempt"),
    ]
    deterministic = "-".join(parts)
    # Bound the deterministic portion so the full id (with prefix + suffix)
    # stays comfortably under Compose's practical name length.
    if len(deterministic) > 80:
        digest = hashlib.sha256(deterministic.encode("utf-8")).hexdigest()[:12]
        deterministic = f"{deterministic[:60].rstrip('-')}-{digest}"
    suffix = secrets.token_hex(4)
    run_id = f"aah-{deterministic}-{suffix}"
    # Final defensive clamp to Compose-safe charset + length.
    run_id = _docker_safe_token(run_id, fallback=f"aah-run-{suffix}")
    return run_id


# --- write-scope trust boundary -------------------------------------------


def _validate_write_scope(target: Path, project_root: Path) -> Path:
    """Trust boundary for any evidence WRITE or explicit DELETE target.

    A target is authorized ONLY if, after realpath collapse (which also
    defeats ``../`` traversal and symlink escapes), it is contained within one
    of:
      * ``<project_root>/.aah/``               (attested project evidence store)
      * ``<project_root>/.claude/worktrees/``  (registered worktree scope)

    The project root itself, arbitrary system-temp paths, and anything outside
    these bases are REFUSED. Namespaced run scratch is created via
    ``TemporaryDirectory`` (self-cleaning) and namespace teardown
    (:func:`cleanup_namespace`) only issues docker commands — neither routes a
    filesystem deletion through this gate — so temp is intentionally NOT in
    scope. This keeps the trust boundary sharp: ``/tmp/outside``, the project root, and
    ``../../`` traversal all refuse, writing/deleting NOTHING.

    Returns the realpath-collapsed target on success; raises EvidenceError on
    any scope escape.
    """
    target_real = Path(os.path.realpath(target))
    root_real = Path(os.path.realpath(project_root))

    allowed_bases = [
        root_real / ".aah",
        root_real / ".claude" / "worktrees",
    ]

    for base in allowed_bases:
        if target_real == base or target_real.is_relative_to(base):
            return target_real

    raise EvidenceError(
        f"Write/delete target escapes the authorized evidence scope: {target_real}"
    )


# --- tool versions --------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _tool_versions() -> dict:
    """Best-effort tool versions for the reproduction manifest.

    Never raises: a missing tool records ``None`` rather than failing the
    bundle write. Docker version is probed with a short timeout.
    """
    versions: dict = {
        "python": sys.version.split()[0],
        "pytest": None,
        "docker": None,
    }
    try:
        import pytest as _pytest  # noqa: PLC0415

        versions["pytest"] = getattr(_pytest, "__version__", None)
    except Exception:
        pass
    if shutil.which("docker"):
        outcome = run_bounded_command(
            CommandSpec(["docker", "--version"], timeout_sec=5)
        )
        if outcome.ok:
            versions["docker"] = outcome.stdout.strip()
    return versions


# --- failure bundle -------------------------------------------------------


def write_failure_bundle(
    *,
    run_id: str,
    subject: dict,
    execution: dict,
    cleanup: dict | None,
    diagnostic: dict | None = None,
    logs: dict,
    retention: dict,
    out_dir: Path,
) -> Path:
    """Write a sanitized reproduction bundle under ``out_dir/<run_id>/``.

    Produces a ``reproduction.json`` manifest identifying the subject
    SHA, argv, cwd (relative), adapter/stack, seed, tool versions, the cleanup
    evidence block, and a per-log sha256; and one sanitized ``*.log`` file per
    stream in ``logs``. EVERY log's text passes through :func:`sanitize_output`
    BEFORE it is written. The number of retained streams uses the fixed
    project-local artifact cap.

    ``out_dir`` is validated via :func:`_validate_write_scope` BEFORE anything
    is written; on scope escape this raises EvidenceError and writes NOTHING
    (fail closed). ``run_id`` is re-sanitized defensively so a crafted id
    cannot traverse out of ``out_dir``.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise EvidenceError("write_failure_bundle requires a non-empty run_id")
    if not isinstance(logs, dict):
        raise EvidenceError("write_failure_bundle requires a logs mapping")

    project_root = execution.get("project_root") if isinstance(execution, dict) else None
    if not project_root:
        raise EvidenceError("write_failure_bundle requires execution.project_root for scope validation")

    # Validate out_dir scope BEFORE constructing any path under it.
    out_real = _validate_write_scope(Path(out_dir), Path(project_root))

    safe_run = _docker_safe_token(run_id, fallback="run")
    bundle_dir = out_real / safe_run
    # The bundle dir must itself remain within scope (defensive; safe_run has
    # no separators, but re-validate so a future change cannot leak).
    bundle_dir_real = _validate_write_scope(bundle_dir, Path(project_root))
    bundle_dir_real.mkdir(parents=True, exist_ok=True)

    # Write sanitized logs, bounded by the fixed local policy.
    log_index: dict = {}
    dropped_streams: list[str] = []
    for i, (name, text) in enumerate(sorted(logs.items())):
        safe_name = _safe_namespace_token(str(name), fallback=f"log{i}")
        if i >= _DEFAULT_ARTIFACTS_CAP:
            dropped_streams.append(safe_name)
            continue
        sanitized = sanitize_output(text if isinstance(text, str) else str(text))
        log_path = bundle_dir_real / f"{safe_name}.log"
        # Re-validate the concrete file path is still in scope, then write.
        _validate_write_scope(log_path, Path(project_root))
        log_path.write_text(sanitized, encoding="utf-8")
        log_index[safe_name] = {
            "file": log_path.name,
            "sha256": hashlib.sha256(sanitized.encode("utf-8", errors="replace")).hexdigest(),
            "bytes": len(sanitized.encode("utf-8", errors="replace")),
        }

    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "subject": {
            "commit_sha": subject.get("commit_sha") if isinstance(subject, dict) else None,
            "branch": subject.get("branch") if isinstance(subject, dict) else None,
            "rel_path": subject.get("rel_path") if isinstance(subject, dict) else None,
        },
        "execution": {
            "argv": execution.get("argv"),
            "cwd": execution.get("cwd"),
            "adapter": execution.get("adapter"),
            "seed": execution.get("seed"),
            "started_at": execution.get("started_at"),
            "finished_at": execution.get("finished_at"),
            "exit_code": execution.get("exit_code"),
        },
        "tool_versions": _tool_versions(),
        "cleanup": cleanup if isinstance(cleanup, dict) else None,
        "diagnostic": diagnostic if isinstance(diagnostic, dict) else None,
        "logs": log_index,
        "dropped_log_streams": dropped_streams,
        "retention": {
            "local_log_days": retention.get("local_log_days") if isinstance(retention, dict) else None,
            "metadata_retention": retention.get("metadata_retention") if isinstance(retention, dict) else None,
            "source": retention.get("source") if isinstance(retention, dict) else None,
        },
    }

    manifest_path = bundle_dir_real / "reproduction.json"
    _validate_write_scope(manifest_path, Path(project_root))
    from aah.core.common.io_utils import write_json

    write_json(manifest, manifest_path)
    return bundle_dir_real


def read_evidence_retention(project_path: Path) -> dict:
    """Return the fixed, local-only, non-destructive retention contract.

    Legacy ``evidence-retention.yaml`` files are intentionally ignored. They
    changed metadata but never enforced deletion and must not imply otherwise.
    """
    del project_path
    return dict(CONSERVATIVE_RETENTION_DEFAULT)
