# CodeMap at Scale: 500K+ File Architecture

## The Problem at 500K Files

| Operation | Python Prototype | Target |
|-----------|-----------------|--------|
| File walk + hash | ~25 min | <30s |
| Parse all files | ~41 min | <2 min |
| Build graph | ~15 min (NetworkX OOM) | <1 min |
| Vector embed (all) | ~8 hours + $200 API | <5 min (structural only) |
| Incremental update (100 files) | ~2s | <200ms |
| Graph query (call chain) | ~50ms | <5ms |
| Disk footprint | ~4GB (JSON graph) | <500MB |

## Core Insight: Tiered Intelligence

Not every file in a 500K-file monorepo deserves the same analysis depth.
The cost of scouting must be proportional to the value of the intelligence.

### Tier 0 — Inventory (every file, ~0.1ms/file)
- Path, size, language, content hash
- No parsing. Just filesystem metadata + xxhash.
- Purpose: change detection, language distribution, file graph

### Tier 1 — Skeleton (every source file, ~0.5ms/file)
- Tree-sitter parse → top-level symbols only (names, kinds, line ranges)
- Import/export strings (unresolved)
- No parameters, no docstrings, no raw text
- Purpose: structural navigation, symbol lookup, module dependency graph

### Tier 2 — Full Extract (on-demand or high-value files, ~2ms/file)
- Full symbol extraction: parameters, types, docstrings, decorators
- Relation extraction: calls, inheritance, implements
- AST-aware code chunking for embedding
- Purpose: deep understanding before surgical edits

### Tier 3 — Semantic (on-demand, ~50ms/file)
- Vector embedding of code chunks
- LLM-generated summaries
- Only triggered when an agent actually needs semantic search in a region
- Purpose: "find code that does X" queries

### Cost Model at 500K Files

| Tier | Files | Time (Rust) | Time (Python parallel) | Disk |
|------|-------|-------------|----------------------|------|
| T0 | 500K | 5s | 30s | 25MB |
| T1 | 300K source | 2.5 min | 12 min | 150MB |
| T2 | 10K hot files | 20s | 2 min | 50MB |
| T3 | 1K on-demand | 50s | 5 min | 20MB |
| **Total initial** | | **~3 min** | **~15 min** | **~250MB** |

The key: you pay T0+T1 upfront (the "scout"). T2+T3 are lazy — triggered
when an agent focuses on a specific area. Incremental updates only re-process
changed files at their existing tier.

## Architecture Components

```
┌──────────────────────────────────────────────────────────────┐
│                     Agent Tools (Python)                      │
│   search · symbol · chain · impact · overview · focus_area   │
├──────────────────────────────────────────────────────────────┤
│                   Tier Manager (Python)                       │
│   Decides what tier each file/region gets promoted to        │
├────────────────┬─────────────────────────────────────────────┤
│  SQLite Graph  │  Vector Index (ChromaDB/Qdrant)             │
│  Symbols+Rels  │  T3 embeddings only — lazy populated       │
│  ~250MB for    │  Grows as agent explores regions            │
│  500K files    │                                             │
├────────────────┴─────────────────────────────────────────────┤
│              Rust Core (PyO3 bindings)                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │ Walker   │ │ Hasher   │ │ Parser   │ │ Extractor      │  │
│  │ (rayon)  │ │ (xxhash) │ │ (TS lib) │ │ (per-language) │  │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────┘  │
│  Parallel file I/O → parse → extract → yield to Python      │
├──────────────────────────────────────────────────────────────┤
│                   Storage Layer                               │
│  SQLite (WAL mode) — single file, concurrent reads,          │
│  ACID transactions, 500K rows = ~200MB                       │
└──────────────────────────────────────────────────────────────┘
```

## Why SQLite Over NetworkX / Neo4j / Custom

- NetworkX: In-memory only. 500K files × ~10 symbols = 5M nodes. 
  JSON serialization of 5M-node graph = minutes to load, GBs of disk.
- Neo4j: Powerful but massive operational overhead. Overkill for
  a local dev tool. Requires a running server.
- SQLite: Single file. WAL mode gives concurrent reads during writes.
  500K files with full symbol data = ~200MB. Queries in <5ms with
  proper indexes. No server. Ships with Python. Battle-tested at
  billions of rows (SQLite powers every iPhone, Android, Chrome).

## Why Rust for the Hot Path

The hot path is: walk directory → read file → hash content → parse with
tree-sitter → extract top-level symbols → return structured data.

In Python (single-threaded): ~5ms/file → 500K files = 41 minutes.
In Python (multiprocessing, 8 cores): ~8 minutes. But IPC overhead
for 500K results is significant, and multiprocessing has GIL edge cases.

In Rust (rayon parallel, 8 cores): ~0.3ms/file → 500K files = 2-3 minutes.
Tree-sitter IS Rust. File I/O with rayon is lock-free parallel. xxhash
in Rust is native. Zero IPC overhead — everything is in-process.

The Python side handles: SQLite writes, tier management, agent tools,
vector embedding, LLM calls — all things where Python excels and
performance is not the bottleneck.
