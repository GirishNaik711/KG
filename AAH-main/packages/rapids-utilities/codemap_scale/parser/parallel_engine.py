"""
Parallel parser engine for large codebases (500K+ files).

Uses multiprocessing.Pool to parallelize tree-sitter parsing across
CPU cores. Each worker process gets its own parser instances (no
shared state). Results are yielded in batches for efficient SQLite
bulk insertion.

Key design decisions for scale:
- Workers parse and extract in one pass (no IPC for raw trees)
- Results are serialized as dicts (no Pydantic overhead in workers)
- Batch size tuned for SQLite transaction throughput
- Memory-bounded: processes files in chunks, never holds full codebase in RAM
"""

from __future__ import annotations

import fnmatch
import logging
import multiprocessing as mp
import os
from pathlib import Path
from typing import Any, Iterator

import xxhash

logger = logging.getLogger(__name__)

# Batch size for SQLite writes — 5000 files per transaction is optimal
WRITE_BATCH_SIZE = 5000

# Maximum files to queue per worker batch
WORKER_BATCH_SIZE = 500


def _detect_language(suffix: str) -> str | None:
    """Fast language detection from suffix (no import overhead)."""
    _map = {
        ".py": "python", ".pyi": "python",
        ".ts": "typescript", ".tsx": "tsx",
        ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
        ".java": "java", ".go": "go", ".rs": "rust",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
        ".cs": "csharp", ".rb": "ruby", ".php": "php",
        ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
        ".scala": "scala",
    }
    return _map.get(suffix.lower())


# -------------------------------------------------------------------
# Worker process functions (run in separate processes)
# -------------------------------------------------------------------

def _worker_init():
    """Called once per worker process. Pre-loads parsers."""
    # Each worker will lazily create parsers — no shared state needed
    pass


def _parse_file_tier0(path_str: str, root_str: str) -> dict[str, Any] | None:
    """
    Tier 0: Inventory only. No parsing, just metadata.
    ~0.1ms per file.
    """
    path = Path(path_str)
    try:
        stat = path.stat()
        source = path.read_bytes()
    except (OSError, PermissionError):
        return None

    try:
        relative = str(path.relative_to(root_str))
    except ValueError:
        relative = str(path)

    suffix = path.suffix.lower()
    language = _detect_language(suffix)
    if not language:
        return None

    return {
        "path": relative,
        "language": language,
        "content_hash": xxhash.xxh64(source).hexdigest(),
        "size_bytes": stat.st_size,
        "line_count": source.count(b"\n") + 1,
        "tier": 0,
    }


def _parse_file_tier1(args: tuple[str, str, int]) -> dict[str, Any] | None:
    """
    Tier 1: Skeleton parse. Top-level symbols + imports only.
    ~0.5ms per file. No parameters, no docstrings, no raw text.
    """
    path_str, root_str, max_size_kb = args
    path = Path(path_str)

    try:
        source = path.read_bytes()
    except (OSError, PermissionError):
        return None

    if len(source) > max_size_kb * 1024:
        return None

    try:
        relative = str(path.relative_to(root_str))
    except ValueError:
        relative = str(path)

    suffix = path.suffix.lower()
    language = _detect_language(suffix)
    if not language:
        return None

    content_hash = xxhash.xxh64(source).hexdigest()

    # Parse with tree-sitter
    parser = _get_worker_parser(language)
    if parser is None:
        return {
            "path": relative,
            "language": language,
            "content_hash": content_hash,
            "size_bytes": len(source),
            "line_count": source.count(b"\n") + 1,
            "tier": 0,
            "symbols": [],
            "relations": [],
            "imports": [],
        }

    tree = parser.parse(source)
    if tree is None:
        return None

    # Extract top-level symbols (Tier 1: names + kinds + locations only)
    symbols = _extract_skeleton(tree, source, relative, language)
    imports = _extract_imports_fast(tree, source, language)

    return {
        "path": relative,
        "language": language,
        "content_hash": content_hash,
        "size_bytes": len(source),
        "line_count": source.count(b"\n") + 1,
        "tier": 1,
        "symbols": symbols,
        "relations": [],
        "imports": imports,
    }


def _parse_file_tier2(args: tuple[str, str, int]) -> dict[str, Any] | None:
    """
    Tier 2: Full extraction. Parameters, docstrings, relations, raw text.
    ~2ms per file. Used on-demand for files the agent focuses on.
    """
    path_str, root_str, max_size_kb = args
    path = Path(path_str)

    try:
        source = path.read_bytes()
    except (OSError, PermissionError):
        return None

    if len(source) > max_size_kb * 1024:
        return None

    try:
        relative = str(path.relative_to(root_str))
    except ValueError:
        relative = str(path)

    suffix = path.suffix.lower()
    language = _detect_language(suffix)
    if not language:
        return None

    content_hash = xxhash.xxh64(source).hexdigest()

    parser = _get_worker_parser(language)
    if parser is None:
        return None

    tree = parser.parse(source)
    if tree is None:
        return None

    # Full extraction using the extractors from the base codemap
    try:
        from codemap.parser.extractors import get_extractor
        from codemap.core.models import Language as LangEnum

        lang_enum = LangEnum(language)
        extractor = get_extractor(lang_enum)

        symbols_raw = extractor.extract_symbols(tree, source, relative)
        relations_raw = extractor.extract_relations(tree, source, relative)
        imports = extractor.extract_imports(tree, source)

        symbols = [
            {
                "fqn": s.fqn,
                "name": s.name,
                "kind": s.kind.value,
                "language": language,
                "file_path": relative,
                "start_line": s.location.start_line,
                "end_line": s.location.end_line,
                "start_col": s.location.start_col,
                "end_col": s.location.end_col,
                "signature": s.signature,
                "return_type": s.return_type,
                "docstring": s.docstring,
                "is_exported": s.is_exported,
                "is_async": s.is_async,
                "params": [{"name": p.name, "type": p.type_annotation, "default": p.default_value} for p in s.parameters],
                "decorators": s.decorators,
                "raw_text": s.raw_text,
                "tier": 2,
            }
            for s in symbols_raw
        ]

        relations = [
            {
                "source_fqn": r.source_fqn,
                "target_fqn": r.target_fqn,
                "kind": r.kind.value,
                "file_path": relative,
                "line": r.location.start_line if r.location else None,
            }
            for r in relations_raw
        ]

    except ImportError:
        # Fallback if codemap base isn't available — use skeleton + relation extraction
        symbols = _extract_skeleton(tree, source, relative, language)
        relations = _extract_relations_fast(tree, source, relative, language, symbols)
        imports = _extract_imports_fast(tree, source, language)

    return {
        "path": relative,
        "language": language,
        "content_hash": content_hash,
        "size_bytes": len(source),
        "line_count": source.count(b"\n") + 1,
        "tier": 2,
        "symbols": symbols,
        "relations": relations,
        "imports": imports,
    }


# -------------------------------------------------------------------
# Skeleton extraction (Tier 1 — fast, cross-language)
# -------------------------------------------------------------------

_FUNCTION_TYPES = {
    "function_definition", "function_declaration", "method_definition",
    "method_declaration", "function_item", "generator_function_declaration",
}
_CLASS_TYPES = {
    "class_definition", "class_declaration", "struct_item",
    "interface_declaration", "enum_declaration", "type_alias_declaration",
}


def _extract_skeleton(tree, source: bytes, file_path: str, language: str) -> list[dict[str, Any]]:
    """
    Fast top-level symbol extraction. No recursion into method bodies.
    Returns lightweight dicts, not Pydantic models.
    """
    symbols: list[dict[str, Any]] = []
    root = tree.root_node

    for child in root.children:
        _extract_node_skeleton(child, source, file_path, language, symbols, depth=0)

    return symbols


def _extract_node_skeleton(
    node, source: bytes, file_path: str, language: str,
    symbols: list[dict[str, Any]], depth: int,
) -> None:
    """Extract symbol skeletons, limiting depth to avoid deep recursion."""
    if depth > 3:
        return

    actual_node = node
    is_exported = True

    # Unwrap export/decorated wrappers
    if node.type in ("export_statement", "decorated_definition"):
        is_exported = node.type == "export_statement"
        for child in node.children:
            if child.type in _FUNCTION_TYPES | _CLASS_TYPES:
                actual_node = child
                break
        else:
            for child in node.children:
                _extract_node_skeleton(child, source, file_path, language, symbols, depth)
            return

    if actual_node.type in _FUNCTION_TYPES:
        name_node = actual_node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            kind = "method" if depth > 0 else "function"
            symbols.append({
                "fqn": _build_fqn(file_path, name),
                "name": name,
                "kind": kind,
                "language": language,
                "file_path": file_path,
                "start_line": actual_node.start_point[0] + 1,
                "end_line": actual_node.end_point[0] + 1,
                "tier": 1,
                "is_exported": is_exported,
            })

    elif actual_node.type in _CLASS_TYPES:
        name_node = actual_node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            kind_map = {
                "interface_declaration": "interface",
                "enum_declaration": "enum",
                "type_alias_declaration": "type_alias",
            }
            kind = kind_map.get(actual_node.type, "class")
            symbols.append({
                "fqn": _build_fqn(file_path, name),
                "name": name,
                "kind": kind,
                "language": language,
                "file_path": file_path,
                "start_line": actual_node.start_point[0] + 1,
                "end_line": actual_node.end_point[0] + 1,
                "tier": 1,
                "is_exported": is_exported,
            })
            # Recurse into class body for methods
            body = actual_node.child_by_field_name("body")
            if body:
                for child in body.children:
                    _extract_node_skeleton(child, source, file_path, language, symbols, depth + 1)

    elif actual_node.type in ("block", "program", "module"):
        for child in actual_node.children:
            _extract_node_skeleton(child, source, file_path, language, symbols, depth)


def _extract_imports_fast(tree, source: bytes, language: str) -> list[str]:
    """Fast import extraction — just raw strings, no resolution."""
    import_types = {"import_statement", "import_from_statement", "use_declaration", "include_statement"}
    imports = []
    for child in tree.root_node.children:
        if child.type in import_types:
            imports.append(source[child.start_byte:child.end_byte].decode("utf-8", errors="replace").strip())
        elif child.type == "expression_statement":
            for inner in child.children:
                if inner.type in import_types:
                    imports.append(source[inner.start_byte:inner.end_byte].decode("utf-8", errors="replace").strip())
    return imports


def _extract_relations_fast(
    tree, source: bytes, file_path: str, language: str, symbols: list[dict]
) -> list[dict[str, Any]]:
    """
    Extract relations without the codemap base package.
    Detects: inheritance, function calls within bodies, imports-as-relations.
    Uses INFERRED provenance since we can't resolve FQNs precisely.
    """
    relations: list[dict[str, Any]] = []
    root = tree.root_node
    module_fqn = file_path.replace("/", ".").replace("\\", ".")
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go", ".rs"):
        if module_fqn.endswith(ext):
            module_fqn = module_fqn[: -len(ext)]
            break

    # Build a set of known symbol names in this file for local call resolution
    local_symbols = {s["name"]: s["fqn"] for s in symbols}

    for child in root.children:
        _extract_node_relations(
            child, source, file_path, language, module_fqn,
            local_symbols, relations, depth=0,
        )

    return relations


def _extract_node_relations(
    node, source: bytes, file_path: str, language: str, module_fqn: str,
    local_symbols: dict[str, str], relations: list[dict], depth: int,
) -> None:
    """Recursively extract relations from tree-sitter nodes."""
    if depth > 5:
        return

    # Inheritance: class Foo(Bar, Baz)
    if node.type in _CLASS_TYPES:
        name_node = node.child_by_field_name("name")
        if name_node:
            class_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            class_fqn = f"{module_fqn}.{class_name}"

            # Python: superclasses in argument_list
            superclass_node = node.child_by_field_name("superclasses") or node.child_by_field_name("arguments")
            if superclass_node is None:
                for child in node.children:
                    if child.type == "argument_list":
                        superclass_node = child
                        break

            if superclass_node:
                for arg in superclass_node.children:
                    if arg.type == "identifier":
                        base_name = source[arg.start_byte:arg.end_byte].decode("utf-8", errors="replace")
                        target_fqn = local_symbols.get(base_name, base_name)
                        relations.append({
                            "source_fqn": class_fqn,
                            "target_fqn": target_fqn,
                            "kind": "INHERITS",
                            "file_path": file_path,
                            "line": node.start_point[0] + 1,
                            "confidence": 0.9,
                            "provenance": "INFERRED",
                        })
                    elif arg.type == "attribute":
                        base_name = source[arg.start_byte:arg.end_byte].decode("utf-8", errors="replace")
                        relations.append({
                            "source_fqn": class_fqn,
                            "target_fqn": base_name,
                            "kind": "INHERITS",
                            "file_path": file_path,
                            "line": node.start_point[0] + 1,
                            "confidence": 0.8,
                            "provenance": "INFERRED",
                        })

    # Function/method calls
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node:
            call_text = source[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
            # Find enclosing function/method
            enclosing_fqn = _find_enclosing_symbol(node, source, module_fqn)
            if enclosing_fqn:
                # Resolve target: local symbol or use raw name
                if func_node.type == "identifier":
                    target = local_symbols.get(call_text, call_text)
                elif func_node.type == "attribute":
                    target = call_text
                else:
                    target = call_text

                relations.append({
                    "source_fqn": enclosing_fqn,
                    "target_fqn": target,
                    "kind": "CALLS",
                    "file_path": file_path,
                    "line": node.start_point[0] + 1,
                    "confidence": 0.7,
                    "provenance": "INFERRED",
                })

    # Import relations
    if node.type == "import_from_statement":
        module_name_node = node.child_by_field_name("module_name")
        if module_name_node:
            from_module = source[module_name_node.start_byte:module_name_node.end_byte].decode("utf-8", errors="replace")
            # Resolve relative imports
            resolved_module = _resolve_import(from_module, module_fqn)
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import"):
                    imported = child
                    if child.type == "aliased_import":
                        imported = child.child_by_field_name("name") or child
                    name = source[imported.start_byte:imported.end_byte].decode("utf-8", errors="replace")
                    if name and name != from_module and not name.startswith("("):
                        relations.append({
                            "source_fqn": module_fqn,
                            "target_fqn": f"{resolved_module}.{name}",
                            "kind": "IMPORTS",
                            "file_path": file_path,
                            "line": node.start_point[0] + 1,
                            "confidence": 1.0,
                            "provenance": "EXTRACTED",
                        })

    # Recurse
    for child in node.children:
        _extract_node_relations(
            child, source, file_path, language, module_fqn,
            local_symbols, relations, depth + 1,
        )


def _resolve_import(from_module: str, current_module_fqn: str) -> str:
    """Resolve relative imports to absolute module paths."""
    if not from_module.startswith("."):
        return from_module
    # Count leading dots
    dots = 0
    for ch in from_module:
        if ch == ".":
            dots += 1
        else:
            break
    remainder = from_module[dots:]
    # Go up `dots` levels from current module
    parts = current_module_fqn.split(".")
    if dots <= len(parts):
        base = ".".join(parts[: -dots]) if dots > 0 else current_module_fqn
    else:
        base = ""
    if base and remainder:
        return f"{base}.{remainder}"
    return base or remainder


def _find_enclosing_symbol(node, source: bytes, module_fqn: str) -> str | None:
    """Walk up parents to find enclosing function/class."""
    current = node.parent
    while current:
        if current.type in _FUNCTION_TYPES | _CLASS_TYPES:
            name_node = current.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                return f"{module_fqn}.{name}"
        current = current.parent
    return module_fqn


def _build_fqn(file_path: str, name: str) -> str:
    module = file_path.replace("/", ".").replace("\\", ".")
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go", ".rs"):
        if module.endswith(ext):
            module = module[:-len(ext)]
            break
    return f"{module}.{name}"


# -------------------------------------------------------------------
# Worker parser cache (per-process)
# -------------------------------------------------------------------

_worker_parsers: dict[str, Any] = {}


def _get_worker_parser(language: str):
    """Get or create a parser for this language in the current worker process."""
    if language in _worker_parsers:
        return _worker_parsers[language]

    try:
        from tree_sitter import Language, Parser

        lang_obj = _get_tree_sitter_language(language)
        if lang_obj is None:
            _worker_parsers[language] = None
            return None

        parser = Parser(Language(lang_obj))
        _worker_parsers[language] = parser
        return parser
    except (ImportError, ValueError, OSError):
        _worker_parsers[language] = None
        return None


def _get_tree_sitter_language(language: str):
    """Get the tree-sitter language capsule for a given language name."""
    _LANG_MODULES = {
        "python": "tree_sitter_python",
        "typescript": "tree_sitter_typescript",
        "tsx": "tree_sitter_typescript",
        "javascript": "tree_sitter_javascript",
        "jsx": "tree_sitter_javascript",
    }
    module_name = _LANG_MODULES.get(language)
    if module_name is None:
        return None

    try:
        import importlib
        mod = importlib.import_module(module_name)
        if language == "tsx":
            return mod.language_tsx()
        elif language == "typescript" and hasattr(mod, "language_typescript"):
            return mod.language_typescript()
        return mod.language()
    except (ImportError, AttributeError):
        return None


# -------------------------------------------------------------------
# Parallel Engine
# -------------------------------------------------------------------


class ParallelParserEngine:
    """
    Parallel parser engine for large codebases.

    Usage:
        engine = ParallelParserEngine(root="/path/to/repo")

        # Tier 0: inventory (fastest)
        for batch in engine.scan_tier0():
            db.upsert_files_batch(batch)

        # Tier 1: skeleton parse (parallel)
        for batch in engine.parse_tier1():
            db.upsert_files_batch(batch["files"])
            db.upsert_symbols_batch(batch["symbols"])

        # Tier 2: deep parse (on-demand, specific files)
        for batch in engine.parse_tier2(file_paths):
            db.upsert_files_batch(batch["files"])
            db.upsert_symbols_batch(batch["symbols"])
            db.upsert_relations_batch(batch["relations"])
    """

    def __init__(
        self,
        root: str | Path,
        *,
        workers: int | None = None,
        max_file_size_kb: int = 500,
        ignore_patterns: list[str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.workers = workers or max(1, (os.cpu_count() or 4) - 1)
        self.max_file_size_kb = max_file_size_kb
        self.ignore_patterns = ignore_patterns or [
            "node_modules", "__pycache__", ".git", ".codemap", "venv", ".venv",
            "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
            "*.pyc", "*.pyo", "*.so", "*.min.js", "*.min.css", "*.map",
            "*.lock", "package-lock.json",
        ]

    def walk_files(self) -> list[Path]:
        """Walk filesystem and return eligible source file paths."""
        files: list[Path] = []
        root_str = str(self.root)

        for dirpath, dirnames, filenames in os.walk(root_str):
            # Prune ignored directories in-place (os.walk respects this)
            dirnames[:] = [
                d for d in dirnames
                if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)
            ]

            for filename in filenames:
                if any(fnmatch.fnmatch(filename, p) for p in self.ignore_patterns):
                    continue
                if _detect_language(Path(filename).suffix) is None:
                    continue
                files.append(Path(dirpath) / filename)

        return files

    def scan_tier0(self, batch_size: int = WRITE_BATCH_SIZE) -> Iterator[list[dict[str, Any]]]:
        """
        Tier 0 inventory scan. Single-threaded (I/O bound, not CPU bound).
        ~0.1ms/file → 500K files in ~50 seconds.

        Yields batches of file dicts for bulk SQLite insertion.
        """
        batch: list[dict[str, Any]] = []
        root_str = str(self.root)

        for path in self.walk_files():
            result = _parse_file_tier0(str(path), root_str)
            if result:
                batch.append(result)
            if len(batch) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def parse_tier1(
        self,
        file_paths: list[Path] | None = None,
        batch_size: int = WRITE_BATCH_SIZE,
    ) -> Iterator[dict[str, list]]:
        """
        Tier 1 parallel skeleton parse.
        ~0.5ms/file × N workers → 300K files in ~2.5 min (8 cores).

        Yields batches: {"files": [...], "symbols": [...]}
        """
        paths = file_paths or self.walk_files()
        root_str = str(self.root)
        args = [(str(p), root_str, self.max_file_size_kb) for p in paths]

        files_batch: list[dict] = []
        symbols_batch: list[dict] = []

        with mp.Pool(processes=self.workers, initializer=_worker_init) as pool:
            for result in pool.imap_unordered(_parse_file_tier1, args, chunksize=WORKER_BATCH_SIZE):
                if result is None:
                    continue

                file_dict = {k: v for k, v in result.items() if k not in ("symbols", "relations")}
                file_dict["imports_json"] = json.dumps(result.get("imports", []))  # noqa: F841 — used via key
                files_batch.append(file_dict)
                symbols_batch.extend(result.get("symbols", []))

                if len(files_batch) >= batch_size:
                    yield {"files": files_batch, "symbols": symbols_batch}
                    files_batch = []
                    symbols_batch = []

        if files_batch:
            yield {"files": files_batch, "symbols": symbols_batch}

    def parse_tier2(
        self,
        file_paths: list[Path],
        batch_size: int = WRITE_BATCH_SIZE,
    ) -> Iterator[dict[str, list]]:
        """
        Tier 2 full parse for specific files (on-demand).
        ~2ms/file → 10K files in ~20 seconds.

        Yields batches: {"files": [...], "symbols": [...], "relations": [...]}
        """
        root_str = str(self.root)
        args = [(str(p), root_str, self.max_file_size_kb) for p in file_paths]

        files_batch: list[dict] = []
        symbols_batch: list[dict] = []
        relations_batch: list[dict] = []

        with mp.Pool(processes=self.workers, initializer=_worker_init) as pool:
            for result in pool.imap_unordered(_parse_file_tier2, args, chunksize=100):
                if result is None:
                    continue

                file_dict = {k: v for k, v in result.items() if k not in ("symbols", "relations")}
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


import json  # noqa: E402 — needed for worker functions
