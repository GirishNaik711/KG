"""Gemini Enterprise Agent Runtime deploy adapter (Q4 GCP side).

Per dossier 07: Gemini Enterprise Agent Runtime is the GCP-side compute
plane (formerly Vertex AI Agent Engine; API resource `reasoningEngines`).

Deploy contract differs fundamentally from AgentCore:
  - AWS: ARM64 container with HTTP/A2A/MCP on a port (cloudless.adapters.aws.agentcore)
  - GCP: picklable Python class with set_up()/query()/stream_query() methods
        that the platform invokes via gRPC/REST (this module)

The picklable-class contract has a sharp edge documented in F13a:

  cloudpickle defaults to pickling user classes BY REFERENCE (recording
  `<module>.<class>`). The remote runtime then tries to `import <module>`
  and fails — `cloudless` users' agent module isn't on the remote PYTHONPATH.

  **Fix this adapter implements**: call `cloudpickle.register_pickle_by_value()`
  on the user's agent module BEFORE building the wrapper instance. cloudpickle
  then embeds the full class definition in the pickle, so the remote can
  deserialize without importing anything from the user's source tree.

The wrapper class this adapter generates exposes the GCP Agent Runtime
contract (set_up / query / stream_query / register_operations) and, in
set_up(), instantiates the user's @cloudless.agent class and bridges its
query() to GCP's request/response API.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeploymentResult:
    """What deploy() returns after a successful Gemini Enterprise deploy."""

    agent_name: str
    resource_name: str
    """projects/<proj-num>/locations/<region>/reasoningEngines/<id>"""

    project: str
    region: str
    staging_bucket: str


# --------------------------------------------------------------------- #
# The wrapper class — instantiated locally with the user's agent class,
# pickled, and re-instantiated on the remote runtime.
#
# It MUST be defined at module-level so cloudpickle can find it cleanly.
# --------------------------------------------------------------------- #


class _CloudlessGCPAgent:
    """GCP Agent Runtime wrapper around a cloudless.Agent subclass.

    Picklable by value (the agent class is registered with cloudpickle
    via register_pickle_by_value, so the class definition itself is
    embedded in the pickle).
    """

    def __init__(self, agent_class: type) -> None:
        # `agent_class` is pickled by VALUE; the remote receives the full
        # class definition without needing to import the user's module.
        self.agent_class = agent_class
        self._agent_instance: Any = None
        # CAPTURE InMemoryContext as an attribute at construction time so
        # it's pickled by value with the wrapper. Without this, `_ctx()`
        # would do `import cloudless` at invocation time, which fails on
        # remote runtime where cloudless isn't installed.
        import cloudless
        self._InMemoryContext = cloudless.InMemoryContext

    def set_up(self) -> None:
        """Called once per worker after deserialization."""
        # Agent Runtime invokes query() from inside its own asyncio loop;
        # bridging cloudless's async generator from a sync wrapper requires
        # nested-loop support. nest_asyncio is the standard pattern.
        import nest_asyncio
        nest_asyncio.apply()
        # Lazy-init: instantiate the user's @cloudless.agent class.
        self._agent_instance = self.agent_class()

    def query(self, prompt: str) -> dict:
        """Synchronous request/response. Returns a JSON-serializable dict."""
        if self._agent_instance is None:
            self.set_up()
        # Bridge cloudless's async-generator query() to a sync dict result
        return self._run_sync(prompt)

    def stream_query(self, prompt: str) -> Iterator[dict]:
        """Streaming variant — yields dict chunks (nest_asyncio enables nested loops)."""
        if self._agent_instance is None:
            self.set_up()
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        agen = self._agent_instance.query(self._ctx(), prompt)
        while True:
            try:
                chunk = loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
            yield chunk.model_dump()

    def register_operations(self) -> dict:
        """Tells Agent Runtime which methods are callable."""
        return {"": ["query"], "stream": ["stream_query"]}

    # ------------------------------------------------------------------ #
    # Helpers (re-built on remote; not pickled)
    # ------------------------------------------------------------------ #

    def _ctx(self) -> Any:
        """Build an InMemoryContext using the class captured at construction."""
        return self._InMemoryContext(session_id="default-gcp-session")

    def _run_sync(self, prompt: str) -> dict:
        """Collect the async generator into a single response dict.

        Relies on nest_asyncio (applied in set_up) so this works whether
        we're running in a fresh thread or inside Agent Runtime's loop.
        """
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self._collect(prompt))

    async def _collect(self, prompt: str) -> dict:
        chunks: list[dict] = []
        async for chunk in self._agent_instance.query(self._ctx(), prompt):
            chunks.append(chunk.model_dump())
        final_text = "".join(c["text"] for c in chunks if c.get("kind") == "text")
        meta = getattr(self.agent_class, "__cloudless_metadata__", None)
        return {
            "chunks": chunks,
            "final_text": final_text,
            "agent": meta.name if meta else self.agent_class.__name__,
        }


# --------------------------------------------------------------------- #
# Deployer
# --------------------------------------------------------------------- #


class AgentRuntimeDeployer:
    """Deploy a cloudless agent to Gemini Enterprise Agent Runtime.

    Pattern (per F13a):
      1. Force cloudpickle to pickle the user's agent module BY VALUE
      2. Wrap the user's agent class in _CloudlessGCPAgent
      3. Build a wheel of cloudless (F17) and add it as extra_packages
      4. Call agent_engines.create(...) — pickles the wrapper + uploads
    """

    def __init__(
        self,
        *,
        project: str,
        location: str = "us-central1",
        staging_bucket: str | None = None,
    ) -> None:
        self.project = project
        self.location = location
        # Generate a default bucket name if not supplied
        self.staging_bucket = staging_bucket or f"cloudless-staging-{project}"

    def _ensure_bucket(self) -> str:
        """Create the staging bucket if missing; return gs:// URL."""
        from google.cloud import storage as gcs_storage
        from google.cloud.exceptions import Conflict
        client = gcs_storage.Client(project=self.project)
        try:
            client.create_bucket(self.staging_bucket, location=self.location)
        except Conflict:
            pass  # already exists
        return f"gs://{self.staging_bucket}"

    @staticmethod
    def _read_project_requirements(project_root: Path) -> list[str]:
        """Read dependencies from the project's pyproject.toml.

        Filters out 'cloudless' (shipped via pickle+wheel) and any local/path
        deps that won't resolve on the remote.
        """
        pyproject_path = project_root / "pyproject.toml"
        if not pyproject_path.is_file():
            return []

        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        raw_deps = data.get("project", {}).get("dependencies", [])
        filtered: list[str] = []
        for dep in raw_deps:
            # Extract package name (before any version specifier or extras)
            name = dep.split(">=")[0].split("==")[0].split("<")[0].split("[")[0].split(">")[0].strip()
            if name.lower() in ("cloudless",):
                continue
            filtered.append(dep)
        return filtered

    def deploy(
        self,
        agent_class: type,
        *,
        display_name: str | None = None,
        description: str | None = None,
        project_root: Path | None = None,
    ) -> DeploymentResult:
        if not hasattr(agent_class, "__cloudless_metadata__"):
            raise ValueError(
                f"{agent_class.__name__!r} is not a @cloudless.agent class — "
                f"missing __cloudless_metadata__."
            )

        # Derive project_root from agent module location if not provided
        if project_root is None:
            agent_module_obj = sys.modules.get(agent_class.__module__)
            if agent_module_obj and hasattr(agent_module_obj, "__file__"):
                # agents/summarizer_agent.py → src/ → project_root/
                project_root = Path(agent_module_obj.__file__).parent.parent.parent
            else:
                project_root = Path.cwd()

        # F13a: force cloudpickle to embed the user agent's module by value.
        import cloudpickle
        agent_module = sys.modules.get(agent_class.__module__)
        if agent_module is not None and agent_module is not sys.modules.get("__main__"):
            cloudpickle.register_pickle_by_value(agent_module)

        # Register ALL submodules in the agent's package so cross-module
        # imports (state, nodes.*) are captured by value in the pickle.
        agent_pkg = agent_class.__module__.rsplit(".", 1)[0] if "." in agent_class.__module__ else None
        if agent_pkg:
            for mod_name, mod_obj in list(sys.modules.items()):
                if mod_obj is not None and mod_name.startswith(agent_pkg + "."):
                    try:
                        cloudpickle.register_pickle_by_value(mod_obj)
                    except Exception:
                        pass

        # Register sibling packages loaded via absolute imports (e.g., "clients"
        # when agents/ imports from clients/). These share the same sys.path root
        # but have a different top-level package name.
        for mod_name, mod_obj in list(sys.modules.items()):
            if mod_obj is None:
                continue
            mod_file = getattr(mod_obj, "__file__", None)
            if not mod_file:
                continue
            # Check if the module lives under the same src/ directory
            if agent_module and hasattr(agent_module, "__file__"):
                agent_src_root = str(Path(agent_module.__file__).parent.parent)
                if str(Path(mod_file)).startswith(agent_src_root):
                    try:
                        cloudpickle.register_pickle_by_value(mod_obj)
                    except Exception:
                        pass

        # F17 mirror, made robust: pickle-by-value EVERY cloudless module that
        # might be referenced in the pickle, after force-importing them. The
        # wheel fallback alone isn't sufficient because Vertex unpickles the
        # agent_engine.pkl BEFORE running pip on extra_packages.
        for mod_name in (
            "cloudless", "cloudless.agent", "cloudless.chunks",
            "cloudless.exceptions", "cloudless.runtime",
            "cloudless.runtime.context", "cloudless.catalog",
            "cloudless.catalog.llm", "cloudless.catalog.memory",
            "cloudless.catalog.secrets", "cloudless.adapters",
            "cloudless.adapters.frameworks",
            "cloudless.adapters.frameworks.langgraph",
            "cloudless.adapters.frameworks.strands",
            # This adapter module itself (defines _CloudlessGCPAgent):
            __name__,
        ):
            try:
                __import__(mod_name)
                cloudpickle.register_pickle_by_value(sys.modules[mod_name])
            except Exception:
                pass

        # Wheel fallback for anything not pickled by value (e.g., if the user
        # imports cloudless transitively after deploy):
        cloudless_wheel_path = self._build_cloudless_wheel()

        meta = agent_class.__cloudless_metadata__
        display_name = display_name or meta.name
        description = description or (meta.description
                                       or f"cloudless agent: {meta.name}")

        bucket = self._ensure_bucket()

        # Initialize Vertex SDK
        import vertexai
        from vertexai import agent_engines
        vertexai.init(project=self.project, location=self.location, staging_bucket=bucket)

        # Wrap and deploy
        wrapper = _CloudlessGCPAgent(agent_class)
        # Requirements — read from the project's pyproject.toml so the remote
        # runtime has all the same deps the project declares locally.
        remote_requirements = self._read_project_requirements(project_root)

        # Ensure platform essentials are present
        platform_essentials = [
            "google-cloud-aiplatform[agent-engines]",
            "pydantic>=2.7.0",
            "nest-asyncio>=1.6.0",
        ]
        existing_names = {r.split(">=")[0].split("==")[0].split("[")[0].lower()
                         for r in remote_requirements}
        for req in platform_essentials:
            req_name = req.split(">=")[0].split("==")[0].split("[")[0].lower()
            if req_name not in existing_names:
                remote_requirements.append(req)

        remote = agent_engines.create(
            agent_engine=wrapper,
            requirements=remote_requirements,
            extra_packages=[cloudless_wheel_path],
            display_name=display_name,
            description=description,
        )

        return DeploymentResult(
            agent_name=meta.name,
            resource_name=remote.resource_name,
            project=self.project,
            region=self.location,
            staging_bucket=bucket,
        )


    @staticmethod
    def _build_cloudless_wheel() -> str:
        """Build a cloudless wheel from the local source tree and return its path.

        Mirrors `cloudless.adapters.aws.agentcore._build_cloudless_wheel`.
        Once cloudless ships to PyPI in v1.0, this can be dropped and
        `cloudless` returns to a normal requirements entry. F17.
        """
        import subprocess
        import tempfile

        import cloudless as cl_module
        cloudless_root = Path(cl_module.__file__).parent.parent.parent
        if not (cloudless_root / "pyproject.toml").is_file():
            raise RuntimeError(
                f"Can't find cloudless source root with pyproject.toml at {cloudless_root}. "
                f"F17 workaround needs cloudless installed editable from a repo."
            )
        wheelhouse = Path(tempfile.mkdtemp(prefix="cloudless-gcp-wheel-"))
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "--wheel-dir", str(wheelhouse), str(cloudless_root)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to build cloudless wheel:\n{proc.stderr[-800:]}"
            )
        wheels = list(wheelhouse.glob("cloudless-*.whl"))
        if not wheels:
            raise RuntimeError(f"No wheel produced in {wheelhouse}")
        return str(wheels[0])


def deploy(
    agent_class: type,
    *,
    project: str,
    location: str = "us-central1",
    staging_bucket: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
) -> DeploymentResult:
    """One-shot GCP deploy. Returns DeploymentResult with the reasoningEngine resource."""
    return AgentRuntimeDeployer(
        project=project, location=location, staging_bucket=staging_bucket,
    ).deploy(agent_class, display_name=display_name, description=description)
