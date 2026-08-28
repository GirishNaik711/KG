#!/usr/bin/env python3
"""Generate `.aah/dependency-policy.json` from project sources.

Issue #247, Phase 3 L5-D. Companion to ``dependency_policy_guard.py``
which BLOCKS package installs that violate the policy. This generator
produces the policy file from the right source for the project type:

  * **Brownfield**: read existing `package.json` /
    `pyproject.toml` / `requirements.txt` / `go.mod` / `Cargo.toml`
    and extract the major-version pin for each dependency.

  * **Greenfield**: read `manifest.yaml: stack_choices.primary` and
    seed from a small per-stack template (e.g.,
    ``node-react-mui5`` → ``{"@mui/*": "^5", "react": "^18"}``).
    When the stack is unknown, write an empty policy so the guard
    becomes a no-op.

The generator is **idempotent**: re-running produces the same output
when sources haven't changed. The user may edit the file by hand —
re-running rebuilds from sources, so prefer adjusting the template /
upstream manifest if the generator's output isn't what you want.

Usage:
    aah run core.plan.build_dependency_policy build [--project-path .]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json, write_json


# Per-stack greenfield templates. Keep small and additive — extend as
# new templates ship.
_GREENFIELD_TEMPLATES: dict[str, dict[str, dict[str, str]]] = {
    "node-react-mui5": {
        "npm": {
            "@mui/*": "^5",
            "react": "^18",
            "react-dom": "^18",
            "react-router-dom": "^6",
        },
    },
    "python-fastapi": {
        "pip": {
            "fastapi": "^0.110",
            "pydantic": "^2",
        },
    },
    "python-django": {
        "pip": {
            "django": "^5",
        },
    },
    "go-net-http": {"go": {}},
    "rust-axum": {"cargo": {"axum": "^0.7"}},
}


def _major_version_pin(spec: str) -> str:
    """Return ``^N`` for a version like ``5.4.2``, ``^5.4.2``, ``~5.4``,
    ``v5.4.2``. Empty string if no major can be extracted."""
    if not spec:
        return ""
    s = spec.lstrip("^~=v><!").strip()
    m = re.match(r"(\d+)", s)
    if not m:
        return ""
    return f"^{m.group(1)}"


def _extract_npm_policy(project_path: Path) -> dict[str, str]:
    """Read package.json's dependencies + devDependencies."""
    pkg = project_path / "package.json"
    if not pkg.is_file():
        return {}
    try:
        data = read_json(pkg)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        section_data = data.get(section) or {}
        if not isinstance(section_data, dict):
            continue
        for name, version in section_data.items():
            if not isinstance(version, str):
                continue
            pin = _major_version_pin(version)
            if pin:
                out[name] = pin
    return out


def _extract_pip_policy(project_path: Path) -> dict[str, str]:
    """Read pyproject.toml [project.dependencies] + requirements.txt."""
    out: dict[str, str] = {}
    pyproject = project_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        # Lightweight TOML scrape: lines like `name = "version"` under
        # [project] / [tool.poetry.dependencies] / dependencies = [...].
        # We use regex rather than a TOML parser to avoid an extra dep.
        for m in re.finditer(
            r"""['"]([\w\-\[\].]+)\s*([=<>~!]=?\s*[\d.]+)['"]""",
            text,
        ):
            name = m.group(1).strip()
            version = m.group(2).strip()
            pin = _major_version_pin(version)
            if pin:
                out[name] = pin
    requirements = project_path / "requirements.txt"
    if requirements.is_file():
        try:
            for line in requirements.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                m = re.match(r"([\w\-\[\].]+)\s*([=<>~!]=?\s*[\d.]+)", line)
                if m:
                    pin = _major_version_pin(m.group(2))
                    if pin:
                        out[m.group(1).strip()] = pin
        except OSError:
            pass
    return out


def _extract_go_policy(project_path: Path) -> dict[str, str]:
    """Read go.mod's require block."""
    gomod = project_path / "go.mod"
    if not gomod.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = gomod.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    # require ( name version, ... )  OR  require name version
    for m in re.finditer(
        r"^\s*([\w./\-]+)\s+v([\d.]+)",
        text,
        re.MULTILINE,
    ):
        pin = _major_version_pin(m.group(2))
        if pin:
            out[m.group(1)] = pin
    return out


def _extract_cargo_policy(project_path: Path) -> dict[str, str]:
    """Read Cargo.toml's [dependencies] section."""
    cargo = project_path / "Cargo.toml"
    if not cargo.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = cargo.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_deps = stripped.lower() in ("[dependencies]", "[dev-dependencies]")
            continue
        if not in_deps:
            continue
        m = re.match(r"^([\w\-]+)\s*=\s*['\"]?([\d.]+|\^[\d.]+)['\"]?", stripped)
        if m:
            pin = _major_version_pin(m.group(2))
            if pin:
                out[m.group(1)] = pin
    return out


def build_dependency_policy(project_path: Path) -> dict:
    """Build the policy dict from project sources."""
    rapids_path = project_path / ".aah"
    manifest_path = rapids_path / "manifest.yaml"
    project_type = "brownfield"
    stack_primary = ""
    if manifest_path.is_file():
        try:
            from aah.core.common.io_utils import read_yaml
            mf = read_yaml(manifest_path) or {}
            project_type = (mf.get("project_type") or "brownfield").lower()
            sc = mf.get("stack_choices") or {}
            stack_primary = (sc.get("primary") or "").lower()
        except Exception:
            pass

    policy: dict[str, dict[str, str]] = {}

    if project_type == "brownfield":
        # Read each ecosystem's manifest if present.
        npm_pol = _extract_npm_policy(project_path)
        if npm_pol:
            policy["npm"] = npm_pol
        pip_pol = _extract_pip_policy(project_path)
        if pip_pol:
            policy["pip"] = pip_pol
        go_pol = _extract_go_policy(project_path)
        if go_pol:
            policy["go"] = go_pol
        cargo_pol = _extract_cargo_policy(project_path)
        if cargo_pol:
            policy["cargo"] = cargo_pol
    else:
        # Greenfield: seed from template if known.
        template = _GREENFIELD_TEMPLATES.get(stack_primary, {})
        for ecosystem, rules in template.items():
            if rules:
                policy[ecosystem] = dict(rules)

    return policy


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate dependency policy")
    sub = parser.add_subparsers(dest="command", required=True)
    build_p = sub.add_parser("build", help="Generate .aah/dependency-policy.json")
    build_p.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)
    rapids_path = project_path / ".aah"
    rapids_path.mkdir(parents=True, exist_ok=True)

    if args.command == "build":
        policy = build_dependency_policy(project_path)
        out_path = rapids_path / "dependency-policy.json"
        write_json(policy, out_path)
        print(f"Wrote {out_path} with {sum(len(v) for v in policy.values())} rule(s) "
              f"across {len(policy)} ecosystem(s).", file=sys.stderr)
        json.dump(policy, sys.stdout, indent=2)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
