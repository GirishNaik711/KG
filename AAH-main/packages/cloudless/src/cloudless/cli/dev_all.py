"""Q13: `cloudless dev --all` — multi-agent local topology.

Spawns every agent declared in cloudless.yaml on consecutive ports and
materializes a local manifest so each agent's `ctx.peer(name).call(...)`
routes to the localhost neighbor instead of a deployed cloud endpoint.

Mechanism:
  1. Parse cloudless.yaml → list of agents
  2. Pick a free port range starting at --base-port (default 8080)
  3. Write a local manifest to .cloudless/dev-manifest.json
  4. Fork one subprocess per agent, passing
       CLOUDLESS_MANIFEST_PATH + CLOUDLESS_DEV_LOCAL=1 in the env
  5. Stream all subprocess stdout/stderr, prefixed with agent name
  6. SIGINT → graceful shutdown of every child
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from rich.console import Console

_console = Console()


def _free_port_from(start: int) -> int:
    """Walk forward until we find an unused TCP port."""
    port = start
    while port < start + 200:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError(f"No free port found between {start} and {start + 200}")


def _build_local_manifest(agents: dict[str, dict], project: str) -> dict:
    """Produce a cloudless-manifest.json content for localhost peer routing."""
    return {
        "project": project,
        "agents": {
            name: {
                "cloud": "local",
                "http_url": f"http://127.0.0.1:{info['port']}/invocations",
                "a2a_url": f"http://127.0.0.1:{info['port']}/a2a",
                "idp_issuer": None,
                "audience": f"local://{name}",
                "residency": [],
            }
            for name, info in agents.items()
        },
    }


def _spawn_agent(
    *,
    agent_name: str,
    port: int,
    manifest_path: Path,
    project_root: Path,
    color: str,
) -> subprocess.Popen:
    """Fork a child `cloudless dev <agent>` process with manifest pinned."""
    env = dict(os.environ)
    env["CLOUDLESS_MANIFEST_PATH"] = str(manifest_path)
    env["CLOUDLESS_DEV_LOCAL"] = "1"
    cmd = [
        sys.executable, "-m", "cloudless.cli.main",
        "dev", agent_name,
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def _pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            _console.print(f"[{color}]{agent_name}[/] {line.rstrip()}")

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    return proc


def run(
    *,
    project_root: Path | None = None,
    base_port: int = 8080,
    block: bool = True,
) -> int:
    """Entry point for `cloudless dev --all`."""
    project_root = (project_root or Path.cwd()).resolve()

    cfg_path = project_root / "cloudless.yaml"
    if not cfg_path.is_file():
        _console.print(f"[red]✗[/] cloudless.yaml not found at {cfg_path}")
        return 1

    from cloudless.config import ConfigValidationError
    from cloudless.config import load as load_cfg
    try:
        cfg = load_cfg(cfg_path)
    except ConfigValidationError as e:
        _console.print("[red]✗[/] cloudless.yaml invalid:")
        for err in e.errors:
            _console.print(f"   - {err}")
        return 2

    if not cfg.agents:
        _console.print("[yellow]No agents declared in cloudless.yaml — nothing to spawn.[/]")
        return 1

    # Assign ports
    agents: dict[str, dict] = {}
    next_port = base_port
    for name in cfg.agents:
        port = _free_port_from(next_port)
        agents[name] = {"port": port}
        next_port = port + 1

    # Write manifest
    manifest_dir = project_root / ".cloudless"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / "dev-manifest.json"
    manifest_path.write_text(
        json.dumps(_build_local_manifest(agents, cfg.project), indent=2)
    )

    _console.print(f"[bold]cloudless dev --all[/]  project=[cyan]{cfg.project}[/]")
    _console.print(f"  manifest    {manifest_path}")
    for name, info in agents.items():
        _console.print(f"  [green]✓[/] {name} → http://127.0.0.1:{info['port']}")
    _console.print()

    if not block:
        # Test mode: return the planned topology without spawning subprocesses
        return 0

    # Color rotation
    palette = ["cyan", "magenta", "yellow", "green", "blue", "red"]
    procs: list[subprocess.Popen] = []
    try:
        for i, (name, info) in enumerate(agents.items()):
            proc = _spawn_agent(
                agent_name=name,
                port=info["port"],
                manifest_path=manifest_path,
                project_root=project_root,
                color=palette[i % len(palette)],
            )
            procs.append(proc)

        # Block until SIGINT or any child dies
        while True:
            for proc in procs:
                if proc.poll() is not None:
                    _console.print(f"[red]✗[/] agent process exited (rc={proc.returncode})")
                    return proc.returncode or 1
            try:
                # Poll every 0.5s
                for _ in range(50):
                    import time as _t
                    _t.sleep(0.01)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass
    finally:
        _console.print()
        _console.print("[yellow]Shutting down all agents...[/]")
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0
