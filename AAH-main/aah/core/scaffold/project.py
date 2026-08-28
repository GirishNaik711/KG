#!/usr/bin/env python3
"""Scaffold a AAH project in a folder (folder=project model).

The target folder **is** the project. Scaffolding writes ``.aah/`` into it —
no workspace, no ``workspace.yaml``, no active-project pointer. A ``git clone``d
or ``mkdir``'d folder both work; git handling is idempotent so an existing repo
is never re-init'd and its history/branch is never stomped.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.build.regenerate_attestation_secret import ensure_attestation_secret
from aah.core.common.git_utils import (
    add_all,
    checkout_branch,
    commit,
    create_branch,
    init_repo,
)
from aah.core.common.execution import CommandSpec, run_bounded_command
from aah.core.common.io_utils import write_text
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.scaffold.claude_md_template import (
    AAH_BLOCK_BEGIN,
    AAH_BLOCK_END,
    render_project_claude_md,
    wrap_aah_block,
)
from aah.core.scaffold.rapids_dir import create_rapids_dir


EXPERTISE_YAML_TEMPLATE = """\
version: 1
project_id: "{project_id}"
last_updated: "{timestamp}"
last_updated_after_feature: null
tech_patterns: []
integrations: []
project_knowledge: []
consumption_views:
  quick_context: ""
  style_guide: ""
  known_concerns: ""
"""

GITATTRIBUTES_BASE = """# Force LF line endings for all text files
* text=auto eol=lf

# Codemap database — regenerable binary, auto-resolve conflicts by keeping current branch
.aah/codebase-intel/codemap.db merge=ours binary

# AAH runtime state files — derived/rebuilt after every merge, always take ours
.aah/feature-list.json merge=ours
.aah/claude-progress.json merge=ours
.aah/audit/token-usage.json merge=ours
"""

GITIGNORE_BASE = """# AAH agent memory
.aah/agent-memory/

# AAH runtime state (never commit — written by hooks at runtime)
.aah/codebase-intel/pending-changes.json
.aah/codebase-intel/.staleness-warned

# AAH project-local attestation key — durable across sessions, never committed
.aah/build/.attestation-secret
# Legacy pre-build-layout location remains ignored during migration.
.aah/implement/.attestation-secret

# Playwright MCP per-run scratch output (screenshots/snapshots at build checkpoints)
.playwright-mcp/

# Local Claude tooling and worktrees
.claude/

# OS
.DS_Store
Thumbs.db

# Editor
.idea/
.vscode/
*.swp
*.swo
*~
.venv/
venv/
node_modules/
"""

GITIGNORE_PYTHON = """# Python
__pycache__/
*.py[cod]
*.pyo
.venv/
venv/
env/
.env
*.egg-info/
dist/
/build/
.pytest_cache/
.mypy_cache/
.ruff_cache/
htmlcov/
.coverage
"""

GITIGNORE_NODE = """# Node.js
node_modules/
dist/
/build/
.next/
.nuxt/
.output/
.cache/
.parcel-cache/
.turbo/
coverage/
# Playwright output. Rewritten on EVERY run, so a tracked copy leaves the tree
# permanently dirty and run_regression_suite refuses to measure an unattested
# subject (tree_dirty_before_regression) — an unbreakable block.
# Deliberately NOT `test-results/`: unanchored, it would also match
# .aah/build/test-results/ and silently un-track AAH's own attested evidence.
.last-run.json
playwright-report/
blob-report/
playwright/.cache/
.env
.env.local
.env.*.local
npm-debug.log*
yarn-debug.log*
yarn-error.log*
pnpm-debug.log*
"""

GITIGNORE_GO = """# Go
bin/
vendor/
*.exe
*.test
*.out
go.work
"""

GITIGNORE_RUST = """# Rust
target/
Cargo.lock
"""

GITIGNORE_JAVA = """# Java
*.class
*.jar
*.war
*.ear
target/
/build/
.gradle/
out/
"""

# ``.env`` is gitignored by every stack section above; ``.env.example`` is the
# opposite — the checked-in catalog of variable NAMES and safe placeholders, and
# the single source of truth the startup checker and every module's
# ``## Required Env Variables`` section agree on. The explicit negation is
# appended LAST so it also overrides a broader user-authored pattern such as
# ``.env*`` in a brownfield repo's existing .gitignore (in gitignore, a negation
# only wins when it comes after the pattern it un-ignores).
GITIGNORE_ENV_EXAMPLE = """# Env catalog — MUST stay committed (names + placeholders only, never values)
!.env.example
"""

STACK_GITIGNORE_MAP = {
    "python": GITIGNORE_PYTHON,
    "fastapi": GITIGNORE_PYTHON,
    "django": GITIGNORE_PYTHON,
    "flask": GITIGNORE_PYTHON,
    "node": GITIGNORE_NODE,
    "react": GITIGNORE_NODE,
    "next": GITIGNORE_NODE,
    "vue": GITIGNORE_NODE,
    "angular": GITIGNORE_NODE,
    "express": GITIGNORE_NODE,
    "typescript": GITIGNORE_NODE,
    "javascript": GITIGNORE_NODE,
    "go": GITIGNORE_GO,
    "golang": GITIGNORE_GO,
    "rust": GITIGNORE_RUST,
    "java": GITIGNORE_JAVA,
    "spring": GITIGNORE_JAVA,
    "maven": GITIGNORE_JAVA,
    "gradle": GITIGNORE_JAVA,
}

DETECT_FILES = {
    "package.json": GITIGNORE_NODE,
    "package-lock.json": GITIGNORE_NODE,
    "yarn.lock": GITIGNORE_NODE,
    "pnpm-lock.yaml": GITIGNORE_NODE,
    "requirements.txt": GITIGNORE_PYTHON,
    "pyproject.toml": GITIGNORE_PYTHON,
    "setup.py": GITIGNORE_PYTHON,
    "Pipfile": GITIGNORE_PYTHON,
    "go.mod": GITIGNORE_GO,
    "Cargo.toml": GITIGNORE_RUST,
    "pom.xml": GITIGNORE_JAVA,
    "build.gradle": GITIGNORE_JAVA,
}


# ---------------------------------------------------------------------------
# Cross-application env-config standard
# ---------------------------------------------------------------------------
#
# Every AAH-built app carries ONE env convention:
#   1. a checked-in ``.env.example`` catalog — variable NAMES + safe
#      placeholders, never real values;
#   2. a startup env checker that fails with ``ERR_CDR_78_EX_CONFIG`` naming
#      every missing variable and pointing at ``cp .env.example .env``;
#   3. a ``## Required Env Variables`` section in every module's feature file,
#      which is what tells each module's author what to add to (1) and register
#      in (2).
#
# Values live ONLY in the gitignored root ``.env``. ``.env.example`` is a
# catalog, never a value source — the same rule the existing test-time
# ``required_env`` gate already follows.

ENV_EXAMPLE_TEMPLATE = """\
# ─────────────────────────────────────────────────────────────────────────────
# {project_name} — environment variable catalog
#
# THIS FILE IS COMMITTED. It lists every variable the application needs, with
# SAFE PLACEHOLDERS ONLY — never a real secret, key, password, or endpoint.
#
#   cp .env.example .env    # then edit .env with real values
#
# .env is gitignored and is the ONLY place real values live.
#
# CONTRACT (enforced at app startup by the env checker):
#   * Every variable the code reads MUST be listed here.
#   * Every module that introduces a variable adds it here in the same change,
#     and registers it in the checker's required list.
#   * A variable that is missing or empty at startup fails the app with
#     ERR_CDR_78_EX_CONFIG naming exactly what is absent.
#
# Format: one `NAME=placeholder` per line, grouped by concern, with a comment
# above each group explaining what reads it.
# ─────────────────────────────────────────────────────────────────────────────

# ── Application ──────────────────────────────────────────────────────────────
# Uncomment and extend as modules land. Nothing is required until a module
# declares it in its feature file's `## Required Env Variables` section AND
# registers it in the startup checker.
# APP_ENV=local
# LOG_LEVEL=info

# ── Datastores ───────────────────────────────────────────────────────────────
# DATABASE_URL=postgresql://user:password@localhost:5432/dbname

# ── Third-party services ─────────────────────────────────────────────────────
# OPENAI_API_KEY=sk-replace-me
"""

ENV_CHECK_PYTHON = '''\
"""Startup environment-configuration check.

Fails the application at boot with ``ERR_CDR_78_EX_CONFIG`` when any required
variable is missing or empty, naming every absent variable at once and pointing
the user at ``.env.example``.

CONTRACT — every module that reads a new environment variable must:
  1. add it to ``REQUIRED_ENV`` below (name + one-line purpose comment), and
  2. add it to the committed ``.env.example`` catalog with a safe placeholder.

Call ``check_env()`` from the application entrypoint BEFORE any module reads
configuration, so a misconfiguration surfaces as this message rather than as a
downstream KeyError, a None-typed client, or a connection timeout.

This module deliberately has NO third-party dependency and does NOT import the
AAH harness — a delivered application must run standalone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ERR_CDR_78_EX_CONFIG = "ERR_CDR_78_EX_CONFIG"

# Variables that MUST be present and non-empty at startup.
# One entry per line: "NAME",  # purpose — who reads it
REQUIRED_ENV: tuple[str, ...] = (
    # "DATABASE_URL",   # Postgres connection string — data layer
    # "OPENAI_API_KEY", # embedding/LLM client
)


class ConfigError(RuntimeError):
    """Raised when required environment configuration is missing or malformed."""


def load_dotenv(path: Path | None = None) -> None:
    """Load ``KEY=value`` pairs from the root ``.env`` without overriding the
    real environment.

    A minimal parser on purpose: no dependency, no interpolation, no export
    syntax. Values already set in the process environment always win, so
    container and CI configuration is never shadowed by a stale local file.
    """
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def missing_env(required: tuple[str, ...] = REQUIRED_ENV) -> list[str]:
    """Names from ``required`` that are absent or empty in the environment."""
    return [name for name in required if not (os.environ.get(name) or "").strip()]


def format_config_error(missing: list[str]) -> str:
    """The canonical ERR_CDR_78_EX_CONFIG failure text."""
    listed = "\\n".join(f"      - {name}" for name in missing)
    return (
        f"{ERR_CDR_78_EX_CONFIG}  \\u2014  configuration mismatch\\n"
        "\\n"
        "  \\u2717 Required environment variables are missing:\\n"
        f"{listed}\\n"
        "  \\u2192 Check .env.example, copy it to .env, and fill in proper values:\\n"
        "      cp .env.example .env    # then edit .env\\n"
        "  See .env.example for the full list and expected format."
    )


def check_env(required: tuple[str, ...] = REQUIRED_ENV) -> None:
    """Load ``.env``, then raise ``ConfigError`` if anything required is absent.

    Reports EVERY missing variable in one message — fixing configuration one
    restart at a time is the failure mode this exists to prevent.
    """
    load_dotenv()
    missing = missing_env(required)
    if missing:
        raise ConfigError(format_config_error(missing))


def main() -> int:
    """Standalone check: ``python config/env_check.py``."""
    try:
        check_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print("env check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

ENV_CHECK_NODE = """\
/**
 * Startup environment-configuration check.
 *
 * Fails the application at boot with ERR_CDR_78_EX_CONFIG when any required
 * variable is missing or empty, naming every absent variable at once and
 * pointing the user at .env.example.
 *
 * CONTRACT - every module that reads a new environment variable must:
 *   1. add it to REQUIRED_ENV below (name + one-line purpose comment), and
 *   2. add it to the committed .env.example catalog with a safe placeholder.
 *
 * Call checkEnv() from the application entrypoint BEFORE any module reads
 * configuration, so a misconfiguration surfaces as this message rather than as
 * a downstream undefined, a null-typed client, or a connection timeout.
 *
 * No third-party dependency (no dotenv package) and no dependency on the AAH
 * harness - a delivered application must run standalone. Written as CommonJS
 * (.cjs) so it loads in both CJS and ESM packages.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ERR_CDR_78_EX_CONFIG = 'ERR_CDR_78_EX_CONFIG';

/**
 * Variables that MUST be present and non-empty at startup.
 * One entry per line: 'NAME', // purpose - who reads it
 */
const REQUIRED_ENV = [
  // 'DATABASE_URL',   // Postgres connection string - data layer
  // 'OPENAI_API_KEY', // embedding/LLM client
];

class ConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ConfigError';
    this.code = ERR_CDR_78_EX_CONFIG;
  }
}

/**
 * Load KEY=value pairs from the root .env without overriding the real
 * environment. Minimal parser on purpose: no dependency, no interpolation, no
 * export syntax. Values already set in process.env always win, so container and
 * CI configuration is never shadowed by a stale local file.
 */
function loadDotenv(envPath) {
  const target = envPath || path.resolve(__dirname, '..', '.env');
  if (!fs.existsSync(target)) return;
  for (const rawLine of fs.readFileSync(target, 'utf8').split(/\\r?\\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const idx = line.indexOf('=');
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim().replace(/^["']|["']$/g, '');
    if (key && process.env[key] === undefined) process.env[key] = value;
  }
}

/** Names from `required` that are absent or empty in the environment. */
function missingEnv(required = REQUIRED_ENV) {
  return required.filter((name) => !String(process.env[name] || '').trim());
}

/** The canonical ERR_CDR_78_EX_CONFIG failure text. */
function formatConfigError(missing) {
  const listed = missing.map((name) => `      - ${name}`).join('\\n');
  return [
    `${ERR_CDR_78_EX_CONFIG}  \\u2014  configuration mismatch`,
    '',
    '  \\u2717 Required environment variables are missing:',
    listed,
    '  \\u2192 Check .env.example, copy it to .env, and fill in proper values:',
    '      cp .env.example .env    # then edit .env',
    '  See .env.example for the full list and expected format.',
  ].join('\\n');
}

/**
 * Load .env, then throw ConfigError if anything required is absent. Reports
 * EVERY missing variable in one message - fixing configuration one restart at a
 * time is the failure mode this exists to prevent.
 */
function checkEnv(required = REQUIRED_ENV) {
  loadDotenv();
  const missing = missingEnv(required);
  if (missing.length > 0) throw new ConfigError(formatConfigError(missing));
}

module.exports = {
  ERR_CDR_78_EX_CONFIG,
  REQUIRED_ENV,
  ConfigError,
  loadDotenv,
  missingEnv,
  formatConfigError,
  checkEnv,
};

// Standalone check: `node config/env-check.cjs`
if (require.main === module) {
  try {
    checkEnv();
  } catch (err) {
    console.error(err.message);
    process.exit(78);
  }
  console.log('env check: OK');
}
"""

_PYTHON_STACK_TOKENS = {
    "python", "fastapi", "django", "flask", "py", "uvicorn", "starlette",
}
_NODE_STACK_TOKENS = {
    "node", "nodejs", "react", "next", "nextjs", "vue", "angular", "express",
    "typescript", "javascript", "ts", "js", "nest", "nestjs", "svelte",
}
_PYTHON_DETECT_FILES = ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")
_NODE_DETECT_FILES = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")

#: Per-stack checker: family -> (relative path, file body).
ENV_CHECK_TEMPLATES = {
    "python": ("config/env_check.py", ENV_CHECK_PYTHON),
    "node": ("config/env-check.cjs", ENV_CHECK_NODE),
}


def resolve_env_check_stack(
    stack: str | None = None, project_path: Path | None = None
) -> str | None:
    """Resolve which env-checker template fits this project, or ``None``.

    ``None`` means "no template ships" — deliberate, not an oversight. Writing a
    Python checker into a Go or Java service would be dead, wrong-language code
    in the delivered app. For those stacks the contract still applies (it is
    stated in the project ``CLAUDE.md`` and in every feature's
    ``## Required Env Variables`` section); the scaffold-first feature implements
    the checker in the project's own language instead.

    Unlike ``build_gitignore``, there is NO Python fallback here: an unused
    ignore section is harmless, an unused source file is not.
    """
    if project_path and project_path.exists():
        for filename in _NODE_DETECT_FILES:
            if (project_path / filename).exists():
                return "node"
        for filename in _PYTHON_DETECT_FILES:
            if (project_path / filename).exists():
                return "python"

    if stack:
        tokens = set(re.split(r"[-_/\s,]+", stack.lower()))
        # Python wins a genuinely mixed stack ("fastapi + react"): the checker's
        # placement default is the backend, and the backend is what boots.
        if tokens & _PYTHON_STACK_TOKENS:
            return "python"
        if tokens & _NODE_STACK_TOKENS:
            return "node"

    return None


def ensure_env_config(
    project_path: Path, project_name: str, stack: str | None = None
) -> list[str]:
    """Write the committed ``.env.example`` catalog and the startup env checker.

    Returns the project-relative paths written (empty entries omitted), so the
    caller can report what the project now carries.

    Never clobbers: a project that already has a ``.env.example`` (brownfield
    repos usually do) keeps its own, because that file is the human-curated
    catalog and overwriting it would delete the very names the checker needs. The
    checker is likewise written only when absent.
    """
    written: list[str] = []

    env_example = project_path / ".env.example"
    if not env_example.exists():
        write_text(
            ENV_EXAMPLE_TEMPLATE.format(project_name=project_name), env_example
        )
        written.append(".env.example")

    family = resolve_env_check_stack(stack, project_path)
    if family is None:
        return written

    rel_path, body = ENV_CHECK_TEMPLATES[family]
    checker = project_path / rel_path
    if not checker.exists():
        checker.parent.mkdir(parents=True, exist_ok=True)
        write_text(body, checker)
        written.append(rel_path)

    return written


# ---------------------------------------------------------------------------
# First-touch lint config
# ---------------------------------------------------------------------------
#
# A newly scaffolded package ships with NO lint configuration, so the build
# standards gate runs ``ruff check`` against ruff's DEFAULT rule set and reports
# dozens of findings — most of them either mandatory framework idiom (FastAPI's
# ``Depends()``/``File()`` in argument defaults) or conventions that do not apply
# to AAH's functional-test style (test-file naming, deliberate broad teardown
# except). That triggers a whole extra fix-and-reverify cycle on the FIRST feature
# of every Python package. Stamping this default pre-empts it — at scaffold time
# for a brownfield repo's existing packages, and at module 0 for the packages the
# build itself creates (``ensure-lint-config --package-root``).
#
# The bodies live in ``_resources/_templates/scaffold/`` as real ``ruff.toml`` and
# ``eslint.config.mjs`` files, not Python strings, so each is highlighted and
# checkable by its own tooling. Each carries its own header comment recording why
# its exceptions exist — those are load-bearing, so read the template before
# changing it. The content is deliberately deterministic (a template, not
# LLM-authored); only PLACEMENT is decided per project, by
# ``discover_package_roots``. Neither template widens its default rule set:
# adding families would introduce new findings rather than resolve the gate.
#
# The eslint default in particular is a flat config with ZERO imports. `export
# default` only loads as ESM, which is why the file is `.mjs` — a `.js` name would
# need `"type": "module"` in package.json. And it imports nothing (not even
# `@eslint/js`) because an unresolvable import makes eslint exit non-zero with a
# module-resolution error, which the standards gate reads as `tool_not_installed`
# and turns into a BLOCK. One dependency (`eslint`) is the whole requirement.
#
#: Per-family lint config: family -> filename, used BOTH as the template's name
#: under ``_resources/_templates/scaffold/`` and as the file written into the
#: package root.
LINT_CONFIG_TEMPLATES = {
    "python": "ruff.toml",
    "node": "eslint.config.mjs",
}


def load_scaffold_template(name: str) -> str:
    """Return the body of ``_resources/_templates/scaffold/<name>``.

    The lint-config bodies are real `.toml`/`.mjs` files rather than Python
    strings so they get syntax highlighting and can be validated by their own
    tooling. Raises ``FileNotFoundError`` if the template is missing from the
    install — a packaging fault, not a project fault, and one that must be loud
    rather than silently writing an empty config the standards gate then reads
    as clean.
    """
    from aah.core.common.config import resolve_framework_root

    framework_root = resolve_framework_root()
    if framework_root is None:
        raise FileNotFoundError(
            f"Cannot locate the AAH framework root to read template {name!r}"
        )
    path = framework_root / "_resources" / "_templates" / "scaffold" / name
    return path.read_text(encoding="utf-8")

#: Version range stamped into a node package's devDependencies. Pinned and
#: committed rather than fetched with `npx --yes` at gate time: an unpinned
#: network fetch means the same commit can pass today and fail tomorrow, and
#: attested evidence has to be reproducible.
ESLINT_DEV_DEPENDENCY = "^9.0.0"


def _has_ruff_config(project_path: Path) -> bool:
    """True if the project already configures ruff (never-clobber guard).

    Honors an existing ``ruff.toml`` / ``.ruff.toml`` (brownfield repos often
    have one) and a ``[tool.ruff]`` table in ``pyproject.toml``.
    """
    for name in ("ruff.toml", ".ruff.toml"):
        if (project_path / name).exists():
            return True
    pyproject = project_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            if "[tool.ruff]" in pyproject.read_text(encoding="utf-8"):
                return True
        except OSError:
            pass
    return False


def _has_eslint_config(package_root: Path) -> bool:
    """True if the package already configures eslint (never-clobber guard).

    Covers flat config (``eslint.config.*``), the legacy ``.eslintrc*`` family,
    and an ``eslintConfig`` key inside ``package.json``.
    """
    if any(package_root.glob("eslint.config.*")) or any(package_root.glob(".eslintrc*")):
        return True
    pkg_json = package_root / "package.json"
    if pkg_json.is_file():
        try:
            return "eslintConfig" in (json.loads(pkg_json.read_text(encoding="utf-8")) or {})
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return False


def _ensure_eslint_dependency(package_root: Path) -> bool:
    """Declare ``eslint`` in the package's devDependencies. True if added.

    A declared, lockfile-installed eslint is what keeps the standards gate
    reproducible — the gate never installs and never fetches with
    ``npx --yes``.
    """
    pkg_json = package_root / "package.json"
    if not pkg_json.is_file():
        return False
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if "eslint" in (data.get("dependencies") or {}) or "eslint" in (
        data.get("devDependencies") or {}
    ):
        return False
    dev = dict(data.get("devDependencies") or {})
    dev["eslint"] = ESLINT_DEV_DEPENDENCY
    data["devDependencies"] = dict(sorted(dev.items()))
    write_text(json.dumps(data, indent=2) + "\n", pkg_json)
    return True


def _stamp_lint_config(project_path: Path, package_root: Path, family: str) -> list[str]:
    """Write one package root's lint config + linter declaration."""
    written: list[str] = []
    filename = LINT_CONFIG_TEMPLATES.get(family)
    if filename is None:
        return written

    def _rel(path: Path) -> str:
        return path.relative_to(project_path).as_posix()

    if family == "node" and _ensure_eslint_dependency(package_root):
        written.append(_rel(package_root / "package.json"))

    already = (
        _has_ruff_config(package_root) if family == "python"
        else _has_eslint_config(package_root)
    )
    if already:
        return written

    config = package_root / filename
    if not config.exists():
        # Read the template only once we know we are going to write it, so a
        # never-clobber no-op never touches the filesystem for nothing.
        write_text(load_scaffold_template(filename), config)
        written.append(_rel(config))

    return written


def ensure_lint_config(
    project_path: Path,
    *,
    package_root: Path | None = None,
    install: bool = False,
) -> list[str]:
    """Write a default lint config at every package root that lacks one.

    Configs live at PACKAGE roots (``backend/ruff.toml``,
    ``frontend/eslint.config.mjs``), not at the project root: one root config
    governs the wrong tree and cannot express two toolchains. The same
    ``discover_package_roots`` that scopes the standards gate decides placement,
    so a config can never land where the gate will not read it.

    Args:
        project_path: the project root.
        package_root: stamp only this root (absolute, or relative to
            ``project_path``). Its family is resolved from the manifest files
            AT that root, so the package manifest must exist first.
        install: also run each root's ``install_deps()`` so the linter is
            actually on disk. Used by the build's pre-gate backstop — never by
            the gate itself, which stays read-only.

    Returns the project-relative paths written (empty if none). Never clobbers:
    a package that already configures its linter keeps its own config.
    """
    from aah.core.build.lang_checks import detect, discover_package_roots

    project_path = Path(project_path).resolve()

    if package_root is not None:
        root = Path(package_root)
        root = (root if root.is_absolute() else project_path / root).resolve()
        # depth 0 = "resolve this directory's own family", nothing below it.
        roots = discover_package_roots(root, max_depth=0)
    else:
        roots = discover_package_roots(project_path)

    written: list[str] = []
    for root, family in roots:
        written.extend(_stamp_lint_config(project_path, root, family))

    if install:
        for root, _family in roots:
            for cmd in detect(root, manifest={}).install_deps():
                run_bounded_command(
                    CommandSpec(cmd.argv, cwd=str(cmd.cwd), timeout_sec=cmd.timeout_sec)
                )

    return written


def build_gitignore(stack: str | None = None, project_path: Path | None = None) -> str:
    """Build a .gitignore tailored to the project's tech stack."""
    sections = [GITIGNORE_BASE]

    if stack:
        stack_lower = stack.lower()
        stack_tokens = set(re.split(r"[-_/\s,]+", stack_lower))
        for keyword, content in STACK_GITIGNORE_MAP.items():
            if keyword in stack_tokens and content not in sections:
                sections.append(content)

    if project_path and project_path.exists():
        for filename, content in DETECT_FILES.items():
            if (project_path / filename).exists() and content not in sections:
                sections.append(content)

    if len(sections) == 1:
        sections.append(GITIGNORE_PYTHON)

    # Last, so the negation outranks every `.env`-ish pattern above it.
    sections.append(GITIGNORE_ENV_EXAMPLE)

    return "\n".join(sections)


def _harness_ignore_section(project_path: Path) -> str:
    """The aah-managed ignore block for machine-specific install artifacts.

    Unions every registered platform adapter's project ignore lines (Claude's
    ``.claude/*``, Codex's ``.agents/skills/*`` + ``.codex/*``, …), sourced from
    the adapters so it always matches what a local install actually links. Lines
    for a platform the user did not install are harmless no-ops. The scaffolder
    is the SOLE writer of the project ``.gitignore`` — ``aah install`` writes no
    git artifacts.
    """
    from aah.core.install import registry

    lines = registry.all_project_ignore_lines(project_path)
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "# aah-managed install artifacts (machine-specific — never commit)\n"
        f"{body}\n"
    )


def ensure_gitignore(project_path: Path, stack: str | None = None) -> None:
    """Ensure the project's .gitignore covers AAH base + stack + install artifacts.

    Single writer of the project ``.gitignore``. Merge-safe: if none exists,
    write a fresh one; if one already exists (the user's own), append only the
    missing non-comment entries under a marker header — never clobber.
    """
    gitignore_path = project_path / ".gitignore"
    new_content = build_gitignore(stack, project_path) + "\n" + _harness_ignore_section(project_path)

    if not gitignore_path.exists():
        write_text(new_content, gitignore_path)
        return

    existing = gitignore_path.read_text(encoding="utf-8")
    existing_lines = {line.strip() for line in existing.splitlines()}
    additions = [
        stripped
        for line in new_content.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
        and stripped not in existing_lines
    ]
    if additions:
        merged = existing.rstrip() + "\n\n# Added by RAPIDS scaffold\n"
        merged += "\n".join(additions) + "\n"
        write_text(merged, gitignore_path)


def write_project_claude_md(project_path: Path, project_name: str, stack: str | None = None) -> None:
    """Write the PROJECT-level delivery ruleset into ``CLAUDE.md``.

    The rendered ruleset is wrapped in AAH block markers so the write is
    idempotent (per the brownfield append contract):

    - No ``CLAUDE.md``: create it from the wrapped ruleset.
    - ``CLAUDE.md`` exists, no AAH block: append the block to the end,
      preserving the user's existing content.
    - ``CLAUDE.md`` exists with an AAH block: replace only that block.
    """
    block = wrap_aah_block(render_project_claude_md(project_name, stack))
    claude_md_path = project_path / "CLAUDE.md"

    if not claude_md_path.exists():
        write_text(block, claude_md_path)
        return

    existing = claude_md_path.read_text(encoding="utf-8")
    begin = existing.find(AAH_BLOCK_BEGIN)
    end = existing.find(AAH_BLOCK_END)
    if begin != -1 and end != -1 and end > begin:
        # Replace the existing block (idempotent re-scaffold).
        end_full = end + len(AAH_BLOCK_END)
        merged = existing[:begin] + block.rstrip("\n") + existing[end_full:]
        write_text(merged, claude_md_path)
    else:
        # Append the block, preserving all existing user content.
        merged = existing.rstrip() + "\n\n" + block
        write_text(merged, claude_md_path)


def is_harness_repo(path: Path) -> bool:
    """True if ``path`` is the aah framework repo itself (not a user project).

    Markers: an ``aah/core/`` package dir plus a ``pyproject.toml`` naming the
    project ``aah``. Guards against scaffolding the framework as a project.
    """
    if not (path / "aah" / "core").is_dir():
        return False
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return 'name = "aah"' in text or "name = 'aah'" in text


# Entries that are NOT the user's project code — tooling/harness metadata that
# can exist in an otherwise-empty folder (e.g. Claude Code writes ``.claude/``
# and ``.mcp.json`` the moment a session starts in the folder, before we ever
# scaffold). These must be ignored when deciding greenfield vs brownfield, or a
# fresh greenfield start would be misread as brownfield. ``.git`` is handled
# separately (its presence alone means an existing repo → brownfield).
_NON_PROJECT_ENTRIES = frozenset({
    ".aah", ".rapids", ".git", ".claude", ".claude.json", ".mcp.json",
})


def detect_project_type(target_dir: Path) -> str:
    """Auto-detect greenfield vs brownfield from a folder's contents.

    Empty and no ``.git`` → greenfield (start from scratch). Has code and/or a
    ``.git`` → brownfield (an existing/cloned repo; respect it). Tooling metadata
    such as ``.claude/`` (see ``_NON_PROJECT_ENTRIES``) is ignored so a fresh
    greenfield session isn't misclassified as brownfield.
    """
    if (target_dir / ".git").exists():
        return "brownfield"
    # Any real content (ignoring harness/tooling metadata) → brownfield.
    for child in target_dir.iterdir():
        if child.name in _NON_PROJECT_ENTRIES:
            continue
        return "brownfield"
    return "greenfield"


def create_project(
    project_name: str | None = None,
    project_type: str | None = None,
    target_dir: Path | None = None,
    stack: str | None = None,
    remote_url: str | None = None,
) -> Path:
    """Scaffold a AAH project in ``target_dir`` (default: cwd).

    The folder is the project. Writes ``.aah/`` into it. ``project_type`` is
    auto-detected from the folder when not given (empty → greenfield, has
    code/.git → brownfield). Returns the project path.
    """
    project_path = (target_dir or Path.cwd()).resolve()

    if is_harness_repo(project_path):
        print(
            "Error: this is the aah framework repo, not a project.\n"
            "  cd to your project folder (a new `mkdir` folder or a `git clone`d\n"
            "  repo) and run /rapids-new-project there.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not project_path.exists():
        project_path.mkdir(parents=True)

    if (project_path / ".aah" / "manifest.yaml").exists():
        print(f"Error: already an AAH project (found .aah/manifest.yaml at {project_path})", file=sys.stderr)
        sys.exit(1)

    # Project name defaults to the folder name.
    if not project_name:
        project_name = project_path.name

    if project_type is None:
        project_type = detect_project_type(project_path)

    if project_type == "greenfield":
        _scaffold_greenfield(project_path, project_name, stack, remote_url)
    elif project_type == "brownfield":
        _scaffold_brownfield(project_path, project_name)
    else:
        print(f"Error: invalid project type '{project_type}'", file=sys.stderr)
        sys.exit(1)

    return project_path


def _scaffold_greenfield(project_path: Path, project_name: str, stack: str | None, remote_url: str | None) -> None:
    """Set up a greenfield project with git, branches, and .aah/."""
    # Verify codemap-scale is installed
    from aah.core.common.codemap_utils import is_available as codemap_available

    if not codemap_available():
        print(
            "warning: codemap-scale not installed. Structural indexing will not be available.\n"
            '         Install: uv tool install "codemap-scale[embeddings-local] @ git+https://github.com/Deloitte-US-Consulting/rapids-utilities.git@main"',
            file=sys.stderr,
        )

    # Initialize git only if the folder is not already a repo. A folder that
    # already has .git (e.g. `git init`'d by the user before scaffolding) must
    # not be re-init'd, and its history/branch must not be stomped.
    already_repo = (project_path / ".git").exists()
    if not already_repo:
        init_repo(project_path)

    # Ensure .gitignore has the AAH base + stack entries. Merge-safe: never
    # clobbers an existing file (the installer may have created one with its
    # aah-managed .claude/* block before scaffolding, in the local venv flow).
    ensure_gitignore(project_path, stack)

    # Create .gitattributes (merge strategy for regenerable binaries)
    gitattributes_path = project_path / ".gitattributes"
    if not gitattributes_path.exists():
        write_text(GITATTRIBUTES_BASE, gitattributes_path)

    # Seed the cross-application env-config standard: the committed
    # .env.example catalog plus the startup checker that raises
    # ERR_CDR_78_EX_CONFIG. Written BEFORE the scaffold commit below so both
    # land in it. Must follow ensure_gitignore so the `!.env.example` negation
    # is already in place when git first sees the file.
    ensure_env_config(project_path, project_name, stack)

    # Stamp a default lint config at every package root so the FIRST feature of
    # a package lands on a green standards gate instead of a fix-and-reverify
    # cycle. Never-clobber; written into the scaffold commit alongside env-config.
    # A greenfield tree has no package manifests yet, so this is a no-op here and
    # module 0 stamps the configs it scaffolds (see aah-feature-implementer).
    ensure_lint_config(project_path)

    # Create .aah/ structure
    rapids_path = create_rapids_dir(project_path)
    ensure_attestation_secret(project_path)

    # Create manifest.yaml
    manifest = get_default_manifest(project_name, "greenfield")
    if stack:
        manifest["stack_choices"]["primary"] = stack
    save_manifest(manifest, rapids_path / "manifest.yaml")

    # Create claude-progress.json
    progress = get_default_progress()
    save_progress(progress, rapids_path / "claude-progress.json")

    # Create expertise.yaml (empty scaffold for greenfield)
    expertise_path = rapids_path / "codebase-intel" / "expertise.yaml"
    expertise_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_text(
        EXPERTISE_YAML_TEMPLATE.format(project_id=project_name, timestamp=timestamp),
        expertise_path,
    )

    # Create domains/ directory (Tier 2 expertise — populated during implementation)
    domains_dir = rapids_path / "codebase-intel" / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)

    # Write the PROJECT-level delivery ruleset into CLAUDE.md so the delivery
    # rules travel with the project. Greenfield: no CLAUDE.md exists, so this
    # creates it from the rendered ruleset (name + --stack).
    write_project_claude_md(project_path, project_name, stack)

    if not already_repo:
        # Fresh repo: create the initial scaffold commit and the develop branch.
        # Per the RAPIDS branching strategy (CLAUDE.md), main is production-only
        # and all feature work flows through develop → integration/wave-N →
        # develop → main. Leaving the tree on main would invert the model.
        add_all(cwd=project_path)
        commit("Initial project scaffold", cwd=project_path)
        create_branch("develop", cwd=project_path)
        checkout_branch("develop", cwd=project_path)
    else:
        # Existing repo: do NOT re-init, force an "Initial scaffold" commit, or
        # hijack the branch. Commit only the added .aah/ scaffold on the
        # current branch, leaving the user's history and branch untouched.
        add_all(cwd=project_path)
        commit("Add AAH project scaffold (.aah/)", cwd=project_path)

    # Configure remote if provided (only when we own the fresh repo)
    if remote_url and not already_repo:
        from aah.core.common.git_utils import run_git
        run_git(["remote", "add", "origin", remote_url], cwd=project_path)


def _scaffold_brownfield(project_path: Path, project_name: str) -> None:
    """Set up a brownfield project (existing repo imported into workspace)."""
    # For brownfield, we assume the repo is already cloned/symlinked
    # Just add .aah/ structure
    rapids_path = create_rapids_dir(project_path)
    ensure_attestation_secret(project_path)

    # Initialize git if not already a repo
    git_dir = project_path / ".git"
    if not git_dir.exists():
        init_repo(project_path)
        ensure_gitignore(project_path)
        write_text(GITATTRIBUTES_BASE, project_path / ".gitattributes")
        # Env-config standard, before the import commit so it lands in it. The
        # stack is not resolved yet on this path (the manifest is written
        # further down), so detection falls back to the repo's own marker files.
        ensure_env_config(project_path, project_name)
        # Default lint config per package root (never-clobber — a brownfield repo
        # that already configures its linter keeps its own).
        ensure_lint_config(project_path)
        add_all(cwd=project_path)
        commit("Initial brownfield import", cwd=project_path)
        # Create develop and check it out — same reasoning as greenfield:
        # main is production-only, feature work must land on develop.
        create_branch("develop", cwd=project_path)
        checkout_branch("develop", cwd=project_path)
    else:
        # Existing repo — merge missing entries into its .gitignore (never clobber).
        ensure_gitignore(project_path)

        # Ensure .gitattributes exists (merge strategy for regenerable binaries)
        gitattributes_path = project_path / ".gitattributes"
        if not gitattributes_path.exists():
            write_text(GITATTRIBUTES_BASE, gitattributes_path)

    # Verify codemap-scale is installed
    from aah.core.common.codemap_utils import is_available as codemap_available

    if not codemap_available():
        print(
            "warning: codemap-scale not installed. Structural indexing will not be available.\n"
            '         Install: uv tool install "codemap-scale[embeddings-local] @ git+https://github.com/Deloitte-US-Consulting/rapids-utilities.git@main"',
            file=sys.stderr,
        )

    # Create manifest
    manifest = get_default_manifest(project_name, "brownfield")
    save_manifest(manifest, rapids_path / "manifest.yaml")

    # Create progress
    progress = get_default_progress()
    progress["next_steps"] = "Run codebase indexer for brownfield intelligence gathering"
    save_progress(progress, rapids_path / "claude-progress.json")

    # Create expertise.yaml (empty scaffold — seeded during brownfield import)
    expertise_path = rapids_path / "codebase-intel" / "expertise.yaml"
    expertise_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_text(
        EXPERTISE_YAML_TEMPLATE.format(project_id=project_name, timestamp=timestamp),
        expertise_path,
    )

    # Create domains/ directory (Tier 2 expertise — populated during implementation)
    domains_dir = rapids_path / "codebase-intel" / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)

    # Write the PROJECT-level delivery ruleset into CLAUDE.md. Brownfield may
    # already have a user-authored CLAUDE.md, so this appends/replaces the AAH
    # block (never clobbering user content). Stack is read from the manifest's
    # stack_choices.primary, falling back to a neutral placeholder if absent.
    stack = manifest.get("stack_choices", {}).get("primary")
    write_project_claude_md(project_path, project_name, stack)

    # Env-config standard. Runs here (not only in the fresh-repo branch above)
    # so an already-cloned brownfield repo gets it too, and runs AFTER the
    # manifest so the resolved stack can pick the checker language. Both writes
    # are never-clobber, so the fresh-repo path's earlier call makes this a no-op
    # rather than a double write.
    ensure_env_config(project_path, project_name, stack)

    # Default lint config for an already-cloned brownfield repo too — one per
    # package root. Never-clobber, so the fresh-repo path's earlier call makes
    # this a no-op rather than a double write.
    ensure_lint_config(project_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH project scaffolder")
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create", help="Scaffold a AAH project in a folder (default: cwd)")
    create_p.add_argument("name", type=str, nargs="?", default=None,
                          help="Project name (default: the folder name)")
    create_p.add_argument("--type", dest="project_type", default=None,
                          choices=["greenfield", "brownfield"],
                          help="Override auto-detection (empty→greenfield, has code/.git→brownfield)")
    create_p.add_argument("--target-dir", type=Path, default=None,
                          help="Folder to scaffold in (default: current directory)")
    create_p.add_argument("--stack", type=str, default=None, help="Primary tech stack")
    create_p.add_argument("--remote", type=str, default=None, help="Git remote URL")

    lint_p = sub.add_parser(
        "ensure-lint-config",
        help="Stamp a default lint config at every package root (never clobbers)",
    )
    lint_p.add_argument("--project-path", type=Path, default=None,
                        help="Project root (default: current directory)")
    lint_p.add_argument("--package-root", type=Path, default=None,
                        help="Stamp only this package root (relative to the project)")
    lint_p.add_argument("--install", action="store_true",
                        help="Also install each root's dependencies so the linter is on disk")

    args = parser.parse_args()

    if args.command == "ensure-lint-config":
        from aah.core.build.lang_checks import discover_package_roots
        from aah.core.common.config import require_project_path

        project_path = require_project_path(args.project_path).resolve()
        written = ensure_lint_config(
            project_path, package_root=args.package_root, install=args.install
        )
        # Report the roots too: "written: []" alone cannot distinguish
        # "already configured" from "no package root was found".
        roots = [
            {
                "root": "." if root == project_path
                else root.relative_to(project_path).as_posix(),
                "stack": stack,
            }
            for root, stack in discover_package_roots(project_path)
        ]
        json.dump({"written": written, "roots": roots}, sys.stdout, indent=2)
        print()
        sys.exit(0)

    if args.command == "create":
        project_path = create_project(
            project_name=args.name,
            project_type=args.project_type,
            target_dir=args.target_dir,
            stack=args.stack,
            remote_url=args.remote,
        )
        # Read back the resolved type from the manifest for accurate reporting.
        from aah.core.common.manifest import load_manifest
        try:
            resolved_type = load_manifest(project_path / ".aah" / "manifest.yaml").get("project_type", args.project_type)
        except Exception:
            resolved_type = args.project_type
        result = {
            "project": project_path.name,
            "type": resolved_type,
            "path": str(project_path),
            "status": "created",
        }
        json.dump(result, sys.stdout, indent=2)
        print()
        print(f"Project '{project_path.name}' scaffolded at {project_path}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
