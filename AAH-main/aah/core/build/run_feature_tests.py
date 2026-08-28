#!/usr/bin/env python3
"""
Run a single feature's test suite and record results.

Looks up the feature's test configuration and executes tests. Official runs
write .aah/build/test-results/<feature-id>.json; ``--provisional`` runs never
write gate-eligible evidence.
Exit 0 = all tests pass, Exit 2 = failures.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from aah.core.build.evidence import (
    DEFAULT_EPHEMERAL_GLOBS,
    EvidenceError,
    _is_ephemeral,
    _safe_namespace_token,
    assert_no_coverage_artifacts,
    assert_subject_unchanged,
    build_evidence_v2_record,
    capture_coverage_artifacts,
    capture_subject,
    evidence_matches_binding,
    hash_feature_contract,
    hash_test_inputs,
    is_coverage_artifact_name,
    make_run_id,
    read_evidence_retention,
    sanitize_output,
    write_attested_result,
    write_failure_bundle,
)
from aah.core.build.lang_checks._helpers import rewrite_bare_venv_command
from aah.core.common.execution import CommandSpec, run_bounded_command
from aah.core.common.redaction import (
    exact_value_patterns,
    sanitize_structure,
)

# Canonical command prefix recorded in the attestation block. The
# orchestrator's per-feature test-result reads (e.g. via the regression
# fallback at run_regression_suite.py) trust this prefix for verify().
COMMAND_PREFIX = ["aah", "run", "core.build.run_feature_tests"]

_TERMINAL_COVERAGE_REPORTS = frozenset({"term", "term-missing", "term:skip-covered"})
_FILE_COVERAGE_REPORTS = {
    "xml": "coverage.xml",
    "html": "htmlcov",
    "json": "coverage.json",
    "lcov": "coverage.lcov",
    "annotate": "coverage-annotate",
    "markdown": "coverage.md",
    "markdown-append": "coverage.md",
}

# A test subprocess needs a small amount of platform bootstrap state to find
# its interpreter and temporary directories.  Everything else is opt-in via a
# feature's validated ``required_env`` declaration.  In particular, arbitrary
# ambient credentials are not inherited merely because the parent process has
# them.
#
# ``AWS_PROFILE`` / ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` are allowed through:
# a profile name is a local ``~/.aws`` alias (not a secret) and the region is
# public. Letting them flow lets a test / the runtime validator authenticate to
# the cloud services ``/aah-access`` validated. The runtime validator also
# injects these from ``cloud-readiness.yaml`` when the shell doesn't carry them.
_SAFE_TEST_ENV_KEYS = frozenset({
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_REGION",
    "CI",
    "COLORTERM",
    "COMSPEC",
    "CONDA_PREFIX",
    "FORCE_COLOR",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "PYENV_ROOT",
    "PYTEST_ADDOPTS",
    "PYTHONHASHSEED",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SHELL",
    "SYSTEMROOT",
    "TERM",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
})

# Planned test-case ids may carry punctuation a Python test name cannot, so the
# planned-to-collected join compares normalized token sequences.
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _parse_junit_xml(
    xml_paths: Path | list[Path],
    *,
    extra_patterns: tuple[re.Pattern[str], ...] = (),
) -> dict:
    """Parse one or more JUnit XML files into structured per-test results.

    Accepts a single path or a list. A list is merged into one summary because
    not every runner writes one file per run the way ``pytest --junitxml``
    does — Maven Surefire and Gradle write one file per test class, so reading
    only the first would undercount the suite.

    Returns a dict with keys: summary, test_cases, failures.
    """
    result = {
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "duration_s": 0.0},
        "test_cases": [],
        "failures": [],
    }

    paths = [xml_paths] if isinstance(xml_paths, Path) else list(xml_paths)
    for xml_path in paths:
        _merge_junit_file(xml_path, result)

    return sanitize_structure(
        result,
        extra_patterns=extra_patterns,
        preserve_keys={"status"},
    )


def _merge_junit_file(xml_path: Path, result: dict) -> None:
    """Accumulate one JUnit XML file into ``result`` in place.

    Unreadable or non-JUnit files are skipped rather than raising: the caller
    distinguishes "no report" from "empty report" by whether it found any
    files at all, so a malformed file must not be able to abort the parse of
    its siblings.
    """
    if not xml_path.exists():
        return

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError:
        return

    # Handle both <testsuite> as root and <testsuites><testsuite>... structures
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        return

    for suite in suites:
        suite_time = float(suite.get("time", 0))
        result["summary"]["duration_s"] += suite_time

        for tc in suite.findall("testcase"):
            name = tc.get("name", "unknown")
            classname = tc.get("classname", "")
            duration = float(tc.get("time", 0))

            # Determine status
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")

            if failure is not None:
                status = "failed"
                result["summary"]["failed"] += 1
                result["failures"].append({
                    "name": name,
                    "classname": classname,
                    "message": (failure.get("message", "") or "")[:500],
                    "traceback": (failure.text or "")[:1000],
                })
            elif error is not None:
                status = "error"
                result["summary"]["errors"] += 1
                result["failures"].append({
                    "name": name,
                    "classname": classname,
                    "message": (error.get("message", "") or "")[:500],
                    "traceback": (error.text or "")[:1000],
                })
            elif skipped is not None:
                status = "skipped"
                result["summary"]["skipped"] += 1
            else:
                status = "passed"
                result["summary"]["passed"] += 1

            result["summary"]["total"] += 1
            result["test_cases"].append({
                "name": name,
                "classname": classname,
                "status": status,
                "duration_s": duration,
            })


def _tc_matches(tc_id: str, collected_test: dict) -> bool:
    """Match a TC id as an exact token sequence in a collected test name.

    Test identifiers may contain punctuation that Python test names cannot
    (for example ``TC-F-MOD000-00-01``).  Comparing normalized token sequences
    preserves that canonical id without allowing prefix matches such as TC1
    matching TC10.
    """
    target = [tok.lower() for tok in _TOKEN_SPLIT.split(tc_id) if tok]
    if not target:
        return False
    for field in ("name", "classname"):
        s = collected_test.get(field, "") or ""
        tokens = [tok.lower() for tok in _TOKEN_SPLIT.split(str(s)) if tok]
        width = len(target)
        for start in range(len(tokens) - width + 1):
            if tokens[start:start + width] == target:
                return True
    return False


def _missing_planned_test_cases(feature_yaml: dict, collected: list[dict]) -> list[str]:
    """Planned test-case ids with no matching collected test.

    The planning-side check proves every AC has a planned test; this proves
    every planned test was actually written and collected. A renamed, deleted,
    or never-written test fails here instead of passing silently.
    """
    planned = [
        tc["id"] for tc in (feature_yaml.get("test_cases") or [])
        if isinstance(tc, dict) and isinstance(tc.get("id"), str) and tc["id"].strip()
    ]
    return [
        tc_id for tc_id in planned
        if not any(_tc_matches(tc_id, test) for test in collected)
    ]


def _dotenv_values(project_path: Path, required_keys: list[str]) -> dict[str, str]:
    """Read declared keys from the real root ``.env`` only.

    Unrelated keys are ignored so a shared project ``.env`` can serve several
    features. Structural errors, duplicate keys, and protected-key overrides
    fail closed. ``.env.example`` is never a value source.
    """
    dotenv = project_path / ".env"
    if not dotenv.exists() or not required_keys:
        return {}

    from aah.core.common.readiness import DEFAULT_PROTECTED_ENV_KEYS, parse_env_file

    env, errors = parse_env_file(
        dotenv,
        required_keys,
        DEFAULT_PROTECTED_ENV_KEYS,
        ignore_undeclared=True,
    )
    if errors:
        raise EvidenceError("Invalid project .env: " + "; ".join(errors))
    return env


def _required_env_keys(feature_yaml: dict, feature_id: str, subject_path: Path) -> list[str]:
    """Return the additive union of root and subject ``required_env`` keys.

    The implementer edits the feature YAML inside its worktree (``.aah/`` is
    git-tracked), so a mid-implementation addition may live there. Subject
    metadata may add requirements but can never remove root requirements.
    """
    from aah.core.common.feature_utils import (
        find_feature_file,
        parse_feature_frontmatter,
    )
    from aah.core.common.validators import validate_required_env_keys

    declarations: list[tuple[str, object]] = [
        ("root required_env", feature_yaml.get("required_env")),
    ]
    subject_features = subject_path / ".aah" / "plan" / "features"
    subject_file = find_feature_file(subject_features, feature_id)
    if subject_file:
        subject_yaml = parse_feature_frontmatter(subject_file)
        if subject_yaml and "required_env" in subject_yaml:
            declarations.append(("subject required_env", subject_yaml.get("required_env")))

    resolved: list[str] = []
    seen: set[str] = set()
    for context, raw_keys in declarations:
        if raw_keys is None:
            continue
        if not isinstance(raw_keys, list):
            raise EvidenceError(f"{context} must be a list of environment-key names")
        errors = validate_required_env_keys(raw_keys, context)
        if errors:
            raise EvidenceError("; ".join(errors))
        for raw_key in raw_keys:
            key = raw_key.strip()
            if key not in seen:
                seen.add(key)
                resolved.append(key)
    return resolved


def _resolve_required_env(
    feature_yaml: dict,
    feature_id: str,
    project_path: Path,
    subject_path: Path,
) -> tuple[dict[str, str], list[str]]:
    """Resolve declared credentials and return ``(values, missing_keys)``.

    Opt-in: a feature with no ``required_env`` resolves to empty values and no
    missing keys. A declared key is satisfied
    only by a non-empty process value or the authoritative root ``.env``.
    Process values win. ``.env.example`` placeholders never satisfy the gate.
    """
    required = _required_env_keys(feature_yaml, feature_id, subject_path)
    if not required:
        return {}, []
    dotenv = _dotenv_values(project_path, required)
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key in required:
        value = os.environ.get(key) or dotenv.get(key)
        if value:
            resolved[key] = value
        else:
            missing.append(key)
    return resolved, missing


def resolve_feature_set_required_env(
    project_path: Path,
    feature_ids: list[str],
    *,
    subject_path: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Resolve the additive required-env union for a feature set."""
    from aah.core.common.feature_utils import load_feature_data

    subject = subject_path or project_path
    features_dir = project_path / ".aah" / "plan" / "features"
    resolved: dict[str, str] = {}
    missing: set[str] = set()
    for feature_id in feature_ids:
        contract = load_feature_data(features_dir, feature_id) or {"id": feature_id}
        values, absent = _resolve_required_env(
            contract,
            feature_id,
            project_path,
            subject,
        )
        resolved.update(values)
        missing.update(absent)
    return resolved, sorted(missing)


def _missing_required_env(
    feature_yaml: dict,
    feature_id: str,
    project_path: Path,
    subject_path: Path,
) -> list[str]:
    """Compatibility helper returning only missing declared keys."""
    return _resolve_required_env(
        feature_yaml,
        feature_id,
        project_path,
        subject_path,
    )[1]


def find_test_command(feature_yaml: dict, project_path: Path) -> str | None:
    """Determine the test command for a feature.

    Returns a shell-style command string (executed with ``shell=True``) or
    ``None`` if no test command can be resolved.

    JUnit XML reporting is configured via PYTEST_ADDOPTS in the runner environment,
    not via command-line injection.

    Per-language defaults are resolved through the lang_checks
    adapter pack rather than inlined per-language branches. The
    ``feature.test_config.command`` explicit override and the init.sh
    precondition are unchanged.
    """
    # Check if feature has explicit test command
    test_config = feature_yaml.get("test_config") or {}
    if not isinstance(test_config, dict):
        return None
    if test_config.get("command"):
        # Worktrees never have their own .venv, so normalize a literal
        # ".venv"-relative command to run via `uv run` instead.
        return rewrite_bare_venv_command(test_config["command"])

    # Check for init.sh test runner — preserved precondition: only
    # auto-resolve a default test command for projects that have
    # been bootstrapped with the standard init.sh layout.
    init_sh = project_path / ".aah" / "init.sh"
    if not init_sh.exists():
        return None

    # Dispatch to the language adapter pack. The adapter
    # consults manifest.stack_choices.primary AND the project's file
    # fingerprints to pick the right adapter, then returns a structured
    # Cmd. We unparse argv back to a shell string here because the
    # call site preserves shell=True for compatibility, so unparse argv.
    import shlex

    from aah.core.build.lang_checks import resolve_test_command

    feature_id = feature_yaml.get("id", "")
    cmd = resolve_test_command(project_path, feature_filter=feature_id)
    if cmd is None:
        return None
    return shlex.join(cmd.argv)


def _resolve_test_inputs(
    test_command: str,
    *,
    subject_path: Path,
    project_path: Path,
    declared_test_paths: list[str] | None = None,
) -> tuple[list[Path], list[str]]:
    """Resolve declared test inputs, with command inference for legacy specs.

    Current contracts provide project-root-relative ``test_paths``. Older
    contracts are supported by inferring filesystem operands from the command.
    An absolute path under the project root is rebound to the subject checkout.
    """
    subject = subject_path.resolve()
    project = project_path.resolve()
    resolved: dict[str, Path] = {}

    try:
        tokens = shlex.split(test_command)
    except ValueError as exc:
        raise EvidenceError(f"Unable to parse declared test command: {exc}") from exc

    if declared_test_paths is None:
        candidates = [
            raw_token
            for raw_token in tokens
            if raw_token
            and not raw_token.startswith("-")
            and raw_token not in {"|", "||", "&&", ";"}
        ]
        strict = False
    else:
        candidates = declared_test_paths
        strict = True

    ignored_parts = {
        ".git", ".aah", ".venv", "venv", "node_modules",
        "__pycache__", ".pytest_cache",
    }
    for raw_token in candidates:
        token = raw_token.strip().split("::", 1)[0]
        if not token:
            continue
        candidate = Path(token)
        try:
            if candidate.is_absolute():
                resolved_candidate = candidate.resolve()
                try:
                    rel_to_project = resolved_candidate.relative_to(project)
                except ValueError:
                    path = resolved_candidate
                else:
                    rebound = subject / rel_to_project
                    path = rebound.resolve() if rebound.exists() else resolved_candidate
            else:
                path = (subject / candidate).resolve()
        except (OSError, RuntimeError) as exc:
            if strict:
                raise EvidenceError(
                    f"Unable to resolve declared test path {raw_token}: {exc}"
                ) from exc
            continue

        try:
            path.relative_to(subject)
        except ValueError:
            if strict:
                raise EvidenceError(
                    f"Declared test path escapes the subject checkout: {raw_token}"
                )
            continue
        if strict and not path.exists():
            raise EvidenceError(f"Declared test path does not exist: {raw_token}")
        if path.is_file():
            rel = path.relative_to(subject).as_posix()
            resolved[rel] = path
        elif path.is_dir():
            visited_dirs: set[Path] = set()
            for root, dirs, files in os.walk(path, followlinks=strict):
                try:
                    resolved_root = Path(root).resolve()
                    resolved_root.relative_to(subject)
                except (OSError, RuntimeError, ValueError) as exc:
                    if strict:
                        raise EvidenceError(
                            f"Declared test directory escapes or cannot be resolved: {root}"
                        ) from exc
                    dirs[:] = []
                    continue
                if resolved_root in visited_dirs:
                    dirs[:] = []
                    continue
                visited_dirs.add(resolved_root)

                retained_dirs: list[str] = []
                for name in sorted(dirs):
                    if name in ignored_parts:
                        continue
                    child_dir = Path(root) / name
                    if child_dir.is_symlink() and not strict:
                        continue
                    try:
                        resolved_dir = child_dir.resolve()
                        dir_rel = resolved_dir.relative_to(subject)
                    except (OSError, RuntimeError, ValueError) as exc:
                        if strict:
                            raise EvidenceError(
                                "Declared test directory contains an escaping or "
                                f"unresolvable symlink: {child_dir}"
                            ) from exc
                        continue
                    if resolved_dir in visited_dirs or any(
                        part in ignored_parts for part in dir_rel.parts
                    ):
                        continue
                    retained_dirs.append(name)
                dirs[:] = retained_dirs

                for name in sorted(files):
                    child = Path(root) / name
                    try:
                        resolved_child = child.resolve()
                        rel = resolved_child.relative_to(subject).as_posix()
                    except (OSError, RuntimeError, ValueError) as exc:
                        # Virtualenv interpreter links and other external
                        # symlinks are execution machinery, not test inputs.
                        if strict:
                            raise EvidenceError(
                                "Declared test directory contains an escaping or "
                                f"unresolvable file: {child}"
                            ) from exc
                        continue
                    if any(part in ignored_parts for part in Path(rel).parts):
                        continue
                    # A directory operand in a legacy shell command can sweep
                    # in outputs of an earlier run; those are not inputs.
                    if _is_ephemeral(rel, DEFAULT_EPHEMERAL_GLOBS):
                        continue
                    if is_coverage_artifact_name(child.name):
                        continue
                    resolved[rel] = resolved_child

    if strict and not resolved:
        raise EvidenceError(
            "test_config.test_paths contains no readable files in the subject checkout"
        )
    rel_paths = sorted(resolved)
    return [resolved[rel] for rel in rel_paths], rel_paths


def _declared_test_paths(feature_yaml: dict) -> list[str] | None:
    """Return current-contract test paths, or None for a legacy contract."""
    test_config = feature_yaml.get("test_config")
    if not isinstance(test_config, dict) or "test_paths" not in test_config:
        return None
    test_paths = test_config.get("test_paths")
    if (
        not isinstance(test_paths, list)
        or not test_paths
        or any(not isinstance(path, str) or not path.strip() for path in test_paths)
    ):
        raise EvidenceError(
            "test_config.test_paths must be a non-empty list of path strings"
        )
    return [path.strip() for path in test_paths]


def _rebind_project_operands(test_command: str, project_path: Path, subject_path: Path) -> str:
    """Rebind absolute project-root operands to the execution checkout.

    Feature contracts are central evidence inputs and may contain an absolute
    test path generated while planning in the root checkout.  Leaving that
    operand untouched would defeat a subject ``cwd`` by executing the root's
    file, so the exact root prefix is rebound before execution.
    """
    project = str(project_path.resolve())
    subject = str(subject_path.resolve())
    if project == subject or subject in test_command:
        return test_command
    return test_command.replace(project + os.sep, subject + os.sep)


def _safe_command_argv(command: str | None) -> list[str]:
    """Best-effort argv for forensic evidence; malformed syntax stays writable."""
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _redirect_coverage_reports(test_command: str, namespace: Path) -> str:
    """Redirect every file-producing pytest-cov report outside the subject.

    Unknown report formats fail closed because their filesystem behavior is not
    known. Terminal-only reports are retained unchanged.
    """
    # The overwhelmingly common path must be a byte-for-byte no-op.  Parsing
    # and rejoining valid shell changes quoting, redirections, env assignments,
    # and control operators even when there is nothing to redirect.
    if "--cov-report" not in test_command:
        return test_command

    try:
        shlex.split(test_command)
    except ValueError as exc:
        raise EvidenceError(f"Unable to parse declared test command: {exc}") from exc

    coverage_dir = namespace / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)

    # Match only a standalone shell word, retaining all unrelated source text.
    # Quoted values are supported; shell metacharacters terminate unquoted
    # values.  If any occurrence cannot be classified, fail closed below.
    option_re = re.compile(
        r"(?<!\S)--cov-report(?:="
        r"(?P<eq>\"[^\"]*\"|'[^']*'|[^\s;&|<>]*)"
        r"|[ \t]+(?P<sep>\"[^\"]*\"|'[^']*'|[^\s;&|<>]+))"
    )
    replacements = 0

    def replace(match: re.Match) -> str:
        nonlocal replacements
        raw_value = match.group("eq") if match.group("eq") is not None else match.group("sep")
        value = raw_value
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        replacements += 1
        prefix = "--cov-report="
        if value == "":
            return prefix
        report_type = value.split(":", 1)[0]
        if report_type in _TERMINAL_COVERAGE_REPORTS:
            return match.group(0)
        default_name = _FILE_COVERAGE_REPORTS.get(report_type)
        if default_name is None:
            raise EvidenceError(f"Unsupported coverage report format: {report_type}")
        return f"{prefix}{report_type}:{coverage_dir / default_name}"

    rewritten = option_re.sub(replace, test_command)
    if replacements != test_command.count("--cov-report"):
        raise EvidenceError(
            "Coverage report syntax cannot be safely redirected without changing shell semantics"
        )
    return rewritten


def _playwright_browsers_path() -> str | None:
    """Where Playwright's browsers really live, resolved from the REAL environment.

    ``_redirected_test_environment`` points XDG_CACHE_HOME at an empty per-run
    directory so a test run cannot litter the subject. Playwright stores its
    downloaded browser *binaries* under that same root, so the redirect sent it to
    an empty directory and every browser test failed at launch ("Executable
    doesn't exist at .../cache/ms-playwright/...") regardless of the code under
    test. Browsers are toolchain — read like the interpreter on PATH, HOME, and
    VIRTUAL_ENV, all already passed through — not output, so pin them.

    Platform-specific because Playwright is: XDG_CACHE_HOME on Linux,
    ~/Library/Caches on macOS, LOCALAPPDATA on Windows (which is not in
    ``_SAFE_TEST_ENV_KEYS`` either, so Windows had no resolvable base at all).

    An already-set value is returned verbatim: it respects a project's own
    choice, keeps Playwright's special ``PLAYWRIGHT_BROWSERS_PATH=0`` ("browsers
    live next to the package") from being turned into a path, and makes the pin
    survive nesting. Returns None when the platform base is unavailable.
    """
    declared = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if declared:
        return declared
    if sys.platform == "darwin":
        base = Path(os.path.expanduser("~")) / "Library" / "Caches"
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        base = Path(local)
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path(os.path.expanduser("~")) / ".cache")
    return str(base / "ms-playwright")


def _secret_patterns(required_env: dict[str, str]) -> list[re.Pattern[str]]:
    """Build exact-value redactors without ever persisting credential values."""
    return list(exact_value_patterns(required_env.values()))


def _sanitize_test_output(text: str, required_env: dict[str, str]) -> str:
    return sanitize_output(text, extra_patterns=_secret_patterns(required_env))


def _redirected_test_environment(
    namespace: Path,
    required_env: dict[str, str] | None = None,
    project_path: Path | None = None,
) -> dict[str, str]:
    """Return a subprocess environment whose common outputs miss the subject.

    Only the feature's validated ``required_env`` values are added. Unrelated
    root-dotenv secrets never enter the subprocess environment.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_TEST_ENV_KEYS or key.startswith("LC_")
    }
    env.update(required_env or {})
    browsers = _playwright_browsers_path()
    cache = namespace / "cache"
    temp = namespace / "tmp"
    logs = namespace / "logs"
    for path in (cache, temp, logs, namespace / "coverage"):
        path.mkdir(parents=True, exist_ok=True)
    env.update({
        "TMPDIR": str(temp),
        "TEMP": str(temp),
        "TMP": str(temp),
        "XDG_CACHE_HOME": str(cache),
        "UV_CACHE_DIR": str(cache / "uv"),
        **({"UV_PROJECT": str(project_path)} if project_path else {}),
        "PYTHONPYCACHEPREFIX": str(cache / "pycache"),
        "MYPY_CACHE_DIR": str(cache / "mypy"),
        "RUFF_CACHE_DIR": str(cache / "ruff"),
        "COVERAGE_FILE": str(namespace / "coverage" / ".coverage"),
        "AAH_TEST_LOG_DIR": str(logs),
        # Deliberately escapes the cache redirect above — browser binaries are
        # toolchain, not output.
        **({"PLAYWRIGHT_BROWSERS_PATH": browsers} if browsers else {}),
    })
    # pytest parses PYTEST_ADDOPTS with POSIX shlex.split, which consumes
    # backslashes as escapes. On Windows str(path) would arrive as
    # "C:UsersnameAppData...junit.xml" — a drive-relative path that resolves
    # against the subject checkout, so outputs land in the tree under test
    # (dirtying the subject) and the JUnit parse finds nothing. POSIX
    # separators survive shlex.split and Windows accepts them.
    junit_xml = namespace / "junit.xml"
    pytest_opts = (
        f"-o cache_dir={(cache / 'pytest').as_posix()} "
        f"--basetemp={(temp / 'pytest').as_posix()} "
        f"--junitxml={junit_xml.as_posix()}"
    )
    existing = env.get("PYTEST_ADDOPTS", "").strip()
    env["PYTEST_ADDOPTS"] = f"{existing} {pytest_opts}".strip()
    return env


def discover_test_reports(
    injected_path: Path,
    subject_path: Path,
) -> list[Path]:
    """Return every JUnit XML file this run produced, newest-first.

    Two sources, in priority order:

      1. ``injected_path`` — the report the harness asked for by name. Only
         pytest honours this (via PYTEST_ADDOPTS), which is why it alone is
         not enough.
      2. The language adapter's ``test_report_globs()``, resolved against the
         subject checkout — where Maven Surefire, Gradle, and a
         JUnit-configured Playwright/vitest/gotestsum/nextest write on their
         own.

    An empty list means "no readable report", which the caller must treat as
    ``no_signal`` — never as a zero-test pass. That conflation is what let a
    Maven feature with passing tests record ``summary.total: 0`` and read as
    green (issue #191), and what hid failing Playwright suites behind the same
    empty summary.
    """
    found: list[Path] = []
    if injected_path.is_file():
        found.append(injected_path)

    try:
        from aah.core.build.lang_checks import detect

        # Detect against the subject, not the central project root: the tree
        # whose tests just ran is the one holding the language fingerprints
        # (package.json, pom.xml). ``detect`` never returns None.
        globs = detect(subject_path).test_report_globs()
    except Exception:
        # Adapter resolution must never break evidence collection; failing to
        # detect simply means no conventional locations to search.
        globs = []

    for pattern in globs:
        try:
            for match in subject_path.glob(pattern):
                if match.is_file() and match not in found:
                    found.append(match)
        except (OSError, ValueError):
            # A malformed pattern or unreadable directory skips that pattern
            # only — the rest of the search still runs.
            continue

    return found


def _junit_artifacts(junit_paths: Path | list[Path]) -> dict:
    """Describe the JUnit report(s) backing this result for the attestation.

    Hashes every file so the signed evidence binds to the complete report set,
    not just the first file of a multi-file (Surefire/Gradle) run.
    """
    paths = [junit_paths] if isinstance(junit_paths, Path) else list(junit_paths)
    files = [p for p in paths if p.is_file()]
    if not files:
        return {"junit_xml": {"present": False}}
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    return {
        "junit_xml": {
            "present": True,
            "name": files[0].name if len(files) == 1 else f"{len(files)} report files",
            "count": len(files),
            "sha256": digest.hexdigest(),
        }
    }


def _attach_v2(
    result: dict,
    *,
    feature_id: str,
    subject: dict,
    contract_hash: str,
    test_input_hash: str,
    test_paths: list[str],
    actor: str,
    attempt_id: str,
    command: str | None,
    started_at: str,
    status: str,
    artifacts: dict,
) -> dict:
    result.update(build_evidence_v2_record(
        feature_id=feature_id,
        producer="run_feature_tests",
        subject=subject,
        contract_hash=contract_hash,
        test_input_hash=test_input_hash,
        test_paths=test_paths,
        execution={
            "actor": actor,
            "attempt_id": attempt_id,
            "argv": _safe_command_argv(command),
            "cwd": subject.get("rel_path"),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": result.get("exit_code", 0 if result.get("passed") else 1),
        },
        status=status,
        artifacts=artifacts,
    ))
    return result


def _early_failure(
    *,
    feature_id: str,
    error: str,
    started: float,
    started_at: str,
    evidence_v2: bool,
    project_path: Path,
    subject_path: Path,
    subject_branch: str | None,
    subject_sha: str | None,
    actor: str,
    attempt_id: str,
    feature_path: Path | None = None,
    test_command: str | None = None,
) -> dict:
    """Build an early failure, always enveloped when evidence v2 is enabled."""
    failed = _err_result(feature_id, error, started, test_command=test_command)
    if not evidence_v2:
        return failed

    subject = {
        "schema_version": 2,
        "rel_path": None,
        "branch": subject_branch,
        "commit_sha": subject_sha,
        "clean": False,
    }
    contract_hash = ""
    test_input_hash = hashlib.sha256().hexdigest()
    evidence_errors: list[str] = []
    try:
        subject = capture_subject(subject_path, project_path)
    except EvidenceError as exc:
        evidence_errors.append(str(exc))
    if feature_path is not None:
        try:
            contract_hash = hash_feature_contract(feature_path)
        except EvidenceError as exc:
            evidence_errors.append(str(exc))
    if evidence_errors:
        failed["evidence_errors"] = evidence_errors
    return _attach_v2(
        failed,
        feature_id=feature_id,
        subject=subject,
        contract_hash=contract_hash,
        test_input_hash=test_input_hash,
        test_paths=[],
        actor=actor,
        attempt_id=attempt_id,
        command=test_command,
        started_at=started_at,
        status="no_signal",
        artifacts={"junit_xml": {"present": False}},
    )


def _detect_stack(project_path: Path) -> str | None:
    """Best-effort adapter/stack name for the reproduction manifest.

    Reads manifest.stack_choices.primary if present; falls back to the
    lang_checks adapter name. Never raises.
    """
    try:
        from aah.core.common.io_utils import read_yaml

        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        if isinstance(manifest, dict):
            stack = manifest.get("stack_choices")
            if isinstance(stack, dict) and stack.get("primary"):
                return str(stack["primary"])
    except Exception:
        pass
    try:
        from aah.core.build.lang_checks import detect

        adapter = detect(project_path)
        return type(adapter).__name__
    except Exception:
        return None


def _diagnostic_rerun(
    test_command: str,
    *,
    subject_path: Path,
    project_path: Path,
    required_env: dict[str, str],
    feature_id: str,
    actor: str,
    attempt_id: str,
    wave: str | int | None,
    first_exit: int,
) -> dict:
    """Re-run a FAILED test ONCE in a FRESH namespace to classify flakiness.

    This NEVER overwrites the original gate status — the caller keeps the
    original ``passed``/``status``. It only reports a classification:
      * ``reproducible_failure``    — failed again (deterministic failure)
      * ``nondeterministic_failure`` — passed on the rerun (flaky/no-retry-to-green)

    Bounded to EXACTLY one extra subprocess in one fresh TemporaryDirectory,
    preserving the 300s timeout.
    """
    rerun_run_id = make_run_id(
        project=project_path.name,
        wave=wave,
        feature=feature_id,
        actor=actor,
        attempt=f"{attempt_id}-diag",
    )
    diagnostic = {
        "classification": "reproducible_failure",
        "first_exit": first_exit,
        "rerun_exit": None,
        "rerun_run_id": rerun_run_id,
    }
    with tempfile.TemporaryDirectory(prefix=f"{rerun_run_id}-") as rerun_dir:
        rerun = run_bounded_command(
            CommandSpec(
                test_command,
                shell=True,
                cwd=subject_path,
                env=_redirected_test_environment(Path(rerun_dir), required_env, project_path=project_path),
                timeout_sec=300,
            )
        )
    if rerun.stdout_truncated or rerun.stderr_truncated:
        diagnostic["rerun_exit"] = 126
        diagnostic["rerun_error"] = "diagnostic output truncated"
        diagnostic["output_truncated"] = True
    elif rerun.state == "timeout":
        diagnostic["rerun_exit"] = 124
        diagnostic["classification"] = "reproducible_failure"
    elif rerun.state != "executed":
        diagnostic["rerun_error"] = _sanitize_test_output(
            rerun.error or rerun.state,
            required_env,
        )
        diagnostic["classification"] = "reproducible_failure"
    else:
        diagnostic["rerun_exit"] = rerun.returncode
        # Store SANITIZED rerun output — every write path is sanitized.
        diagnostic["rerun_stdout"] = _sanitize_test_output(rerun.stdout, required_env)[-2000:]
        diagnostic["rerun_stderr"] = _sanitize_test_output(rerun.stderr, required_env)[-1000:]
        if rerun.returncode == 0:
            diagnostic["classification"] = "nondeterministic_failure"
    return diagnostic


def run_feature_tests(
    feature_id: str,
    project_path: Path,
    *,
    subject_path: Path | None = None,
    subject_branch: str | None = None,
    subject_sha: str | None = None,
    actor: str = "implementer",
    attempt_id: str = "attempt-001",
    wave: str | int | None = None,
    diagnostic_rerun: bool = False,
    provisional: bool = False,
) -> dict:
    """
    Run tests for a specific feature and return structured results.

    Ensures required infrastructure (DB, cache, etc.) is running before
    executing tests. If services are down, attempts to start them.

    Uses --junitxml for structured per-test-case output when pytest is detected.

    Every official run gets a collision-resistant namespace, a required-service
    setup failure fails
    closed (no_signal + block), failures write a sanitized reproduction bundle,
    and an optional single diagnostic rerun classifies flakiness WITHOUT
    converting a failure to a pass. Provisional runs accept dirty subjects,
    skip evidence production, and retain the same credential scoping and
    output-redaction boundary.
    """
    from aah.core.build.ensure_infra import cleanup_namespace, ensure_test_environment

    # Track elapsed time for attestation regardless of which return path fires.
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    project_path = Path(project_path).resolve()
    subject_path = Path(subject_path or project_path).resolve()
    # Every official run is subject-bound. Provisional runs are deliberately
    # non-authoritative and never write gate-eligible evidence.
    evidence_v2 = not provisional

    isolated = not provisional
    # Collision-resistant run namespace (isolated mode only). Derived from
    # project/wave/feature/actor/attempt + random suffix.
    run_id = None
    if isolated:
        run_id = make_run_id(
            project=project_path.name,
            wave=wave,
            feature=feature_id,
            actor=actor,
            attempt=attempt_id,
        )

    # Load feature .md file (frontmatter format)
    from aah.core.common.feature_utils import (
        find_feature_file,
        parse_feature_frontmatter,
    )

    # The command and its contract hash must come from the checkout whose code
    # is being tested. Artifacts and attestation secrets remain project-scoped.
    features_dir = subject_path / ".aah" / "plan" / "features"
    feature_path = find_feature_file(features_dir, feature_id)
    if not feature_path:
        return _early_failure(
            feature_id=feature_id,
            error=f"Feature file not found for {feature_id} in {features_dir}",
            started=started,
            started_at=started_at,
            evidence_v2=evidence_v2,
            project_path=project_path,
            subject_path=subject_path,
            subject_branch=subject_branch,
            subject_sha=subject_sha,
            actor=actor,
            attempt_id=attempt_id,
        )

    feature_yaml = parse_feature_frontmatter(feature_path)
    if not feature_yaml:
        return _early_failure(
            feature_id=feature_id,
            error=f"Failed to parse feature file: {feature_path}",
            started=started,
            started_at=started_at,
            evidence_v2=evidence_v2,
            project_path=project_path,
            subject_path=subject_path,
            subject_branch=subject_branch,
            subject_sha=subject_sha,
            actor=actor,
            attempt_id=attempt_id,
            feature_path=feature_path,
        )

    # Credential declarations remain additive across the original plan and
    # subject contract; a subject may add a key but cannot remove a root key.
    required_env_yaml = feature_yaml
    root_feature_path = find_feature_file(
        project_path / ".aah" / "plan" / "features", feature_id
    )
    if root_feature_path and root_feature_path != feature_path:
        required_env_yaml = (
            parse_feature_frontmatter(root_feature_path) or feature_yaml
        )

    if run_id:
        namespace_prefix = f"{run_id}-"
    else:
        namespace_prefix = "-".join((
            "aah",
            _safe_namespace_token(feature_id, fallback="feature"),
            _safe_namespace_token(actor, fallback="actor"),
            _safe_namespace_token(attempt_id, fallback="attempt"),
            "",
        ))
    with tempfile.TemporaryDirectory(prefix=namespace_prefix) as temp_dir:
        namespace = Path(temp_dir)
        junit_path = namespace / "junit.xml"
        test_command = find_test_command(feature_yaml, subject_path)

        if test_command is None:
            return _early_failure(
                feature_id=feature_id,
                error="No test command configured. Set test_config.command in feature YAML.",
                started=started,
                started_at=started_at,
                evidence_v2=evidence_v2,
                project_path=project_path,
                subject_path=subject_path,
                subject_branch=subject_branch,
                subject_sha=subject_sha,
                actor=actor,
                attempt_id=attempt_id,
                feature_path=feature_path,
            )
        test_command = _rebind_project_operands(test_command, project_path, subject_path)
        try:
            test_command = _redirect_coverage_reports(test_command, namespace)
        except EvidenceError as exc:
            return _early_failure(
                feature_id=feature_id,
                error=str(exc),
                started=started,
                started_at=started_at,
                evidence_v2=evidence_v2,
                project_path=project_path,
                subject_path=subject_path,
                subject_branch=subject_branch,
                subject_sha=subject_sha,
                actor=actor,
                attempt_id=attempt_id,
                feature_path=feature_path,
                test_command=test_command,
            )

        captured_subject: dict = {
            "schema_version": 2,
            "rel_path": None,
            "branch": subject_branch,
            "commit_sha": subject_sha,
            "clean": False,
        }
        contract_hash = ""
        test_input_hash = ""
        test_paths: list[str] = []

        if evidence_v2:
            try:
                captured_subject = capture_subject(subject_path, project_path)
                contract_hash = hash_feature_contract(feature_path)
                input_files, test_paths = _resolve_test_inputs(
                    test_command,
                    subject_path=subject_path,
                    project_path=project_path,
                    declared_test_paths=_declared_test_paths(feature_yaml),
                )
                test_input_hash = hash_test_inputs(input_files, subject_path)
            except EvidenceError as exc:
                failed = _err_result(feature_id, str(exc), started, test_command=test_command)
                return _attach_v2(
                    failed,
                    feature_id=feature_id,
                    subject=captured_subject,
                    contract_hash=contract_hash,
                    test_input_hash=test_input_hash,
                    test_paths=test_paths,
                    actor=actor,
                    attempt_id=attempt_id,
                    command=test_command,
                    started_at=started_at,
                    status="no_signal",
                    artifacts={"junit_xml": {"present": False}},
                )

            binding_view = {
                "schema_version": 2,
                "feature_id": feature_id,
                "subject": captured_subject,
            }
            if not evidence_matches_binding(
                binding_view,
                feature_id=feature_id,
                expected_branch=subject_branch or captured_subject.get("branch") or "",
                expected_sha=subject_sha or captured_subject.get("commit_sha") or "",
            ):
                failed = _err_result(
                    feature_id,
                    "Subject is dirty or does not match the expected branch and SHA",
                    started,
                    test_command=test_command,
                )
                return _attach_v2(
                    failed,
                    feature_id=feature_id,
                    subject=captured_subject,
                    contract_hash=contract_hash,
                    test_input_hash=test_input_hash,
                    test_paths=test_paths,
                    actor=actor,
                    attempt_id=attempt_id,
                    command=test_command,
                    started_at=started_at,
                    status="no_signal",
                    artifacts={"junit_xml": {"present": False}},
                )

        # ---- official-run isolation helpers (no-ops for provisional runs) ----
        cleanup_state: dict = {"done": False, "result": None}
        infra_status: dict = {"value": None}
        docker_cleanup_needed: dict = {"value": False}
        resolved_required_env: dict[str, str] = {}

        def _run_cleanup() -> dict | None:
            if not isolated or not run_id or cleanup_state["done"]:
                return cleanup_state["result"]
            cleanup_state["done"] = True
            if not docker_cleanup_needed["value"]:
                cleanup_state["result"] = {
                    "transport": "none",
                    "action": "cleanup_namespace",
                    "namespace": run_id,
                    "removed": [],
                    "leftover": [],
                    "errors": [],
                    "stopped": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return cleanup_state["result"]
            try:
                cleanup_state["result"] = cleanup_namespace(run_id, project_path)
            except Exception as exc:  # cleanup must never mask the real result
                cleanup_state["result"] = {
                    "transport": "docker",
                    "action": "cleanup_namespace",
                    "namespace": run_id,
                    "removed": [],
                    "leftover": [],
                    "errors": [f"cleanup raised: {exc}"],
                    "stopped": False,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            return cleanup_state["result"]

        def _finish(result: dict, *, status: str, logs: dict | None = None) -> dict:
            """Official-run envelope: diagnostic rerun + cleanup + bundle.

            Provisional runs return ``result`` untouched. This never converts
            a failure to a pass: the original gate ``passed``/``status`` are
            preserved; the diagnostic only classifies flakiness.
            """
            if isolated:
                result["run_id"] = run_id
                # Surface the infra setup signal on every isolated-mode result so
                # consumers can branch on ready / not_applicable / no_signal.
                if isinstance(infra_status.get("value"), str):
                    result.setdefault("setup_status", infra_status["value"])
                # Diagnostic rerun ONLY for a genuine test failure (never for a
                # setup/no_signal problem). Bound to exactly one extra subprocess.
                if diagnostic_rerun and status == "fail":
                    result["diagnostic"] = _diagnostic_rerun(
                        test_command,
                        subject_path=subject_path,
                        project_path=project_path,
                        required_env=resolved_required_env,
                        feature_id=feature_id,
                        actor=actor,
                        attempt_id=attempt_id,
                        wave=wave,
                        first_exit=result.get("exit_code", 1),
                    )
                cleanup_result = _run_cleanup()
                if cleanup_result is not None:
                    result["cleanup"] = cleanup_result
                # Sanitized reproduction bundle for any non-passing outcome.
                if status in ("fail", "no_signal") or result.get("passed") is not True:
                    _attach_failure_bundle(
                        result,
                        logs=logs or {},
                        cleanup=cleanup_result,
                        diagnostic=result.get("diagnostic"),
                    )
            return result

        def _attach_failure_bundle(result: dict, *, logs: dict, cleanup, diagnostic) -> None:
            try:
                retention = read_evidence_retention(project_path)
                out_dir = project_path / ".aah" / "build" / "failure-bundles"
                execution = {
                    "argv": _safe_command_argv(test_command),
                    "cwd": captured_subject.get("rel_path"),
                    "adapter": _detect_stack(project_path),
                    "seed": os.environ.get("PYTHONHASHSEED"),
                    "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "exit_code": result.get("exit_code"),
                    "project_root": str(project_path),
                }
                bundle_dir = write_failure_bundle(
                    run_id=run_id or "run",
                    subject=captured_subject,
                    execution=execution,
                    cleanup=cleanup,
                    diagnostic=diagnostic,
                    logs=logs,
                    retention=retention,
                    out_dir=out_dir,
                )
                artifacts = result.setdefault("artifacts", {})
                if not isinstance(artifacts, dict):
                    artifacts = {}
                    result["artifacts"] = artifacts
                repro = bundle_dir / "reproduction.json"
                artifacts["failure_bundle"] = {
                    "dir": str(bundle_dir),
                    "manifest": repro.name,
                    "manifest_sha256": hashlib.sha256(repro.read_bytes()).hexdigest()
                    if repro.is_file() else None,
                }
            except EvidenceError as exc:
                # Fail closed on bundle write (e.g. scope escape) without
                # crashing the runner — record the refusal.
                result.setdefault("evidence_errors", []).append(str(exc))

        # Preflight: required external creds (required_env) must be present
        # before we spend a test run. Missing keys → no_signal + block with a
        # human-actionable reason (fill root .env from .env.example), NOT a
        # real service-connection failure. Opt-in per feature; no-op otherwise.
        # In provisional mode (implementer iterating), missing env is non-blocking
        # — the implementer uses mock data; real credentials are enforced by the
        # official pre-QA feature-test run.
        try:
            resolved_required_env, missing_env = _resolve_required_env(
                required_env_yaml,
                feature_id,
                project_path,
                subject_path,
            )
        except EvidenceError as exc:
            if provisional:
                resolved_required_env, missing_env = {}, []
            else:
                failed = _err_result(
                    feature_id,
                    str(exc),
                    started,
                    test_command=test_command,
                )
                failed["setup_status"] = "no_signal"
                failed["block"] = True
                failed["env_validation_errors"] = [str(exc)]
                if evidence_v2:
                    _attach_v2(
                        failed,
                        feature_id=feature_id,
                        subject=captured_subject,
                        contract_hash=contract_hash,
                        test_input_hash=test_input_hash,
                        test_paths=test_paths,
                        actor=actor,
                        attempt_id=attempt_id,
                        command=test_command,
                        started_at=started_at,
                        status="no_signal",
                        artifacts={"junit_xml": {"present": False}},
                    )
                return _finish(
                    failed,
                    status="no_signal",
                    logs={"stderr": failed.get("error", "")},
                )
        if missing_env and not provisional:
            failed = _err_result(
                feature_id,
                "Missing required environment keys "
                f"{missing_env}: add values to the project root .env "
                "(see .env.example), then re-run.",
                started,
                test_command=test_command,
            )
            failed["setup_status"] = "no_signal"
            failed["block"] = True
            failed["missing_required_env"] = missing_env
            if evidence_v2:
                _attach_v2(
                    failed,
                    feature_id=feature_id,
                    subject=captured_subject,
                    contract_hash=contract_hash,
                    test_input_hash=test_input_hash,
                    test_paths=test_paths,
                    actor=actor,
                    attempt_id=attempt_id,
                    command=test_command,
                    started_at=started_at,
                    status="no_signal",
                    artifacts={"junit_xml": {"present": False}},
                )
            return _finish(
                failed,
                status="no_signal",
                logs={"stderr": failed.get("error", "")},
            )
        elif missing_env and provisional:
            print(
                f"[provisional] Missing env keys {missing_env} — "
                "proceeding with mock/dummy data. Real credentials "
                "will be required for the official pre-QA feature-test run.",
                file=sys.stderr,
            )

        # Full test environment setup belongs to the execution checkout.
        if isolated:
            infra = ensure_test_environment(subject_path, run_id=run_id)
        else:
            infra = ensure_test_environment(subject_path)
        infra_status["value"] = infra.get("setup_status")
        docker_cleanup_needed["value"] = bool(infra.get("docker_available"))
        infra_warning = None

        # Isolated mode: a REQUIRED-service setup failure fails closed —
        # do NOT run tests, emit no_signal + block. (not_applicable proceeds.)
        if isolated and infra.get("setup_status") == "no_signal":
            failed = _err_result(
                feature_id,
                f"Required test infrastructure unavailable: {infra.get('required_failures')}",
                started,
                test_command=test_command,
            )
            failed["setup_status"] = "no_signal"
            failed["block"] = True
            failed["required_failures"] = infra.get("required_failures", [])
            if evidence_v2:
                _attach_v2(
                    failed,
                    feature_id=feature_id,
                    subject=captured_subject,
                    contract_hash=contract_hash,
                    test_input_hash=test_input_hash,
                    test_paths=test_paths,
                    actor=actor,
                    attempt_id=attempt_id,
                    command=test_command,
                    started_at=started_at,
                    status="no_signal",
                    artifacts={"junit_xml": {"present": False}},
                )
            return _finish(
                failed,
                status="no_signal",
                logs={"stderr": failed.get("error", "")},
            )

        if not infra["ready"]:
            infra_warning = f"Test environment not ready: {infra['message']}. Tests will proceed anyway."

        # Execute tests in the subject checkout with output redirected away
        # from the source/test tree.
        coverage_before = set()
        if evidence_v2:
            try:
                coverage_before = capture_coverage_artifacts(subject_path, project_path)
            except EvidenceError:
                pass

        result = run_bounded_command(
            CommandSpec(
                test_command,
                shell=True,
                cwd=subject_path,
                env=_redirected_test_environment(namespace, resolved_required_env, project_path=project_path),
                timeout_sec=300,
            )
        )
        if result.state == "executed" and not (
            result.stdout_truncated or result.stderr_truncated
        ):
            duration_ms = int((time.monotonic() - started) * 1000)
            passed = result.returncode == 0
            report_files = discover_test_reports(junit_path, subject_path)
            junit_data = _parse_junit_xml(
                report_files,
                extra_patterns=tuple(_secret_patterns(resolved_required_env)),
            )
            safe_stdout = _sanitize_test_output(result.stdout, resolved_required_env)
            safe_stderr = _sanitize_test_output(result.stderr, resolved_required_env)

            test_result = {
                "feature_id": feature_id,
                "passed": passed,
                "exit_code": result.returncode,
                "command": test_command,
                "summary": junit_data["summary"],
                "test_cases": junit_data["test_cases"],
                "failures": junit_data["failures"],
                "stdout": safe_stdout[-2000:] if len(safe_stdout) > 2000 else safe_stdout,
                "stderr": safe_stderr[-1000:] if len(safe_stderr) > 1000 else safe_stderr,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                # Private metadata for the attestation block; popped in main().
                "_attestation_meta": {
                    "command": shlex.split(test_command),
                    "exit_code": result.returncode,
                    "stdout": safe_stdout,
                    "stderr": safe_stderr,
                    "duration_ms": duration_ms,
                },
            }

            if junit_data["summary"]["total"] == 0 and not passed:
                test_result["failures"] = _parse_failures(safe_stdout, safe_stderr)
            if infra_warning:
                test_result["infra_warning"] = infra_warning

            # A zero-exit run whose report cannot be read proves nothing: the
            # summary would be an all-zero record indistinguishable from "the
            # suite is empty and everything is fine". Demote to no_signal so a
            # missing report can never be consumed as a pass. A non-zero exit
            # keeps its `fail` verdict — the run really did fail, and an
            # unreadable report cannot turn that into a pass or a no-signal.
            unreadable_report = passed and not report_files
            if unreadable_report:
                passed = False
                test_result["passed"] = False
                test_result["signal_reason"] = "test_report_unreadable"
                test_result["error"] = (
                    "Tests exited 0 but wrote no readable JUnit report, so the "
                    "result cannot be verified. Configure the test runner to "
                    "emit JUnit XML (pytest --junitxml is injected "
                    "automatically; Maven/Gradle write one by default; "
                    "Playwright, vitest, gotestsum and nextest need a JUnit "
                    "reporter named in project config)."
                )

            # Planned-to-collected assertion for an otherwise-passing run with
            # real collected results. Zero-test runner behavior is unchanged.
            if passed and junit_data["summary"]["total"] > 0:
                missing_tcs = _missing_planned_test_cases(feature_yaml, junit_data["test_cases"])
                if missing_tcs:
                    passed = False
                    test_result["passed"] = False
                    test_result["missing_test_cases"] = missing_tcs
                    test_result["error"] = (
                        "Planned test cases have no matching collected test: "
                        + ", ".join(missing_tcs)
                    )

            exec_logs = {"stdout": safe_stdout, "stderr": safe_stderr}
            # An unreadable report is neither pass nor fail — the run produced
            # no verdict to record.
            outcome_status = (
                "no_signal" if unreadable_report else ("pass" if passed else "fail")
            )

            if evidence_v2:
                try:
                    assert_subject_unchanged(captured_subject, subject_path, project_path)
                    assert_no_coverage_artifacts(coverage_before, subject_path, project_path)
                except EvidenceError as exc:
                    test_result["passed"] = False
                    test_result["error"] = str(exc)
                    test_result["subject_unchanged"] = False
                    _attach_v2(
                        test_result,
                        feature_id=feature_id,
                        subject=captured_subject,
                        contract_hash=contract_hash,
                        test_input_hash=test_input_hash,
                        test_paths=test_paths,
                        actor=actor,
                        attempt_id=attempt_id,
                        command=test_command,
                        started_at=started_at,
                        status="no_signal",
                        artifacts=_junit_artifacts(report_files),
                    )
                    return _finish(test_result, status="no_signal", logs=exec_logs)
                test_result["subject_unchanged"] = True
                _attach_v2(
                    test_result,
                    feature_id=feature_id,
                    subject=captured_subject,
                    contract_hash=contract_hash,
                    test_input_hash=test_input_hash,
                    test_paths=test_paths,
                    actor=actor,
                    attempt_id=attempt_id,
                    command=test_command,
                    started_at=started_at,
                    status=outcome_status,
                    artifacts=_junit_artifacts(report_files),
                )
            return _finish(
                test_result,
                status=outcome_status,
                logs=exec_logs,
            )

        elif result.stdout_truncated or result.stderr_truncated:
            failed = _err_result(
                feature_id,
                "Test execution output truncated",
                started,
                test_command=test_command,
                exit_code=126,
            )
            failed["output_truncated"] = True
            timeout_logs = {"stderr": "Test execution output truncated"}
        elif result.state == "timeout":
            failed = _err_result(
                feature_id,
                "Test execution timed out (300s)",
                started,
                test_command=test_command,
                exit_code=124,
            )
            timeout_logs = {
                "stdout": _sanitize_test_output(result.stdout, resolved_required_env),
                "stderr": _sanitize_test_output(
                    result.stderr or "Test execution timed out (300s)",
                    resolved_required_env,
                ),
            }
        else:
            error = result.error or result.state
            failed = _err_result(
                feature_id,
                error,
                started,
                test_command=test_command,
                exit_code=result.returncode,
            )
            timeout_logs = {
                "stderr": _sanitize_test_output(error, resolved_required_env),
            }

        if evidence_v2:
            try:
                assert_subject_unchanged(captured_subject, subject_path, project_path)
                assert_no_coverage_artifacts(coverage_before, subject_path, project_path)
            except EvidenceError as exc:
                failed["error"] = str(exc)
            _attach_v2(
                failed,
                feature_id=feature_id,
                subject=captured_subject,
                contract_hash=contract_hash,
                test_input_hash=test_input_hash,
                test_paths=test_paths,
                actor=actor,
                attempt_id=attempt_id,
                command=test_command,
                started_at=started_at,
                status="no_signal",
                artifacts=_junit_artifacts(
                    discover_test_reports(junit_path, subject_path)
                ),
            )
            return _finish(failed, status="no_signal", logs=timeout_logs)
        return _finish(failed, status="fail", logs=timeout_logs)


def _err_result(
    feature_id: str,
    error: str,
    started: float,
    test_command: str | list[str] | None = None,
    exit_code: int = 1,
    stdout: str = "",
    stderr: str = "",
) -> dict:
    """Build a structured failure dict with the _attestation_meta the writer needs.

    Used by every early-error and exception branch in run_feature_tests
    so main() can call write_attested uniformly.
    """
    if isinstance(test_command, list):
        cmd_list = list(test_command)
    elif isinstance(test_command, str):
        cmd_list = _safe_command_argv(test_command)
    else:
        cmd_list = list(COMMAND_PREFIX)
    return {
        "feature_id": feature_id,
        "passed": False,
        "error": error,
        "command": test_command,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_attestation_meta": {
            "command": cmd_list,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr or error,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    }


def _parse_failures(stdout: str, stderr: str) -> list[dict]:
    """Best-effort parsing of test failure details."""
    failures = []
    combined = stdout + "\n" + stderr

    # Look for common failure patterns
    for line in combined.split("\n"):
        line = line.strip()
        if line.startswith("FAILED") or line.startswith("FAIL:") or "AssertionError" in line:
            failures.append({"name": line[:200], "reason": "See test output for details"})

    return failures[:20]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run feature tests")
    parser.add_argument("--feature-id", type=str, required=True)
    parser.add_argument(
        "--project-path", type=Path, default=None,
        help="Central project/evidence root (artifacts and attestation secret)",
    )
    parser.add_argument(
        "--subject-path", type=Path, default=None,
        help="Exact checkout in which the declared test command executes",
    )
    parser.add_argument("--subject-branch", type=str, default=None)
    parser.add_argument("--subject-sha", type=str, default=None)
    parser.add_argument("--actor", type=str, default="implementer")
    parser.add_argument("--attempt-id", type=str, default="attempt-001")
    parser.add_argument("--wave", type=str, default=None,
                        help="Wave label for the run namespace (isolated mode)")
    parser.add_argument("--diagnostic-rerun", action="store_true",
                        help="On failure, re-run ONCE in a fresh namespace to classify flakiness")
    parser.add_argument(
        "--provisional",
        action="store_true",
        help=(
            "Run against a possibly dirty subject with scoped required_env values; "
            "do not write gate-eligible evidence"
        ),
    )
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    results = run_feature_tests(
        args.feature_id,
        project_path,
        subject_path=args.subject_path,
        subject_branch=args.subject_branch,
        subject_sha=args.subject_sha,
        actor=args.actor,
        attempt_id=args.attempt_id,
        wave=args.wave,
        diagnostic_rerun=args.diagnostic_rerun,
        provisional=args.provisional,
    )

    if args.provisional:
        # Provisional iterations are intentionally non-authoritative.  Drop
        # private attestation metadata and label the result so no consumer can
        # mistake console JSON for official evidence.
        results.pop("_attestation_meta", None)
        results["mode"] = "provisional"
        results["evidence_eligible"] = False
    else:
        write_attested_result(
            results,
            project_path / ".aah" / "build" / "test-results" / f"{args.feature_id}.json",
            project_path=project_path,
            command=COMMAND_PREFIX + sys.argv[1:],
            artifact_name=f"{args.feature_id} test results",
        )

    # NOTE: passes=true is NOT auto-marked here. Feature status is only set
    # via the QA report writer (write_qa_report.py) after QA passes.
    # This enforces: tests pass → QA evaluates → QA passes → feature marked.

    # Output results
    json.dump(results, sys.stdout, indent=2)
    print()

    if results["passed"]:
        if args.provisional:
            print(
                f"Feature {args.feature_id}: PROVISIONAL TESTS PASSED — NO EVIDENCE WRITTEN",
                file=sys.stderr,
            )
        else:
            print(f"Feature {args.feature_id}: ALL TESTS PASSED", file=sys.stderr)
        sys.exit(0)
    else:
        label = "PROVISIONAL TESTS FAILED" if args.provisional else "TESTS FAILED"
        print(f"Feature {args.feature_id}: {label}", file=sys.stderr)
        if results.get("error"):
            print(f"  Error: {results['error']}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
