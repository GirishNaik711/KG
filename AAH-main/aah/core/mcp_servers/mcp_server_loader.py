#!/usr/bin/env python3
"""
MCP Server Loader — discovers connected MCP servers via `claude mcp list`
and builds context summary for session injection.

Called by load_impl_context.py at session start.
"""

import subprocess
from pathlib import Path

import yaml

from aah.core.common.config import resolve_framework_root


# Reference docs live in references/ sibling directory
REFERENCES_DIR = Path(__file__).parent / "references"


def _run_claude_mcp_list() -> str:
    """
    Run `claude mcp list` from the framework root directory.
    Returns raw stdout. Returns empty string if CLI fails or times out.
    """
    framework_root = resolve_framework_root()
    if not framework_root:
        return ""

    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(framework_root),
        )
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _parse_mcp_list_output(output: str) -> list[dict]:
    """
    Parse `claude mcp list` output into structured list.

    Input format:
        Checking MCP server health...

        playwright: npx -y @playwright/mcp@latest - ✓ Connected
        context7: npx -y @upstash/context7-mcp - ✓ Connected
        firecrawl: npx -y firecrawl-mcp - ✗ Failed

    Only returns servers with "Connected" status.
    """
    servers = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or "Checking MCP" in line:
            continue
        if ":" not in line:
            continue
        name = line.split(":", 1)[0].strip()
        if "Connected" in line:
            servers.append({"name": name, "status": "connected"})
    return servers


def _parse_frontmatter(file_path: Path) -> dict:
    """
    Parse YAML frontmatter from a reference markdown file.
    Returns dict with frontmatter fields, or empty dict if none found.
    """
    content = file_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}
    try:
        end_idx = content.index("---", 3)
        frontmatter_str = content[3:end_idx]
        return yaml.safe_load(frontmatter_str) or {}
    except (ValueError, yaml.YAMLError):
        return {}


def _enrich_with_references(servers: list[dict]) -> list[dict]:
    """
    For each server, find its reference file and parse frontmatter.
    Adds: purpose, action_hint, reference_path.
    """
    enriched = []
    for server in servers:
        name = server["name"]
        ref_path = REFERENCES_DIR / f"{name}.md"

        entry = {"name": name, "status": server["status"]}

        if ref_path.exists():
            meta = _parse_frontmatter(ref_path)
            entry["purpose"] = meta.get("purpose", "No description")
            entry["action_hint"] = meta.get("action_hint", "See reference doc")
            entry["reference_path"] = str(ref_path)
        else:
            entry["purpose"] = "No guide available"
            entry["action_hint"] = "Check tool list"
            entry["reference_path"] = None

        enriched.append(entry)
    return enriched


def get_enabled_mcp_servers() -> list[dict]:
    """
    Discover connected MCP servers and enrich with reference metadata.

    Steps:
    1. Run `claude mcp list` → parse connected server names
    2. For each connected server, look up reference file in references/
    3. Parse frontmatter for purpose, action_hint

    Returns empty list if CLI fails.
    """
    output = _run_claude_mcp_list()
    if not output:
        return []

    servers = _parse_mcp_list_output(output)
    if not servers:
        return []

    return _enrich_with_references(servers)


def build_mcp_context_summary() -> str:
    """
    Build the markdown block to inject into session context.
    Lists all connected servers with action hints and reference paths.
    Returns empty string if no servers found.
    """
    servers = get_enabled_mcp_servers()
    if not servers:
        return ""

    lines = [
        "## MCP Servers",
        "",
        "| Server | Action Hint | Reference |",
        "|--------|-------------|-----------|",
    ]
    for s in servers:
        ref = s["reference_path"] or "N/A"
        lines.append(f"| {s['name']} | {s['action_hint']} | {ref} |")
    lines.append("")
    lines.append("Read reference before first use of a server's tools.")

    return "\n".join(lines)


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="MCP Server Loader")
    parser.add_argument("command", choices=["raw", "list", "summary"], help="Command to run")
    args = parser.parse_args()

    if args.command == "raw":
        output = _run_claude_mcp_list()
        if output:
            print(output)
        else:
            print("CLI returned no output (failed or timed out)")

    elif args.command == "list":
        output = _run_claude_mcp_list()
        if not output:
            print("CLI returned no output (failed or timed out)")
            return
        print("Raw CLI output:")
        print(output)
        print("\nParsed servers:")
        servers = _parse_mcp_list_output(output)
        print(json.dumps(servers, indent=2))
        print("\nEnriched with references:")
        enriched = _enrich_with_references(servers)
        print(json.dumps(enriched, indent=2))

    elif args.command == "summary":
        result = build_mcp_context_summary()
        if result:
            print(result)
        else:
            print("No MCP servers found.")


if __name__ == "__main__":
    main()
