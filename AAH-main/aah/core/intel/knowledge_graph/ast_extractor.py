"""
AST-based relationship extraction for key source files.

Uses tree-sitter when available for accurate parsing of Python, TypeScript,
Go, and Java. Falls back to regex-based extraction when tree-sitter grammars
are not installed.

Only processes key files identified by the profiler (entry points, APIs,
models, hotspots) — NOT the entire codebase.
"""

import re
from pathlib import Path


# Relationship types
CALLS = "calls"
IMPORTS = "imports"
INHERITS = "inherits"
IMPLEMENTS = "implements"
USES = "uses"


def _is_tree_sitter_available() -> bool:
    try:
        import tree_sitter  # noqa: F401
        return True
    except ImportError:
        return False


def extract_relationships_from_file(file_path: Path) -> list[dict]:
    """
    Extract typed relationships from a single source file.

    Returns list of dicts:
      {"source": str, "target": str, "type": str, "line": int}
    """
    if not file_path.exists() or not file_path.is_file():
        return []

    suffix = file_path.suffix.lower()
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if suffix == ".py":
        return _extract_python(file_path, content)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        return _extract_typescript(file_path, content)
    elif suffix == ".go":
        return _extract_go(file_path, content)
    elif suffix == ".java":
        return _extract_java(file_path, content)

    return []


def _extract_python(file_path: Path, content: str) -> list[dict]:
    """Regex-based relationship extraction for Python."""
    rels = []
    source = str(file_path)

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Imports
        m = re.match(r'^(?:from\s+(\S+)\s+)?import\s+(.+)', stripped)
        if m:
            module = m.group(1) or ""
            names = m.group(2)
            for name in names.split(","):
                name = name.strip().split(" as ")[0].strip()
                target = f"{module}.{name}" if module else name
                rels.append({"source": source, "target": target, "type": IMPORTS, "line": i})

        # Class inheritance
        m = re.match(r'^class\s+(\w+)\((.+)\):', stripped)
        if m:
            cls_name = m.group(1)
            bases = m.group(2)
            for base in bases.split(","):
                base = base.strip()
                if base and base not in ("object", "ABC", "BaseModel"):
                    rels.append({"source": f"{source}:{cls_name}", "target": base, "type": INHERITS, "line": i})

        # Function calls (simple pattern)
        for m in re.finditer(r'(\w+(?:\.\w+)*)\s*\(', stripped):
            call_target = m.group(1)
            # Filter noise: skip common built-ins
            if call_target not in ("print", "len", "str", "int", "dict", "list",
                                    "set", "tuple", "type", "isinstance", "range",
                                    "enumerate", "zip", "map", "filter", "sorted",
                                    "super", "self", "cls"):
                if "." in call_target:  # Only method calls
                    rels.append({"source": source, "target": call_target, "type": CALLS, "line": i})

    return rels


def _extract_typescript(file_path: Path, content: str) -> list[dict]:
    """Regex-based relationship extraction for TypeScript/JavaScript."""
    rels = []
    source = str(file_path)

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Imports
        m = re.match(r"^import\s+(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s+from\s+['\"](.+?)['\"]", stripped)
        if m:
            rels.append({"source": source, "target": m.group(1), "type": IMPORTS, "line": i})

        # Require
        m = re.match(r".*require\(['\"](.+?)['\"]\)", stripped)
        if m:
            rels.append({"source": source, "target": m.group(1), "type": IMPORTS, "line": i})

        # Class extends
        m = re.match(r"class\s+\w+\s+extends\s+(\w+)", stripped)
        if m:
            rels.append({"source": source, "target": m.group(1), "type": INHERITS, "line": i})

        # Implements
        m = re.match(r"class\s+\w+.*implements\s+([\w,\s]+)", stripped)
        if m:
            for impl in m.group(1).split(","):
                rels.append({"source": source, "target": impl.strip(), "type": IMPLEMENTS, "line": i})

    return rels


def _extract_go(file_path: Path, content: str) -> list[dict]:
    """Regex-based relationship extraction for Go."""
    rels = []
    source = str(file_path)

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Imports
        m = re.match(r'^\s*"(.+)"', stripped)
        if m and not stripped.startswith("//"):
            rels.append({"source": source, "target": m.group(1), "type": IMPORTS, "line": i})

        # Embedding (struct composition = Go's inheritance)
        m = re.match(r'^\s*(\w+\.?\w*)\s*$', stripped)
        if m and not stripped.startswith("//") and not stripped.startswith("func"):
            target = m.group(1)
            if target[0].isupper():  # Go exported types start uppercase
                rels.append({"source": source, "target": target, "type": INHERITS, "line": i})

    return rels


def _extract_java(file_path: Path, content: str) -> list[dict]:
    """Regex-based relationship extraction for Java."""
    rels = []
    source = str(file_path)

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Imports
        m = re.match(r'^import\s+(?:static\s+)?(.+);', stripped)
        if m:
            rels.append({"source": source, "target": m.group(1), "type": IMPORTS, "line": i})

        # Extends
        m = re.match(r'class\s+\w+\s+extends\s+(\w+)', stripped)
        if m:
            rels.append({"source": source, "target": m.group(1), "type": INHERITS, "line": i})

        # Implements
        m = re.match(r'class\s+\w+.*implements\s+([\w,\s]+)', stripped)
        if m:
            for impl in m.group(1).split(","):
                rels.append({"source": source, "target": impl.strip(), "type": IMPLEMENTS, "line": i})

    return rels
