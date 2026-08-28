#!/usr/bin/env python3
"""
AWS Bedrock AgentCore Runtime deploy — standardized CLI wrapped in one script.

Owns the *deterministic* AgentCore CLI sequence so the deploy agent never types
raw `agentcore`/`aws` commands: prereq validation, config scaffolding
(`agentcore create --no-agent`), a pre-deploy credential scan, CDK bootstrap,
deploy, status, invoke, logs, and teardown. The reasoning-heavy work (writing the
agent's protocol contract, AG-UI/A2A layers) lives in the build phase / skills.

All configuration comes from CLI arguments — nothing is hardcoded.

Usage:
    # Primary entry point — the deploy engineer calls this ONE command; it runs every
    # step below idempotently and resumes from the ledger on a re-run:
    aah run core.deploy.agentcore_deploy up --project-path /path \
        --account-id 123456789012 --region us-east-1 [--profile default] \
        [--project-name QuizBot] [--env "K=V,K=V"] [--memory-strategies SEMANTIC,SUMMARIZATION] \
        [--secret-arn arn:...] [--frontend yes|no] [--force]

    # Individual steps (normally only invoked by `up`, or by hand for diagnosis):
    aah run core.deploy.agentcore_deploy validate-prereqs --project-path /path
    aah run core.deploy.agentcore_deploy scaffold --project-path /path \
        --project-name QuizBot --framework LangChain_LangGraph --protocol HTTP \
        --account-id 123456789012 --region us-east-1 [--profile default]
    aah run core.deploy.agentcore_deploy set-env --project-path /path --env "K=V,K=V"
    aah run core.deploy.agentcore_deploy credential-scan --project-path /path [--fix]
    aah run core.deploy.agentcore_deploy bootstrap --account-id 123... --region us-east-1 --profile default
    aah run core.deploy.agentcore_deploy deploy --project-path /path --profile default --region us-east-1
    aah run core.deploy.agentcore_deploy status  --project-path /path --profile default --region us-east-1
    aah run core.deploy.agentcore_deploy invoke  --project-path /path --profile default --region us-east-1 --prompt "hi"
    aah run core.deploy.agentcore_deploy logs    --project-path /path --profile default --region us-east-1 [--since 30m]
    aah run core.deploy.agentcore_deploy teardown --project-path /path --profile default --region us-east-1

Design (mirrors ecs_deploy.py):
    - Every parameter comes from CLI args
    - MSYS_NO_PATHCONV=1 for aws/agentcore calls on Windows
    - Commands run from the project root (codeLocation: "./")
    - Real-time progress + JSON result after ---DEPLOY_RESULT_JSON---
    - Idempotent: scaffold/bootstrap are create-if-not-exists
    - The runtime uses the default credential chain (IAM role) — never a named profile
"""

import argparse
import ast
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path


_IS_WINDOWS = platform.system() == "Windows"


def _shell_quote(s: str) -> str:
    """Quote a string for the shell `_run()` actually invokes. `subprocess.run(shell=True)`
    uses cmd.exe on Windows — NOT a POSIX shell, regardless of MSYS_NO_PATHCONV or running
    under Git Bash. Verified empirically: `shlex.quote`'s single-quote escaping is not
    understood by cmd.exe and corrupts the argument (a payload with an apostrophe arrived
    at the target process as mangled garbage, not the intended string). On POSIX, `shlex.quote`
    is correct and cmd.exe-style escaping would be wrong — branch on platform.
    """
    if _IS_WINDOWS:
        return '"' + s.replace('"', '\\"') + '"'
    return shlex.quote(s)


# Detect protocol from code markers (order matters — most specific first).
_PROTOCOL_MARKERS = [
    ("MCP", ["FastMCP"]),
    ("AGUI", ["text/event-stream", "EventEncoder", "AGUI", "AG-UI"]),
    ("A2A", ["A2A", "agent-to-agent", "JSONRPCResponse", "agent-card"]),
    ("HTTP", ["/invocations", "@app.entrypoint", "BedrockAgentCoreApp"]),
]

# Detect framework from imports → maps to `agentcore create --framework` flag.
_FRAMEWORK_MARKERS = [
    ("LangChain_LangGraph", ["from langgraph", "import langgraph", "StateGraph"]),
    ("Strands", ["from strands", "import strands"]),
    ("GoogleADK", ["from google.adk", "import google.adk"]),
    ("OpenAIAgents", ["from agents import", "import agents"]),
]


def _run(cmd: list[str] | str, profile: str | None = None, timeout: int = 600,
         cwd: Path | None = None, check: bool = True, stream: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command with AgentCore-friendly env.

    Prepends AWS_PROFILE (runtime deploy step reads it) and MSYS_NO_PATHCONV=1 on
    Windows. When stream=True the child inherits stdout/stderr (used for `deploy`
    which prints long-running progress and must NOT be polled in the background).
    """
    env = os.environ.copy()
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"
    if profile:
        env["AWS_PROFILE"] = profile

    shell = isinstance(cmd, str)
    try:
        return subprocess.run(
            cmd,
            capture_output=not stream,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=shell,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=check,
        )
    except subprocess.TimeoutExpired:
        # A timeout on a non-critical probe (check=False) is treated as a failed
        # command, not a fatal error — the caller records it in the JSON result.
        if not check:
            return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timed out")
        print(f"  ERROR: command timed out ({timeout}s): {cmd}", flush=True)
        sys.exit(1)


def _emit(result: dict) -> None:
    """Print the JSON result block the agent/pipeline parses."""
    print("---DEPLOY_RESULT_JSON---")
    json.dump(result, sys.stdout, indent=2)
    print()


# Directories that hold vendored deps / build output / framework state — never
# first-party agent code. Scanning them yields false positives (e.g. a vendored
# botocore contains `profile_name=`).
_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "agentcore", "__pycache__", ".git",
    ".rapids", ".aah", "proxy_lambda", "build", "dist", "out", "site-packages",
    ".aws-sam", ".cache", ".tox", ".mypy_cache", ".pytest_cache",
    ".claude", "worktrees",
    # Deploy-time tooling — never the runtime entrypoint, and its scripts
    # legitimately use hardcoded profiles/deploy-only env vars locally.
    "deploy",
    # Test suites — a functional test that POSTs to /invocations and asserts on
    # "text/event-stream" carries the exact same protocol markers as the real
    # entrypoint, and was observed picking a test file as the "detected" entrypoint.
    "tests", "test",
}
# Filenames that carry protocol markers but are NOT the agent entrypoint
# (proxies/clients that stream or sign, deploy drivers, generated helpers).
_NON_ENTRYPOINT_FILES = {
    "agent_proxy.py", "invoke_client.py", "lambda_function.py", "conftest.py",
    "agentcore_deploy.py", "amplify_deploy.py", "proxy_deploy.py",
    "secrets_setup.py", "s3_setup.py", "iam_role.py", "log_group_setup.py",
    "cloudwatch_alarms.py",
}
# pytest naming conventions — catches test files co-located next to source (not just
# inside a tests/ dir, which _SKIP_DIRS already handles).
_TEST_FILE_RE = re.compile(r"^(test_.+|.+_test)\.py$")

# Single source of truth for "is this string an AgentCore RUNTIME ARN" — used by both
# _extract_arn (agentcore status/deploy output) and _resolve_runtime_arn's fallback.
# These used to be two independently-drifted patterns: _extract_arn's was looser (any
# bedrock-agentcore ARN, e.g. it could match a memory ARN appearing earlier in the same
# text), _resolve_runtime_arn's fallback required a `runtime/` segment. Unify on the
# stricter one so both call sites reject a non-runtime ARN the same way.
_RUNTIME_ARN_RE = re.compile(r"arn:aws:bedrock-agentcore:[^\s\"')]+runtime/[A-Za-z0-9_\-]+")


def _iter_agent_py(project_path: Path):
    """Yield candidate first-party agent .py files, in a deterministic (sorted) order —
    excludes vendored/build dirs and test files. Order matters: callers that pick the
    "first" match among several candidates need that to be stable across runs, not an
    arbitrary filesystem-dependent rglob order."""
    for py in sorted(project_path.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        if _TEST_FILE_RE.match(py.name):
            continue
        yield py


def _code_location(project_path: Path) -> Path:
    """The directory that becomes /var/task in the runtime container.

    codeLocation is usually "./" (zip the whole project), but a project whose agent
    package sits one level down (e.g. backend/hrbot/) can set codeLocation="./backend"
    so the package lands at the zip root and `import hrbot` resolves without a
    PYTHONPATH hack. Every path in agentcore.json is relative to THIS directory, not
    to project_path — resolving them against the project root instead silently
    discards a valid config.
    """
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if not cfg_path.exists():
        return project_path
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        loc = ((cfg.get("runtimes") or [{}])[0]).get("codeLocation") or "./"
    except (json.JSONDecodeError, OSError):
        return project_path
    resolved = (project_path / loc).resolve()
    return resolved if resolved.is_dir() else project_path


def _entrypoint_from_config(project_path: Path) -> str | None:
    """When already scaffolded, the agentcore.json entrypoint is authoritative."""
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        rt = (cfg.get("runtimes") or [{}])[0]
        ep = rt.get("entrypoint")
        # Only trust it if the file actually exists - resolved against codeLocation
        # (the zip root), NOT the project root; those differ whenever codeLocation
        # points into a subdirectory.
        # Entrypoints use ASGI "module:attr" notation (e.g. "main.py:app",
        # "src/api/app.py:app") — strip the ":attr" suffix before the file check,
        # otherwise a valid config is silently discarded and we fall back to a scan.
        if ep:
            ep_file = ep.split(":", 1)[0]
            if (_code_location(project_path) / ep_file).exists():
                return ep
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _detect_entrypoint_and_protocol(project_path: Path) -> tuple[str | None, str]:
    """Find the file carrying the protocol contract and infer the protocol.

    Prefers the scaffolded agentcore.json entrypoint; otherwise scans first-party
    files, skipping known non-entrypoint helpers (proxy/client/lambda).
    """
    configured = _entrypoint_from_config(project_path)
    best_file = None
    best_protocol = "HTTP"
    for py in _iter_agent_py(project_path):
        if py.name in _NON_ENTRYPOINT_FILES:
            continue
        try:
            content = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for proto, markers in _PROTOCOL_MARKERS:
            if any(m in content for m in markers):
                rel = py.relative_to(project_path).as_posix()
                # The configured entrypoint anchors both file and protocol.
                if configured and rel == configured:
                    return rel, proto
                if best_file is None:
                    best_file = rel
                    best_protocol = proto
                elif proto != "HTTP" and best_protocol == "HTTP":
                    # Upgrade an earlier plain-HTTP guess to a more specific protocol
                    # ONCE. Do NOT keep overriding on every subsequent non-HTTP match —
                    # that "last match wins" behavior is exactly how a second AGUI-
                    # flavored file (e.g. a functional test asserting on
                    # "text/event-stream") could bump the real entrypoint out in favor
                    # of whatever else matched later in iteration order.
                    best_file = rel
                    best_protocol = proto
                break
    # If the config names an entrypoint we didn't flag with a marker, still trust it.
    return (configured or best_file), best_protocol


def _detect_framework(project_path: Path) -> str:
    """Infer the framework flag for `agentcore create` from source imports."""
    for py in _iter_agent_py(project_path):
        try:
            content = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for flag, markers in _FRAMEWORK_MARKERS:
            if any(m in content for m in markers):
                return flag
    return "Strands"  # default — framework flag does not affect deploy behaviour


# ---------------------------------------------------------------------------
# validate-prereqs
# ---------------------------------------------------------------------------

def _entrypoint_file(project_path: Path, entrypoint: str | None) -> Path | None:
    """Resolve the entrypoint's source file from a 'file.py', 'file.py:attr', or
    'module:attr' spec. Returns None if it can't be located."""
    if not entrypoint:
        return None
    head = entrypoint.split(":", 1)[0]           # 'main.py:app' -> 'main.py', 'main:app' -> 'main'
    # Entrypoints are relative to the zip root (codeLocation); fall back to the project
    # root so a detected-by-scan entrypoint (always project-relative) still resolves.
    for base in (_code_location(project_path), project_path):
        cand = base / head
        if cand.exists():
            return cand
        # module-dotted form without .py (e.g. 'main:app' or 'src.api.app:app')
        mod = base / (head.replace(".", "/") + ".py")
        if mod.exists():
            return mod
    return None


def _check_self_serve(project_path: Path, entrypoint: str | None, protocol: str) -> str | None:
    """For ANY protocol, verify SOMETHING actually starts the server.

    AgentCore runs the container's start command; if the entrypoint just *defines* a
    handler/app with nothing that actually binds a listening process — no
    `uvicorn.run(...)`, no self-serving SDK app (`BedrockAgentCoreApp`/`@app.entrypoint`,
    which runs its own server via `app.run()`), no FastMCP `.run(...)` — the process
    exits immediately, `/ping` never binds, and AgentCore reports a misleading '30s
    initialization timeout'. This applies to AGUI/MCP/A2A exactly as much as HTTP: the
    real `agentcore create --protocol AGUI` template is FastAPI + `uvicorn.run(...)`
    (same bootstrap as HTTP), and the Step 4 A2A template uses the same pattern — only
    the *reasoning* differs by protocol, not whether a server must self-start. This
    check used to skip everything but HTTP, which is exactly how a hand-authored
    `lambda_handler(event, context)`-shaped AGUI entrypoint — no server, ever — passed
    validate-prereqs undetected in practice. Returns an error string if nothing
    self-serves; else None.
    """
    f = _entrypoint_file(project_path, entrypoint)
    if not f:
        return None
    try:
        src = f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # Self-serving SDK apps run their own server — no explicit uvicorn needed.
    if any(mk in src for mk in ("BedrockAgentCoreApp", "@app.entrypoint")):
        return None
    # FastMCP servers self-serve via mcp.run(...)/.run(transport=...).
    if protocol == "MCP" and re.search(r"\.run\(\s*(transport\s*=|\))", src):
        return None
    # Plain ASGI app (HTTP/AGUI/A2A — all three CLI templates use FastAPI+uvicorn) must
    # bootstrap a server itself.
    if "uvicorn.run" in src or "app.run(" in src:
        return None
    rel = f.relative_to(project_path).as_posix()
    fix = (
        '    if __name__ == "__main__":\n'
        "        mcp.run()"
        if protocol == "MCP" else
        '    if __name__ == "__main__":\n'
        "        import uvicorn\n"
        '        uvicorn.run(app, host="0.0.0.0", port=8080)'
    )
    return (
        f"Entrypoint '{rel}' (protocol={protocol}) defines a handler/app but nothing starts "
        f"a server - the container will exit before /ping binds (surfaces as a 30s init "
        f"timeout). Add:\n{fix}"
    )


def _check_import_reachability(project_path: Path, entrypoint: str | None) -> str | None:
    """Verify the entrypoint's absolute first-party imports actually resolve from the zip
    root (/var/task == the codeLocation dir at runtime, which is project_path when
    codeLocation is the default "./").

    Python only searches /var/task itself, not its subdirectories - a package nested one
    level deeper (e.g. backend/hrbot/) is invisible to `from hrbot... import ...` even
    though the file exists on disk. This surfaced in practice as a ModuleNotFoundError
    only at DEPLOY time; catch it here, at BUILD/validate time, with the exact fix.
    Returns an error string (with the PYTHONPATH fix) if a mismatch is found; else None.
    """
    f = _entrypoint_file(project_path, entrypoint)
    if not f:
        return None
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return None

    top_level_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level_names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level_names.add(node.module.split(".")[0])

    # Imports resolve from the zip root, which is the codeLocation dir — not necessarily
    # the project root.
    zip_root = _code_location(project_path)
    for name in top_level_names:
        if (zip_root / name).exists() or (zip_root / f"{name}.py").exists():
            continue  # resolves fine from the zip root - nothing to check
        matches = [
            p for p in zip_root.rglob(name)
            if p.is_dir() and any(p.glob("*.py"))
            and not any(part in _SKIP_DIRS for part in p.parts)
        ]
        if matches:
            nested_at = matches[0].parent.relative_to(zip_root).as_posix()
            try:
                ep_rel = f.relative_to(zip_root).as_posix()
            except ValueError:
                ep_rel = f.name
            return (
                f"Entrypoint '{ep_rel}' does `import {name}`, "
                f"but '{name}/' lives under '{nested_at}/', not at the zip root - "
                f"unreachable from /var/task at runtime. Either set codeLocation to "
                f"'./{nested_at}' (and make the entrypoint relative to it), or add "
                f"PYTHONPATH=/var/task/{nested_at} to agentcore.json's envVars."
            )
    return None


def _detect_secret_dependency(project_path: Path) -> str | None:
    """Detect whether the app reads AWS Secrets Manager at runtime.

    Returns the secret ARN/id the app targets (from agentcore.json envVars or a
    SECRETS_MANAGER_SECRET_PATH env) if a dependency is found, an empty string if a
    dependency is found but no ARN is discoverable, or None if the app uses no secret.
    The runtime's IAM role needs `secretsmanager:GetSecretValue` on that ARN — a
    missing grant surfaces as a runtime 500, not a deploy error, so we flag it early.
    """
    uses_secret = False
    secret_ref = ""

    # 1) Code marker: any first-party file calling get_secret_value / SecretsManager.
    for py in _iter_agent_py(project_path):
        try:
            content = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "get_secret_value" in content or "SECRETS_MANAGER_" in content or "secretsmanager" in content:
            uses_secret = True
            break

    # 2) Config: a SECRETS_MANAGER_SECRET_PATH env var pins the ARN.
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for item in (cfg.get("runtimes") or [{}])[0].get("envVars") or []:
                if isinstance(item, dict) and item.get("name") == "SECRETS_MANAGER_SECRET_PATH":
                    uses_secret = True
                    secret_ref = str(item.get("value") or "")
        except (json.JSONDecodeError, OSError):
            pass

    return secret_ref if uses_secret else None


def _check_bedrock_models(project_path: Path) -> tuple[list[str], list[str]]:
    """Inspect BEDROCK_*_MODEL_ID env vars in agentcore.json for usability traps.

    Returns (model_ids_found, warnings). Two runtime-failure classes we catch early:
      - Bare on-demand IDs (e.g. `anthropic.claude-…-v1:0`) that newer Claude models
        reject with `ValidationException: on-demand throughput isn't supported` — they
        require a cross-region INFERENCE-PROFILE id (region prefix like `us.`, e.g.
        `us.anthropic.claude-haiku-4-5-…-v1:0`).
      - Whether the model is enabled in the account/region (can't verify statically —
        emit a reminder; the AccessDeniedException/marketplace-subscribe 503 is opaque).
    """
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if not cfg_path.exists():
        return [], []
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []

    model_ids: list[str] = []
    for item in (cfg.get("runtimes") or [{}])[0].get("envVars") or []:
        if isinstance(item, dict) and str(item.get("name", "")).startswith("BEDROCK_") \
                and str(item.get("name", "")).endswith("_MODEL_ID"):
            v = str(item.get("value") or "").strip()
            if v:
                model_ids.append(v)

    warnings: list[str] = []
    # Region-prefixed inference-profile ids look like "<region>.<vendor>.<model>",
    # e.g. "us.anthropic...", "eu.anthropic...", "apac.anthropic...".
    profile_prefix = re.compile(r"^(us|eu|apac|us-gov)\.", re.IGNORECASE)
    for mid in sorted(set(model_ids)):
        # A bare vendor id (anthropic./meta./amazon. …) with no region prefix is the
        # on-demand form that newer models reject. ARNs (arn:…inference-profile/…) are fine.
        if not mid.startswith("arn:") and not profile_prefix.match(mid) and "." in mid:
            warnings.append(
                f"Bedrock model '{mid}' looks like a bare on-demand id - newer Claude "
                f"models require a cross-region inference-profile id (prefix the region, "
                f"e.g. 'us.{mid}'), else Converse fails with ValidationException "
                f"(on-demand throughput not supported)."
            )
    if model_ids:
        warnings.append(
            "Verify every BEDROCK_*_MODEL_ID is ENABLED in the target account/region "
            "(Bedrock console → Model access), else Converse returns AccessDeniedException "
            "(aws-marketplace:Subscribe) at request time — surfaces as an opaque 503."
        )
    return sorted(set(model_ids)), warnings


# Critical import → the pyproject package that must declare it. Import names here
# match their package names closely, so the check is reliable (no full resolver).
_IMPORT_PKG_CHECKS = [
    ("bedrock_agentcore", "bedrock-agentcore"),  # memory/runtime SDK — the one that silently no-op'd
    ("fastapi", "fastapi"),                       # HTTP app
    ("uvicorn", "uvicorn"),                       # ASGI server
    ("langgraph", "langgraph"),                   # framework
]

# Plausible-but-nonexistent package names — confused for a real SDK's submodule.
# `bedrock_agentcore_memory` shipped in practice: agentcore-memory/SKILL.md documents
# `from bedrock_agentcore.memory import MemorySessionManager` (a submodule of the real
# `bedrock_agentcore` package), but the guessed sibling-package name looks equally
# plausible and isn't caught by _check_dependency_completeness — that check only matches
# the exact token `bedrock_agentcore` with a word boundary, so `bedrock_agentcore_memory`
# (a different identifier) is invisible to it. ModuleNotFoundError at request time, every
# time — this is a hard error, not a warning.
_KNOWN_WRONG_IMPORTS = [
    ("bedrock_agentcore_memory", "bedrock_agentcore.memory",
     "there is no `bedrock_agentcore_memory` package — AgentCore Memory is a submodule of "
     "the `bedrock_agentcore` SDK: `from bedrock_agentcore.memory import MemorySessionManager`"),
]


def _check_known_wrong_imports(project_path: Path) -> list[str]:
    errors: list[str] = []
    for py in _iter_agent_py(project_path):
        try:
            content = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = py.relative_to(project_path).as_posix()
        for wrong, right, note in _KNOWN_WRONG_IMPORTS:
            if re.search(rf"(?m)^\s*(?:import|from)\s+{re.escape(wrong)}\b", content):
                errors.append(f"{rel}: imports `{wrong}`, which does not exist — {note}")
    return errors


def _check_dependency_completeness(project_path: Path) -> list[str]:
    """Catch 'imported but not packaged' → runtime ModuleNotFoundError.

    Scans first-party code for critical third-party imports and verifies each is
    declared in pyproject.toml dependencies (the uv/CodeZip source of truth). Also
    flags a requirements.txt that's a stale SUBSET of pyproject (a packaging landmine).
    This class of bug hit twice: fastapi/uvicorn missing, then bedrock_agentcore
    (memory silently died because the SDK wasn't in the deployed package).
    """
    warnings: list[str] = []
    toml = project_path / "pyproject.toml"
    pyproj = ""
    if toml.exists():
        try:
            pyproj = toml.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            pyproj = ""

    imported: set[tuple[str, str]] = set()
    for py in _iter_agent_py(project_path):
        try:
            content = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token, pkg in _IMPORT_PKG_CHECKS:
            if re.search(rf"(?m)^\s*(?:import|from)\s+{re.escape(token)}\b", content):
                imported.add((token, pkg))

    if not toml.exists():
        if imported:
            warnings.append(
                "No pyproject.toml found, but code imports "
                + ", ".join(sorted(t for t, _ in imported))
                + " — ensure these are packaged into the runtime or they'll ModuleNotFoundError."
            )
        return warnings

    for token, pkg in sorted(imported):
        if pkg.lower() not in pyproj:
            warnings.append(
                f"Code imports `{token}` but `{pkg}` is NOT in pyproject.toml dependencies - "
                f"the deployed runtime will fail with ModuleNotFoundError (e.g. memory silently "
                f"no-ops). Fix: `uv add {pkg}` then `uv sync`, and redeploy."
            )

    # Stale requirements.txt subset — a packaging landmine (pyproject is authoritative for uv).
    req = project_path / "requirements.txt"
    if req.exists() and imported:
        try:
            req_text = req.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            req_text = ""
        stale = sorted({pkg for _, pkg in imported if pkg.lower() in pyproj and pkg.lower() not in req_text})
        if stale:
            warnings.append(
                "requirements.txt is missing runtime deps present in pyproject.toml "
                f"({', '.join(stale)}). pyproject is authoritative for uv/CodeZip — delete "
                "requirements.txt or sync it, so it can't cause a stale/incomplete package."
            )
    return warnings


def _check_otel_dependency(project_path: Path) -> str | None:
    """agentcore-implement calls OTEL mandatory for AgentCore runtimes, and the real
    `agentcore create` CLI always adds `aws-opentelemetry-distro` (verified against actual
    CLI output for both LangGraph and Strands — NOT the generic `opentelemetry-api`/`-sdk`
    package names). Its total absence is a strong signal the project was hand-authored
    instead of scaffolded via the CLI — not a deliberate opt-out, since there is no
    supported way to opt out. Warn only (not a hard error): this doesn't block a working
    deploy, but its absence is a reliable tell for OTHER hand-authoring mismatches
    (entrypoint shape, agentcore.json schema) worth checking before shipping.
    """
    toml = project_path / "pyproject.toml"
    if not toml.exists():
        return None
    try:
        text = toml.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return None
    if "aws-opentelemetry-distro" in text:
        return None
    return (
        "No `aws-opentelemetry-distro` dependency found - agentcore-implement calls OTEL "
        "mandatory for AgentCore runtimes, and the real `agentcore create` CLI always adds "
        "it. Its absence usually means this project was hand-authored instead of scaffolded "
        "via `agentcore_scaffold.py`/`agentcore create` - worth checking the entrypoint and "
        "agentcore.json for other CLI-schema mismatches too. Fix: "
        "`uv add aws-opentelemetry-distro` (add the matching "
        "`opentelemetry-instrumentation-<framework>` package too if one exists for your "
        "framework)."
    )


def _validate_prereqs_core(project_path: Path) -> dict:
    """Pure check — returns the result dict, never emits/exits. `validate_prereqs` (the
    CLI subcommand) wraps this with `_emit` + `sys.exit`; `up()`'s s_validate() calls this
    directly so a failure's REAL reason (not just an exit code) reaches the ledger/report."""
    errors: list[str] = []
    warnings: list[str] = []

    # Every agentcore CLI call in this script runs with cwd=project_path, and the CLI
    # itself requires the PROJECT ROOT (which contains an `agentcore/` subdir), not the
    # `agentcore/` subdir itself — running it from inside `agentcore/` silently does
    # nothing useful. Catch the exact shape of that mistake early rather than let it
    # cascade into a confusing "agentcore deploy did nothing" failure downstream.
    if project_path.name == "agentcore" and (project_path / "agentcore.json").exists() \
            and not (project_path / "agentcore").exists():
        errors.append(
            f"--project-path points at the agentcore/ subdirectory itself "
            f"('{project_path}'), not the project root. Pass the project root that "
            f"CONTAINS agentcore/ — e.g. '{project_path.parent}'."
        )

    # Node.js 20+
    node = _run("node --version", check=False, timeout=30)
    node_ver = (node.stdout or "").strip()
    if node.returncode != 0 or not node_ver:
        errors.append("Node.js not found. AgentCore CLI needs Node.js 20+.")
    else:
        m = re.match(r"v(\d+)", node_ver)
        if m and int(m.group(1)) < 20:
            warnings.append(f"Node.js {node_ver} < 20 — AgentCore CLI recommends 20+.")

    # AgentCore CLI
    cli = _run("agentcore --version", check=False, timeout=30)
    cli_ok = cli.returncode == 0
    if not cli_ok:
        errors.append("agentcore CLI not found. Install: npm install -g @aws/agentcore")

    # AWS CLI
    aws = _run("aws --version", check=False, timeout=30)
    if aws.returncode != 0:
        errors.append("AWS CLI not found. Install AWS CLI v2.")

    entrypoint, protocol = _detect_entrypoint_and_protocol(project_path)
    framework = _detect_framework(project_path)
    if not entrypoint:
        errors.append(
            "No protocol contract found (POST /invocations + GET /ping, @app.entrypoint, "
            "or FastMCP). Run the build phase (agentcore-implement) first."
        )
    else:
        # Self-serve check — catches the "defines app but never starts a server" trap
        # that otherwise surfaces only as an opaque 30s cold-start timeout post-deploy.
        self_serve_err = _check_self_serve(project_path, entrypoint, protocol)
        if self_serve_err:
            errors.append(self_serve_err)

        # Import-reachability check — catches a first-party package nested deeper than
        # the zip root (codeLocation stays "./"), which otherwise surfaces only as a
        # ModuleNotFoundError post-deploy.
        import_err = _check_import_reachability(project_path, entrypoint)
        if import_err:
            errors.append(import_err)

    # Secrets Manager dependency — the runtime IAM role needs GetSecretValue, or the
    # app 500s at request time (not at deploy). Warn (can't verify the runtime role
    # pre-deploy) with the exact grant to attach.
    secret_dep = _detect_secret_dependency(project_path)
    if secret_dep is not None:
        resource = secret_dep or "<the secret ARN the app reads>"
        warnings.append(
            "App reads AWS Secrets Manager at runtime — ensure the AgentCore runtime "
            "IAM role has `secretsmanager:GetSecretValue` on the secret, else the app "
            f"returns 500 at request time. Grant: secretsmanager:GetSecretValue on {resource}"
        )

    # Bedrock model usability — bare on-demand ids / not-enabled models fail at request
    # time as an opaque 503; flag both as pre-deploy warnings with the fix.
    model_ids, model_warnings = _check_bedrock_models(project_path)
    warnings.extend(model_warnings)

    # Dependency completeness — an imported SDK not in pyproject → runtime ModuleNotFoundError
    # (this is what made memory silently die). Also flags a stale requirements.txt subset.
    warnings.extend(_check_dependency_completeness(project_path))

    # Known-wrong imports (e.g. bedrock_agentcore_memory) — always a ModuleNotFoundError,
    # never legitimate, so this is an error, not a warning.
    errors.extend(_check_known_wrong_imports(project_path))

    # OTEL is mandatory per agentcore-implement; its total absence is a hand-authoring tell.
    otel_warning = _check_otel_dependency(project_path)
    if otel_warning:
        warnings.append(otel_warning)

    already_scaffolded = (project_path / "agentcore" / "agentcore.json").exists()

    result = {
        "command": "validate-prereqs",
        "node_version": node_ver,
        "agentcore_cli": cli_ok,
        "cli_version": (cli.stdout or "").strip() if cli_ok else None,
        "entrypoint": entrypoint,
        "protocol": protocol,
        "framework": framework,
        "already_scaffolded": already_scaffolded,
        "reads_secret": secret_dep is not None,
        "secret_ref": secret_dep or None,
        "bedrock_model_ids": model_ids,
        "warnings": warnings,
        "errors": errors,
        "passed": not errors,
    }
    return result


def validate_prereqs(args: argparse.Namespace) -> None:
    result = _validate_prereqs_core(Path(args.project_path).resolve())
    _emit(result)
    if result["errors"]:
        sys.exit(1)


# ---------------------------------------------------------------------------
# scaffold  (agentcore create --no-agent + fix config)
# ---------------------------------------------------------------------------

def _pascal_case(name: str) -> str:
    parts = re.split(r"[-_\s]+", name.strip())
    pc = "".join(p[:1].upper() + p[1:] for p in parts if p)
    pc = re.sub(r"[^A-Za-z0-9]", "", pc)
    if pc and not pc[0].isalpha():
        pc = "A" + pc
    return (pc or "Agent")[:23]


def _scaffold_core(project_path: Path, project_name_arg: str | None, profile: str | None,
                    region: str, account_id: str, framework_arg: str | None,
                    protocol_arg: str | None, env_arg: str | None) -> dict:
    """Pure scaffold logic — returns the result dict on every path (including failure,
    via an `error` key) instead of sys.exit'ing, so `up()`'s s_scaffold() gets the REAL
    reason a failure happened, not just an exit code. `scaffold` (the CLI subcommand)
    wraps this with `_emit` + `sys.exit`."""
    project_name = _pascal_case(project_name_arg or project_path.name)

    entrypoint, detected_protocol = _detect_entrypoint_and_protocol(project_path)
    protocol = protocol_arg or detected_protocol
    framework = framework_arg or _detect_framework(project_path)

    agentcore_dir = project_path / "agentcore"
    if (agentcore_dir / "agentcore.json").exists():
        print("  agentcore/ already scaffolded — validating + fixing config only", flush=True)
    else:
        print(f"[1/3] Scaffolding agentcore config (project={project_name}, protocol={protocol})", flush=True)
        # --no-agent: config only, never overwrite existing agent code.
        cmd = (
            f"agentcore create --name {project_name} --project-name {project_name} "
            f"--framework {framework} --model-provider Bedrock --memory none "
            f"--build CodeZip --language Python --protocol {protocol} "
            f"--network-mode PUBLIC --skip-git --skip-python-setup --skip-install --no-agent"
        )
        r = _run(cmd, profile=profile, cwd=project_path, check=False, timeout=300)
        if r.returncode != 0:
            err = f"agentcore create failed: {(r.stderr or r.stdout)[:600]}"
            print(f"  ERROR: {err}", flush=True)
            return {"command": "scaffold", "validated": False, "status": "failed", "error": err}
        # The CLI writes into <ProjectName>/agentcore — move it to project root.
        nested = project_path / project_name / "agentcore"
        if nested.exists() and not agentcore_dir.exists():
            import shutil
            shutil.move(str(nested), str(agentcore_dir))
            shutil.rmtree(project_path / project_name, ignore_errors=True)

    # [2/3] Fix agentcore.json — entrypoint, codeLocation, protocol, runtime version.
    print("[2/3] Fixing agentcore.json", flush=True)
    cfg_path = agentcore_dir / "agentcore.json"
    if not cfg_path.exists():
        err = f"{cfg_path} not found after scaffold"
        print(f"  ERROR: {err}", flush=True)
        return {"command": "scaffold", "validated": False, "status": "failed", "error": err}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    runtimes = cfg.get("runtimes") or [{}]
    runtime_version = _detect_runtime_version(project_path)
    rt = runtimes[0]
    # Idempotent: only set the entrypoint when the config doesn't already carry a
    # valid one. A re-run must never overwrite a good "module:attr" entrypoint with
    # a fresh content scan (which could latch onto the wrong file).
    if entrypoint and not _entrypoint_from_config(project_path):
        rt["entrypoint"] = entrypoint
    # Idempotent, like the entrypoint above: preserve a deliberate codeLocation that
    # points into a real subdirectory (used to put a nested agent package at the zip
    # root). Unconditionally resetting to "./" silently reverts that on every re-run
    # and reintroduces the ModuleNotFoundError it was set to fix.
    existing_loc = rt.get("codeLocation")
    if not (existing_loc and existing_loc not in ("./", ".")
            and (project_path / existing_loc).is_dir()):
        rt["codeLocation"] = "./"
    rt["protocol"] = protocol
    rt["build"] = rt.get("build", "CodeZip")
    rt["networkMode"] = rt.get("networkMode", "PUBLIC")
    rt["runtimeVersion"] = runtime_version
    # Runtime env vars in the correct `envVars` schema (idempotent; also normalizes any
    # legacy `environment`/`environmentVariables` key the runtime would silently ignore).
    env_pairs = _parse_env_pairs(env_arg)
    if env_pairs or rt.get("environment") or rt.get("environmentVariables"):
        _apply_env_vars(rt, env_pairs)
    cfg["runtimes"] = runtimes
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # aws-targets.json — target MUST be named "default".
    targets_path = agentcore_dir / "aws-targets.json"
    targets_path.write_text(
        json.dumps([{"name": "default", "account": account_id, "region": region}], indent=2),
        encoding="utf-8",
    )

    # [3/3] Validate
    print("[3/3] Validating agentcore config", flush=True)
    v = _run("agentcore validate", profile=profile, cwd=project_path, check=False, timeout=120)
    valid = v.returncode == 0

    result = {
        "command": "scaffold",
        "project_name": project_name,
        "entrypoint": entrypoint,
        "protocol": protocol,
        "framework": framework,
        "runtime_version": runtime_version,
        "agentcore_json": str(cfg_path),
        "aws_targets": {"account": account_id, "region": region},
        "validated": valid,
        "validate_output": (v.stdout or v.stderr or "").strip()[:600],
        "status": "scaffolded" if valid else "scaffolded_invalid",
        "error": None if valid else "agentcore validate failed — see validate_output",
    }
    return result


def scaffold(args: argparse.Namespace) -> None:
    result = _scaffold_core(
        Path(args.project_path).resolve(), args.project_name, args.profile, args.region,
        args.account_id, args.framework, args.protocol, getattr(args, "env", None),
    )
    _emit(result)
    if not result.get("validated"):
        sys.exit(1)


# These name a *local* AWS identity (a profile in ~/.aws/, or the files backing it) —
# meaningless inside the deployed container, which authenticates via its execution role.
# A container with AWS_PROFILE=<name> and no ~/.aws/ fails credential resolution for
# anything that reads it (boto3, OTEL's exporter). Never let these into agentcore.json.
_LOCAL_ONLY_ENV_KEYS = {"AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"}


def _parse_env_pairs(env_arg: str | None) -> list[dict]:
    """Parse a "K=V,K=V" string into AgentCore's envVars shape [{name, value}].

    Values may contain '=' (e.g. secret ARNs) — only the first '=' splits each pair.
    Pairs are comma-separated; a literal comma inside a value is not supported (none
    of the runtime env values we set contain commas). Local-only AWS identity keys
    (_LOCAL_ONLY_ENV_KEYS) are dropped, not passed through — see its docstring.
    """
    pairs: list[dict] = []
    for chunk in (env_arg or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip()
        if not k:
            continue
        if k in _LOCAL_ONLY_ENV_KEYS:
            print(f"  WARNING: dropping '{k}' from runtime env — it's a local AWS "
                  f"identity pointer, meaningless inside the deployed container "
                  f"(which uses its execution role instead). Not written to agentcore.json.",
                  flush=True)
            continue
        pairs.append({"name": k, "value": v.strip()})
    return pairs


def _apply_env_vars(rt: dict, new_pairs: list[dict]) -> list[dict]:
    """Merge new_pairs into the runtime's `envVars` array (idempotent, by name).

    AgentCore's ONLY accepted schema is `envVars: [{"name": k, "value": v}]`. An
    `environment` object or `environmentVariables` map is SILENTLY IGNORED by the
    runtime — so this normalizes any legacy key it finds into `envVars`.
    """
    # Absorb any legacy shapes so a mis-authored config self-heals.
    existing: dict[str, str] = {}
    legacy = rt.pop("environment", None)
    if isinstance(legacy, dict):
        existing.update({str(k): str(v) for k, v in legacy.items()})
    legacy2 = rt.pop("environmentVariables", None)
    if isinstance(legacy2, dict):
        existing.update({str(k): str(v) for k, v in legacy2.items()})
    for item in rt.get("envVars") or []:
        if isinstance(item, dict) and "name" in item:
            existing[str(item["name"])] = str(item.get("value", ""))
    # New pairs win on conflict.
    for pair in new_pairs:
        existing[pair["name"]] = pair["value"]
    merged = [{"name": k, "value": v} for k, v in existing.items()]
    rt["envVars"] = merged
    return merged


def set_env(args: argparse.Namespace) -> None:
    """Write/merge runtime env vars into agentcore.json in the correct envVars schema."""
    project_path = Path(args.project_path).resolve()
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if not cfg_path.exists():
        _emit({"command": "set-env", "status": "no_config",
               "error": f"{cfg_path} not found — run scaffold first."})
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    runtimes = cfg.get("runtimes") or [{}]
    rt = runtimes[0]
    merged = _apply_env_vars(rt, _parse_env_pairs(args.env))
    cfg["runtimes"] = runtimes
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    _emit({"command": "set-env", "status": "ok",
           "env_var_names": [e["name"] for e in merged],
           "agentcore_json": str(cfg_path)})


def _detect_runtime_version(project_path: Path) -> str:
    """Read requires-python from pyproject.toml → PYTHON_<major>_<minor>."""
    toml = project_path / "pyproject.toml"
    if toml.exists():
        m = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', toml.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return f"PYTHON_{m.group(1)}_{m.group(2)}"
    return "PYTHON_3_12"


def _primary_runtime(project_path: Path) -> dict:
    """Read the first (primary/coordinator) runtime entry from agentcore.json, or {}."""
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if not cfg_path.exists():
        return {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return (cfg.get("runtimes") or [{}])[0] or {}
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# credential-scan  (profile_name= breaks on AgentCore runtime)
# ---------------------------------------------------------------------------

def credential_scan(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    hits: list[dict] = []
    pattern = re.compile(r'profile_name\s*=\s*["\'][^"\']+["\']')

    for py in _iter_agent_py(project_path):
        try:
            content = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                hits.append({"file": py.relative_to(project_path).as_posix(), "line": i, "text": line.strip()})

        if args.fix and pattern.search(content):
            # Replace hardcoded profile with the env-driven default chain (None in runtime).
            new = pattern.sub('profile_name=os.environ.get("AWS_PROFILE")', content)
            if "import os" not in new:
                new = "import os\n" + new
            py.write_text(new, encoding="utf-8")

    result = {
        "command": "credential-scan",
        "fixed": bool(args.fix),
        "hits": hits,
        "clean": not hits or bool(args.fix),
        "note": "AgentCore runtime has no named profiles — code must use the default credential chain (IAM role).",
    }
    _emit(result)
    return result


# ---------------------------------------------------------------------------
# bootstrap  (CDK, one-time per account/region)
# ---------------------------------------------------------------------------

def bootstrap(args: argparse.Namespace) -> None:
    profile, region, account_id = args.profile, args.region, args.account_id
    check = _run(
        f"aws cloudformation describe-stacks --stack-name CDKToolkit --region {region}",
        profile=profile, check=False, timeout=60,
    )
    bootstrapped = check.returncode == 0 and "StackId" in (check.stdout or "")
    if bootstrapped:
        _emit({"command": "bootstrap", "already_bootstrapped": True, "status": "ok"})
        return

    print(f"[1/1] CDK bootstrap aws://{account_id}/{region}", flush=True)
    r = _run(f"npx cdk bootstrap aws://{account_id}/{region}", profile=profile, check=False, timeout=600, stream=True)
    ok = r.returncode == 0
    _emit({"command": "bootstrap", "already_bootstrapped": False, "status": "ok" if ok else "failed"})
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------------------
# deploy / status / invoke / logs / teardown
# ---------------------------------------------------------------------------

def deploy(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    profile, region = args.profile, args.region

    print(f"\n{'='*60}\n  AgentCore Deploy (region={region}, profile={profile})\n{'='*60}\n", flush=True)
    print("[1/2] agentcore deploy -y  (first deploy 3-5 min; blocks until done)", flush=True)
    # Single synchronous call — inherit stdio, no background/poll (per skill guidance).
    r = _run("agentcore deploy -y", profile=profile, cwd=project_path, check=False, timeout=900, stream=True)
    if r.returncode != 0:
        print("  ERROR: deploy failed. Re-run with: AWS_PROFILE=<p> agentcore deploy -v", flush=True)
        _emit({"command": "deploy", "status": "failed", "region": region})
        sys.exit(1)

    print("[2/2] Reading status", flush=True)
    st = _run("agentcore status", profile=profile, cwd=project_path, check=False, timeout=120)
    status_out = (st.stdout or "") + (st.stderr or "")
    runtime_arn = _extract_arn(status_out)
    ready = "READY" in status_out.upper()

    result = {
        "command": "deploy",
        "status": "deployed" if ready else "deployed_not_ready",
        "ready": ready,
        "runtime_arn": runtime_arn,
        "endpoint_url": _runtime_url(runtime_arn, region) if runtime_arn else None,
        "region": region,
        "teardown_command": "agentcore remove all",
    }
    _emit(result)


def _extract_arn(text: str) -> str | None:
    m = _RUNTIME_ARN_RE.search(text)
    return m.group(0) if m else None


def _runtime_url(arn: str, region: str) -> str:
    from urllib.parse import quote
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    )


def status(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    r = _run("agentcore status", profile=args.profile, cwd=project_path, check=False, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    arn = _extract_arn(out)
    _emit({
        "command": "status",
        "ready": "READY" in out.upper(),
        "runtime_arn": arn,
        "endpoint_url": _runtime_url(arn, args.region) if arn else None,
        "raw": out.strip()[:800],
    })


def invoke(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    flag = "--stream" if args.stream else ""
    # `--payload` sends the app's EXACT invoke contract (e.g. '{"message":"hi"}'). Prefer
    # it over `--prompt`, which the CLI wraps as {"prompt": ...} — that 422s a REST app
    # whose contract is a different key (see agentcore-implement rule #7). Pass the raw
    # JSON as the CLI's positional payload arg.
    if getattr(args, "payload", None):
        payload = args.payload
        try:
            json.loads(payload)  # fail fast on malformed JSON before hitting the runtime
        except json.JSONDecodeError as e:
            _emit({"command": "invoke", "ok": False,
                   "error": f"--payload is not valid JSON: {e}", "payload": payload[:400]})
            sys.exit(1)
        # _shell_quote (platform-aware — not a naive single-quote wrap, and not a bare
        # shlex.quote either) — a payload value containing an apostrophe (e.g.
        # '{"message": "what's up"}') would otherwise break out of a hand-rolled '...'
        # wrapper, and shlex.quote's POSIX escaping corrupts the argument under cmd.exe.
        cmd = f"agentcore invoke {_shell_quote(payload)} {flag}".strip()
    else:
        cmd = f"agentcore invoke --prompt {_shell_quote(args.prompt)} {flag}".strip()
    r = _run(cmd, profile=args.profile, cwd=project_path, check=False, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    _emit({"command": "invoke", "ok": r.returncode == 0, "response": out.strip()[:2000]})
    if r.returncode != 0:
        sys.exit(1)


def logs(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    since = f"--since {args.since}" if args.since else ""
    level = f"--level {args.level}" if args.level else ""
    r = _run(f"agentcore logs {since} {level}".strip(), profile=args.profile,
             cwd=project_path, check=False, timeout=120)
    _emit({"command": "logs", "output": ((r.stdout or "") + (r.stderr or "")).strip()[:4000]})


def teardown(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    print("[1/1] agentcore remove all", flush=True)
    r = _run("agentcore remove all", profile=args.profile, cwd=project_path, check=False, timeout=600, stream=True)
    _emit({"command": "teardown", "status": "removed" if r.returncode == 0 else "failed"})
    if r.returncode != 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# up  — single idempotent, resumable orchestrator (the whole deploy in ONE call)
# ---------------------------------------------------------------------------

def _ledger_path(project_path: Path) -> Path:
    return project_path / ".aah" / "deploy" / "agentcore-state.json"


def _read_ledger(project_path: Path) -> dict:
    p = _ledger_path(project_path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"steps": {}}


def _write_ledger(project_path: Path, led: dict) -> None:
    p = _ledger_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(led, indent=2), encoding="utf-8")


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _call_inproc(fn, ns) -> tuple[bool, str]:
    """Run an in-module subcommand fn(ns); it signals failure via sys.exit(!=0).
    Capture that as (ok, note) so one failing step doesn't abort the whole orchestrator."""
    try:
        fn(ns)
        return True, ""
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code in (None, "") else 1)
        return code == 0, f"exit={code}"


def up(args: argparse.Namespace) -> None:
    """Run the entire AgentCore deploy as ONE idempotent, resumable command.

    The SCRIPT owns the sequence (matching the ecs_deploy/cloudrun_deploy 'one invocation'
    contract) — not the agent turn-by-turn. A progress ledger (.aah/deploy/agentcore-state.json)
    records completed steps; a re-run SKIPS them (and each step is idempotent anyway). On the
    first failing step it stops with a clear JSON result so the agent can diagnose THAT step,
    then re-run `up` to resume. `--force` clears the ledger and re-runs everything.
    """
    project_path = Path(args.project_path).resolve()
    profile, region, account_id = args.profile, args.region, args.account_id
    led = {"steps": {}} if args.force else _read_ledger(project_path)
    led.setdefault("steps", {})

    def done(step: str) -> bool:
        return led["steps"].get(step) == "done"

    def mark(step: str, status: str, **extra) -> None:
        led["steps"][step] = status
        led.update(extra)
        _write_ledger(project_path, led)

    def _add_warnings(tag: str, ws: list[str]) -> None:
        """Roll a step's warnings into led['warnings'] immediately (not just at the end) —
        a poller reading the ledger mid-run sees them as soon as the step finishes, and
        they survive into up()'s final report instead of being lost in collapsed stdout."""
        if not ws:
            return
        led.setdefault("warnings", []).extend(f"[{tag}] {w}" for w in ws)

    # ---- step definitions (name, thunk -> (ok, note)) ------------------------
    def s_validate():
        # Calls the pure core directly (not the CLI wrapper) so a failure's REAL reason
        # reaches `note` — the old _call_inproc path only ever saw "exit=1".
        result = _validate_prereqs_core(project_path)
        _add_warnings("validate", result.get("warnings") or [])
        if result["errors"]:
            return False, "; ".join(result["errors"])[:400]
        return True, ""

    def s_scaffold():
        result = _scaffold_core(
            project_path, args.project_name, profile, region, account_id,
            None, None, args.env,
        )
        if not result.get("validated"):
            return False, (result.get("error") or "scaffold failed")[:400]
        return True, ""

    def s_memory():
        if not args.memory_strategies:
            return True, "no memory in scope"
        name = args.memory_name or (_pascal_case(args.project_name or project_path.name) + "Memory")
        r = _run(f'agentcore add memory --name {name} --strategies "{args.memory_strategies}"',
                 profile=profile, cwd=project_path, check=False, timeout=300)
        led["memory_name"] = name
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "")[:300]
        # The CLI injects MEMORY_<NAME>_ID (uppercased resource name) — but app code
        # written at BUILD time can't reliably predict the exact resource name deploy
        # will choose (this happened in practice: build guessed a thematic name, deploy
        # derived a different one from the project's technical name, and neither matched
        # — MEMORY_ID Sessions 500). Alias the CLI's value to a stable, predictable
        # MEMORY_ID so app code has one name it can always rely on, regardless of what
        # the actual resource ends up named.
        cfg_path = project_path / "agentcore" / "agentcore.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            runtimes = cfg.get("runtimes") or [{}]
            rt = runtimes[0]
            env_key = f"MEMORY_{re.sub(r'[^A-Za-z0-9]', '', name).upper()}_ID"
            value = next((e.get("value") for e in (rt.get("envVars") or [])
                         if e.get("name") == env_key and e.get("value")), None)
            if value:
                _apply_env_vars(rt, [{"name": "MEMORY_ID", "value": value}])
                cfg["runtimes"] = runtimes
                cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass  # best-effort alias — the CLI-native MEMORY_<NAME>_ID still works either way
        return True, (r.stderr or r.stdout or "")[:300]

    def s_credscan():
        result = credential_scan(_ns(project_path=str(project_path), fix=True))
        hits = (result or {}).get("hits") or []
        if hits:
            _add_warnings("credential-scan", [
                f"auto-fixed hardcoded profile_name= at {h['file']}:{h['line']}" for h in hits
            ])
        return True, ""

    def s_bootstrap():
        return _call_inproc(bootstrap, _ns(account_id=account_id, region=region, profile=profile))

    def s_sync():
        r = _run("uv sync", cwd=project_path, check=False, timeout=600)
        return r.returncode == 0, (r.stderr or "")[:300]

    def _resolve_runtime_arn() -> str | None:
        """Resolve the runtime ARN.

        Prefer the structured `agentcore/.cli/deployed-state.json` (the same file
        s_memory_iam reads for roleArn) — it carries `runtimeArn` reliably. The
        `agentcore status` text is line-wrapped/cluttered and the regex scrape has
        been observed to miss the ARN even when the runtime is deployed and READY,
        so it is only a fallback.
        """
        # A multi-runtime "multi-agent distributed" project (agentcore-implement Step 2d)
        # has one deployed-state entry per specialist PLUS the coordinator. Scanning dict
        # values in arbitrary order could wire the proxy/frontend to a specialist instead
        # of the coordinator — prefer the primary runtime's name from agentcore.json.
        preferred_name = _primary_runtime(project_path).get("name")
        state = project_path / "agentcore" / ".cli" / "deployed-state.json"
        if state.exists():
            try:
                blob = json.loads(state.read_text(encoding="utf-8"))
                for tgt in (blob.get("targets") or {}).values():
                    rt = ((tgt.get("resources") or {}).get("runtimes") or {})
                    items = list(rt.items())
                    if preferred_name and preferred_name in rt:
                        items = [(preferred_name, rt[preferred_name])] + \
                                [(k, v) for k, v in items if k != preferred_name]
                    for _name, r in items:
                        # Don't bet on the exact key name (runtimeArn / agentRuntimeArn /
                        # arn vary by CLI version) — take the first value on the runtime
                        # object that is a runtime ARN.
                        for v in (r or {}).values():
                            if isinstance(v, str) and re.match(
                                    r"arn:aws:bedrock-agentcore:[^\s\"']+runtime/", v):
                                return v
            except (json.JSONDecodeError, OSError):
                pass
        # Fallback: scrape `agentcore status` text.
        st = _run("agentcore status", profile=profile, cwd=project_path, check=False, timeout=120)
        return _extract_arn((st.stdout or "") + (st.stderr or ""))

    def _runtime_ready() -> bool:
        """True only if `agentcore status` reports the runtime READY."""
        st = _run("agentcore status", profile=profile, cwd=project_path, check=False, timeout=120)
        return "READY" in ((st.stdout or "") + (st.stderr or "")).upper()

    def s_deploy():
        ok, note = _call_inproc(deploy, _ns(project_path=str(project_path), profile=profile, region=region))
        if not ok:
            return ok, note
        # The deploy COMMAND succeeding is not enough — the runtime may still be CREATING.
        # Treat not-READY as a step failure so `up` stops here (and a resume re-checks)
        # rather than running memory-IAM / proxy against a runtime that isn't serving.
        if not _runtime_ready():
            return False, "agentcore deploy completed but the runtime is not READY yet (still initializing). Re-run `up` to resume once it settles."
        # Runtime exists + ready → capture ARN + endpoint into the ledger NOW (frontend-
        # independent). Backend-only deploys must still report runtime_arn/endpoint_url.
        arn = _resolve_runtime_arn()
        if arn:
            led["runtime_arn"] = arn
            led["endpoint_url"] = _runtime_url(arn, region)
        # READY only means the container answers /ping — it does NOT prove the protocol
        # contract works (e.g. a strict payload schema that 422s every real caller). Run
        # one real invoke as a smoke test. Non-fatal by design (a warning, not a failed
        # step): a deliberately non-defensive contract or an AG-UI-only handler can
        # legitimately reject a bare --prompt, so this only surfaces the signal —
        # agentcore-implement rule #7 is what makes a compliant app pass it.
        smoke_flag = " --stream" if _primary_runtime(project_path).get("protocol") == "AGUI" else ""
        smoke = _run(f"agentcore invoke --prompt {_shell_quote('AAH deploy smoke test')}{smoke_flag}",
                     profile=profile, cwd=project_path, check=False, timeout=120)
        smoke_out = ((smoke.stdout or "") + (smoke.stderr or "")).strip()[:500]
        led["invoke_smoke_test"] = {"ok": smoke.returncode == 0, "response": smoke_out}
        if smoke.returncode != 0:
            print(f"  WARNING: post-deploy invoke smoke test failed (see ledger.invoke_smoke_test) — "
                  f"the runtime is READY but may not answer requests correctly:\n  {smoke_out}", flush=True)
        return True, note

    def s_memory_iam():
        # Best-effort: ensure the runtime execution role can reach the memory data plane.
        # `agentcore deploy` (CDK) usually grants this; we attach a WILDCARD on the account's
        # memory resources idempotently as a backstop (never hand-name actions — SDK method
        # names are NOT IAM action names). Never fails the deploy (best-effort).
        if not args.memory_strategies:
            return True, "no memory in scope"
        state = project_path / "agentcore" / ".cli" / "deployed-state.json"
        role_arn = None
        if state.exists():
            try:
                blob = json.loads(state.read_text(encoding="utf-8"))
                for tgt in (blob.get("targets") or {}).values():
                    rt = ((tgt.get("resources") or {}).get("runtimes") or {})
                    for r in rt.values():
                        role_arn = r.get("roleArn") or role_arn
            except (json.JSONDecodeError, OSError):
                pass
        if not role_arn or ":role/" not in role_arn:
            return True, "runtime role not found in deployed-state; relying on CDK grant"
        role_name = role_arn.split(":role/", 1)[1]
        policy = ('{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                  '"Action":"bedrock-agentcore:*",'
                  f'"Resource":"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/*"}}]}}')
        r = _run(f"aws iam put-role-policy --role-name {role_name} --policy-name runtime-memory "
                 f"--policy-document '{policy}' --region {region}",
                 profile=profile, check=False, timeout=60)
        return True, ("granted" if r.returncode == 0 else f"grant best-effort failed: {(r.stderr or '')[:150]}")

    def s_proxy():
        if str(args.frontend).lower() not in ("yes", "true", "1"):
            return True, "no browser frontend"
        # Reuse the ARN captured by s_deploy; re-resolve only if missing (e.g. resume).
        runtime_arn = led.get("runtime_arn") or _resolve_runtime_arn()
        if not runtime_arn:
            return False, "could not resolve runtime ARN from `agentcore status`"
        led["runtime_arn"] = runtime_arn
        # Only pass --profile when set (bare `--profile None` would fail every aws call).
        prof = f" --profile {profile}" if profile else ""
        sec = f" --secret-arn {args.secret_arn}" if args.secret_arn else ""
        # AMPLIFY_ORIGIN travels in the runtime --env string (per the SKILL) but the proxy
        # Lambda is a SEPARATE resource with its own env — forward it explicitly so the
        # Lambda's CORS header is scoped to the real frontend origin instead of "*". At
        # first deploy this is often still the placeholder from the SKILL (the frontend
        # doesn't exist yet); `agentcore_proxy update-origin` reconciles it once the real
        # Amplify URL is known (see aah-deploy SKILL Step 1.7e).
        origin = next((p["value"] for p in _parse_env_pairs(args.env) if p["name"] == "AMPLIFY_ORIGIN"), None)
        org = f' --allowed-origin "{origin}"' if origin else ""
        proxy_project_name = _pascal_case(args.project_name or project_path.name)
        r = _run(f'aah run core.deploy.agentcore_proxy deploy --project-name "{proxy_project_name}" '
                 f'--runtime-arn "{runtime_arn}" '
                 f'--region {region}{prof} --work-dir "{project_path}/proxy_lambda"{sec}{org}',
                 profile=profile, cwd=project_path, check=False, timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        mep = re.search(r'"api_endpoint":\s*"([^"]+)"', out)
        if mep:
            led["api_endpoint"] = mep.group(1)
        return r.returncode == 0 and bool(mep), out[-400:]

    steps = [
        ("validate", s_validate),
        ("scaffold", s_scaffold),
        ("memory", s_memory),
        ("credential-scan", s_credscan),
        ("bootstrap", s_bootstrap),
        ("uv-sync", s_sync),
        ("deploy", s_deploy),
        ("memory-iam", s_memory_iam),
        ("proxy", s_proxy),
    ]

    for name, thunk in steps:
        if done(name):
            print(f"[skip] {name} — already done (ledger)", flush=True)
            continue
        print(f"\n=== up: {name} ===", flush=True)
        ok, note = thunk()
        if not ok:
            mark(name, "failed", failed_note=note)
            _emit({"command": "up", "status": "failed", "failed_step": name,
                   "detail": note, "warnings": led.get("warnings") or [], "ledger": led,
                   "hint": f"Fix the '{name}' step, then re-run `up` to resume (completed steps skip)."})
            sys.exit(1)
        mark(name, "done")

    _emit({"command": "up", "status": "deployed",
           "runtime_arn": led.get("runtime_arn"),
           "endpoint_url": led.get("endpoint_url"),
           "ready": True,  # all steps passed (a failed step exits earlier with status=failed)
           "memory_name": led.get("memory_name"),
           "api_endpoint": led.get("api_endpoint"),
           "frontend_env": {"VITE_API_ENDPOINT": led.get("api_endpoint")} if led.get("api_endpoint") else {},
           # "deployed"/"ready" only mean the container answers /ping — invoke_smoke_test
           # is the one signal that the protocol contract actually answers a real request.
           # ok=false here means investigate before calling the deploy done, even though
           # every ledger step passed.
           "invoke_smoke_test": led.get("invoke_smoke_test"),
           # Rolled up from validate/scaffold/credential-scan as each step finished — the
           # ONLY place these are guaranteed to surface; the steps' own intermediate
           # prints are collapsed Bash output the caller must not rely on seeing.
           "warnings": led.get("warnings") or [],
           "ledger": led})


def main() -> None:
    p = argparse.ArgumentParser(description="AgentCore Runtime deploy (standardized CLI wrapper)")
    sub = p.add_subparsers(dest="command", required=True)

    up_p = sub.add_parser("up", help="Run the WHOLE deploy idempotently in one call (resumable via ledger)")
    up_p.add_argument("--project-path", required=True)
    up_p.add_argument("--project-name", default=None)
    up_p.add_argument("--account-id", required=True)
    up_p.add_argument("--region", default="us-east-1")
    up_p.add_argument("--profile", default=None)
    up_p.add_argument("--env", default=None, help='Runtime env vars "K=V,K=V" (models, secret path, origin)')
    up_p.add_argument("--memory-name", default=None, help="Memory resource name (omit -> <Project>Memory)")
    up_p.add_argument("--memory-strategies", default=None,
                      help="CSV of strategies (e.g. SEMANTIC,SUMMARIZATION); omit to skip memory")
    up_p.add_argument("--secret-arn", default=None, help="Secrets Manager ARN for proxy edge auth")
    up_p.add_argument("--frontend", default="no", help="yes -> deploy the API-Gateway signing proxy")
    up_p.add_argument("--force", action="store_true", help="Clear the ledger and re-run every step")

    vp = sub.add_parser("validate-prereqs", help="Check node/CLI + detect entrypoint/protocol/framework")
    vp.add_argument("--project-path", required=True)

    sc = sub.add_parser("scaffold", help="agentcore create --no-agent + fix agentcore.json/aws-targets.json")
    sc.add_argument("--project-path", required=True)
    sc.add_argument("--project-name", default=None)
    sc.add_argument("--framework", default=None,
                    help="LangChain_LangGraph|Strands|GoogleADK|OpenAIAgents (auto-detect if omitted)")
    sc.add_argument("--protocol", default=None, help="HTTP|AGUI|MCP|A2A (auto-detect if omitted)")
    sc.add_argument("--account-id", required=True)
    sc.add_argument("--region", required=True)
    sc.add_argument("--profile", default=None)
    sc.add_argument("--env", default=None,
                    help='Runtime env vars as "K=V,K=V" (written to envVars[]; e.g. '
                         '"BEDROCK_REGION=us-east-1,MEMORY_ID=mem-123")')

    se = sub.add_parser("set-env", help="Write/merge runtime env vars into agentcore.json (envVars schema)")
    se.add_argument("--project-path", required=True)
    se.add_argument("--env", required=True,
                    help='Runtime env vars as "K=V,K=V" — merged idempotently by name')

    cs = sub.add_parser("credential-scan", help="Find/fix hardcoded profile_name= (breaks on runtime)")
    cs.add_argument("--project-path", required=True)
    cs.add_argument("--fix", action="store_true")

    bs = sub.add_parser("bootstrap", help="CDK bootstrap (one-time per account/region)")
    bs.add_argument("--account-id", required=True)
    bs.add_argument("--region", required=True)
    bs.add_argument("--profile", default=None)

    for name, helptext in [("deploy", "agentcore deploy -y"), ("status", "agentcore status"),
                           ("teardown", "agentcore remove all")]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--project-path", required=True)
        sp.add_argument("--profile", default=None)
        sp.add_argument("--region", default="us-east-1")

    iv = sub.add_parser("invoke", help="agentcore invoke")
    iv.add_argument("--project-path", required=True)
    iv.add_argument("--profile", default=None)
    iv.add_argument("--region", default="us-east-1")
    iv.add_argument("--prompt", default="Hello",
                    help="Convenience: sends {\"prompt\": <text>}. For a REST app whose contract "
                         "is a different key, use --payload instead.")
    iv.add_argument("--payload", default=None,
                    help="Raw JSON body matching the app's EXACT invoke contract, e.g. "
                         "'{\"message\":\"hi\"}'. Takes precedence over --prompt (avoids 422).")
    iv.add_argument("--stream", action="store_true")

    lg = sub.add_parser("logs", help="agentcore logs")
    lg.add_argument("--project-path", required=True)
    lg.add_argument("--profile", default=None)
    lg.add_argument("--region", default="us-east-1")
    lg.add_argument("--since", default="30m")
    lg.add_argument("--level", default=None, help="error|warn|info")

    args = p.parse_args()
    {
        "up": up,
        "validate-prereqs": validate_prereqs,
        "scaffold": scaffold,
        "set-env": set_env,
        "credential-scan": credential_scan,
        "bootstrap": bootstrap,
        "deploy": deploy,
        "status": status,
        "invoke": invoke,
        "logs": logs,
        "teardown": teardown,
    }[args.command](args)


if __name__ == "__main__":
    main()
