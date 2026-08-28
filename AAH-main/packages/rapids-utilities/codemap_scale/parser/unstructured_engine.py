"""
Unstructured document parser engine using LiteParse.

Handles non-code files: PDFs, Office documents, images, markdown, and plain text.
Uses LiteParse (https://github.com/run-llama/liteparse) as the parsing backend
for PDFs and Office docs, with native Python fallback for text-based formats.

Design:
- LiteParse CLI (`lit parse`) handles PDFs, DOCX, PPTX, XLSX via subprocess
- Native Python handles .md, .rst, .txt, .csv directly (no external dependency)
- All documents are stored as files with kind="document" in the graph
- Document sections (headings, paragraphs, tables) become symbols
- Cross-references between docs and code are detected as relations
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# File extensions we handle
_DOCUMENT_EXTENSIONS: dict[str, str] = {
    # Native text formats (parsed in Python)
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".txt": "plaintext",
    ".csv": "csv",
    ".tsv": "csv",
    # LiteParse-handled formats
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".odt": "odt",
    ".odp": "odp",
    # Image formats (LiteParse OCR)
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".bmp": "image",
    ".tiff": "image",
    ".webp": "image",
    ".svg": "image",
}

# Formats that need LiteParse CLI
_LITEPARSE_FORMATS = {
    "pdf", "docx", "doc", "pptx", "ppt",
    "xlsx", "xls", "odt", "odp", "image",
}

# Formats we handle natively in Python
_NATIVE_FORMATS = {"markdown", "restructuredtext", "plaintext", "csv"}


def is_document_file(suffix: str) -> str | None:
    """Return document type for a file suffix, or None if not a document."""
    return _DOCUMENT_EXTENSIONS.get(suffix.lower())


def has_liteparse() -> bool:
    """Check if LiteParse CLI (`lit`) is available on PATH."""
    return shutil.which("lit") is not None


def _hash_content(content: bytes) -> str:
    """Hash document content using SHA-256."""
    return hashlib.sha256(content).hexdigest()


# -------------------------------------------------------------------
# Native text parsers
# -------------------------------------------------------------------

def _parse_markdown(text: str, file_path: str) -> list[dict[str, Any]]:
    """Extract sections from markdown text as symbols."""
    symbols: list[dict[str, Any]] = []
    lines = text.split("\n")
    current_heading = None
    heading_start = 0

    for i, line in enumerate(lines):
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            # Close previous heading section
            if current_heading is not None:
                symbols.append(_make_section_symbol(
                    name=current_heading,
                    kind="heading",
                    file_path=file_path,
                    start_line=heading_start + 1,
                    end_line=i,
                    level=len(heading_match.group(1)),
                ))
            current_heading = heading_match.group(2).strip()
            heading_start = i

    # Close last section
    if current_heading is not None:
        symbols.append(_make_section_symbol(
            name=current_heading,
            kind="heading",
            file_path=file_path,
            start_line=heading_start + 1,
            end_line=len(lines),
            level=1,
        ))

    # If no headings found, create a single document symbol
    if not symbols and text.strip():
        symbols.append(_make_section_symbol(
            name=Path(file_path).stem,
            kind="section",
            file_path=file_path,
            start_line=1,
            end_line=len(lines),
        ))

    return symbols


def _parse_rst(text: str, file_path: str) -> list[dict[str, Any]]:
    """Extract sections from reStructuredText."""
    symbols: list[dict[str, Any]] = []
    lines = text.split("\n")
    underline_chars = set("=-~`^_*+#")

    for i, line in enumerate(lines):
        if (
            i > 0
            and line.strip()
            and len(set(line.strip())) == 1
            and line.strip()[0] in underline_chars
            and len(line.strip()) >= len(lines[i - 1].strip())
            and lines[i - 1].strip()
        ):
            heading_text = lines[i - 1].strip()
            symbols.append(_make_section_symbol(
                name=heading_text,
                kind="heading",
                file_path=file_path,
                start_line=i,  # previous line
                end_line=i + 1,
            ))

    if not symbols and text.strip():
        symbols.append(_make_section_symbol(
            name=Path(file_path).stem,
            kind="section",
            file_path=file_path,
            start_line=1,
            end_line=len(lines),
        ))

    return symbols


def _parse_plaintext(text: str, file_path: str) -> list[dict[str, Any]]:
    """Create a single section symbol for plain text files."""
    if not text.strip():
        return []
    lines = text.split("\n")
    return [_make_section_symbol(
        name=Path(file_path).stem,
        kind="section",
        file_path=file_path,
        start_line=1,
        end_line=len(lines),
    )]


def _parse_csv(text: str, file_path: str) -> list[dict[str, Any]]:
    """Extract header row from CSV as a table symbol."""
    lines = text.split("\n")
    if not lines or not lines[0].strip():
        return []

    header = lines[0].strip()
    columns = [c.strip().strip('"') for c in header.split(",")]

    return [_make_section_symbol(
        name=f"table({', '.join(columns[:5])}{'...' if len(columns) > 5 else ''})",
        kind="table",
        file_path=file_path,
        start_line=1,
        end_line=len(lines),
        metadata={"columns": columns, "row_count": len(lines) - 1},
    )]


_NATIVE_PARSERS = {
    "markdown": _parse_markdown,
    "restructuredtext": _parse_rst,
    "plaintext": _parse_plaintext,
    "csv": _parse_csv,
}


# -------------------------------------------------------------------
# LiteParse integration
# -------------------------------------------------------------------

def _parse_with_liteparse(file_path: Path, rel_path: str) -> dict[str, Any] | None:
    """
    Parse a document using LiteParse CLI.

    Returns a dict with extracted text, sections, and metadata.
    Falls back to basic metadata if LiteParse is not available.
    """
    if not has_liteparse():
        logger.debug("LiteParse not available, creating metadata-only entry for %s", rel_path)
        return _metadata_only_entry(file_path, rel_path)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "output.json"
            result = subprocess.run(
                ["lit", "parse", str(file_path), "--format", "json", "-o", str(out_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                logger.warning("LiteParse failed for %s: %s", rel_path, result.stderr[:200])
                return _metadata_only_entry(file_path, rel_path)

            if out_path.exists():
                parsed = json.loads(out_path.read_text())
                return _liteparse_result_to_symbols(parsed, rel_path, file_path)
            else:
                # Try reading from stdout
                if result.stdout.strip():
                    return _liteparse_text_to_symbols(result.stdout, rel_path, file_path)
                return _metadata_only_entry(file_path, rel_path)

    except subprocess.TimeoutExpired:
        logger.warning("LiteParse timed out for %s", rel_path)
        return _metadata_only_entry(file_path, rel_path)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("LiteParse error for %s: %s", rel_path, e)
        return _metadata_only_entry(file_path, rel_path)


def _liteparse_result_to_symbols(
    parsed: dict | list, rel_path: str, file_path: Path
) -> dict[str, Any]:
    """Convert LiteParse JSON output to our symbol format."""
    symbols: list[dict[str, Any]] = []
    full_text = ""

    # LiteParse returns pages as list or dict with pages key
    pages = parsed if isinstance(parsed, list) else parsed.get("pages", [parsed])

    for page_num, page in enumerate(pages, 1):
        page_text = page.get("text", "") if isinstance(page, dict) else str(page)
        if page_text.strip():
            full_text += page_text + "\n"
            symbols.append(_make_section_symbol(
                name=f"page_{page_num}",
                kind="section",
                file_path=rel_path,
                start_line=page_num,
                end_line=page_num,
                metadata={"page": page_num, "source": "liteparse"},
            ))

    stat = file_path.stat()
    content_hash = _hash_content(file_path.read_bytes())

    return {
        "path": rel_path,
        "language": is_document_file(file_path.suffix) or "document",
        "content_hash": content_hash,
        "size_bytes": stat.st_size,
        "line_count": full_text.count("\n") + 1,
        "tier": 1,
        "symbols": symbols,
        "relations": [],
        "imports": [],
        "full_text": full_text,
        "summary": full_text[:500] if full_text else None,
    }


def _liteparse_text_to_symbols(
    text: str, rel_path: str, file_path: Path
) -> dict[str, Any]:
    """Convert LiteParse plain text output to symbols."""
    symbols = _parse_markdown(text, rel_path) if text.strip() else []
    stat = file_path.stat()

    return {
        "path": rel_path,
        "language": is_document_file(file_path.suffix) or "document",
        "content_hash": _hash_content(file_path.read_bytes()),
        "size_bytes": stat.st_size,
        "line_count": text.count("\n") + 1,
        "tier": 1,
        "symbols": symbols,
        "relations": [],
        "imports": [],
        "full_text": text,
        "summary": text[:500] if text else None,
    }


def _metadata_only_entry(file_path: Path, rel_path: str) -> dict[str, Any]:
    """Create a file entry with metadata only (no parsed content)."""
    try:
        stat = file_path.stat()
        content_hash = _hash_content(file_path.read_bytes())
    except OSError:
        return None  # type: ignore[return-value]

    doc_type = is_document_file(file_path.suffix) or "document"
    return {
        "path": rel_path,
        "language": doc_type,
        "content_hash": content_hash,
        "size_bytes": stat.st_size,
        "line_count": 0,
        "tier": 0,
        "symbols": [],
        "relations": [],
        "imports": [],
        "full_text": None,
        "summary": None,
    }


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _make_section_symbol(
    *,
    name: str,
    kind: str,
    file_path: str,
    start_line: int,
    end_line: int,
    level: int = 0,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Create a document section symbol dict."""
    module = file_path.replace("/", ".").replace("\\", ".")
    # Remove extension
    for ext in _DOCUMENT_EXTENSIONS:
        if module.endswith(ext):
            module = module[: -len(ext)]
            break

    fqn = f"{module}.{_sanitize_name(name)}"

    sym: dict[str, Any] = {
        "fqn": fqn,
        "name": name,
        "kind": kind,
        "language": "document",
        "file_path": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "tier": 1,
        "is_exported": True,
    }
    if level:
        sym["metadata"] = {"level": level, **(metadata or {})}
    elif metadata:
        sym["metadata"] = metadata
    return sym


def _sanitize_name(name: str) -> str:
    """Sanitize a document heading/section name for use in FQN."""
    # Remove special characters, collapse whitespace
    sanitized = re.sub(r"[^\w\s-]", "", name)
    sanitized = re.sub(r"\s+", "_", sanitized.strip())
    return sanitized[:80] or "untitled"


# -------------------------------------------------------------------
# Cross-reference detection
# -------------------------------------------------------------------

def detect_doc_code_references(
    doc_text: str,
    doc_file_path: str,
    code_symbols: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Detect references from documents to code symbols.

    Scans document text for mentions of class names, function names,
    and file paths that match known code symbols.

    Returns relations with provenance='INFERRED' and confidence scores.
    """
    relations: list[dict[str, Any]] = []
    if not doc_text:
        return relations

    doc_fqn_base = doc_file_path.replace("/", ".").replace("\\", ".")
    for ext in _DOCUMENT_EXTENSIONS:
        if doc_fqn_base.endswith(ext):
            doc_fqn_base = doc_fqn_base[: -len(ext)]
            break

    # Build lookup of symbol names → fqns
    name_to_fqns: dict[str, list[str]] = {}
    for sym in code_symbols:
        name = sym.get("name", "")
        if len(name) >= 3:  # Skip very short names to avoid false positives
            name_to_fqns.setdefault(name, []).append(sym["fqn"])

    # Search for symbol names in document text
    doc_lower = doc_text.lower()
    seen: set[str] = set()

    for name, fqns in name_to_fqns.items():
        if len(name) < 4:
            continue

        # Use word boundary matching
        pattern = r"\b" + re.escape(name) + r"\b"
        matches = list(re.finditer(pattern, doc_text, re.IGNORECASE))

        if matches:
            confidence = min(1.0, len(matches) * 0.3)
            for target_fqn in fqns:
                key = f"{doc_fqn_base}->{target_fqn}"
                if key not in seen:
                    seen.add(key)
                    relations.append({
                        "source_fqn": f"{doc_fqn_base}.document",
                        "target_fqn": target_fqn,
                        "kind": "references",
                        "file_path": doc_file_path,
                        "line": None,
                        "confidence": round(confidence, 2),
                        "provenance": "INFERRED",
                    })

    return relations


# -------------------------------------------------------------------
# Unstructured Parser Engine
# -------------------------------------------------------------------

class UnstructuredParserEngine:
    """
    Parser engine for unstructured documents (PDFs, Office, markdown, etc.).

    Usage:
        engine = UnstructuredParserEngine(root="/path/to/repo")

        # Discover all document files
        docs = engine.walk_documents()

        # Parse documents (uses LiteParse for PDFs/Office, native for text)
        for batch in engine.parse_documents():
            graph.upsert_files_batch(batch["files"])
            graph.upsert_symbols_batch(batch["symbols"])
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_file_size_kb: int = 10_000,  # 10MB default for documents
        ignore_patterns: list[str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_file_size_kb = max_file_size_kb
        self.ignore_patterns = ignore_patterns or [
            "node_modules", "__pycache__", ".git", ".codemap", "venv", ".venv",
            "dist", "build", ".tox",
        ]
        self._liteparse_available: bool | None = None

    @property
    def liteparse_available(self) -> bool:
        if self._liteparse_available is None:
            self._liteparse_available = has_liteparse()
            if self._liteparse_available:
                logger.info("LiteParse CLI available — will parse PDFs/Office docs")
            else:
                logger.info("LiteParse CLI not found — PDFs/Office docs will be metadata-only")
        return self._liteparse_available

    def walk_documents(self) -> list[Path]:
        """Walk filesystem and return document file paths."""
        import fnmatch

        files: list[Path] = []
        root_str = str(self.root)

        for dirpath, dirnames, filenames in os.walk(root_str):
            dirnames[:] = [
                d for d in dirnames
                if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)
            ]

            for filename in filenames:
                suffix = Path(filename).suffix.lower()
                if is_document_file(suffix) is None:
                    continue
                full = Path(dirpath) / filename
                try:
                    if full.stat().st_size > self.max_file_size_kb * 1024:
                        continue
                except OSError:
                    continue
                files.append(full)

        return files

    def parse_documents(
        self,
        file_paths: list[Path] | None = None,
        batch_size: int = 100,
    ) -> Iterator[dict[str, list]]:
        """
        Parse document files and yield batches.

        Yields: {"files": [...], "symbols": [...], "relations": [...]}
        """
        paths = file_paths or self.walk_documents()

        files_batch: list[dict] = []
        symbols_batch: list[dict] = []
        relations_batch: list[dict] = []

        for path in paths:
            result = self._parse_single(path)
            if result is None:
                continue

            file_dict = {k: v for k, v in result.items()
                         if k not in ("symbols", "relations", "full_text")}
            files_batch.append(file_dict)
            symbols_batch.extend(result.get("symbols", []))
            relations_batch.extend(result.get("relations", []))

            if len(files_batch) >= batch_size:
                yield {"files": files_batch, "symbols": symbols_batch, "relations": relations_batch}
                files_batch = []
                symbols_batch = []
                relations_batch = []

        if files_batch:
            yield {"files": files_batch, "symbols": symbols_batch, "relations": relations_batch}

    def _parse_single(self, path: Path) -> dict[str, Any] | None:
        """Parse a single document file."""
        try:
            rel_path = str(path.relative_to(self.root))
        except ValueError:
            rel_path = str(path)

        suffix = path.suffix.lower()
        doc_type = is_document_file(suffix)
        if doc_type is None:
            return None

        if doc_type in _NATIVE_FORMATS:
            return self._parse_native(path, rel_path, doc_type)
        elif doc_type in _LITEPARSE_FORMATS:
            return _parse_with_liteparse(path, rel_path)
        return None

    def _parse_native(
        self, path: Path, rel_path: str, doc_type: str
    ) -> dict[str, Any] | None:
        """Parse a text-based document natively."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        parser = _NATIVE_PARSERS.get(doc_type)
        symbols = parser(text, rel_path) if parser else []

        content_hash = _hash_content(path.read_bytes())
        stat = path.stat()

        return {
            "path": rel_path,
            "language": doc_type,
            "content_hash": content_hash,
            "size_bytes": stat.st_size,
            "line_count": text.count("\n") + 1,
            "tier": 1,
            "symbols": symbols,
            "relations": [],
            "imports": [],
            "full_text": text,
            "summary": text[:500] if text else None,
        }
