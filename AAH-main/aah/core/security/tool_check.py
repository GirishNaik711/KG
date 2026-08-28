#!/usr/bin/env python3
"""Detect security scanning tools available on PATH."""

import json
import re
import shutil
import subprocess
import sys


TOOLS = {
    # Phase 1 tools
    "semgrep": {
        "version_cmd": ["semgrep", "--version"],
        "install_hint": "pip install semgrep  OR  brew install semgrep",
    },
    "trivy": {
        "version_cmd": ["trivy", "--version"],
        "install_hint": "brew install trivy  OR  https://aquasecurity.github.io/trivy/latest/getting-started/installation/",
    },
    "gitleaks": {
        "version_cmd": ["gitleaks", "version"],
        "install_hint": "brew install gitleaks  OR  https://github.com/gitleaks/gitleaks#installing",
    },
    # Phase 2 tools
    "codeql": {
        "version_cmd": ["codeql", "version"],
        "install_hint": "https://github.com/github/codeql-cli-binaries/releases",
    },
    "syft": {
        "version_cmd": ["syft", "version"],
        "install_hint": "curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s",
    },
    "grype": {
        "version_cmd": ["grype", "version"],
        "install_hint": "curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s",
    },
    "nuclei": {
        "version_cmd": ["nuclei", "-version"],
        "install_hint": "https://github.com/projectdiscovery/nuclei/releases",
    },
    "cosign": {
        "version_cmd": ["cosign", "version"],
        "install_hint": "https://docs.sigstore.dev/cosign/system_config/installation/",
    },
}


def check_tools(only: set[str] | None = None) -> dict:
    """Return availability map for security scanning tools.

    Args:
        only: If provided, only check these tool names (plus docker if
              zap-docker is requested). Pass None to check all tools.
    """
    results = {}
    for name, info in TOOLS.items():
        if only is not None and name not in only:
            results[name] = {"available": False, "version": "", "install_hint": info["install_hint"]}
            continue
        available = shutil.which(name) is not None
        version = ""
        if available:
            try:
                proc = subprocess.run(
                    info["version_cmd"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                raw = (proc.stdout + proc.stderr).strip()
                version = _extract_version(raw)
            except (subprocess.TimeoutExpired, OSError):
                version = "unknown"
        results[name] = {
            "available": available,
            "version": version,
            "install_hint": info["install_hint"],
        }
    return results



def _extract_version(raw: str) -> str:
    """Extract a version number from tool output, ignoring banners."""
    match = re.search(r"(\d+\.\d+\.\d+)", raw)
    if match:
        return match.group(1)
    first_line = raw.split("\n")[0].strip()
    return first_line[:60] if first_line else "unknown"


def main() -> None:
    tools = check_tools()

    if "--json" in sys.argv:
        json.dump(tools, sys.stdout, indent=2)
        print()
        return

    print("Security Tool Availability")
    print("=" * 50)
    for name, info in tools.items():
        status = "AVAILABLE" if info["available"] else "MISSING"
        version = f"  ({info['version']})" if info["version"] else ""
        print(f"  {name:<12} {status}{version}")
        if not info["available"]:
            print(f"               Install: {info['install_hint']}")
    print()

    available_count = sum(1 for t in tools.values() if t["available"])
    print(f"{available_count}/{len(tools)} tools available")


if __name__ == "__main__":
    main()
