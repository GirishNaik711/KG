#!/usr/bin/env python3
"""
Knowledge base discovery and parsing using LiteParse.

Scans a project's knowledge folder (any of: knowledge/, knowledge-base/,
project-documentation/), parses documents with LiteParse, and caches the
results in .aah/knowledge/ so unchanged files are not re-parsed.

Parsed content is used to inject domain knowledge into Research, Analysis,
and Planning phase context.
"""

import hashlib
import json
import sys
from pathlib import Path
from datetime import datetime, timezone


# Folder names to search for in the project root (in priority order)
KNOWLEDGE_FOLDER_NAMES = [
    "knowledge",
    "knowledge-base",
    "knowledge_base",
    "project-documentation",
    "project_documentation",
    "docs",
]

# File types LiteParse can handle
PARSEABLE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".docm", ".odt", ".rtf",
    ".ppt", ".pptx", ".pptm", ".odp",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp",
    ".html", ".htm",  # HTML — parsed to markdown, original also handed to agents
    ".md", ".txt", ".rst",  # plain text — read directly, no LiteParse needed
    ".yaml", ".yml", ".json",  # structured text — read directly, no LiteParse needed
}

PLAIN_TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".yaml", ".yml", ".json"}

# Image formats — parsed for OCR text, but the original file is also handed to
# agents by path so they can read the image directly (charts, diagrams,
# screenshots, handwriting) for exact context that OCR alone would lose.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


# HTML files — parsed to markdown text (which flattens markup), but the
# original file is also handed to agents by path so they can read the raw
# HTML directly for design/layout/styling context the markdown loses.
HTML_EXTENSIONS = {".html", ".htm"}


def _is_html(path: Path) -> bool:
    return path.suffix.lower() in HTML_EXTENSIONS


def find_knowledge_dir(project_path: Path) -> Path | None:
    """Find the knowledge folder in the project root."""
    for name in KNOWLEDGE_FOLDER_NAMES:
        candidate = project_path / name
        if candidate.is_dir():
            return candidate
    return None


def _file_hash(path: Path) -> str:
    """SHA-256 of file contents for cache invalidation."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _cache_path(cache_dir: Path, file_path: Path, file_hash: str) -> Path:
    """Return the cache file path for a given source document."""
    stem = file_path.stem[:40]
    return cache_dir / f"{stem}-{file_hash}.md"


def _is_liteparse_available() -> bool:
    try:
        from liteparse import LiteParse  # noqa: F401
        return True
    except ImportError:
        return False


def _is_markitdown_available() -> bool:
    try:
        from markitdown import MarkItDown  # noqa: F401
        return True
    except ImportError:
        return False


def _parse_with_markitdown(file_path: Path) -> str:
    """Convert a document to markdown text via MarkItDown (pure-Python fallback).

    Raises on failure so the caller can surface a combined error. Used when
    LiteParse is unavailable or fails (e.g. LibreOffice not installed for
    office files). Handles .docx/.pptx/.xlsx/.xls/.pdf/.csv/images natively;
    legacy .doc/.ppt binary formats are not supported by either engine.
    """
    from markitdown import MarkItDown
    md = MarkItDown(enable_plugins=False)
    result = md.convert(str(file_path))
    return result.text_content or ""


def _is_node_available() -> bool:
    import subprocess
    result = subprocess.run(["node", "--version"], capture_output=True, check=False)
    return result.returncode == 0


def parse_document(file_path: Path, cache_dir: Path) -> dict:
    """
    Parse a single document. Returns a dict with:
      - text: full document text
      - pages: page count
      - source: original file path
      - cached: whether result came from cache
      - error: error message if parsing failed

    Results are cached by file hash. Re-parse only happens when file changes.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    suffix = file_path.suffix.lower()

    # Plain text files — read directly, no LiteParse needed
    if suffix in PLAIN_TEXT_EXTENSIONS:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            return {
                "text": text,
                "pages": text.count("\n\n") + 1,  # rough estimate
                "source": str(file_path),
                "cached": False,
                "full_text_path": str(file_path.resolve()),
                "error": None,
            }
        except Exception as e:
            return {"text": "", "pages": 0, "source": str(file_path), "cached": False, "error": str(e)}

    if suffix not in PARSEABLE_EXTENSIONS:
        return {
            "text": "",
            "pages": 0,
            "source": str(file_path),
            "cached": False,
            "error": f"Unsupported file type: {suffix}",
        }

    # Check cache
    file_hash = _file_hash(file_path)
    cached_file = _cache_path(cache_dir, file_path, file_hash)
    if cached_file.exists():
        return {
            "text": cached_file.read_text(encoding="utf-8"),
            "pages": None,
            "source": str(file_path),
            "cached": True,
            "full_text_path": str(cached_file.resolve()),
            "error": None,
        }

    # Parse with LiteParse (primary), falling back to MarkItDown (pure-Python)
    # when LiteParse is unavailable or fails. LiteParse needs LibreOffice for
    # office formats; MarkItDown handles .docx/.pptx/.xlsx/.xls/.pdf/.csv/images
    # with no external system tools. Legacy .doc/.ppt work in neither.
    liteparse_error = None

    if _is_liteparse_available():
        try:
            from liteparse import LiteParse
            lp = LiteParse()
            result = lp.parse(str(file_path))
            text = result.text or ""
            pages = result.num_pages if hasattr(result, "num_pages") else (
                len(result.pages) if hasattr(result, "pages") and result.pages else None
            )

            # Write to cache
            cached_file.write_text(text, encoding="utf-8")

            return {
                "text": text,
                "pages": pages,
                "source": str(file_path),
                "cached": False,
                "parser": "liteparse",
                "full_text_path": str(cached_file.resolve()),
                "error": None,
            }
        except Exception as e:
            liteparse_error = str(e)

    # Fallback: MarkItDown
    if _is_markitdown_available():
        try:
            text = _parse_with_markitdown(file_path)

            # Write to cache
            cached_file.write_text(text, encoding="utf-8")

            return {
                "text": text,
                "pages": None,
                "source": str(file_path),
                "cached": False,
                "parser": "markitdown",
                "full_text_path": str(cached_file.resolve()),
                "error": None,
            }
        except Exception as e:
            markitdown_error = str(e)
            detail = f"liteparse: {liteparse_error}; " if liteparse_error else ""
            return {
                "text": "",
                "pages": 0,
                "source": str(file_path),
                "cached": False,
                "parser": None,
                "error": f"both parsers failed — {detail}markitdown: {markitdown_error}",
            }

    # Neither engine could handle it
    if liteparse_error:
        error = (
            f"liteparse failed ({liteparse_error}) and markitdown is not installed — "
            "reinstall the aah dependencies (./installers/AAH-Dependency-Installer.sh)"
        )
    else:
        error = (
            "no document parser available — liteparse and markitdown are both missing. "
            "Reinstall the aah dependencies (./installers/AAH-Dependency-Installer.sh) "
            "or run: pip install liteparse markitdown[all]"
        )
    return {
        "text": "",
        "pages": 0,
        "source": str(file_path),
        "cached": False,
        "parser": None,
        "error": error,
    }


def parse_knowledge_dir(project_path: Path) -> list[dict]:
    """
    Discover and parse all documents in the knowledge folder.
    Returns list of parse results, one per document.
    """
    knowledge_dir = find_knowledge_dir(project_path)
    if not knowledge_dir:
        return []

    cache_dir = project_path / ".aah" / "knowledge" / "parsed"
    results = []

    # Walk all files recursively
    for file_path in sorted(knowledge_dir.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in PARSEABLE_EXTENSIONS:
            continue
        if file_path.name.startswith("."):
            continue

        result = parse_document(file_path, cache_dir)
        result["relative_path"] = str(file_path.relative_to(project_path))
        result["file_name"] = file_path.name
        # For images, hand the original file path to agents so they can read
        # the image directly (multimodal) alongside the OCR text.
        result["is_image"] = _is_image(file_path)
        result["image_path"] = str(file_path.resolve()) if _is_image(file_path) else None
        # For HTML, hand the original file path too so agents can read the raw
        # markup for design/layout/styling context the flattened text loses.
        result["is_html"] = _is_html(file_path)
        result["html_path"] = str(file_path.resolve()) if _is_html(file_path) else None
        results.append(result)

    return results


def build_knowledge_context(project_path: Path, max_chars_per_doc: int = 8000) -> str:
    """
    Build a formatted knowledge context string for injection into agent prompts.

    Truncates each document to max_chars_per_doc to keep total context manageable.
    Returns empty string if no knowledge folder exists or no documents found.
    """
    results = parse_knowledge_dir(project_path)
    if not results:
        return ""

    knowledge_dir = find_knowledge_dir(project_path)
    lines = [
        f"## Project Knowledge Base",
        f"Source: `{knowledge_dir.name}/` ({len(results)} document(s))",
        "",
        "The following documents were provided by the project team. "
        "Use them to inform requirements, architecture decisions, and feature design.",
        "",
    ]

    for r in results:
        if r.get("error"):
            lines.append(f"### {r['file_name']} _(parse error: {r['error']})_")
            lines.append("")
            continue

        text = r.get("text", "").strip()

        # Images and HTML always emit an entry (with their path) even when the
        # extracted text is empty — the agent can read the original directly.
        # Other files with no text are skipped as before.
        if not text and not r.get("is_image") and not r.get("is_html"):
            continue

        lines.append(f"### {r['file_name']}")
        lines.append(f"_Path: {r['relative_path']}_")

        if r.get("is_image"):
            lines.append(
                f"_Original image — read this file directly for exact visual detail "
                f"(diagrams, charts, layout) beyond the OCR text below: "
                f"`{r['image_path']}`_"
            )
        if r.get("is_html"):
            lines.append(
                f"_Original HTML — read this file directly for design/layout/styling "
                f"detail (markup, structure) beyond the flattened text below: "
                f"`{r['html_path']}`_"
            )
        lines.append("")

        if not text:
            lines.append("_(no OCR text extracted — rely on the original image above)_")
        # Truncate with note if too long — point the agent at the full text on
        # disk so it can Read the remainder for exact context when needed.
        elif len(text) > max_chars_per_doc:
            lines.append(text[:max_chars_per_doc])
            remaining = len(text) - max_chars_per_doc
            full_path = r.get("full_text_path")
            if full_path:
                lines.append(
                    f"\n_[truncated — {remaining} more characters. Read the full "
                    f"parsed text directly for exact/complete context: `{full_path}`]_"
                )
            else:
                lines.append(f"\n_[truncated — {remaining} more characters]_")
        else:
            lines.append(text)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def get_knowledge_index(project_path: Path) -> dict:
    """
    Return metadata about the knowledge base without full text content.
    Used for status summaries.
    """
    knowledge_dir = find_knowledge_dir(project_path)
    if not knowledge_dir:
        return {"found": False, "folder": None, "documents": []}

    results = parse_knowledge_dir(project_path)
    return {
        "found": True,
        "folder": str(knowledge_dir.relative_to(project_path)),
        "document_count": len(results),
        "documents": [
            {
                "name": r["file_name"],
                "path": r["relative_path"],
                "parsed": not bool(r.get("error")),
                "cached": r.get("cached", False),
                "is_image": r.get("is_image", False),
                "image_path": r.get("image_path"),
                "is_html": r.get("is_html", False),
                "html_path": r.get("html_path"),
                "full_text_path": r.get("full_text_path"),
                "error": r.get("error"),
            }
            for r in results
        ],
    }
