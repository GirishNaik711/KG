"""`cloudless init` — scaffold a project per the Q24 src/ layout convention.

Generates:
  <project>/
    cloudless.yaml
    pyproject.toml
    src/
      agents/
        hello.py
    evals/datasets/hello.jsonl
    tests/test_hello.py
    .cloudless/dev-secrets.yaml.example
    .gitignore
    README.md

No cloud calls. Fully offline. Smoke-testable in unit tests.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from rich.console import Console

_console = Console()


def run(
    project_name: str,
    *,
    framework: str = "langgraph",
    cloud: str = "aws",
    force: bool = False,
    target_dir: Path | None = None,
) -> int:
    """Create the project. Returns 0 on success, 1 on conflict, 2 on error."""
    base = (target_dir or Path.cwd()) / project_name

    if base.exists():
        if not force:
            _console.print(
                f"[red]✗[/] directory [bold]{base}[/] already exists. "
                "Use --force to overwrite."
            )
            return 1
        shutil.rmtree(base)

    base.mkdir(parents=True)

    # cloudless.yaml requires a kebab-case identifier — derive from basename.
    project_slug = base.name
    files = _generate_files(project_slug, framework=framework, cloud=cloud)
    for relpath, content in files.items():
        path = base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    _console.print(f"[green]✓[/] Created {base}/")
    _console.print()
    _console.print("[bold]Next steps:[/]")
    _console.print(f"  cd {project_name}")
    _console.print("  python -m pip install -e .  [dim](or: uv pip install -e .)[/]")
    _console.print("  python -m pytest tests/      [dim](runs the unit test)[/]")
    _console.print("  [dim]# (cloudless deploy ships in M3)[/]")
    return 0


# --------------------------------------------------------------------- #
# Template content — kept as plain strings so the CLI has no Jinja dep.
# --------------------------------------------------------------------- #


def _generate_files(project: str, *, framework: str, cloud: str) -> dict[str, str]:
    """Return a {relpath: file_content} map for the project."""
    files: dict[str, str] = {}

    # cloudless.yaml — per Q10 config model
    files["cloudless.yaml"] = _CLOUDLESS_YAML.format(project=project, cloud=cloud)

    # pyproject.toml — minimal Python project
    files["pyproject.toml"] = _PYPROJECT_TOML.format(project=project, framework=framework)

    # README.md
    files["README.md"] = _README.format(project=project, framework=framework, cloud=cloud)

    # .gitignore
    files[".gitignore"] = _GITIGNORE

    # Hello-world agent — per Q5 framework rollout
    if framework == "langgraph":
        files["src/agents/hello.py"] = _HELLO_LANGGRAPH
    elif framework == "strands":
        files["src/agents/hello.py"] = _HELLO_STRANDS
    else:
        raise ValueError(f"Unknown framework: {framework}")

    # Eval dataset stub — Q8
    files["evals/datasets/hello.jsonl"] = _HELLO_EVAL_DATASET

    # Unit test — Q25
    files["tests/test_hello.py"] = _HELLO_TEST

    # Local dev secrets template — Q13
    files[".cloudless/dev-secrets.yaml.example"] = _DEV_SECRETS_EXAMPLE

    return files


# Templates ----------------------------------------------------------------

_CLOUDLESS_YAML = """\
# cloudless.yaml — project-wide config per Q10
# https://github.com/<TBD>/cloudless

project: {project}
default_cloud: {cloud}

clouds:
  aws:
    accounts:
      dev: {{region: us-east-1}}

environments:
  dev: {{aws: dev}}

# Service catalog defaults (Q9)
service_catalog:
  llm: {{provider: bedrock, model: nova-micro}}        # F15-safe default
  memory: {{strategy: semantic, retention_days: 90}}

agents:
  hello:
    cloud: {cloud}
    interfaces: [http]                                 # add 'a2a' for peer calls
"""

_PYPROJECT_TOML = '''\
[project]
name = "{project}"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = [
    "cloudless[{framework},aws]",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
'''

_HELLO_LANGGRAPH = '''\
"""Hello world cloudless agent — LangGraph-backed."""
from typing_extensions import TypedDict
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, START, END

import cloudless


class State(TypedDict):
    messages: list


@cloudless.agent(name="hello", framework="langgraph", interfaces=["http"])
class HelloAgent(cloudless.LangGraphAgent):
    def build(self):
        # F15: Nova Micro is streaming-safe out of the box.
        llm = init_chat_model(
            "us.amazon.nova-micro-v1:0",
            model_provider="bedrock_converse",
            region_name="us-east-1",
        )

        def chat(state: State) -> State:
            response = llm.invoke(state["messages"])
            return {"messages": state["messages"] + [response]}

        gb = StateGraph(State)
        gb.add_node("chat", chat)
        gb.add_edge(START, "chat")
        gb.add_edge("chat", END)
        return gb.compile()
'''

_HELLO_STRANDS = '''\
"""Hello world cloudless agent — Strands-backed."""
from strands import Agent as StrandsCoreAgent

import cloudless


@cloudless.agent(name="hello", framework="strands", interfaces=["http"])
class HelloAgent(cloudless.StrandsAgent):
    def build(self):
        return StrandsCoreAgent(
            name="hello",
            model="us.amazon.nova-micro-v1:0",   # F15 safe default
            system_prompt="You are a helpful, concise assistant.",
        )
'''

_HELLO_EVAL_DATASET = """\
{"prompt": "say pong", "expected_contains": "pong"}
{"prompt": "what is 2+2?", "expected_contains": "4"}
"""

_HELLO_TEST = '''\
"""Unit smoke test for the hello agent — no cloud required."""
import pytest


def test_hello_agent_has_cloudless_metadata():
    from agents.hello import HelloAgent
    m = HelloAgent.__cloudless_metadata__
    assert m.name == "hello"
    assert "http" in m.interfaces
'''

_DEV_SECRETS_EXAMPLE = """\
# Copy to dev-secrets.yaml and fill in for `cloudless dev`.
# This file is GITIGNORED — never commit real secrets.
#
# example:
#   bedrock_api_key: ...
"""

_GITIGNORE = """\
__pycache__/
*.pyc
.venv/
.cloudless/dev-secrets.yaml
.cloudless/cache/
dist/
build/
*.egg-info/
"""

_README = """\
# {project}

Generated by `cloudless init`. Default cloud: **{cloud}**. Framework: **{framework}**.

## Get started

```bash
uv pip install -e .
pytest tests/
```

## Files

- `cloudless.yaml` — project config (clouds, environments, service catalog)
- `src/agents/hello.py` — the hello-world agent
- `evals/datasets/hello.jsonl` — example eval cases
- `tests/test_hello.py` — unit test

## Deploy

`cloudless deploy hello` ships in M3.
"""
