# CodeMap-Scale

Persistent code intelligence for AI agents — designed for 500K+ file monorepos.

CodeMap-Scale gives LLM agents a fast, structured way to understand massive codebases without reading every file. Instead of brute-force `grep` or guessing file locations, an agent can **surgically find** the exact symbols, files, and call chains it needs to make a code change.

## How It Works

CodeMap-Scale uses **tiered analysis** — cheap indexing upfront, deep analysis on demand:

| Tier | What | Cost | When |
|------|------|------|------|
| **T0** | File inventory + content hashing | ~50s for 500K files | `scout()` — always |
| **T1** | Skeleton parse (names, kinds, locations) | ~2.5 min for 300K files | `scout()` — always |
| **T2** | Full extract (params, docstrings, call relations) | On demand per directory | `focus()` / `focus_file()` |
| **T3** | Wiki layer (LLM-synthesized knowledge pages) | On demand | `build_wiki()` |

Three integrated subsystems:
- **Code parsing**: Two backends for the hot path — Rust (2-3x faster) or Python fallback
- **Unstructured document ingestion**: PDFs, Office docs, markdown, images via [LiteParse](https://github.com/run-llama/liteparse)
- **Knowledge layer**: Community detection (Leiden algorithm), semantic edges, and an LLM Wiki (inspired by [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and [Graphify](https://github.com/safishamsi/graphify))

The orchestrator auto-detects which backend is available. Same API either way.

## Quick Start

### Python-Only Setup (No Rust Required)

```bash
# Requires Python 3.11+
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### With Rust Backend (Recommended for Large Repos)

```bash
# Requires Python 3.11+ and Rust toolchain
# Install Rust: brew install rust (macOS) or https://rustup.rs (Linux)

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" maturin

# Build the Rust extension
cd codemap_rs && maturin develop --release && cd ..
```

> **Anaconda users**: If `maturin` picks up the wrong Python, unset `CONDA_PREFIX` before building:
> ```bash
> unset CONDA_PREFIX && unset CONDA_DEFAULT_ENV
> source .venv/bin/activate
> cd codemap_rs && maturin develop --release
> ```

### Verify Setup

```bash
python -c "from codemap_scale.orchestrator import _HAS_RUST; print(f'Rust backend: {_HAS_RUST}')"
# → Rust backend: True   (with Rust)
# → Rust backend: False  (Python-only)
```

## CLI Examples

### Index a Codebase

```bash
# Scout: inventory + skeleton parse of all files
$ python -m codemap_scale scout /path/to/django

files: 3,006
symbols: 33,918
relations: 0
scout_time: 1.46s
backend: rust

# Re-index from scratch (even if DB exists)
$ python -m codemap_scale scout /path/to/django --force

# Control parallelism
$ python -m codemap_scale scout /path/to/django --workers 8
```

### Find Symbols (Surgical Find)

This is the primary query for LLM agents — find exactly where something is defined.

```bash
# Find all classes matching a pattern
$ python -m codemap_scale query /path/to/django "*Middleware*" --kind class

pattern: *Middleware*
kind_filter: class
total: 50
matches:
  class  XViewMiddleware           → django/contrib/admindocs/middleware.py:9
  class  AuthenticationMiddleware  → django/contrib/auth/middleware.py:28
  class  SessionMiddleware         → django/contrib/sessions/middleware.py:12
  class  SecurityMiddleware        → django/middleware/security.py:8
  class  CsrfViewMiddleware       → django/middleware/csrf.py:130
  ...

# Find functions by name
$ python -m codemap_scale query /path/to/flask "*route*"

total: 17
matches:
  function  routes_command   → src/flask/cli.py:1061
  method    _method_route    → src/flask/sansio/scaffold.py:284
  method    route            → src/flask/sansio/scaffold.py:336
  ...

# JSON output for machine consumption
$ python -m codemap_scale query /path/to/fastapi "*APIRouter*" --kind class --json

{
  "pattern": "*APIRouter*",
  "kind_filter": "class",
  "matches": [
    {
      "fqn": "fastapi.routing.APIRouter",
      "name": "APIRouter",
      "kind": "class",
      "file_path": "fastapi/routing.py",
      "start_line": 1005,
      "end_line": 1820
    }
  ],
  "total": 1
}
```

### Codebase Overview

```bash
$ python -m codemap_scale stats /path/to/django

files: 3,006
symbols: 33,918
relations: 0
languages:
  python: 2,892
  javascript: 114
tiers:
  tier_1: 3,006
db_size_mb: 12.5
```

### Deep-Parse a Region (Focus)

Promote a directory to T2 — extracts parameters, docstrings, and call relations.

```bash
$ python -m codemap_scale focus /path/to/django django/db/models/

promoted: 45
symbols: 2,082
relations: 0
elapsed: 0.2s
```

### Call Chain Traversal

```bash
# Forward: what does this function call?
$ python -m codemap_scale chain /path/to/repo module.func_a --depth 3

# JSON output for programmatic use
$ python -m codemap_scale chain /path/to/repo module.func_a --depth 2 --json
```

### Impact Analysis

```bash
# What depends on this file?
$ python -m codemap_scale impact /path/to/django django/db/models/base.py
```

### Incremental Update

After code changes, re-index only what changed. T2 files stay at T2.

```bash
$ python -m codemap_scale update /path/to/repo
```

### Ingest Documents (Phase 1: LiteParse)

Scan folders/subfolders for unstructured files and parse them into the graph.

```bash
# Ingest all documents in the repo (PDFs, DOCX, Markdown, CSV, images)
$ python -m codemap_scale ingest-docs /path/to/repo

documents: 12
symbols: 45
references: 8
liteparse_available: true
elapsed_seconds: 1.2

# Ingest from a specific docs/ directory
$ python -m codemap_scale ingest-docs /path/to/repo --directory docs/

# Skip doc→code cross-reference detection
$ python -m codemap_scale ingest-docs /path/to/repo --no-refs
```

Supported formats:
- **Native** (no dependencies): `.md`, `.rst`, `.txt`, `.csv`, `.tsv`
- **Via LiteParse** (`npm i -g @llamaindex/liteparse`): `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.doc`, `.ppt`, `.xls`, `.odt`, `.odp`
- **Via LiteParse OCR**: `.png`, `.jpg`, `.gif`, `.bmp`, `.tiff`, `.webp`, `.svg`

### Community Detection (Phase 2: Graphify-Inspired)

Detect module communities (clusters) and find god nodes (most connected symbols).

```bash
# Detect communities using Leiden algorithm
$ python -m codemap_scale communities /path/to/repo

communities: 5
god_nodes:
  [0] function process (payments/service.py) — total_degree: 8
  [1] class Payment (payments/models.py) — total_degree: 6

# Find god nodes (most connected symbols)
$ python -m codemap_scale god-nodes /path/to/repo --top 10

# Tune Leiden resolution (higher = more granular communities)
$ python -m codemap_scale communities /path/to/repo --resolution 2.0
```

### Wiki Layer (Phase 3: LLM Wiki Pattern)

Build a persistent, searchable knowledge wiki from your codebase.

```bash
# Build wiki from T2 data
$ python -m codemap_scale wiki-build /path/to/repo

pages_created: 15
pages_updated: 0
wiki_stats:
  total_pages: 15
  by_type:
    module: 12
    directory: 2
    overview: 1

# Search the wiki
$ python -m codemap_scale wiki-search /path/to/repo "payment processing"

# View a specific wiki page
$ python -m codemap_scale wiki-page /path/to/repo src_service_py

# Health-check: find orphans, broken refs, gaps
$ python -m codemap_scale wiki-lint /path/to/repo

# Export as Obsidian-compatible markdown
$ python -m codemap_scale wiki-export /path/to/repo -o ./wiki-output/
```

## Python API Examples

### Basic: Scout and Query

```python
from codemap_scale.orchestrator import CodeMapScale

cm = CodeMapScale("/path/to/django")
stats = cm.scout()
print(f"Indexed {stats['files']} files, {stats['symbols']} symbols")

# Find the QuerySet class
results = cm.tools.search_structural("*QuerySet*", kind="class")
for match in results["matches"][:5]:
    print(f"  {match['name']} → {match['file_path']}:{match['start_line']}")

cm.close()
```

Output:
```
Indexed 3006 files, 33918 symbols
  PreventQuerySetCloning → django/db/models/query.py:301
  QuerySet → django/db/models/query.py:323
  EmptyQuerySet → django/db/models/query.py:2334
  RawQuerySet → django/db/models/query.py:2344
  ...
```

### File Map: Understand a File Before Editing

```python
cm = CodeMapScale("/path/to/flask")
cm.scout()

# Get structured outline of a file
file_map = cm.tools.get_file_map("src/flask/app.py")
for sym in file_map["symbols"][:10]:
    print(f"  {sym['kind']:10s} {sym['name']:30s} lines {sym['start_line']}-{sym['end_line']}")

cm.close()
```

Output:
```
  function   _make_timedelta                lines 73-77
  class      Flask                          lines 109-1625
  method     __init__                       lines 310-363
  method     send_static_file               lines 392-412
  method     open_resource                  lines 414-445
  method     run                            lines 583-637
  method     test_client                    lines 639-654
  method     register_blueprint             lines 695-720
  method     add_url_rule                   lines 722-790
  method     route                          lines 1192-1262
```

### Focus: Deep-Parse a Region

```python
cm = CodeMapScale("/path/to/django")
cm.scout()

# Promote django/db/models/ to T2 (extracts params, docstrings, relations)
result = cm.focus("django/db/models/")
print(f"Promoted {result['promoted']} files, extracted {result['symbols']} symbols")

# Now queries in this region have full detail
symbol = cm.tools.get_symbol("django.db.models.query.QuerySet")
print(f"  {symbol['name']} at {symbol['file_path']}:{symbol['start_line']}-{symbol['end_line']}")

cm.close()
```

Output:
```
Promoted 45 files, extracted 2082 symbols
  QuerySet at django/db/models/query.py:323-2326
```

### Incremental Update After Code Changes

```python
cm = CodeMapScale("/path/to/repo")
cm.scout()

# ... developer makes changes to some files ...

# Re-index only changed files (T2 files stay at T2)
update_result = cm.update()
print(f"Changed: {update_result['changed']}, Deleted: {update_result['deleted']}")

cm.close()
```

### Full LLM Agent Workflow

This is the intended use case — an LLM agent receives a task and uses CodeMap-Scale as a tool to find the right files to modify.

```python
from codemap_scale.orchestrator import CodeMapScale

cm = CodeMapScale("/path/to/fastapi")
cm.scout()

# Step 1: Agent receives task "add rate limiting to the API router"
# Agent searches for the APIRouter class
results = cm.tools.search_structural("*APIRouter*", kind="class")
print(f"Found APIRouter at: {results['matches'][0]['file_path']}")
# → fastapi/routing.py

# Step 2: Agent gets the file structure to understand what to modify
file_map = cm.tools.get_file_map("fastapi/routing.py")
print(f"File has {file_map['symbol_count']} symbols")
for sym in file_map["symbols"][:5]:
    print(f"  {sym['kind']:10s} {sym['name']:30s} lines {sym['start_line']}-{sym['end_line']}")
# → class APIRouter (line 1005), method add_api_route (line 1070), ...

# Step 3: Agent focuses on the routing module for full detail
cm.focus("fastapi/")
chain = cm.tools.get_call_chain("fastapi.routing.APIRouter", depth=2)

# Step 4: Agent knows exactly which file and lines to edit
# → Edit fastapi/routing.py around line 1005

# Step 5: After making changes, update the index
cm.update()

cm.close()
```

### Search Across Security-Related Code

```python
cm = CodeMapScale("/path/to/fastapi")
cm.scout()

# Find all security-related symbols
results = cm.tools.search_structural("*security*")
print(f"Found {results['total']} security-related symbols:")
for m in results["matches"][:10]:
    print(f"  {m['kind']:10s} {m['name']:35s} → {m['file_path']}:{m['start_line']}")

cm.close()
```

Output:
```
Found 50 security-related symbols:
  method     _is_security_scheme                → fastapi/dependencies/models.py:87
  class      SecuritySchemeType                 → fastapi/openapi/models.py:321
  class      SecurityBase                       → fastapi/openapi/models.py:328
  function   get_openapi_security_definitions   → fastapi/openapi/utils.py:81
  function   Security                           → fastapi/param_functions.py:2372
  class      Security                           → fastapi/params.py:753
  class      SecurityBase                       → fastapi/security/base.py:4
  class      SecurityScopes                     → fastapi/security/oauth2.py:653
  ...
```

### Ingest Documents from Folders

```python
from codemap_scale.orchestrator import CodeMapScale

cm = CodeMapScale("/path/to/repo")
cm.scout()

# Ingest all documents (PDFs, markdown, etc.) from the repo
doc_result = cm.ingest_documents()
print(f"Ingested {doc_result['documents']} documents, {doc_result['symbols']} sections")
print(f"Detected {doc_result['references']} doc→code cross-references")
print(f"LiteParse available: {doc_result['liteparse_available']}")

# Or target a specific folder
doc_result = cm.ingest_documents("docs/")

# Search documents alongside code
doc_results = cm.tools.search_documents("*API*")
for m in doc_results["matches"]:
    print(f"  {m['kind']:10s} {m['name']:30s} → {m['file_path']}")

cm.close()
```

Output:
```
Ingested 12 documents, 45 sections
Detected 8 doc→code cross-references
LiteParse available: True
  heading    API Guide                      → docs/api-guide.md
  heading    Authentication                 → docs/api-guide.md
  heading    Endpoints                      → docs/api-guide.md
```

### Community Detection & God Nodes

```python
cm = CodeMapScale("/path/to/django")
cm.scout()
cm.focus("django/db/models/")

# Detect module communities (Leiden algorithm)
result = cm.detect_communities(resolution=1.0)
print(f"Found {result['communities']} communities")

for god in result["god_nodes"][:5]:
    print(f"  God node: {god['name']} ({god['total_degree']} connections) → {god['file_path']}")

# Query communities via tools
communities = cm.tools.get_communities()
for comm in communities["communities"][:3]:
    print(f"  Community: {comm['label']} ({comm['member_count']} members)")

# What community is a specific symbol in?
comm = cm.tools.get_symbol_community("django.db.models.query.QuerySet")

cm.close()
```

Output:
```
Found 8 communities
  God node: QuerySet (42 connections) → django/db/models/query.py
  God node: Model (38 connections) → django/db/models/base.py
  God node: Field (31 connections) → django/db/models/fields/__init__.py
  Community: django/db/models (128 members)
  Community: django/contrib/admin (45 members)
  Community: django/http (22 members)
```

### Build & Search the Wiki

```python
cm = CodeMapScale("/path/to/repo")
cm.scout()
cm.focus("src/")

# Build wiki pages from T2 data (template-based, or pass summary_fn for LLM)
wiki_result = cm.build_wiki()
print(f"Wiki: {wiki_result['pages_created']} pages created")

# Search the wiki (uses FTS5 full-text search)
results = cm.tools.search_wiki("payment processing")
for r in results["results"]:
    print(f"  [{r['page_type']}] {r['title']} (v{r['version']})")

# Get a specific wiki page
page = cm.tools.get_wiki_page("src_service_py")
print(page["content"][:200])

# Health-check the wiki
lint_result = cm.tools.lint_wiki()
print(f"Issues: {lint_result['issue_count']}, Coverage: {lint_result['coverage']['covered_files']}/{lint_result['coverage']['t2_files']} files")

# Export wiki as Obsidian-compatible markdown
cm.wiki.export_markdown("./wiki-output/")

# Optional: use an LLM for richer wiki pages
def llm_summarizer(symbols, module_path):
    # Call your LLM here (Claude, GPT, etc.)
    return f"AI-generated summary for {module_path}..."

cm.build_wiki(summary_fn=llm_summarizer)

cm.close()
```

Output:
```
Wiki: 15 pages created
  [module] Service (v1)
  [directory] Src (v1)
  [overview] Codebase Overview (v1)
Issues: 0, Coverage: 8/8 files
```

### Full Agent Workflow (All Three Phases)

```python
from codemap_scale.orchestrator import CodeMapScale

cm = CodeMapScale("/path/to/monorepo")

# Phase 0: Scout the codebase
stats = cm.scout()
print(f"Indexed {stats['files']} files, {stats['symbols']} symbols")

# Phase 1: Ingest all documents
docs = cm.ingest_documents()
print(f"Ingested {docs['documents']} docs with {docs['references']} cross-refs")

# Phase 2: Detect communities & semantic structure
cm.focus("src/core/")
communities = cm.detect_communities()
print(f"Found {communities['communities']} communities")

# Phase 3: Build the knowledge wiki
wiki = cm.build_wiki(include_communities=True)
print(f"Wiki: {wiki['wiki_stats']['total_pages']} pages")

# Now an LLM agent can query everything:
# Code symbols
cm.tools.search_structural("*PaymentHandler*", kind="class")

# Documents
cm.tools.search_documents("*deployment*")

# Wiki knowledge
cm.tools.search_wiki("authentication flow")

# Call chains
cm.tools.get_call_chain("src.core.auth.authenticate", depth=3)

# Community context
cm.tools.get_god_nodes(top_n=10)

# Impact analysis
cm.tools.analyze_impact("src/core/auth.py")

cm.close()
```

### Check Which Backend Is Active

```python
from codemap_scale.orchestrator import _HAS_RUST, CodeMapScale

print(f"Rust backend available: {_HAS_RUST}")

cm = CodeMapScale("/path/to/repo")
# The orchestrator uses Rust automatically when available
# Force Python fallback:
cm_python = CodeMapScale("/path/to/repo", use_rust=False)
```

### Use the Rust Extension Directly (Advanced)

```python
import codemap_rs

# T0: Parallel file inventory + hashing
files = codemap_rs.scan_inventory("/path/to/repo", max_file_size_kb=500)
print(f"Found {len(files)} files")
for f in files[:3]:
    print(f"  {f['path']} ({f['language']}) hash={f['content_hash'][:12]}...")

# T1: Parallel skeleton parse with tree-sitter
parsed = codemap_rs.parse_skeleton("/path/to/repo")
total_symbols = sum(len(f.get("symbols", [])) for f in parsed)
print(f"Parsed {len(parsed)} files, {total_symbols} symbols")

# Incremental: detect changed files
stored_hashes = {"src/app.py": "abc123...", "src/utils.py": "def456..."}
changes = codemap_rs.detect_changes("/path/to/repo", stored_hashes)
print(f"Added: {len(changes['added'])}, Modified: {len(changes['modified'])}, Deleted: {len(changes['deleted'])}")
```

## Performance

Benchmarked on Apple Silicon (M-series), real open-source repos. All numbers are from single runs with warm disk cache.

### Repos Tested

| Repo | Language | Files | Description |
|------|----------|-------|-------------|
| [Flask](https://github.com/pallets/flask) | Python | 83 | Lightweight web framework |
| [FastAPI](https://github.com/fastapi/fastapi) | Python | 1,125 | Modern async web framework |
| [Django](https://github.com/django/django) | Python + JS | 3,006 | Full-stack web framework |
| [CPython](https://github.com/python/cpython) | Python + C | 3,418 | Python interpreter itself |
| [Kubernetes](https://github.com/kubernetes/kubernetes) | Go | 16,927 | Container orchestration platform |

### Scout Performance: Rust Backend

Full index (T0 inventory + T1 skeleton parse) of the entire codebase:

| Repo | Files | Symbols | Scout Time | T0 (walk+hash) | T1 (parse) | T1 Rate |
|------|-------|---------|------------|-----------------|------------|---------|
| Flask | 83 | 852 | **0.06s** | 0.0s | 0.0s | 2,474/s |
| FastAPI | 1,125 | 4,888 | **0.23s** | 0.1s | 0.1s | 8,023/s |
| Django | 3,006 | 33,918 | **1.34s** | 0.3s | 0.9s | 3,517/s |
| CPython | 3,418 | 62,774 | **1.52s** | 0.1s | 1.4s | 2,518/s |
| Kubernetes | 16,927 | 124,518 | **6.43s** | 0.8s | 5.1s | 3,288/s |

### Scout Performance: Python Backend

Same repos, Python multiprocessing fallback (no Rust extension):

| Repo | Files | Symbols | Scout Time | T0 (walk+hash) | T1 (parse) | T1 Rate |
|------|-------|---------|------------|-----------------|------------|---------|
| Flask | 83 | 852 | **0.29s** | 0.1s | 0.2s | 379/s |
| FastAPI | 1,125 | 4,888 | **1.03s** | 0.4s | 0.6s | 1,783/s |
| Django | 3,005 | 33,918 | **2.33s** | 0.8s | 1.2s | 2,507/s |
| CPython | 3,404 | 62,548 | **3.62s** | 0.9s | 2.7s | 1,254/s |
| Kubernetes | 16,927 | 28* | **6.85s** | 4.7s | 1.7s | 10,128/s |

> \* Kubernetes is a Go codebase. The Python backend only ships tree-sitter grammars for Python, TypeScript, and JavaScript — it found only 28 symbols (from 7 Python + 6 C files). The Rust backend includes native Go, Java, and Rust grammars and extracted **124,518 symbols**. This is the most significant functional difference between backends.

### Rust vs Python: Head-to-Head

| Repo | Python Scout | Rust Scout | Speedup | Python Symbols | Rust Symbols |
|------|-------------|------------|---------|----------------|--------------|
| Flask | 0.29s | 0.06s | **4.8x** | 852 | 852 |
| FastAPI | 1.03s | 0.23s | **4.5x** | 4,888 | 4,888 |
| Django | 2.33s | 1.34s | **1.7x** | 33,918 | 33,918 |
| CPython | 3.62s | 1.52s | **2.4x** | 62,548 | 62,774 |
| Kubernetes | 6.85s | 6.43s | **1.1x** | 28 | 124,518 |

Where the speedup comes from:
- **T0 (file walk + hash)**: Rust's `ignore` crate + rayon parallel walk is 2-5x faster than Python's `os.walk` + multiprocessing
- **T1 (tree-sitter parse)**: Native Rust tree-sitter has zero FFI overhead vs Python bindings — 2-6x faster
- **Django/CPython**: The gap narrows because SQLite batch inserts become the bottleneck at 30K+ symbols
- **Kubernetes**: Similar total time but Rust extracts **4,400x more symbols** (Go grammar support)

### Query Performance

Query latency is **identical** between backends — queries hit the SQLite database, not the parser. These numbers apply to both Rust and Python.

| Repo | Symbols | p50 | p95 | p99 |
|------|---------|-----|-----|-----|
| Flask | 852 | 0.28ms | 0.42ms | 0.42ms |
| FastAPI | 4,888 | 0.81ms | 1.27ms | 1.27ms |
| Django | 33,918 | 1.67ms | 4.82ms | 4.82ms |
| CPython | 62,774 | 2.38ms | 7.86ms | 7.86ms |
| Kubernetes | 124,518 | 0.70ms | 3.15ms | 3.15ms |

All queries under 8ms even on 60K+ symbol codebases.

### T2 Focus (Deep Parse a Directory)

| Repo | Focus Directory | Time | Symbols Extracted |
|------|----------------|------|-------------------|
| Flask | `src/flask/`, `tests/` | 0.38s | — |
| FastAPI | `fastapi/`, `tests/` | 0.39s | — |
| Django | `django/db/models/`, `django/http/`, `django/core/` | 0.60s | 2,082 (models alone) |
| CPython | `Lib/` | 1.58s | — |
| Kubernetes | `pkg/api/`, `pkg/controller/`, `cmd/` | 0.52s | — |

### Incremental Update

Detect changed files and re-index only those. Rust backend numbers:

| Repo | No-op (nothing changed) | 10 files modified |
|------|------------------------|-------------------|
| Flask | 0.01s | 0.14s |
| FastAPI | 0.05s | 0.17s |
| Django | 0.25s | 0.42s |
| CPython | 0.22s | 0.21s |
| Kubernetes | 0.88s | 0.82s |

Python backend incremental update:

| Repo | No-op (nothing changed) |
|------|------------------------|
| Flask | 0.01s |
| FastAPI | 0.25s |
| Django | 0.59s |
| CPython | 0.46s |
| Kubernetes | 2.47s |

Rust `detect_changes()` uses rayon for parallel hashing — the advantage grows with repo size (2.8x faster on Kubernetes).

### Resource Usage (Rust Backend)

| Repo | Files | Symbols | DB Size | Peak RSS |
|------|-------|---------|---------|----------|
| Flask | 83 | 852 | < 1 MB | 35 MB |
| FastAPI | 1,125 | 4,888 | < 1 MB | 46 MB |
| Django | 3,006 | 33,918 | 12.5 MB | 81 MB |
| CPython | 3,418 | 62,774 | 20.9 MB | 119 MB |
| Kubernetes | 16,927 | 124,518 | 60.4 MB | 230 MB |

SQLite storage is efficient — ~0.5 KB per symbol on average.

## Supported Formats

### Code Languages

| Language | Rust Backend | Python Backend |
|----------|-------------|----------------|
| Python | Yes | Yes |
| TypeScript | Yes | Yes |
| TSX | Yes | Yes |
| JavaScript | Yes | Yes |
| JSX | Yes | Yes |
| Java | Yes | No |
| Go | Yes | No |
| Rust | Yes | No |

Both backends use tree-sitter for parsing. The Rust backend compiles all 6 language grammars into the binary. The Python backend uses `tree-sitter-python`, `tree-sitter-typescript`, and `tree-sitter-javascript` pip packages. Java, Go, and Rust grammars are only available in the Rust backend — **for non-Python/JS codebases, the Rust backend is strongly recommended**.

### Unstructured Documents

| Format | Parser | Dependency |
|--------|--------|------------|
| Markdown (.md) | Native Python | None |
| reStructuredText (.rst) | Native Python | None |
| Plain text (.txt) | Native Python | None |
| CSV/TSV (.csv, .tsv) | Native Python | None |
| PDF (.pdf) | LiteParse CLI | `npm i -g @llamaindex/liteparse` |
| Word (.docx, .doc, .odt) | LiteParse CLI | `npm i -g @llamaindex/liteparse` |
| PowerPoint (.pptx, .ppt, .odp) | LiteParse CLI | `npm i -g @llamaindex/liteparse` |
| Excel (.xlsx, .xls) | LiteParse CLI | `npm i -g @llamaindex/liteparse` |
| Images (.png, .jpg, .gif, etc.) | LiteParse OCR | `npm i -g @llamaindex/liteparse` |

Without LiteParse installed, PDF/Office/image files are tracked as metadata-only entries (file path, size, hash). Markdown, RST, TXT, and CSV are always fully parsed without any external dependencies.

## Running Tests

```bash
# All tests (254 tests, ~25s including real repo cloning)
pytest tests/ -v

# Fast tests only (239 tests, ~20s, no network needed)
pytest tests/ -m "not slow" -v

# Single test
pytest tests/test_orchestrator.py::TestScout::test_indexes_all_files

# Micro-benchmarks
pytest tests/test_benchmarks.py --benchmark-only

# Large-repo benchmark harness
python benchmarks/run_benchmark.py --repos flask,fastapi,django
python benchmarks/run_benchmark.py --repos flask,fastapi,django,cpython,kubernetes
python benchmarks/run_benchmark.py --include-linux   # Linux kernel (~75K files)
```

## Architecture

```
codemap_scale/
  orchestrator.py            # CodeMapScale + ScaledCodeMapTools (main entry point)
  cli.py                     # Click CLI (all commands)
  core/
    tier_manager.py          # Tier promotion/demotion, query tracking
  graph/
    sqlite_graph.py          # SQLite storage (WAL mode), call chains, impact analysis
    community_detection.py   # Leiden/connected-component community detection, god nodes
  parser/
    parallel_engine.py       # Python fallback: multiprocessing + tree-sitter
    unstructured_engine.py   # LiteParse integration for docs, PDFs, images
  wiki/
    engine.py                # LLM Wiki: ingest, search, lint, export

codemap_rs/                  # Rust backend (optional, 2-3x faster)
  src/
    lib.rs                   # PyO3 module: scan_inventory, parse_skeleton, detect_changes
    walker.rs                # Parallel file walk (ignore crate, respects .gitignore)
    hasher.rs                # xxhash64 content hashing
    extractor.rs             # Tree-sitter skeleton extraction (6 languages)
```

**Data flow:**
1. `scout()` runs T0 (inventory + hash) + T1 (skeleton parse) on all code files
2. `ingest_documents()` scans folders for PDFs, markdown, Office docs — parses via LiteParse or native Python, detects doc→code cross-references
3. Results stored in SQLite via `SQLiteSymbolGraph` (with confidence/provenance on edges)
4. Agent queries via `ScaledCodeMapTools` (search code, documents, wiki, communities)
5. `detect_communities()` runs Leiden algorithm, identifies god nodes and module clusters
6. `build_wiki()` generates T3 wiki pages from T2 data — searchable via FTS5
7. `TierManager` auto-promotes frequently queried files; `focus()` runs T2 on regions
8. `update()` detects changes via content hashing, re-parses at stored tier level

## License

MIT
