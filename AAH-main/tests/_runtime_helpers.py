"""Shared builders for runtime-profile functional tests (NO MOCKS).

Every helper builds a REAL temporary project: a real attestation secret, a real
discuss slug registry built through the discuss CLI (the runtime decision
source), a real cloud-readiness.yaml, real manifest, and real governance via the
official CLI. Nothing here mocks a writer, reader, parser or subprocess.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
from tests.support.aah_project import AAHProjectBuilder

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_MODULE = "aah.core.registry.registry"
DISCUSS_REGISTRY_MODULE = "aah.core.discuss.registry"

# Map a runtime topology to the discuss `development-methodology` value. The
# discuss vocabulary IS the topology vocabulary, so this is identity.
_TOPOLOGY_TO_METHODOLOGY = {
    "full-local": "full-local",
    "local-cloud-ready": "local-cloud-ready",
    "full-integrated-cloud": "full-integrated-cloud",
}


def run_module(module: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{module} {args} failed:\n{result.stderr or result.stdout}")
    return result


def write_secret(project_path: Path) -> None:
    from aah.core.build.regenerate_attestation_secret import ensure_attestation_secret

    ensure_attestation_secret(project_path)


def write_manifest(
    aah_path: Path,
    *,
    project_name: str = "demo",
    runtime_profile_v1="report_only",
    runtime_cloud_probes_v1: bool = False,
    semantic_smoke_v2: bool = False,
    stack_choices: dict | None = None,
) -> None:
    AAHProjectBuilder(aah_path.parent).manifest(
        project_name=project_name,
        project_type="greenfield",
        current_phase="plan",
        features={
            "runtime_profile_v1": runtime_profile_v1,
            "runtime_cloud_probes_v1": runtime_cloud_probes_v1,
            "semantic_smoke_v2": semantic_smoke_v2,
        },
        stack_choices=stack_choices,
    )


def build_registry(
    aah_path: Path,
    *,
    topology: str = "full-local",
    transport: str | None = "docker-compose",
) -> Path:
    """Seed the discuss slug registry with the runtime decision.

    The runtime profile is derived from the ``/aah-discuss`` slug registry:
      - ``development-methodology`` → deployment topology
      - ``docker-installed`` → local transport (yes → docker-compose, else
        localhost) for local topologies.
    Returns the discuss registry path. (The gate no longer reads DDRs.)
    """
    project_root = aah_path.parent
    run_module(
        DISCUSS_REGISTRY_MODULE, "--project-path", str(project_root), "init",
        "--project-name", "demo",
        "--complexity", "mvp",
        "--force",
    )
    methodology = _TOPOLOGY_TO_METHODOLOGY[topology]
    run_module(
        DISCUSS_REGISTRY_MODULE, "--project-path", str(project_root), "add-decision",
        "--slug-id", "development-methodology",
        "--area", "infrastructure",
        "--question", "How will your team develop across local & cloud environments?",
        "--response", methodology,
        "--response-type", "single-select",
        "--source", "user",
    )
    # docker-installed only informs local_transport for local topologies.
    if topology != "full-integrated-cloud":
        docker_value = "yes" if transport == "docker-compose" else "no"
        run_module(
            DISCUSS_REGISTRY_MODULE, "--project-path", str(project_root), "add-decision",
            "--slug-id", "docker-installed",
            "--area", "infrastructure",
            "--question", "Is a container runtime installed?",
            "--response", docker_value,
            "--response-type", "single-select",
            "--source", "user",
        )
    return project_root / ".rapids" / "discuss" / "decision-registry.yaml"


def write_cloud_readiness(aah_path: Path, *, provider: str = "aws", region: str = "us-east-1") -> None:
    from aah.core.common.io_utils import write_yaml

    arch_path = aah_path / "architecture"
    arch_path.mkdir(parents=True, exist_ok=True)
    write_yaml(
        {
            "cloud_provider": provider,
            "deployment_method": "direct-cloud",
            "services": [
                {
                    "id": "primary-db",
                    "service_type": "rds-postgres",
                    "config": {
                        "host": "db.example.internal",
                        "port": 5432,
                        "database": "appdb",
                        "region": region,
                        "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:db",
                    },
                }
            ],
        },
        arch_path / "cloud-readiness.yaml",
    )


def configure_governance_cli(
    project_path: Path,
    *,
    delivery_owner: str = "alice",
    qa_governance_owner: str = "qa-lead",
    security_owner: str | None = "sec-owner",
    cloud_platform_owner: str | None = None,
    cost_owner: str | None = None,
) -> None:
    args = [
        "configure", "--project-path", str(project_path),
        "--delivery-owner", delivery_owner,
        "--qa-governance-owner", qa_governance_owner,
    ]
    if security_owner is not None:
        args += ["--security-owner", security_owner]
    if cloud_platform_owner is not None:
        args += ["--cloud-platform-owner", cloud_platform_owner]
    if cost_owner is not None:
        args += ["--cost-owner", cost_owner]
    run_module("aah.core.common.governance", *args)


def make_project(
    tmp_path: Path,
    *,
    topology: str = "full-local",
    transport: str | None = "docker-compose",
    runtime_profile_v1="report_only",
    runtime_cloud_probes_v1: bool = False,
    semantic_smoke_v2: bool = False,
    stack_choices: dict | None = None,
    with_cloud_readiness: bool = True,
    with_governance: bool = True,
    cloud_platform_owner: str | None = None,
    cost_owner: str | None = None,
) -> Path:
    """Assemble a full real project and return the project root (containing .aah)."""
    builder = AAHProjectBuilder(tmp_path).dirs("plan/features", "build").secret()
    aah_path = builder.aah
    write_manifest(
        aah_path,
        runtime_profile_v1=runtime_profile_v1,
        runtime_cloud_probes_v1=runtime_cloud_probes_v1,
        semantic_smoke_v2=semantic_smoke_v2,
        stack_choices=stack_choices,
    )
    build_registry(aah_path, topology=topology, transport=transport)
    if with_cloud_readiness:
        write_cloud_readiness(aah_path)
    if with_governance:
        configure_governance_cli(
            tmp_path,
            cloud_platform_owner=cloud_platform_owner,
            cost_owner=cost_owner,
        )
    return tmp_path


def free_port() -> int:
    """Allocate an OS-assigned free port, then close the socket so a child can
    bind it. A brief race window is acceptable for tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class ThreadedHTTPApp:
    """A real threaded HTTP server whose per-path responses the test declares.

    ``routes`` maps a path to a ``(status, headers, body)`` triple; ``body`` is
    bytes or a str. Unmatched paths return 404. Use as a context manager; the
    bound port is ``.port``.
    """

    def __init__(self, routes: dict[str, tuple]):
        self._routes = routes
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):  # noqa: N802
                match = app._routes.get(self.path)
                if match is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                status, headers, body = match
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            do_GET = _respond  # noqa: N815
            do_POST = _respond  # noqa: N815

            def log_message(self, *a):  # silence
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def confirm_profile_cli(project_path: Path) -> str:
    """Propose + governed-confirm the runtime profile via the real CLI.

    Returns the confirmed profile_hash. Requires a project built with governance
    (delivery + security owners; cloud owner for cloud topologies).
    """
    rp = "aah.core.common.runtime_profile"
    run_module(rp, "propose", "--project-path", str(project_path))
    aah_path = project_path / ".aah"
    ph = proposal_profile_hash(aah_path)
    proposal = yaml.safe_load(
        (aah_path / "plan" / "runtime-profile-proposal.yaml").read_text()
    )
    topology = proposal.get("deployment_topology") or ""
    args = [
        "confirm", "--project-path", str(project_path),
        "--reviewer", "alice", "--security-reviewer", "sec-owner",
        "--rationale", "reviewed and approved", "--profile-hash", ph,
    ]
    if topology in ("local-cloud-ready", "full-integrated-cloud"):
        args += ["--cloud-reviewer", "cloud-owner"]
    run_module(rp, *args)
    return ph


def write_waves(aah_path: Path, features: list[str], wave: int = 0) -> None:
    waves: list = [[] for _ in range(wave + 1)]
    waves[wave] = features
    AAHProjectBuilder(aah_path.parent).waves(waves, include_total=False)


def write_feature_md(
    aah_path: Path,
    feature_id: str,
    *,
    endpoints: list[dict] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> None:
    """Write a feature .md with YAML frontmatter the smoke generator reads."""
    frontmatter: dict = {}
    if acceptance_criteria:
        frontmatter["acceptance_criteria"] = acceptance_criteria
    if endpoints:
        frontmatter["endpoints"] = endpoints
    AAHProjectBuilder(aah_path.parent).feature(
        feature_id, frontmatter=frontmatter, body="body\n"
    )


def generate_smoke_via_generator(project_path: Path, wave: int = 0) -> subprocess.CompletedProcess:
    """Generate the wave smoke YAML through the REAL plan generator CLI."""
    return run_module(
        "aah.core.plan.generate_smoke_tests", "generate",
        "--project-path", str(project_path), "--wave", str(wave),
    )


def run_verify_cli(project_path: Path, wave: int = 0) -> subprocess.CompletedProcess:
    """Invoke the real verify CLI via subprocess.

    `--no-runtime` and `--port` are gone: verify.py is a read-only evidence
    verifier, so there is no runtime to suppress and no port to bind. Normal
    orchestration calls verify_wave_evidence() directly; this CLI exists for
    operators and focused tests. Exit codes: 0 pass, 1 evidence failures,
    2 system error.
    """
    return run_module(
        "aah.core.build.verify",
        "--wave", str(wave),
        "--project-path", str(project_path),
        check=False,
    )


def read_attested_wave(project_path: Path, wave: int = 0) -> dict:
    path = project_path / ".aah" / "build" / "runtime-results" / f"wave-{wave}-all.json"
    return json.loads(path.read_text())


def proposal_profile_hash(aah_path: Path) -> str:
    proposal = yaml.safe_load((aah_path / "plan" / "runtime-profile-proposal.yaml").read_text())
    return proposal["profile_hash"]
