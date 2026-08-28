"""`cloudless dev <agent>` — local runner.

Q13: run the user's agent on 127.0.0.1:<port> with the same HTTP contract
the deployed AgentCore Runtime uses. Real LLM calls (Bedrock) so prompt
iteration feels real; in-memory context for sessions/cost/peer-mocks;
no cloud deploy in the loop.

Implementation: wraps the same `BedrockAgentCoreApp` entrypoint we
generate for deploy, but binds locally and serves from the in-process
user agent class (no Docker, no CodeBuild).

Hot reload, --record/--replay cassettes, multi-agent local topology
defer to later milestones — this is the M1 minimum: one agent, real
Bedrock, real HTTP.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from rich.console import Console

import cloudless

_console = Console()


def _discover_agent_class(agent_name: str, agents_dir: Path) -> type:
    """Walk src/agents/*.py for a class with __cloudless_metadata__.name == agent_name.

    Mirrors the logic in `cloudless.cli.deploy` so dev/deploy share discovery semantics.
    """
    if not agents_dir.is_dir():
        raise FileNotFoundError(f"agents dir not found: {agents_dir}")

    candidates: list[type] = []
    for py_file in agents_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, type) and hasattr(obj, "__cloudless_metadata__"):
                if obj.__cloudless_metadata__.name == agent_name:
                    candidates.append(obj)
    if not candidates:
        raise LookupError(
            f"No @cloudless.agent class named {agent_name!r} in {agents_dir}/*.py"
        )
    if len(candidates) > 1:
        names = [f"{c.__module__}.{c.__name__}" for c in candidates]
        raise LookupError(f"Multiple agents named {agent_name!r}: {names}")
    return candidates[0]


def _build_local_app(agent_class: type, *, session_id: str = "dev-session"):
    """Wrap the user's agent class in a BedrockAgentCoreApp for local serving.

    Two routes:
      POST /invocations         — returns aggregated JSON {chunks, final_text, agent}
      POST /invocations/stream  — returns Server-Sent Events; one chunk per SSE event

    Returns the app object (.run() to start uvicorn).
    """
    # Defer the import so `cloudless dev --help` works without the AWS extra.
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    instance = agent_class()
    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invocations(payload: dict, context):
        prompt = payload.get("prompt", "")
        # Local dev always uses InMemoryContext — Q13. Cloud deploy uses
        # the real Context bridged from AgentCore's RequestContext.
        ctx = cloudless.InMemoryContext(session_id=session_id)
        chunks: list[dict] = []
        async for chunk in instance.query(ctx, prompt):
            chunks.append(chunk.model_dump())
        final_text = "".join(c["text"] for c in chunks if c.get("kind") == "text")
        return {"chunks": chunks, "final_text": final_text,
                "agent": agent_class.__cloudless_metadata__.name}

    # SSE streaming route — adds /invocations/stream
    _attach_sse_route(app, instance, agent_class, session_id=session_id)
    return app


def _attach_sse_route(app, instance, agent_class, *, session_id: str) -> None:
    """Mount POST /invocations/stream as a Server-Sent Events endpoint."""
    import json

    from starlette.requests import Request
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    async def _stream_handler(request: Request) -> StreamingResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        prompt = payload.get("prompt", "")

        async def _events():
            ctx = cloudless.InMemoryContext(session_id=session_id)
            try:
                async for chunk in instance.query(ctx, prompt):
                    data = json.dumps(chunk.model_dump())
                    # SSE format: "event: <kind>\ndata: <json>\n\n"
                    yield f"event: {chunk.kind}\ndata: {data}\n\n".encode()
            except Exception as e:
                err = {"kind": "error", "error": str(e), "recoverable": False}
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
            yield b"event: done\ndata: {}\n\n"

        return StreamingResponse(_events(), media_type="text/event-stream",
                                  headers={
                                      "Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                  })

    # BedrockAgentCoreApp IS a Starlette app — register the route directly.
    if hasattr(app, "add_route"):
        app.add_route("/invocations/stream", _stream_handler, methods=["POST"])
    elif hasattr(app, "router"):
        app.router.routes.append(
            Route("/invocations/stream", _stream_handler, methods=["POST"]),
        )


def run(
    *,
    agent_name: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    project_root: Path | None = None,
    block: bool = True,
    record_cassette: str | None = None,
    replay_cassette: str | None = None,
    reload: bool = False,
) -> int:
    """Entry point for `cloudless dev <agent>`.

    Args:
        record_cassette: Path to cassette JSONL. Real LLM calls + persist.
        replay_cassette: Path to cassette JSONL. Replay-only mode.

    Mutually exclusive: pass at most one of record/replay.
    """
    if record_cassette and replay_cassette:
        _console.print("[red]✗[/] --record and --replay are mutually exclusive")
        return 2

    project_root = (project_root or Path.cwd()).resolve()

    cfg_path = project_root / "cloudless.yaml"
    if cfg_path.is_file():
        from cloudless.config import ConfigValidationError
        from cloudless.config import load as load_cfg
        try:
            cfg = load_cfg(cfg_path)
        except ConfigValidationError as e:
            _console.print("[red]✗[/] cloudless.yaml is invalid:")
            for err in e.errors:
                _console.print(f"   - {err}")
            return 2
        if agent_name not in cfg.agents:
            _console.print(
                f"[yellow]![/] Agent {agent_name!r} not declared in cloudless.yaml. "
                f"Running anyway."
            )

    src_agents = project_root / "src" / "agents"
    src_dir = project_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    try:
        agent_class = _discover_agent_class(agent_name, src_agents)
    except (FileNotFoundError, LookupError) as e:
        _console.print(f"[red]✗[/] {e}")
        return 1

    meta = agent_class.__cloudless_metadata__
    _console.print(f"[bold]cloudless dev[/]  agent=[cyan]{meta.name}[/]  "
                   f"framework=[cyan]{meta.framework or 'custom'}[/]")
    _console.print(f"  serving on  http://{host}:{port}/invocations")
    _console.print(f"  health      http://{host}:{port}/ping")
    if record_cassette:
        _console.print(f"  cassette    [yellow]recording[/] → {record_cassette}")
    if replay_cassette:
        _console.print(f"  cassette    [green]replaying[/] ← {replay_cassette}")
    _console.print("  Ctrl-C to stop")

    app = _build_local_app(agent_class)

    if not block:
        # Used by integration tests that spawn this in a thread.
        return 0

    # `BedrockAgentCoreApp.run()` binds to 0.0.0.0 in containers and
    # 127.0.0.1 locally. We want explicit 127.0.0.1 + custom port.
    import uvicorn
    # The BedrockAgentCoreApp wraps a Starlette app on .app
    starlette_app = getattr(app, "app", None) or app

    if reload:
        return _run_with_reload(
            agent_name=agent_name, host=host, port=port,
            project_root=project_root, src_agents=src_agents,
            record_cassette=record_cassette, replay_cassette=replay_cassette,
        )

    if record_cassette or replay_cassette:
        from cloudless.testing.cassettes import CassetteMode, llm_cassette
        cassette_path = record_cassette or replay_cassette
        cassette_mode = CassetteMode.RECORD if record_cassette else CassetteMode.REPLAY
        with llm_cassette(cassette_path, mode=cassette_mode):
            uvicorn.run(starlette_app, host=host, port=port, log_level="info")
        return 0

    uvicorn.run(starlette_app, host=host, port=port, log_level="info")
    return 0


# --------------------------------------------------------------------- #
# --reload supervisor — parent process watches src/agents for mtime
# changes and re-spawns the child server.
# --------------------------------------------------------------------- #


def _scan_mtimes(root: Path) -> dict[str, float]:
    """Return a {filepath: mtime} snapshot of every .py file under `root`."""
    out: dict[str, float] = {}
    if not root.is_dir():
        return out
    for f in root.rglob("*.py"):
        try:
            out[str(f)] = f.stat().st_mtime
        except OSError:
            pass
    return out


def _run_with_reload(
    *,
    agent_name: str,
    host: str,
    port: int,
    project_root: Path,
    src_agents: Path,
    record_cassette: str | None,
    replay_cassette: str | None,
) -> int:
    """Supervisor: spawn `cloudless dev <agent>` in a child; respawn on file change."""
    import signal
    import subprocess
    import sys
    import time

    cmd = [
        sys.executable, "-m", "cloudless.cli.main", "dev", agent_name,
        "--host", host, "--port", str(port),
    ]
    if record_cassette:
        cmd += ["--record", record_cassette]
    elif replay_cassette:
        cmd += ["--replay", replay_cassette]

    _console.print("[bold]cloudless dev[/]  [yellow]--reload enabled[/]")

    proc: subprocess.Popen | None = None
    mtimes = _scan_mtimes(src_agents)
    try:
        proc = subprocess.Popen(cmd, cwd=str(project_root), env=dict(os.environ))
        while True:
            time.sleep(0.5)
            if proc.poll() is not None:
                return proc.returncode or 1
            current = _scan_mtimes(src_agents)
            if current != mtimes:
                _console.print("[yellow]↻[/] agent source changed — reloading")
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = subprocess.Popen(cmd, cwd=str(project_root), env=dict(os.environ))
                mtimes = current
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0
