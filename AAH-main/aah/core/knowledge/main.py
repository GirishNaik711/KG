#!/usr/bin/env python3
"""
CLI for knowledge base parsing and status.

Usage:
  aah run core.knowledge.main init           # create knowledge folder and show guidance
  aah run core.knowledge.main parse          # parse all docs in knowledge folder
  aah run core.knowledge.main status         # show what's in the knowledge base
  aah run core.knowledge.main context        # print full context string (for debugging)
  aah run core.knowledge.main clear-cache    # delete all cached parses (force re-parse)
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from aah.core.knowledge.parser import (
    PARSEABLE_EXTENSIONS,
    PLAIN_TEXT_EXTENSIONS,
    build_knowledge_context,
    find_knowledge_dir,
    get_knowledge_index,
    parse_knowledge_dir,
)


def cmd_init(project_path: Path) -> None:
    """Create knowledge/ folder and display guidance on supported file types."""
    existing = find_knowledge_dir(project_path)
    if existing:
        print(f"Knowledge base already exists: {existing}", file=sys.stderr)
        json.dump({
            "created": False,
            "path": str(existing),
            "reason": "already_exists",
        }, sys.stdout, indent=2)
        print()
        return

    knowledge_dir = project_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # Categorize supported formats for display
    doc_formats = sorted(ext for ext in PARSEABLE_EXTENSIONS - PLAIN_TEXT_EXTENSIONS
                         if ext in {".pdf", ".doc", ".docx", ".docm", ".odt", ".rtf"})
    presentation_formats = sorted(ext for ext in PARSEABLE_EXTENSIONS
                                  if ext in {".ppt", ".pptx", ".pptm", ".odp"})
    spreadsheet_formats = sorted(ext for ext in PARSEABLE_EXTENSIONS
                                 if ext in {".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv"})
    image_formats = sorted(ext for ext in PARSEABLE_EXTENSIONS
                           if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"})
    text_formats = sorted(PLAIN_TEXT_EXTENSIONS)

    print(f"\nCreated: {knowledge_dir}/", file=sys.stderr)
    print("\nSupported file types:", file=sys.stderr)
    print(f"  Documents:      {', '.join(doc_formats)}", file=sys.stderr)
    print(f"  Presentations:  {', '.join(presentation_formats)}", file=sys.stderr)
    print(f"  Spreadsheets:   {', '.join(spreadsheet_formats)}", file=sys.stderr)
    print(f"  Images:         {', '.join(image_formats)}", file=sys.stderr)
    print(f"  Plain text:     {', '.join(text_formats)}", file=sys.stderr)
    print("\nPlace your project documents in this folder, then confirm.", file=sys.stderr)

    json.dump({
        "created": True,
        "path": str(knowledge_dir),
        "supported_formats": {
            "documents": doc_formats,
            "presentations": presentation_formats,
            "spreadsheets": spreadsheet_formats,
            "images": image_formats,
            "plain_text": text_formats,
        },
    }, sys.stdout, indent=2)
    print()


def cmd_parse(project_path: Path) -> None:
    knowledge_dir = find_knowledge_dir(project_path)
    if not knowledge_dir:
        print(f"No knowledge folder found in {project_path}", file=sys.stderr)
        print("Create one of: knowledge/, knowledge-base/, project-documentation/", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing knowledge base: {knowledge_dir}", file=sys.stderr)
    results = parse_knowledge_dir(project_path)

    parsed = [r for r in results if not r.get("error")]
    errors = [r for r in results if r.get("error")]
    cached = [r for r in parsed if r.get("cached")]

    print(f"\nResults:", file=sys.stderr)
    for r in results:
        icon = "✓" if not r.get("error") else "✗"
        cached_note = " (cached)" if r.get("cached") else " (parsed)"
        err_note = f" ERROR: {r['error']}" if r.get("error") else ""
        print(f"  {icon} {r['file_name']}{cached_note}{err_note}", file=sys.stderr)

    print(f"\n{len(parsed)} parsed ({len(cached)} from cache), {len(errors)} failed", file=sys.stderr)

    json.dump({
        "documents_found": len(results),
        "documents_parsed": len(parsed),
        "documents_cached": len(cached),
        "documents_failed": len(errors),
        "errors": [{"file": r["file_name"], "error": r["error"]} for r in errors],
    }, sys.stdout, indent=2)
    print()


def cmd_status(project_path: Path) -> None:
    index = get_knowledge_index(project_path)

    if not index["found"]:
        print("No knowledge base found.", file=sys.stderr)
        print("Create one of these folders in your project root:", file=sys.stderr)
        print("  knowledge/", file=sys.stderr)
        print("  knowledge-base/", file=sys.stderr)
        print("  project-documentation/", file=sys.stderr)
        json.dump(index, sys.stdout, indent=2)
        print()
        return

    print(f"\nKnowledge base: {index['folder']}/", file=sys.stderr)
    print(f"{index['document_count']} document(s):\n", file=sys.stderr)
    for doc in index["documents"]:
        status = "✓" if doc["parsed"] else "✗"
        cached = " [cached]" if doc["cached"] else ""
        err = f" — {doc['error']}" if doc.get("error") else ""
        print(f"  {status} {doc['name']}{cached}{err}", file=sys.stderr)

    json.dump(index, sys.stdout, indent=2)
    print()


def cmd_context(project_path: Path) -> None:
    """Print the full knowledge context string (for debugging injection)."""
    ctx = build_knowledge_context(project_path)
    if not ctx:
        print("No knowledge context available (no folder or no documents).", file=sys.stderr)
    else:
        print(ctx)


def cmd_clear_cache(project_path: Path) -> None:
    cache_dir = project_path / ".aah" / "knowledge" / "parsed"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
        print(f"Cache cleared: {cache_dir}", file=sys.stderr)
    else:
        print("No cache to clear.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS knowledge base management")
    parser.add_argument("command", choices=["init", "parse", "status", "context", "clear-cache"])
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    # If an explicit path is given, trust it as long as it exists.
    # require_project_path requires manifest.yaml which may not exist yet
    # (e.g., during intake phase or in test scenarios).
    if args.project_path and args.project_path.is_dir():
        project_path = args.project_path.resolve()
    else:
        from aah.core.common.config import require_project_path
        project_path = require_project_path(args.project_path)

    if args.command == "init":
        cmd_init(project_path)
    elif args.command == "parse":
        cmd_parse(project_path)
    elif args.command == "status":
        cmd_status(project_path)
    elif args.command == "context":
        cmd_context(project_path)
    elif args.command == "clear-cache":
        cmd_clear_cache(project_path)


if __name__ == "__main__":
    main()
