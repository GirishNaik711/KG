# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CodeMap-Scale is a persistent code intelligence system for AI agents, designed for 500K+ file monorepos. It uses a **tiered analysis** approach (T0 inventory → T1 skeleton → T2 full extract → T3 wiki) where analysis depth is proportional to agent need. Three integrated subsystems: (1) **Code parsing** via Rust or Python backends, (2) **Unstructured document ingestion** via LiteParse for PDFs/Office/markdown, (3) **Knowledge layer** with Leiden community detection, semantic edges, and an LLM Wiki. Two backends are available: a **Rust backend** (2-3x faster) via PyO3 for the hot path, and a **Python fallback** using multiprocessing. The orchestrator auto-detects which is available at import time.

## Setup: Python-Only (No Rust Toolchain Required)

This is the simplest path — works out of the box with Python 3.11+.

```bash
# Create a virtual environment (recommended to avoid Anaconda/system Python conflicts)
python3 -m venv .venv
source .venv/bin/activate

# Install package + dev dependencies
pip install -e ".[dev]"

# Verify
python -c "from codemap_scale.orchestrator import _HAS_RUST; print(f'Rust backend: {_HAS_RUST}')"
# → Rust backend: False
```

The Python backend uses `multiprocessing.Pool` with per-process tree-sitter parser caches. It handles all the same operations as Rust, just slower on large repos.

## Setup: With Rust Backend (Recommended for Large Repos)

The Rust backend gives ~2-3x speedup on scout/update operations. Requires a Rust toolchain and `maturin`.

```bash
# 1. Install Rust (if not present)
#    macOS: brew install rust
#    Linux: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# 2. Create a venv (important: avoids Anaconda conflicts with maturin)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install Python deps + maturin
pip install -e ".[dev]" maturin

# 4. Build the Rust extension (release mode)
cd codemap_rs && maturin develop --release && cd ..

# 5. Verify Rust is active
python -c "from codemap_scale.orchestrator import _HAS_RUST; print(f'Rust backend: {_HAS_RUST}')"
# → Rust backend: True
```

**Anaconda users**: If you have Anaconda installed, `maturin` may pick up the wrong Python and target the wrong architecture (e.g., x86_64 on an arm64 Mac). Always use a venv created from your pyenv/system Python, and ensure `CONDA_PREFIX` is unset:
```bash
unset CONDA_PREFIX && unset CONDA_DEFAULT_ENV
source .venv/bin/activate
cd codemap_rs && maturin develop --release
```

**What the Rust extension provides** (`codemap_rs` PyO3 module):
- `scan_inventory(root, max_file_size_kb=500)` — T0: parallel file walk + xxhash64 hashing via rayon
- `parse_skeleton(root, file_paths=None, max_file_size_kb=500)` — T1: tree-sitter skeleton extraction (names, kinds, locations)
- `detect_changes(root, stored_hashes)` — Incremental: parallel hash comparison, returns added/modified/deleted

The orchestrator calls these automatically when available. No code changes needed — same API regardless of backend.

## Makefile Shortcuts

```bash
make install       # pip install -e .[dev]
make rust          # cd codemap_rs && maturin develop --release
make test          # pytest tests/ -v (all 123 tests)
make test-fast     # pytest tests/ -v -m "not slow" (108 tests, ~5s, no network)
make lint          # ruff check .
make all           # lint + test
```

## Running Tests

```bash
# All tests (254 tests, ~25s including real repo cloning)
pytest tests/ -v

# Fast tests only (239 tests, ~20s, no network needed)
pytest tests/ -m "not slow" -v

# Single test
pytest tests/test_orchestrator.py::TestScout::test_indexes_all_files

# Micro-benchmarks (pytest-benchmark)
pytest tests/test_benchmarks.py --benchmark-only

# Large-repo benchmark harness (clones real repos to ~/.cache/codemap-bench/)
python benchmarks/run_benchmark.py --repos flask,fastapi,django
python benchmarks/run_benchmark.py --repos flask,fastapi,django,cpython,kubernetes
python benchmarks/run_benchmark.py --include-linux   # adds Linux kernel (~75K files)
```

## CLI Usage

All commands work identically with either backend. Add `--json` to any command for machine-readable output.

```bash
# Index a codebase (T0 inventory + T1 skeleton parse)
python -m codemap_scale scout /path/to/repo
python -m codemap_scale scout /path/to/repo --force          # re-index even if DB exists
python -m codemap_scale scout /path/to/repo --workers 8      # control parallelism

# Surgical find: search symbols by pattern
python -m codemap_scale query /path/to/repo "*ClassName*" --kind class
python -m codemap_scale query /path/to/repo "*route*"
python -m codemap_scale query /path/to/repo "*Middleware*" --kind class --limit 20

# Call chain traversal
python -m codemap_scale chain /path/to/repo module.Class.method --depth 3

# Impact analysis: who depends on this file?
python -m codemap_scale impact /path/to/repo path/to/file.py

# Codebase overview
python -m codemap_scale stats /path/to/repo

# Deep-parse a region (promote to T2 with params/docstrings/relations)
python -m codemap_scale focus /path/to/repo src/core/

# Re-index changed files (preserves tier levels)
python -m codemap_scale update /path/to/repo

# Ingest documents (PDFs, markdown, Office docs from folders/subfolders)
python -m codemap_scale ingest-docs /path/to/repo
python -m codemap_scale ingest-docs /path/to/repo --directory docs/

# Community detection (Leiden algorithm)
python -m codemap_scale communities /path/to/repo
python -m codemap_scale god-nodes /path/to/repo --top 10

# Wiki layer (build, search, lint, export)
python -m codemap_scale wiki-build /path/to/repo
python -m codemap_scale wiki-search /path/to/repo "payment processing"
python -m codemap_scale wiki-lint /path/to/repo
python -m codemap_scale wiki-export /path/to/repo -o ./wiki/
```

## Python API (for LLM Agent Tool Integration)

```python
from codemap_scale.orchestrator import CodeMapScale

# Initialize and scout
cm = CodeMapScale("/path/to/repo")
stats = cm.scout()                        # T0+T1 on all files
# stats = cm.scout(force=True)            # re-index from scratch

# Surgical find — the primary agent query
results = cm.tools.search_structural("*Flask*", kind="class")
# → {"matches": [{"name": "Flask", "file_path": "src/flask/app.py", "start_line": 109, ...}], "total": 4}

# File map — understand a file before editing
file_map = cm.tools.get_file_map("src/flask/app.py")
# → {"symbols": [{"name": "Flask", "kind": "class", "start_line": 109, "end_line": 1625}, ...]}

# Symbol lookup (auto-promotes from T1→T2 on access)
symbol = cm.tools.get_symbol("src.flask.app.Flask")

# Call chain traversal
chain = cm.tools.get_call_chain("module.func_a", depth=3)

# Reverse: who calls this?
callers = cm.tools.get_callers("module.func_b")

# Impact analysis
impact = cm.tools.analyze_impact("path/to/file.py")

# Overview / stats
overview = cm.tools.get_overview()

# Deep-parse a directory (promote to T2)
cm.focus("src/core/")

# Incremental update after code changes (preserves T2 files at T2)
cm.update()

# Ingest documents from folders/subfolders (PDFs, markdown, Office, images)
docs = cm.ingest_documents()              # entire repo
docs = cm.ingest_documents("docs/")       # specific folder

# Search documents
cm.tools.search_documents("*API*")

# Community detection (Leiden algorithm)
communities = cm.detect_communities()
god_nodes = cm.tools.get_god_nodes(top_n=10)
cm.tools.get_communities()
cm.tools.get_symbol_community("module.ClassName")

# Build wiki (T3 layer)
cm.build_wiki()                           # template-based
cm.build_wiki(summary_fn=my_llm_fn)      # LLM-powered

# Wiki tools
cm.tools.search_wiki("payment processing")
cm.tools.get_wiki_page("src_service_py")
cm.tools.get_wiki_overview()
cm.tools.lint_wiki()
cm.wiki.export_markdown("./wiki-output/")

# Check which backend is active
from codemap_scale.orchestrator import _HAS_RUST
print(f"Using Rust: {_HAS_RUST}")  # True if codemap_rs is compiled

cm.close()
```

## Performance: Rust vs Python Backend

Benchmarked on Apple Silicon, single run per repo with warm disk cache:

| Repo | Files | Symbols | Python Scout | Rust Scout | Speedup | Query p95 |
|------|-------|---------|-------------|------------|---------|-----------|
| Flask | 83 | 852 | 0.29s | 0.06s | 4.8x | 0.42ms |
| FastAPI | 1,125 | 4,888 | 1.03s | 0.23s | 4.5x | 1.27ms |
| Django | 3,006 | 33,918 | 2.33s | 1.34s | 1.7x | 4.82ms |
| CPython | 3,418 | 62,774 | 3.62s | 1.52s | 2.4x | 7.86ms |
| Kubernetes | 16,927 | 124,518 | 6.85s | 6.43s | 1.1x | 3.15ms |

- **Rust extracts more symbols**: Rust backend includes Go, Java, and Rust grammars. Kubernetes (Go) yields 124K symbols with Rust vs 28 with Python.
- **T0 (inventory/hash)** sees the largest Rust speedup — rayon parallel walk + xxhash vs Python multiprocessing
- **Query latency is identical** — both backends write to the same SQLite DB; queries don't touch the parser
- **Incremental update (no-op)** Kubernetes: 0.88s (Rust) vs 2.47s (Python) — parallel hash comparison
- **DB size**: ~0.5 KB per symbol. Kubernetes at 124K symbols = 60 MB. Django at 34K = 12.5 MB.
- **For Go/Java/Rust codebases, the Rust backend is required** — Python backend only has Python/TypeScript/JavaScript grammars

## Architecture

**Two execution paths for the hot loop (walk → hash → parse → extract):**
- **Rust path** (`codemap_rs/`): PyO3 bindings exposing `scan_inventory()`, `parse_skeleton()`, `detect_changes()`. Uses rayon for parallelism, the `ignore` crate for .gitignore-aware walking, xxhash64 for hashing, and native tree-sitter grammars compiled into the binary.
- **Python fallback** (`codemap_scale/parser/parallel_engine.py`): `multiprocessing.Pool` with per-process tree-sitter parser cache. Uses `tree-sitter-python`, `tree-sitter-typescript`, `tree-sitter-javascript` pip packages.

**Backend auto-detection** (`codemap_scale/orchestrator.py`):
```python
try:
    import codemap_rs
    if not hasattr(codemap_rs, "scan_inventory"):
        raise ImportError("not compiled")
    _HAS_RUST = True
except ImportError:
    _HAS_RUST = False
```
The orchestrator's `scout()` dispatches to `_scout_rust()` or `_scout_python()` based on this flag. `update()` uses `codemap_rs.detect_changes()` when Rust is available, otherwise falls back to `_detect_changes_python()`.

**Key modules:**
- `codemap_scale/orchestrator.py` — `CodeMapScale` orchestrator (scout/focus/update/ingest_documents/detect_communities/build_wiki) and `ScaledCodeMapTools` (agent-facing tool interface)
- `codemap_scale/core/tier_manager.py` — `TierManager` tracks per-file tier levels, handles promotion/demotion based on query frequency and file role
- `codemap_scale/graph/sqlite_graph.py` — `SQLiteSymbolGraph` stores files, symbols, relations, and communities in SQLite (WAL mode). Relations have `confidence` and `provenance` (EXTRACTED/INFERRED/AMBIGUOUS) fields
- `codemap_scale/graph/community_detection.py` — `CommunityDetector` runs Leiden algorithm (via graspologic) or connected components (NetworkX) or SQL directory grouping fallback. `detect_god_nodes()` finds highest-degree hub symbols
- `codemap_scale/parser/parallel_engine.py` — Python fallback: multiprocessing + tree-sitter for code files
- `codemap_scale/parser/unstructured_engine.py` — `UnstructuredParserEngine` for documents: walks folders, parses markdown/RST/CSV natively, uses LiteParse CLI for PDFs/Office/images. `detect_doc_code_references()` creates INFERRED edges from docs to code symbols
- `codemap_scale/wiki/engine.py` — `WikiEngine` implements the LLM Wiki pattern: `ingest_module()`, `ingest_overview()`, `ingest_directory()`, `search()` (FTS5), `lint()`, `export_markdown()`. Accepts optional `summary_fn` for LLM-powered summarization
- `codemap_scale/cli.py` — Click-based CLI wrapping all orchestrator operations including `ingest-docs`, `communities`, `god-nodes`, `wiki-build`, `wiki-search`, `wiki-lint`, `wiki-export`
- `codemap_rs/src/lib.rs` — Rust PyO3 module definition with `scan_inventory`, `parse_skeleton`, `detect_changes`
- `codemap_rs/src/walker.rs` — Parallel file walk via `ignore` crate (respects .gitignore)
- `codemap_rs/src/hasher.rs` — xxhash64 content hashing
- `codemap_rs/src/extractor.rs` — Tree-sitter skeleton extraction for Python/TS/JS/Java/Go/Rust

**Data flow:** `scout()` runs T0+T1 on all code files → `ingest_documents()` parses docs from folders and detects cross-references → results stored in SQLiteSymbolGraph → `detect_communities()` runs Leiden clustering → agent queries via `ScaledCodeMapTools` (code, docs, wiki, communities) → `TierManager` auto-promotes frequently queried files → `focus()`/`focus_file()` runs T2 on promoted files → `build_wiki()` generates T3 wiki pages from T2 data → `update()` preserves tier levels — T2 files are re-parsed at T2 after modification.

## Test Structure

- `tests/conftest.py` — Shared fixtures: `sample_repo` (synthetic Python project), `sample_repo_multilang`, `populated_graph` (pre-loaded SQLiteSymbolGraph), `codemap_instance`, session-scoped `flask_repo`/`django_repo`/`fastapi_repo` (clone real repos)
- `tests/test_parallel_engine.py` — Parser engine: walk, T0, T1, language detection, FQN building
- `tests/test_tier_manager.py` — Tier classification, promotion, demotion, query tracking
- `tests/test_orchestrator.py` — Integration: scout, focus, update, full lifecycle, DB persistence
- `tests/test_tools.py` — Agent tools: search, symbol lookup, call chains, impact analysis
- `tests/test_sqlite_graph.py` — Graph layer: upsert, queries, call chains, cascading deletes
- `tests/test_call_chain_accuracy.py` — Call chain correctness: forward/reverse, cycles, diamond deps
- `tests/test_self_index.py` — codemap-scale indexes its own codebase (`@pytest.mark.slow`)
- `tests/test_surgical_find.py` — Real-repo validation: find Django Compiler, Flask Blueprint, etc. (`@pytest.mark.slow`)
- `tests/test_benchmarks.py` — pytest-benchmark: upsert, query, traversal, engine micro-benchmarks
- `tests/test_unstructured_engine.py` — Document parsing: markdown, RST, CSV, plaintext, LiteParse mocking, cross-reference detection, folder walking
- `tests/test_community_detection.py` — Community detection: Leiden, connected components, SQL fallback, god nodes, relation provenance/confidence, community storage
- `tests/test_wiki_engine.py` — Wiki engine: ingest, search (FTS5), lint, export, content generation, orchestrator integration
- `tests/test_integration_phases.py` — Full workflow: scout + ingest docs + communities + wiki together

## Key Design Decisions

- **SQLite over NetworkX/Neo4j**: Single-file, WAL-mode concurrent reads, <200MB for 500K files, no server process needed.
- **Tiered lazy promotion**: T0+T1 are paid upfront ("scout"). T2/T3 are on-demand when an agent focuses on a region. `scout()` skips if DB already populated (use `force=True` to re-index).
- **Tree-sitter for all parsing**: Both Rust and Python paths use tree-sitter. The Rust path compiles grammars into the binary; the Python path uses `tree-sitter-*` pip packages directly.
- **Rust is optional**: The Python fallback handles all operations. Rust is a performance optimization, not a functional requirement. The same tests and API work with either backend.
- **`codemap` base package is optional**: T2 full extraction tries `from codemap.parser.extractors`, falls back to skeleton extraction if unavailable. Core functionality works without it.
- Supported languages: Python, TypeScript/TSX, JavaScript/JSX, Java, Go, Rust.
