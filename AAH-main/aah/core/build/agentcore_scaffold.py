#!/usr/bin/env python3
"""
Build-phase AgentCore scaffolding helper — the deterministic slice of the
agentcore-implement skill.

The skill owns the *reasoning* (which protocol/framework, writing the AG-UI SSE
layer, A2A adapters, adapt-mode contract injection). This helper owns the
*predictable* CLI: run `agentcore create`, restructure the CLI's nested output to
the project root, fix `agentcore.json`, and validate. The feature-implementer
subagent calls these instead of typing raw `agentcore` commands.

Usage:
    aah run core.build.agentcore_scaffold scaffold --project-path /path \
        --project-name QuizBot --framework LangChain_LangGraph --protocol HTTP \
        [--no-agent] [--profile default]
    aah run core.build.agentcore_scaffold restructure --project-path /path --project-name QuizBot
    aah run core.build.agentcore_scaffold fix-config --project-path /path [--entrypoint main.py] [--protocol HTTP]
    aah run core.build.agentcore_scaffold validate --project-path /path [--profile default]

Notes:
    - Scaffold mode (default) generates code+config. Adapt mode (--no-agent) makes
      config only and never overwrites existing agent code.
    - Code moves into src/ (matching AAH's project layout) — the whole generated
      tree moves there as one unit, so the CLI's sibling-relative imports (e.g.
      `from model.load import load_model`) still resolve. `codeLocation` stays
      "./" (project root); `entrypoint` carries the "src/" prefix instead.
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


_IS_WINDOWS = platform.system() == "Windows"

_FRAMEWORK_FLAGS = {"LangChain_LangGraph", "Strands", "GoogleADK", "OpenAIAgents"}
_PROTOCOL_FLAGS = {"HTTP", "AGUI", "MCP", "A2A"}


def _run(cmd: str, profile: str | None, cwd: Path | None, timeout: int = 300,
         check: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"
    if profile:
        env["AWS_PROFILE"] = profile
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          shell=True, cwd=str(cwd) if cwd else None, env=env, check=check)


def _emit(result: dict) -> None:
    print("---SCAFFOLD_RESULT_JSON---")
    json.dump(result, sys.stdout, indent=2)
    print()


def _pascal_case(name: str) -> str:
    parts = re.split(r"[-_\s]+", name.strip())
    pc = re.sub(r"[^A-Za-z0-9]", "", "".join(p[:1].upper() + p[1:] for p in parts if p))
    if pc and not pc[0].isalpha():
        pc = "A" + pc
    return (pc or "Agent")[:23]


def _runtime_version(project_path: Path) -> str:
    toml = project_path / "pyproject.toml"
    if toml.exists():
        m = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"',
                      toml.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return f"PYTHON_{m.group(1)}_{m.group(2)}"
    return "PYTHON_3_12"


def scaffold(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    project_name = _pascal_case(args.project_name or project_path.name)
    framework = args.framework if args.framework in _FRAMEWORK_FLAGS else "Strands"
    protocol = args.protocol if args.protocol in _PROTOCOL_FLAGS else "HTTP"
    # AGUI is a native CLI protocol on current agentcore-cli versions (verified: it
    # generates a correct, self-serving, framework-specific template — ag_ui_strands /
    # ag_ui_langgraph / etc. — with a real uvicorn.run(...)). Pass it straight through
    # instead of downgrading to HTTP; hand-authoring the SSE layer afterward is what
    # produced a broken, non-self-serving entrypoint in practice. If a framework's CLI
    # support ever regresses, `agentcore create` fails loudly (r.returncode != 0) rather
    # than silently producing the wrong template.
    no_agent = args.no_agent

    flags = (
        f"--name {project_name} --project-name {project_name} "
        f"--framework {framework} --model-provider Bedrock --memory none "
        f"--build CodeZip --language Python --protocol {protocol} "
        f"--network-mode PUBLIC --skip-git"
    )
    if no_agent:
        flags += " --skip-python-setup --skip-install --no-agent"

    print(f"[1/2] agentcore create ({'adapt/config-only' if no_agent else 'scaffold'}, "
          f"framework={framework}, protocol={protocol})", flush=True)
    r = _run(f"agentcore create {flags}", args.profile, project_path)
    if r.returncode != 0:
        print(f"  ERROR: agentcore create failed:\n{(r.stderr or r.stdout)[:600]}", flush=True)
        _emit({"command": "scaffold", "status": "failed",
               "error": (r.stderr or r.stdout).strip()[:600]})
        sys.exit(1)

    print("[2/2] Restructuring output to project root", flush=True)
    moved = _restructure(project_path, project_name, code=not no_agent)

    _emit({
        "command": "scaffold",
        "project_name": project_name,
        "framework": framework,
        "protocol": protocol,
        "mode": "adapt" if no_agent else "scaffold",
        "moved": moved,
        "status": "scaffolded",
        "next": "For AGUI/HTTP/MCP/A2A the CLI template is already correct and self-serving — "
                "do NOT hand-write a streaming/SSE layer on top of it. Adapt any business logic "
                "into the generated file, then fix-config + validate.",
    })


def _restructure(project_path: Path, project_name: str, code: bool) -> dict:
    """Move agentcore/ to the project root; move agent code (scaffold mode) into src/."""
    nested = project_path / project_name
    moved = {"agentcore": False, "code": False, "venv": False}
    if not nested.exists():
        return moved

    src_agentcore = nested / "agentcore"
    if src_agentcore.exists() and not (project_path / "agentcore").exists():
        shutil.move(str(src_agentcore), str(project_path / "agentcore"))
        moved["agentcore"] = True

    if code:
        app_dir = nested / "app" / project_name
        if app_dir.exists():
            # Move the whole generated tree into src/ as one unit — every file
            # moves together, so the CLI's sibling-relative imports still resolve.
            src_dir = project_path / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            for item in app_dir.iterdir():
                dest = src_dir / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
            moved["code"] = True
        nested_venv = nested / ".venv"
        if nested_venv.exists() and not (project_path / ".venv").exists():
            shutil.move(str(nested_venv), str(project_path / ".venv"))
            moved["venv"] = True

    shutil.rmtree(nested, ignore_errors=True)
    return moved


def _cmd_restructure(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    project_name = _pascal_case(args.project_name or project_path.name)
    moved = _restructure(project_path, project_name, code=not args.config_only)
    _emit({"command": "restructure", "moved": moved, "status": "ok"})


def fix_config(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    cfg_path = project_path / "agentcore" / "agentcore.json"
    if not cfg_path.exists():
        print(f"  ERROR: {cfg_path} not found", flush=True)
        _emit({"command": "fix-config", "status": "missing_config"})
        sys.exit(1)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    runtimes = cfg.get("runtimes") or [{}]
    rt = runtimes[0]

    entrypoint = args.entrypoint
    if not entrypoint:
        # Prefer an existing file referenced by the config, else main.py — resolved
        # under src/, since scaffold mode moves generated code there. codeLocation
        # stays "./" (project root); the "src/" prefix lives on entrypoint instead,
        # so callers that check file existence relative to project_path (e.g.
        # agentcore_deploy.py's _entrypoint_from_config) keep working unchanged.
        current = rt.get("entrypoint", "")
        base = os.path.basename(current) if current else "main.py"
        src_dir = project_path / "src"
        entrypoint = f"src/{base}" if (src_dir / base).exists() else (
            "src/main.py" if (src_dir / "main.py").exists() else current or "main.py"
        )
    rt["entrypoint"] = entrypoint
    rt["codeLocation"] = "./"
    if args.protocol:
        rt["protocol"] = args.protocol
    rt.setdefault("protocol", "HTTP")
    rt["build"] = rt.get("build", "CodeZip")
    rt["networkMode"] = rt.get("networkMode", "PUBLIC")
    rt["runtimeVersion"] = rt.get("runtimeVersion") or _runtime_version(project_path)
    cfg["runtimes"] = runtimes
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    _emit({
        "command": "fix-config",
        "entrypoint": entrypoint,
        "protocol": rt["protocol"],
        "runtime_version": rt["runtimeVersion"],
        "status": "fixed",
    })


def validate(args: argparse.Namespace) -> None:
    project_path = Path(args.project_path).resolve()
    r = _run("agentcore validate", args.profile, project_path, timeout=120)
    schema_valid = r.returncode == 0

    # Schema-valid does not mean the entrypoint actually starts a server — a hand-authored
    # handler (e.g. a Lambda-shaped `def lambda_handler(event, context)` with no
    # uvicorn.run/app.run/mcp.run) can reference a syntactically fine file and still never
    # bind /ping, surfacing only as an opaque 30s init timeout at DEPLOY time. Catch that
    # here too, at BUILD time, right after scaffolding — this is the same check
    # `agentcore_deploy.py validate-prereqs` runs later; duplicating the call (not the
    # logic — imported from the single source of truth) closes the gap for whichever
    # feature actually writes the entrypoint.
    self_serve_err = None
    import_err = None
    if schema_valid:
        cfg_path = project_path / "agentcore" / "agentcore.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            rt = (cfg.get("runtimes") or [{}])[0]
            from aah.core.deploy.agentcore_deploy import _check_self_serve, _check_import_reachability
            self_serve_err = _check_self_serve(project_path, rt.get("entrypoint"), rt.get("protocol", "HTTP"))
            import_err = _check_import_reachability(project_path, rt.get("entrypoint"))
        except (json.JSONDecodeError, OSError):
            pass  # config genuinely missing/unreadable — nothing to self-serve-check yet
        except ImportError as e:
            # This is NOT the same as "nothing to check" — the check silently never ran,
            # and `ok = schema_valid and not self_serve_err` would then report `valid:
            # true` on a schema-valid-but-possibly-non-self-serving entrypoint with no
            # indication the self-serve gate was skipped. Surface it instead of no-op'ing.
            print(f"  WARNING: could not import the self-serve/import checks ({e}) — schema "
                  f"validity was confirmed, but whether the entrypoint actually starts a "
                  f"server or its imports resolve was NOT verified this run.", flush=True)

    ok = schema_valid and not self_serve_err and not import_err
    _emit({
        "command": "validate",
        "valid": ok,
        "schema_valid": schema_valid,
        "self_serve_error": self_serve_err,
        "import_reachability_error": import_err,
        "output": ((r.stdout or "") + (r.stderr or "")).strip()[:800],
        "status": "valid" if ok else "invalid",
    })
    if not ok:
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="AgentCore build-phase scaffolding helper")
    sub = p.add_subparsers(dest="command", required=True)

    sc = sub.add_parser("scaffold", help="agentcore create (+ restructure)")
    sc.add_argument("--project-path", required=True)
    sc.add_argument("--project-name", default=None)
    sc.add_argument("--framework", default=None)
    sc.add_argument("--protocol", default="HTTP")
    sc.add_argument("--no-agent", action="store_true", help="Adapt mode: config only, keep existing code")
    sc.add_argument("--profile", default=None)

    rs = sub.add_parser("restructure", help="Move nested CLI output to project root")
    rs.add_argument("--project-path", required=True)
    rs.add_argument("--project-name", default=None)
    rs.add_argument("--config-only", action="store_true")

    fc = sub.add_parser("fix-config", help="Fix agentcore.json entrypoint/codeLocation/protocol/version")
    fc.add_argument("--project-path", required=True)
    fc.add_argument("--entrypoint", default=None)
    fc.add_argument("--protocol", default=None)

    va = sub.add_parser("validate", help="agentcore validate")
    va.add_argument("--project-path", required=True)
    va.add_argument("--profile", default=None)

    args = p.parse_args()
    {
        "scaffold": scaffold,
        "restructure": _cmd_restructure,
        "fix-config": fix_config,
        "validate": validate,
    }[args.command](args)


if __name__ == "__main__":
    main()
