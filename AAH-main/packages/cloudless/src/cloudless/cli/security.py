"""`cloudless security` — SBOM generation + dependency vulnerability scan (Q33).

Subcommands:

  sbom    Generate a CycloneDX 1.4 JSON SBOM for the current environment.
          Output to stdout by default, or --output PATH.

  audit   Shell out to `pip-audit` if installed; report vulnerable
          packages. If pip-audit isn't installed, prints install instructions
          and exits non-zero.

Why no hard dep on cyclonedx-py: we generate a minimal compliant SBOM
from stdlib (`importlib.metadata`). Sufficient for SPDX-like inventory
and matches the CycloneDX 1.4 schema for tools like Dependency-Track.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from cloudless._version import __version__

_console = Console()


# --------------------------------------------------------------------- #
# SBOM
# --------------------------------------------------------------------- #


def _purl(name: str, version: str) -> str:
    """Construct a Package-URL (purl) for a PyPI distribution."""
    return f"pkg:pypi/{name}@{version}"


def _component(dist: md.Distribution) -> dict[str, Any]:
    """Map an `importlib.metadata.Distribution` to a CycloneDX component."""
    name = dist.metadata["Name"] or "<unknown>"
    version = dist.version or "0.0.0"
    return {
        "type": "library",
        "bom-ref": _purl(name, version),
        "name": name,
        "version": version,
        "purl": _purl(name, version),
        # We don't ship a license check; users can validate downstream.
        "licenses": [{"license": {"name": dist.metadata["License"]}}]
                    if dist.metadata.get("License") else [],
    }


def generate_sbom() -> dict[str, Any]:
    """Produce a CycloneDX 1.4 JSON SBOM for the current Python env."""
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dist in md.distributions():
        try:
            comp = _component(dist)
        except Exception:
            continue
        if comp["name"] in seen:
            continue
        seen.add(comp["name"])
        components.append(comp)

    components.sort(key=lambda c: c["name"].lower())

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "tools": [{
                "vendor": "cloudless",
                "name": "cloudless-security",
                "version": __version__,
            }],
            "component": {
                "type": "application",
                "bom-ref": _purl("cloudless", __version__),
                "name": "cloudless",
                "version": __version__,
            },
        },
        "components": components,
    }


def sbom_command(*, output_path: str | None = None) -> int:
    sbom = generate_sbom()
    text = json.dumps(sbom, indent=2)
    if output_path:
        Path(output_path).write_text(text)
        # Compute hash for evidence retention
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        _console.print(f"[green]✓[/] SBOM written to {output_path} "
                       f"({len(sbom['components'])} components, sha256:{h})")
    else:
        print(text)
    return 0


# --------------------------------------------------------------------- #
# Audit (pip-audit shell-out)
# --------------------------------------------------------------------- #


def audit_command(*, json_output: bool = False) -> int:
    exe = shutil.which("pip-audit")
    if exe is None:
        _console.print(
            "[red]✗[/] pip-audit not found on PATH.\n"
            "  Install with: [cyan]pip install pip-audit[/]\n"
            "  Or: [cyan]pipx install pip-audit[/]"
        )
        return 2

    args = [exe]
    if json_output:
        args += ["-f", "json"]

    _console.print(f"[bold]cloudless security audit[/]  using {exe}")
    try:
        result = subprocess.run(args, check=False, text=True, encoding='utf-8', errors='replace',
                                capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        _console.print("[red]✗[/] pip-audit timed out after 120s")
        return 1

    if json_output:
        print(result.stdout)
    else:
        if result.stdout:
            _console.print(result.stdout)
        if result.stderr:
            _console.print(f"[yellow]{result.stderr}[/]")
    return result.returncode
