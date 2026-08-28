"""cloudless.yaml schema validation.

Hand-rolled validator (no JSON Schema dependency) returning a typed
`CloudlessConfig`. Errors are accumulated and reported together rather
than fail-fast, so users see all problems in one pass.

Top-level shape (per Q10 / current init template):

    project: my-thing           # required, kebab-case
    default_cloud: aws | gcp    # required
    clouds:
      aws:
        accounts:
          dev: {region: us-east-1}
      gcp:
        projects:
          dev: {project: ..., region: us-central1}
    environments:
      dev: {aws: dev}           # named env → account/project mapping
    service_catalog:
      llm: {provider: bedrock, model: nova-micro}
      memory: {strategy: semantic, retention_days: 90}
      embeddings: {provider: bedrock, model: titan-v2}
    policies:
      cost_cap_usd_per_session: 5.0
      retries: {attempts: 3, backoff_seconds: 0.25}
    agents:
      hello:
        cloud: aws
        interfaces: [http, a2a]
        peers: [orders]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------- #
# Typed config model
# --------------------------------------------------------------------- #


_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
VALID_CLOUDS = frozenset({"aws", "gcp"})
VALID_INTERFACES = frozenset({"http", "a2a", "mcp", "ag-ui"})
VALID_FRAMEWORKS = frozenset({"langgraph", "strands", "adk", "maf"})


@dataclass
class AgentConfig:
    name: str
    cloud: str
    framework: str | None = None
    interfaces: tuple[str, ...] = ("http",)
    peers: tuple[str, ...] = ()
    version: str | None = None


@dataclass
class CloudlessConfig:
    project: str
    default_cloud: str
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    service_catalog: dict[str, dict] = field(default_factory=dict)
    policies: dict[str, Any] = field(default_factory=dict)
    environments: dict[str, dict] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class ConfigValidationError(Exception):
    """Aggregates multiple errors found during validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        msg = f"cloudless.yaml has {len(errors)} validation error(s):\n  - " + "\n  - ".join(errors)
        super().__init__(msg)


# --------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------- #


def validate(data: dict[str, Any]) -> CloudlessConfig:
    """Validate a parsed cloudless.yaml dict; raise ConfigValidationError on failure."""
    errors: list[str] = []

    # ----- project ----- #
    project = data.get("project")
    if not project:
        errors.append("missing required field: project")
    elif not isinstance(project, str) or not _NAME_RE.match(project):
        errors.append(f"project: must be kebab-case identifier, got {project!r}")

    # ----- default_cloud ----- #
    default_cloud = data.get("default_cloud", "aws")
    if default_cloud not in VALID_CLOUDS:
        errors.append(
            f"default_cloud: must be one of {sorted(VALID_CLOUDS)}, got {default_cloud!r}"
        )

    # ----- agents ----- #
    agents_raw = data.get("agents") or {}
    agents: dict[str, AgentConfig] = {}
    if not isinstance(agents_raw, dict):
        errors.append("agents: must be a mapping of name -> config")
    else:
        for name, body in agents_raw.items():
            if not _NAME_RE.match(name):
                errors.append(f"agents.{name}: name must be kebab-case")
                continue
            if not isinstance(body, dict):
                errors.append(f"agents.{name}: must be a mapping, got {type(body).__name__}")
                continue
            agent_cloud = body.get("cloud", default_cloud)
            if agent_cloud not in VALID_CLOUDS:
                errors.append(
                    f"agents.{name}.cloud: must be one of {sorted(VALID_CLOUDS)}, "
                    f"got {agent_cloud!r}"
                )
            iface = body.get("interfaces", ["http"])
            if not isinstance(iface, list) or not iface:
                errors.append(f"agents.{name}.interfaces: must be non-empty list")
                iface = ["http"]
            else:
                bad = [i for i in iface if i not in VALID_INTERFACES]
                if bad:
                    errors.append(
                        f"agents.{name}.interfaces: unknown {bad!r}; "
                        f"valid: {sorted(VALID_INTERFACES)}"
                    )
            framework = body.get("framework")
            if framework is not None and framework not in VALID_FRAMEWORKS:
                errors.append(
                    f"agents.{name}.framework: must be one of {sorted(VALID_FRAMEWORKS)}, "
                    f"got {framework!r}"
                )
            peers = body.get("peers", [])
            if not isinstance(peers, list):
                errors.append(f"agents.{name}.peers: must be a list of agent names")
                peers = []
            version = body.get("version")
            if version is not None and not isinstance(version, str):
                errors.append(f"agents.{name}.version: must be string, got {type(version).__name__}")

            agents[name] = AgentConfig(
                name=name,
                cloud=agent_cloud,
                framework=framework,
                interfaces=tuple(iface),
                peers=tuple(peers),
                version=version,
            )

        # Cross-check peer references after all agents loaded
        for name, ac in agents.items():
            for peer in ac.peers:
                if peer not in agents and peer != name:
                    errors.append(
                        f"agents.{name}.peers: {peer!r} not declared in agents:"
                    )

    # ----- service_catalog ----- #
    service_catalog = data.get("service_catalog") or {}
    if not isinstance(service_catalog, dict):
        errors.append("service_catalog: must be a mapping")
        service_catalog = {}
    else:
        # Validate the LLM provider if present
        llm = service_catalog.get("llm")
        if llm is not None:
            if not isinstance(llm, dict):
                errors.append("service_catalog.llm: must be a mapping")
            else:
                provider = llm.get("provider", "bedrock")
                if provider not in {"bedrock", "gemini"}:
                    errors.append(
                        f"service_catalog.llm.provider: must be 'bedrock' or 'gemini', got {provider!r}"
                    )

    # ----- policies ----- #
    policies = data.get("policies") or {}
    if not isinstance(policies, dict):
        errors.append("policies: must be a mapping")
        policies = {}
    else:
        cap = policies.get("cost_cap_usd_per_session")
        if cap is not None and not isinstance(cap, (int, float)):
            errors.append(
                f"policies.cost_cap_usd_per_session: must be a number, got {type(cap).__name__}"
            )
        retries = policies.get("retries")
        if retries is not None:
            if not isinstance(retries, dict):
                errors.append("policies.retries: must be a mapping")
            else:
                if "attempts" in retries and not isinstance(retries["attempts"], int):
                    errors.append("policies.retries.attempts: must be an integer")
                if "backoff_seconds" in retries and not isinstance(retries["backoff_seconds"], (int, float)):
                    errors.append("policies.retries.backoff_seconds: must be a number")

    # ----- environments ----- #
    environments = data.get("environments") or {}
    if not isinstance(environments, dict):
        errors.append("environments: must be a mapping")
        environments = {}

    if errors:
        raise ConfigValidationError(errors)

    # `project` is guaranteed str here — error path raised above if missing/invalid
    assert isinstance(project, str)
    return CloudlessConfig(
        project=project,
        default_cloud=default_cloud,
        agents=agents,
        service_catalog=service_catalog,
        policies=policies,
        environments=environments,
        raw=data,
    )


def load(path: str | Path, *, resolve_refs: bool = True,
         secrets: Any = None) -> CloudlessConfig:
    """Load + validate a cloudless.yaml file.

    Args:
        path: cloudless.yaml location.
        resolve_refs: If True (default), resolve ${secret:..} and ${env:..}
            references in string fields before validation.
        secrets: Optional pre-built cloudless.Secrets instance.
    """
    import yaml
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"cloudless.yaml not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigValidationError(["cloudless.yaml top-level must be a mapping"])
    if resolve_refs:
        from cloudless.config_refs import resolve_refs as _resolve
        data = _resolve(data, secrets=secrets)
    return validate(data)
