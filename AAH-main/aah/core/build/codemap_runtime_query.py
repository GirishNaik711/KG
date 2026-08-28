#!/usr/bin/env python3
"""Runtime on-demand codemap querying — agent-driven, stored in feature YAML.

Replaces the pre-dispatch static generation model (codemap_pre_query.py).
The aah-feature-implementer agent decides what to query, when to query, and how
many times to query. All queries and responses are stored directly in the
feature YAML under `codemap_context.queries`.

Usage:
    aah run core.build.codemap_runtime_query ask \
        --query "How does the auth middleware chain work?" \
        --feature-id F001 --wave 2 \
        [--project-path /path/to/project] \
        [--top-k 10] \
        [--impact-files "src/auth.py,src/middleware.py"]

    aah run core.build.codemap_runtime_query check \
        --feature-id F001 [--project-path /path] [--wave 2]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.codemap_utils import get_codemap, get_db_path, is_available
from aah.core.common.feature_utils import append_section, find_feature_file, parse_feature_frontmatter
from aah.core.common.io_utils import read_yaml
from aah.core.common.progress import load_progress


def ask_codemap(
    project_path: Path,
    feature_id: str,
    wave: int,
    query: str,
    top_k: int = 10,
    impact_files: list[str] | None = None,
) -> dict:
    """Run a codemap query and append the result to the feature YAML.

    Returns structured JSON response with relevant symbols and file impact.
    """
    aah_path = project_path / ".aah"
    features_dir = aah_path / "plan" / "features"

    # Locate feature file
    feature_yaml_path = find_feature_file(features_dir, feature_id)
    if not feature_yaml_path:
        return {
            "success": False,
            "feature_id": feature_id,
            "error": f"Feature file not found for {feature_id}",
        }

    # Check codemap.db exists
    codemap_db = get_db_path(project_path)
    if not codemap_db.exists():
        return {
            "success": False,
            "feature_id": feature_id,
            "error": "codemap.db does not exist. Run codemap scout first.",
            "codemap_available": False,
        }

    if not is_available():
        return {
            "success": False,
            "feature_id": feature_id,
            "error": "codemap-scale not installed.",
            "codemap_available": False,
        }

    # Load codemap instance
    cm = get_codemap(project_path)

    # Run ask query
    try:
        ask_response = cm.ask(query, top_k=top_k)
        relevant_symbols = ask_response.get("results", [])
    except Exception as e:
        relevant_symbols = [{"error": str(e)}]

    # Run impact analysis for specified files
    file_impact = []
    if impact_files:
        for file_path in impact_files:
            try:
                impact = cm.tools.analyze_impact(file_path)
                file_impact.append(impact)
            except Exception as e:
                file_impact.append({
                    "file": file_path,
                    "error": str(e),
                    "direct_dependents": [],
                    "transitive_dependents": [],
                    "total_impact_radius": 0,
                })

    # Derive file_scope from relevant symbols
    file_scope = sorted(set(
        s.get("file", "").split(":")[0]
        for s in relevant_symbols
        if isinstance(s, dict) and s.get("file") and not s.get("error")
    ))

    # Build query record
    timestamp = datetime.now(timezone.utc).isoformat()
    query_record = {
        "query": query,
        "timestamp": timestamp,
        "response": {
            "relevant_symbols": relevant_symbols,
            "file_impact": file_impact,
            "file_scope": file_scope,
        },
    }

    # Append codemap query to the feature .md file as a section
    section_lines = [
        f"- Wave: {wave}",
        f"- Query: {query}",
        f"- Timestamp: {timestamp}",
        "- Relevant symbols:",
    ]
    for sym in relevant_symbols[:5]:
        if isinstance(sym, dict) and not sym.get("error"):
            section_lines.append(f"  - {sym.get('file', '')} ({sym.get('name', '')})")
    if file_scope:
        section_lines.append("- File scope:")
        for fp in file_scope:
            section_lines.append(f"  - {fp}")

    append_section(feature_yaml_path, "Codemap Context", section_lines, accumulate=True)

    # Count queries by reading existing section
    content = feature_yaml_path.read_text(encoding="utf-8")
    total_queries = content.count("- Query:")

    return {
        "success": True,
        "feature_id": feature_id,
        "query": query,
        "timestamp": timestamp,
        "response": {
            "relevant_symbols": relevant_symbols,
            "file_impact": file_impact,
            "file_scope": file_scope,
        },
        "total_queries_for_feature": total_queries,
    }


def check_queries(
    project_path: Path,
    feature_id: str,
    wave: int | None = None,
) -> dict:
    """Verify at least one codemap query exists in the feature YAML.

    Used by QA evaluator to enforce minimum query requirement.
    Returns check result with query count.
    """
    aah_path = project_path / ".aah"
    features_dir = aah_path / "plan" / "features"
    codemap_db = get_db_path(project_path)

    # Greenfield wave 0 with no codemap.db auto-passes
    if wave is not None:
        manifest_path = aah_path / "manifest.yaml"
        if manifest_path.exists():
            manifest = read_yaml(manifest_path)
            project_type = manifest.get("project_type", "greenfield")

            if project_type == "greenfield" and wave == 0 and not codemap_db.exists():
                return {
                    "feature_id": feature_id,
                    "has_queries": True,
                    "query_count": 0,
                    "codemap_available": False,
                    "reason": "greenfield_no_db",
                }

    # If no codemap.db at all (regardless of wave), auto-pass
    if not codemap_db.exists():
        return {
            "feature_id": feature_id,
            "has_queries": True,
            "query_count": 0,
            "codemap_available": False,
            "reason": "no_codemap_db",
        }

    # Locate feature file
    feature_yaml_path = find_feature_file(features_dir, feature_id)
    if not feature_yaml_path:
        return {
            "feature_id": feature_id,
            "has_queries": False,
            "query_count": 0,
            "codemap_available": True,
            "error": f"Feature file not found for {feature_id}",
        }

    # Check for queries in ## Codemap Context markdown section
    # (queries are stored as markdown via append_section, not as a YAML dict)
    try:
        content = feature_yaml_path.read_text(encoding="utf-8")
        query_count = content.count("- Query:")
    except Exception:
        query_count = 0

    return {
        "feature_id": feature_id,
        "has_queries": query_count > 0,
        "query_count": query_count,
        "codemap_available": True,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Runtime on-demand codemap querying (agent-driven)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ask subcommand
    ask_p = sub.add_parser("ask", help="Run a codemap query and store in feature YAML")
    ask_p.add_argument("--query", type=str, required=True, help="The query to ask codemap")
    ask_p.add_argument("--feature-id", type=str, required=True, help="Feature ID (e.g., F001)")
    ask_p.add_argument("--wave", type=int, required=True, help="Current wave number")
    ask_p.add_argument("--project-path", type=Path, default=None, help="Project root path")
    ask_p.add_argument("--top-k", type=int, default=10, help="Number of top results to return")
    ask_p.add_argument(
        "--impact-files", type=str, default=None,
        help="Comma-separated list of files to analyze impact for"
    )

    # check subcommand
    check_p = sub.add_parser("check", help="Verify feature has codemap queries (QA enforcement)")
    check_p.add_argument("--feature-id", type=str, required=True, help="Feature ID (e.g., F001)")
    check_p.add_argument("--project-path", type=Path, default=None, help="Project root path")
    check_p.add_argument("--wave", type=int, default=None, help="Current wave number")

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "ask":
        impact_files = None
        if args.impact_files:
            impact_files = [f.strip() for f in args.impact_files.split(",") if f.strip()]

        result = ask_codemap(
            project_path=project_path,
            feature_id=args.feature_id,
            wave=args.wave,
            query=args.query,
            top_k=args.top_k,
            impact_files=impact_files,
        )
        json.dump(result, sys.stdout, indent=2)
        print()

        if result.get("success"):
            print(
                f"Query stored in feature YAML ({result['total_queries_for_feature']} total).",
                file=sys.stderr,
            )
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "check":
        result = check_queries(
            project_path=project_path,
            feature_id=args.feature_id,
            wave=args.wave,
        )
        json.dump(result, sys.stdout, indent=2)
        print()

        if result["has_queries"]:
            reason = result.get("reason", "")
            if reason:
                print(f"Check passed: {reason}", file=sys.stderr)
            else:
                print(f"Check passed: {result['query_count']} queries found.", file=sys.stderr)
        else:
            print("Check FAILED: No codemap queries found in feature YAML.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
