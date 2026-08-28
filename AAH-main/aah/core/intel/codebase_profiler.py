#!/usr/bin/env python3
# Requires Python 3.9+
"""
Codebase profiler for AAH codebase intelligence.

Performs the T0 (inventory) and T1 (skeleton) phases of codebase profiling
using only filesystem operations and regex-based symbol extraction.
No external dependencies beyond Python stdlib.

Outputs a JSON report that Claude uses as the foundation for generating
mermaid diagrams and the codebase learning document.

Usage:
    python codebase_profiler.py /path/to/codebase [--output profile.json] [--max-files 50000]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


# ============================================================================
# Configuration
# ============================================================================

LANGUAGE_MAP = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
    ".java": "java", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".r": "r", ".R": "r",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql",
    ".proto": "protobuf", ".thrift": "thrift",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".toml": "toml",
    ".tf": "terraform", ".hcl": "hcl",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".dockerfile": "docker",
}

# Directories to always skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target", "out",
    ".gradle", ".idea", ".vscode", ".eclipse",
    "vendor", "bower_components", ".terraform",
    "coverage", ".nyc_output", ".cache",
}

# Files to always skip
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Cargo.lock",
    "go.sum", "composer.lock", "Gemfile.lock",
}

# Entry point patterns (auto-promote to deep analysis)
ENTRY_PATTERNS = [
    "main.py", "app.py", "server.py", "wsgi.py", "asgi.py",
    "manage.py", "cli.py", "__main__.py",
    "index.ts", "index.js", "main.ts", "main.go", "main.rs", "lib.rs",
    "Main.java", "Application.java", "App.java",
    "Startup.cs", "Program.cs",
]

ENTRY_DIR_PATTERNS = [
    "routes", "handlers", "controllers", "api", "endpoints",
    "views", "resolvers", "middleware", "services",
]

CONFIG_PATTERNS = [
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json",
    "Cargo.toml", "go.mod", "build.gradle", "pom.xml",
    "Makefile", "CMakeLists.txt",
    ".env.example", "requirements.txt", "Pipfile",
    "terraform.tf", "main.tf",
]


# ============================================================================
# T0: Inventory Scan
# ============================================================================

def scan_inventory(root: Path, max_files: int = 50000) -> dict[str, Any]:
    """
    Tier 0 inventory: walk filesystem, classify files, compute basic stats.
    No parsing. ~0.1ms/file.
    """
    files: list[dict[str, Any]] = []
    language_counts: Counter = Counter()
    dir_tree: dict[str, int] = defaultdict(int)
    total_lines = 0
    total_bytes = 0
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""

        for fname in filenames:
            if fname in SKIP_FILES:
                continue
            if len(files) >= max_files:
                skipped += 1
                continue

            fpath = Path(dirpath) / fname
            suffix = fpath.suffix.lower()

            # Special case: Dockerfile has no extension
            if fname == "Dockerfile" or fname.startswith("Dockerfile."):
                language = "docker"
            elif suffix not in LANGUAGE_MAP:
                continue
            else:
                language = LANGUAGE_MAP[suffix]

            try:
                stat = fpath.stat()
                size = stat.st_size
            except OSError:
                continue

            # Skip very large files (>1MB)
            if size > 1_000_000:
                continue

            try:
                content = fpath.read_bytes()
                line_count = content.count(b"\n") + 1
            except (OSError, PermissionError):
                continue

            rel_path = str(fpath.relative_to(root))
            content_hash = hashlib.md5(content).hexdigest()[:12]

            # Classify role
            role = _classify_file_role(fname, rel_path)

            files.append({
                "path": rel_path,
                "language": language,
                "size_bytes": size,
                "line_count": line_count,
                "hash": content_hash,
                "role": role,
            })

            language_counts[language] += 1
            total_lines += line_count
            total_bytes += size

            # Track directory depth-1 structure
            top_dir = rel_path.split(os.sep)[0] if os.sep in rel_path else ""
            if top_dir:
                dir_tree[top_dir] += 1

    return {
        "root": str(root),
        "total_files": len(files),
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "skipped_over_limit": skipped,
        "languages": dict(language_counts.most_common()),
        "directory_structure": dict(sorted(dir_tree.items(), key=lambda x: -x[1])[:50]),
        "files": files,
    }


def _classify_file_role(fname: str, rel_path: str) -> str:
    """Classify a file's role in the codebase."""
    if fname in ENTRY_PATTERNS:
        return "entry_point"
    if fname in CONFIG_PATTERNS:
        return "config"
    if any(d in rel_path.split(os.sep) for d in ENTRY_DIR_PATTERNS):
        return "api_layer"

    # Test files
    parts = rel_path.lower().split(os.sep)
    if any(p in ("test", "tests", "__tests__", "spec", "specs") for p in parts):
        return "test"
    if fname.startswith("test_") or fname.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js")):
        return "test"

    # Data / model files
    if any(p in ("models", "entities", "schemas", "migrations") for p in parts):
        return "data_model"
    if any(p in ("db", "database", "persistence", "repository", "repositories") for p in parts):
        return "data_access"

    return "source"


# ============================================================================
# T1: Skeleton Parse (regex-based symbol extraction)
# ============================================================================

# Language-specific symbol extraction patterns
SYMBOL_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        (r"^class\s+(\w+)", "class"),
        (r"^def\s+(\w+)", "function"),
        (r"^async\s+def\s+(\w+)", "async_function"),
        (r"^(\w+)\s*=\s*(?:Flask|FastAPI|Django|Celery|SQLAlchemy)", "framework_instance"),
    ],
    "typescript": [
        (r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
        (r"^(?:export\s+)?interface\s+(\w+)", "interface"),
        (r"^(?:export\s+)?type\s+(\w+)", "type"),
        (r"^(?:export\s+)?enum\s+(\w+)", "enum"),
        (r"^(?:export\s+)?const\s+(\w+)\s*=", "const"),
    ],
    "javascript": [
        (r"^(?:export\s+)?class\s+(\w+)", "class"),
        (r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
        (r"^(?:export\s+)?const\s+(\w+)\s*=", "const"),
        (r"^module\.exports\s*=\s*(\w+)", "export"),
    ],
    "java": [
        (r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:public|private|protected)?\s*interface\s+(\w+)", "interface"),
        (r"^\s*(?:public|private|protected)?\s*enum\s+(\w+)", "enum"),
        (r"^\s*(?:public|private|protected)\s+(?:static\s+)?(?:\w+(?:<[^>]+>)?)\s+(\w+)\s*\(", "method"),
    ],
    "go": [
        (r"^type\s+(\w+)\s+struct", "struct"),
        (r"^type\s+(\w+)\s+interface", "interface"),
        (r"^func\s+(\w+)\s*\(", "function"),
        (r"^func\s+\(\w+\s+\*?\w+\)\s+(\w+)\s*\(", "method"),
    ],
    "rust": [
        (r"^pub(?:\(crate\))?\s+struct\s+(\w+)", "struct"),
        (r"^pub(?:\(crate\))?\s+enum\s+(\w+)", "enum"),
        (r"^pub(?:\(crate\))?\s+trait\s+(\w+)", "trait"),
        (r"^pub(?:\(crate\))?\s+(?:async\s+)?fn\s+(\w+)", "function"),
        (r"^impl(?:<[^>]+>)?\s+(\w+)", "impl"),
    ],
    "csharp": [
        (r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:public|private|protected|internal)?\s*interface\s+(\w+)", "interface"),
        (r"^\s*(?:public|private|protected|internal)?\s*enum\s+(\w+)", "enum"),
    ],
}

# Import extraction patterns
IMPORT_PATTERNS: dict[str, list[str]] = {
    "python": [
        r"^(?:from\s+(\S+)\s+)?import\s+(.+)",
    ],
    "typescript": [
        r"^import\s+.*?from\s+['\"]([^'\"]+)['\"]",
        r"^import\s+['\"]([^'\"]+)['\"]",
    ],
    "javascript": [
        r"^(?:import|const\s+\w+\s*=\s*require)\s*\(?['\"]([^'\"]+)['\"]",
    ],
    "java": [
        r"^import\s+(?:static\s+)?([^;]+);",
    ],
    "go": [
        r'^\s*"([^"]+)"',
        r"^import\s+\"([^\"]+)\"",
    ],
    "rust": [
        r"^use\s+([^;]+);",
    ],
}


def extract_symbols(root: Path, files: list[dict[str, Any]], max_files: int = 5000) -> dict[str, Any]:
    """
    Tier 1 skeleton parse: extract top-level symbols and imports.
    Regex-based, no AST parser required.
    """
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    module_graph: dict[str, list[str]] = defaultdict(list)
    files_parsed = 0

    # Sort by role priority: entry_points and api_layer first
    role_priority = {"entry_point": 0, "config": 1, "api_layer": 2, "data_model": 3, "data_access": 4, "source": 5, "test": 6}
    sorted_files = sorted(files, key=lambda f: role_priority.get(f["role"], 99))

    for file_info in sorted_files[:max_files]:
        lang = file_info["language"]
        fpath = root / file_info["path"]

        if lang not in SYMBOL_PATTERNS:
            continue

        try:
            content = fpath.read_text(errors="replace")
        except (OSError, PermissionError):
            continue

        lines = content.split("\n")
        patterns = SYMBOL_PATTERNS.get(lang, [])
        import_pats = IMPORT_PATTERNS.get(lang, [])

        file_symbols = []
        file_imports = []

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", "/*", "*", "'''", '"""')):
                continue

            # Extract symbols
            for pattern, kind in patterns:
                m = re.match(pattern, stripped)
                if m:
                    name = m.group(1)
                    file_symbols.append({
                        "name": name,
                        "kind": kind,
                        "file": file_info["path"],
                        "line": i + 1,
                        "fqn": f"{file_info['path']}:{name}",
                    })
                    break

            # Extract imports
            for imp_pat in import_pats:
                m = re.match(imp_pat, stripped)
                if m:
                    target = m.group(1) if m.group(1) else m.group(0)
                    file_imports.append(target.strip())

        symbols.extend(file_symbols)
        if file_imports:
            imports.append({"file": file_info["path"], "imports": file_imports})
            for imp in file_imports:
                module_graph[file_info["path"]].append(imp)

        files_parsed += 1

    return {
        "files_parsed": files_parsed,
        "total_symbols": len(symbols),
        "symbols": symbols,
        "imports": imports,
        "module_graph": dict(module_graph),
    }


# ============================================================================
# Analysis: Derived Intelligence
# ============================================================================

def analyze_architecture(inventory: dict, skeleton: dict) -> dict[str, Any]:
    """Derive architectural patterns from inventory + skeleton data."""

    files = inventory["files"]

    # Identify layers
    layers: dict[str, list[str]] = defaultdict(list)
    for f in files:
        role = f["role"]
        if role in ("entry_point", "api_layer"):
            layers["presentation"].append(f["path"])
        elif role in ("data_model", "data_access"):
            layers["data"].append(f["path"])
        elif role == "config":
            layers["infrastructure"].append(f["path"])
        elif role == "test":
            layers["testing"].append(f["path"])
        else:
            # Try to infer from directory structure
            parts = f["path"].lower().split(os.sep)
            if any(p in ("services", "service", "core", "domain", "business", "logic") for p in parts):
                layers["business_logic"].append(f["path"])
            elif any(p in ("utils", "helpers", "common", "shared", "lib") for p in parts):
                layers["utilities"].append(f["path"])
            else:
                layers["application"].append(f["path"])

    # Detect frameworks and tools from config files
    frameworks = _detect_frameworks(files, inventory["root"])

    # Identify data stores from imports and file patterns
    data_stores = _detect_data_stores(skeleton.get("imports", []), files)

    # Compute module coupling
    module_graph = skeleton.get("module_graph", {})
    coupling = _analyze_coupling(module_graph)

    # Identify hotspots (files with most symbols = likely complex)
    symbol_counts = Counter()
    for sym in skeleton.get("symbols", []):
        symbol_counts[sym["file"]] += 1
    hotspots = symbol_counts.most_common(20)

    return {
        "layers": {k: len(v) for k, v in layers.items()},
        "layer_files": {k: sorted(v)[:10] for k, v in layers.items()},  # Top 10 per layer
        "frameworks": frameworks,
        "data_stores": data_stores,
        "coupling": coupling,
        "hotspots": [{"file": f, "symbols": c} for f, c in hotspots],
        "primary_language": max(inventory["languages"], key=inventory["languages"].get) if inventory["languages"] else "unknown",
        "language_breakdown": inventory["languages"],
    }


def _detect_frameworks(files: list[dict], root: str) -> list[dict[str, str]]:
    """Detect frameworks from config and entry point files."""
    frameworks = []
    root_path = Path(root)

    # Check package.json
    pkg_json = root_path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            framework_map = {
                "react": "React", "next": "Next.js", "vue": "Vue.js", "nuxt": "Nuxt",
                "angular": "Angular", "express": "Express", "fastify": "Fastify",
                "nestjs": "NestJS", "prisma": "Prisma", "typeorm": "TypeORM",
                "sequelize": "Sequelize", "mongoose": "Mongoose",
            }
            for key, name in framework_map.items():
                if any(key in d.lower() for d in deps):
                    frameworks.append({"name": name, "type": "javascript"})
        except (json.JSONDecodeError, OSError):
            pass

    # Check pyproject.toml / requirements.txt
    for req_file in ["requirements.txt", "pyproject.toml", "Pipfile", "setup.cfg"]:
        req_path = root_path / req_file
        if req_path.exists():
            try:
                content = req_path.read_text().lower()
                py_frameworks = {
                    "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
                    "sqlalchemy": "SQLAlchemy", "alembic": "Alembic",
                    "celery": "Celery", "pydantic": "Pydantic",
                    "pandas": "Pandas", "numpy": "NumPy",
                    "tensorflow": "TensorFlow", "pytorch": "PyTorch", "torch": "PyTorch",
                    "scikit-learn": "scikit-learn", "langchain": "LangChain",
                    "openai": "OpenAI SDK", "anthropic": "Anthropic SDK",
                }
                for key, name in py_frameworks.items():
                    if key in content:
                        frameworks.append({"name": name, "type": "python"})
            except OSError:
                pass

    # Check go.mod
    go_mod = root_path / "go.mod"
    if go_mod.exists():
        try:
            content = go_mod.read_text().lower()
            go_frameworks = {
                "gin-gonic": "Gin", "gorilla/mux": "Gorilla Mux",
                "echo": "Echo", "fiber": "Fiber",
                "gorm.io": "GORM", "ent/ent": "Ent",
            }
            for key, name in go_frameworks.items():
                if key in content:
                    frameworks.append({"name": name, "type": "go"})
        except OSError:
            pass

    # Check Cargo.toml
    cargo = root_path / "Cargo.toml"
    if cargo.exists():
        try:
            content = cargo.read_text().lower()
            rust_frameworks = {
                "actix-web": "Actix Web", "axum": "Axum", "rocket": "Rocket",
                "diesel": "Diesel", "sea-orm": "SeaORM", "sqlx": "SQLx",
                "tokio": "Tokio", "serde": "Serde",
            }
            for key, name in rust_frameworks.items():
                if key in content:
                    frameworks.append({"name": name, "type": "rust"})
        except OSError:
            pass

    return frameworks


def _detect_data_stores(imports_list: list[dict], files: list[dict]) -> list[str]:
    """Detect data stores from imports and file patterns."""
    stores = set()
    all_imports = []
    for imp_entry in imports_list:
        all_imports.extend(imp_entry.get("imports", []))

    import_text = " ".join(all_imports).lower()
    file_paths = " ".join(f["path"].lower() for f in files)

    db_signals = {
        "postgresql": ["psycopg", "asyncpg", "pg_", "postgresql", "postgres"],
        "mysql": ["mysql", "pymysql", "mysqlclient"],
        "sqlite": ["sqlite3", "sqlite"],
        "mongodb": ["pymongo", "mongoose", "mongodb", "mongo"],
        "redis": ["redis", "ioredis", "aioredis"],
        "elasticsearch": ["elasticsearch", "elastic"],
        "dynamodb": ["dynamodb", "boto3.dynamodb"],
        "cassandra": ["cassandra"],
        "kafka": ["kafka", "confluent_kafka"],
        "rabbitmq": ["rabbitmq", "amqp", "pika"],
        "s3": ["boto3", "s3", "minio"],
    }

    for store, signals in db_signals.items():
        for sig in signals:
            if sig in import_text or sig in file_paths:
                stores.add(store)
                break

    return sorted(stores)


def _analyze_coupling(module_graph: dict[str, list[str]]) -> dict[str, Any]:
    """Analyze module coupling from import graph."""
    if not module_graph:
        return {"afferent": [], "efferent": [], "instability": []}

    # Efferent coupling: how many modules does this file depend on
    efferent = {f: len(deps) for f, deps in module_graph.items()}

    # Afferent coupling: how many files depend on this module
    afferent: Counter = Counter()
    for deps in module_graph.values():
        for d in deps:
            afferent[d] += 1

    # Top coupled modules
    top_efferent = sorted(efferent.items(), key=lambda x: -x[1])[:15]
    top_afferent = sorted(afferent.items(), key=lambda x: -x[1])[:15]

    return {
        "afferent_top": [{"module": m, "dependents": c} for m, c in top_afferent],
        "efferent_top": [{"module": m, "dependencies": c} for m, c in top_efferent],
    }


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Codebase Profiler")
    parser.add_argument("root", type=str, help="Root directory of the codebase to profile")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output JSON file path")
    parser.add_argument("--max-files", type=int, default=50000, help="Max files to inventory")
    parser.add_argument("--max-parse", type=int, default=5000, help="Max files for symbol extraction")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress output")

    args = parser.parse_args()
    root = Path(args.root).resolve()

    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    # T0: Inventory
    if not args.quiet:
        print(f"Phase 1: Inventory scan of {root}...", file=sys.stderr)
    inventory = scan_inventory(root, max_files=args.max_files)
    if not args.quiet:
        print(f"  Found {inventory['total_files']} source files, "
              f"{inventory['total_lines']:,} lines, "
              f"{len(inventory['languages'])} languages", file=sys.stderr)

    # T1: Skeleton
    if not args.quiet:
        print(f"Phase 2: Symbol extraction (up to {args.max_parse} files)...", file=sys.stderr)
    skeleton = extract_symbols(root, inventory["files"], max_files=args.max_parse)
    if not args.quiet:
        print(f"  Extracted {skeleton['total_symbols']} symbols "
              f"from {skeleton['files_parsed']} files", file=sys.stderr)

    # Analysis
    if not args.quiet:
        print("Phase 3: Architecture analysis...", file=sys.stderr)
    architecture = analyze_architecture(inventory, skeleton)

    # Compose final report
    report = {
        "profile_version": "1.0",
        "root": str(root),
        "summary": {
            "total_files": inventory["total_files"],
            "total_lines": inventory["total_lines"],
            "total_bytes": inventory["total_bytes"],
            "primary_language": architecture["primary_language"],
            "languages": inventory["languages"],
            "frameworks": architecture["frameworks"],
            "data_stores": architecture["data_stores"],
        },
        "directory_structure": inventory["directory_structure"],
        "architecture": {
            "layers": architecture["layers"],
            "layer_files": architecture["layer_files"],
            "coupling": architecture["coupling"],
            "hotspots": architecture["hotspots"],
        },
        "symbols": {
            "total": skeleton["total_symbols"],
            "files_parsed": skeleton["files_parsed"],
            "by_kind": dict(Counter(s["kind"] for s in skeleton["symbols"])),
            "top_level": skeleton["symbols"][:200],  # Cap for JSON size
        },
        "imports": {
            "module_graph_size": len(skeleton["module_graph"]),
            "entries": skeleton["imports"][:100],  # Cap for JSON size
        },
        "entry_points": [f["path"] for f in inventory["files"] if f["role"] == "entry_point"],
        "config_files": [f["path"] for f in inventory["files"] if f["role"] == "config"],
        "data_model_files": [f["path"] for f in inventory["files"] if f["role"] == "data_model"],
        "api_layer_files": [f["path"] for f in inventory["files"] if f["role"] == "api_layer"],
        "test_files_count": sum(1 for f in inventory["files"] if f["role"] == "test"),
    }

    # Output
    output_json = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(output_json)
        if not args.quiet:
            print(f"Profile written to {args.output}", file=sys.stderr)
    else:
        print(output_json)

    if not args.quiet:
        print(f"\nProfile complete. {report['summary']['total_files']} files, "
              f"{report['symbols']['total']} symbols, "
              f"{len(report['summary']['frameworks'])} frameworks detected.", file=sys.stderr)


if __name__ == "__main__":
    main()
