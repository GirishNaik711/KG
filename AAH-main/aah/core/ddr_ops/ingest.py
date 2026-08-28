#!/usr/bin/env python3
"""Ingest source material from URLs, PDFs, and Word documents for DDR creation."""

import argparse
import json
import sys
import tempfile
from pathlib import Path


def extract_from_url(url: str) -> dict:
    """Extract text content from a URL.

    Note: In the RAPIDS framework, actual URL fetching is performed by Claude's
    WebFetch tool. This function provides the structure for the result.
    """
    return {
        "source_type": "url",
        "source": url,
        "status": "pending",
        "note": "URL content extraction is handled by Claude's WebFetch tool. "
                "Pass the URL to the LLM for fetching and analysis.",
    }


def extract_from_pdf(file_path: Path) -> dict:
    """Extract text content from a PDF file using LiteParse.

    Falls back to basic extraction if LiteParse is unavailable.
    """
    if not file_path.exists():
        return {"source_type": "pdf", "source": str(file_path), "status": "error",
                "error": f"File not found: {file_path}"}

    try:
        from liteparse import parse_file
        result = parse_file(str(file_path))
        content = result.get("text", "") if isinstance(result, dict) else str(result)
        return {
            "source_type": "pdf",
            "source": str(file_path),
            "status": "success",
            "content": content,
            "content_length": len(content),
        }
    except ImportError:
        # LiteParse not available — try basic PyPDF fallback
        try:
            import subprocess
            # Use pdftotext if available (part of poppler-utils)
            result = subprocess.run(
                ["pdftotext", str(file_path), "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return {
                    "source_type": "pdf",
                    "source": str(file_path),
                    "status": "success",
                    "content": result.stdout,
                    "content_length": len(result.stdout),
                    "method": "pdftotext",
                }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return {
            "source_type": "pdf",
            "source": str(file_path),
            "status": "fallback",
            "note": "LiteParse and pdftotext unavailable. Pass the file path to Claude "
                    "for direct PDF reading via multi-modal capabilities.",
        }


def extract_from_docx(file_path: Path) -> dict:
    """Extract text content from a Word (.docx) file.

    Uses LiteParse or falls back to basic extraction.
    """
    if not file_path.exists():
        return {"source_type": "docx", "source": str(file_path), "status": "error",
                "error": f"File not found: {file_path}"}

    try:
        from liteparse import parse_file
        result = parse_file(str(file_path))
        content = result.get("text", "") if isinstance(result, dict) else str(result)
        return {
            "source_type": "docx",
            "source": str(file_path),
            "status": "success",
            "content": content,
            "content_length": len(content),
        }
    except ImportError:
        # Try python-docx as fallback
        try:
            from docx import Document
            doc = Document(str(file_path))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            content = "\n\n".join(paragraphs)
            return {
                "source_type": "docx",
                "source": str(file_path),
                "status": "success",
                "content": content,
                "content_length": len(content),
                "method": "python-docx",
            }
        except ImportError:
            return {
                "source_type": "docx",
                "source": str(file_path),
                "status": "fallback",
                "note": "LiteParse and python-docx unavailable. Pass the file path to Claude "
                        "for direct document reading.",
            }


def extract_from_text(file_path: Path) -> dict:
    """Extract content from a plain text or markdown file."""
    if not file_path.exists():
        return {"source_type": "text", "source": str(file_path), "status": "error",
                "error": f"File not found: {file_path}"}

    content = file_path.read_text(encoding="utf-8")
    return {
        "source_type": "text",
        "source": str(file_path),
        "status": "success",
        "content": content,
        "content_length": len(content),
    }


def detect_source_type(source: str) -> str:
    """Detect the type of source from its value."""
    if source.startswith(("http://", "https://")):
        return "url"

    path = Path(source)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    elif suffix in (".docx", ".doc"):
        return "docx"
    elif suffix in (".txt", ".md", ".yaml", ".yml", ".json"):
        return "text"
    else:
        # Assume text if file exists, otherwise might be inline content
        if path.exists():
            return "text"
        return "inline"


def extract(source: str) -> dict:
    """Auto-detect source type and extract content."""
    source_type = detect_source_type(source)

    if source_type == "url":
        return extract_from_url(source)
    elif source_type == "pdf":
        return extract_from_pdf(Path(source))
    elif source_type == "docx":
        return extract_from_docx(Path(source))
    elif source_type == "text":
        return extract_from_text(Path(source))
    elif source_type == "inline":
        return {
            "source_type": "inline",
            "source": "(inline content)",
            "status": "success",
            "content": source,
            "content_length": len(source),
        }
    else:
        return {"source_type": "unknown", "source": source, "status": "error",
                "error": f"Unsupported source type: {source_type}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR ingest — extract content from sources")
    sub = parser.add_subparsers(dest="command", required=True)

    # extract
    extract_p = sub.add_parser("extract", help="Extract text from a source (auto-detect type)")
    extract_p.add_argument("--source", type=str, required=True, help="URL, file path, or inline text")
    extract_p.add_argument("--output", type=Path, default=None,
                           help="Write extracted content to this file")

    # parse-pdf
    pdf_p = sub.add_parser("parse-pdf", help="Parse a PDF file")
    pdf_p.add_argument("--path", type=Path, required=True)
    pdf_p.add_argument("--output", type=Path, default=None)

    # parse-docx
    docx_p = sub.add_parser("parse-docx", help="Parse a Word document")
    docx_p.add_argument("--path", type=Path, required=True)
    docx_p.add_argument("--output", type=Path, default=None)

    # batch
    batch_p = sub.add_parser("batch", help="Extract from multiple sources")
    batch_p.add_argument("--sources", type=str, required=True,
                         help="Comma-separated list of sources (URLs, paths)")
    batch_p.add_argument("--output-dir", type=Path, default=None,
                         help="Directory to write extracted content files")

    args = parser.parse_args()

    if args.command == "extract":
        result = extract(args.source)
        if args.output and result.get("content"):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result["content"], encoding="utf-8")
            result["output_path"] = str(args.output)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "parse-pdf":
        result = extract_from_pdf(args.path)
        if args.output and result.get("content"):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result["content"], encoding="utf-8")
            result["output_path"] = str(args.output)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "parse-docx":
        result = extract_from_docx(args.path)
        if args.output and result.get("content"):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result["content"], encoding="utf-8")
            result["output_path"] = str(args.output)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "batch":
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        results = []

        for i, source in enumerate(sources):
            result = extract(source)
            if args.output_dir and result.get("content"):
                args.output_dir.mkdir(parents=True, exist_ok=True)
                output_file = args.output_dir / f"source-{i:03d}.txt"
                output_file.write_text(result["content"], encoding="utf-8")
                result["output_path"] = str(output_file)
            results.append(result)

        json.dump({"sources": results, "total": len(results)}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
