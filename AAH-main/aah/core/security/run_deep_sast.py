#!/usr/bin/env python3
"""CodeQL deep SAST orchestrator — cross-file taint analysis.

Entry point: aah run core.security.run_deep_sast run [options]
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.io_utils import ensure_dir, read_yaml, write_json, write_text
from aah.core.security import parsers_phase2, policy, suppressions, tool_check
from aah.core.security.state import SecurityStateMigrationError, ensure_security_state

CODEQL_QUERY_SUITES = {
    "python": "codeql/python-queries:codeql-suites/python-security-and-quality.qls",
    "javascript": "codeql/javascript-queries:codeql-suites/javascript-security-and-quality.qls",
    "java": "codeql/java-queries:codeql-suites/java-security-and-quality.qls",
    "go": "codeql/go-queries:codeql-suites/go-security-and-quality.qls",
}

TOOL_TIMEOUT = 600


def _run_tool(cmd: list[str], timeout: int = TOOL_TIMEOUT) -> tuple[int, str, str]:
    """Run a tool, returning (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Tool timed out after {timeout}s"
    except OSError as e:
        return -1, "", str(e)


def _sanitize_stderr(stderr: str, project_path: Path) -> str:
    """Strip project path from stderr to avoid leaking filesystem paths in reports."""
    if not stderr:
        return ""
    abs_str = str(project_path.resolve())
    raw_str = str(project_path)
    result = stderr.replace(abs_str, "<project>")
    if raw_str != abs_str:
        result = result.replace(raw_str, "<project>")
    return result


def _detect_language(project_path: Path) -> str:
    """Detect project language from manifest or file heuristics."""
    manifest_path = project_path / ".aah" / "manifest.yaml"
    if manifest_path.exists():
        manifest = read_yaml(manifest_path) or {}
        lang = manifest.get("stack_choices", {}).get("language", "")
        if lang:
            return lang.lower()
    return "python"


def run_deep_sast(project_path: Path, language: str | None = None) -> dict:
    """Run CodeQL deep SAST analysis."""
    aah_path = ensure_security_state(project_path, notify=True).security_dir.parent
    raw_dir = ensure_dir(aah_path / "security" / "raw")
    reports_dir = ensure_dir(aah_path / "security" / "reports")

    tools = tool_check.check_tools()
    if not tools.get("codeql", {}).get("available", False):
        return {
            "tool": "codeql",
            "findings": [],
            "errors": [{
                "tool": "codeql",
                "error_type": "not_installed",
                "message": "CodeQL CLI not found. Install from https://github.com/github/codeql-cli-binaries/releases",
                "stderr_tail": "",
            }],
        }

    lang = language or _detect_language(project_path)
    query_suite = CODEQL_QUERY_SUITES.get(lang)
    if not query_suite:
        return {
            "tool": "codeql",
            "findings": [],
            "errors": [{
                "tool": "codeql",
                "error_type": "unsupported_language",
                "message": f"No CodeQL query suite for language: {lang}",
                "stderr_tail": "",
            }],
        }

    db_path = raw_dir / "codeql-db"
    sarif_path = raw_dir / "codeql-results.sarif"

    # Create CodeQL database
    cmd_create = [
        "codeql", "database", "create", str(db_path),
        f"--language={lang}",
        f"--source-root={project_path}",
        "--overwrite",
    ]
    returncode, stdout, stderr = _run_tool(cmd_create)
    if returncode != 0:
        return {
            "tool": "codeql",
            "findings": [],
            "errors": [{
                "tool": "codeql",
                "error_type": "crash",
                "message": f"Database creation failed (exit {returncode})",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            }],
        }

    # Run analysis
    cmd_analyze = [
        "codeql", "database", "analyze", str(db_path),
        "--format=sarifv2.1.0",
        f"--output={sarif_path}",
        query_suite,
    ]
    returncode, stdout, stderr = _run_tool(cmd_analyze)
    if returncode != 0 and not sarif_path.exists():
        return {
            "tool": "codeql",
            "findings": [],
            "errors": [{
                "tool": "codeql",
                "error_type": "crash",
                "message": f"Analysis failed (exit {returncode})",
                "stderr_tail": _sanitize_stderr(stderr[-500:], project_path) if stderr else "",
            }],
        }

    findings = parsers_phase2.parse_codeql_sarif(sarif_path, project_path)

    return {
        "tool": "codeql",
        "findings": findings,
        "errors": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Deep SAST Scanner (CodeQL)")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run CodeQL deep SAST scan")
    run_parser.add_argument(
        "--language",
        choices=list(CODEQL_QUERY_SUITES.keys()),
        help="Target language (default: auto-detect)",
    )
    run_parser.add_argument("--project-path", help="Explicit project path")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    explicit = Path(args.project_path) if args.project_path else None
    project_path = resolve_project_path(explicit)
    if project_path is None:
        print("Error: could not resolve project path", file=sys.stderr)
        sys.exit(1)

    try:
        ensure_security_state(project_path, notify=True)
    except SecurityStateMigrationError as exc:
        print(f"Error: security state migration failed: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"Running CodeQL deep SAST scan...")
    print(f"Project: {project_path}")

    result = run_deep_sast(project_path, args.language)

    findings = result["findings"]
    errors = result["errors"]
    print(f"\nCodeQL: {len(findings)} findings, {len(errors)} errors")

    if errors:
        for err in errors:
            print(f"  Error: {err['message']}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
