"""
Scaled CodeMap orchestrator for 500K+ file codebases.

Coordinates:
- Tiered parsing (T0 inventory → T1 skeleton → T2 deep → T3 semantic)
- SQLite-backed graph with parallel writes
- On-demand promotion (agent focuses on area → promote that area)
- Incremental updates via content hashing

The key contract: initial scout is fast and cheap (T0+T1).
Deep intelligence is paid for only when the agent needs it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from codemap_scale.core.tier_manager import TierManager
from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph
from codemap_scale.parser.parallel_engine import ParallelParserEngine
from codemap_scale.parser.unstructured_engine import (
    UnstructuredParserEngine,
    detect_doc_code_references,
)
from codemap_scale.graph.community_detection import CommunityDetector, detect_god_nodes
from codemap_scale.wiki.engine import WikiEngine

logger = logging.getLogger(__name__)

# Try to import the Rust backend for accelerated T0/T1
try:
    import codemap_rs

    # Verify it's the actual compiled extension, not the source directory
    if not hasattr(codemap_rs, "scan_inventory"):
        raise ImportError("codemap_rs found but not compiled — missing scan_inventory")
    _HAS_RUST = True
    logger.info("Rust backend (codemap_rs) available — using accelerated path")
except ImportError:
    codemap_rs = None  # type: ignore[assignment]
    _HAS_RUST = False
    logger.info("Rust backend not available — using Python fallback")


class CodeMapScale:
    """
    Orchestrator for large-scale code intelligence.

    Usage:
        cm = CodeMapScale("/path/to/monorepo")

        # Initial scout (T0+T1): ~3 min for 500K files with Rust, ~15 min Python
        stats = cm.scout()

        # Agent needs deep info on a specific area
        cm.focus("src/payments/")

        # Agent queries
        result = cm.tools.search_structural("payment validation")
        chain = cm.tools.get_call_chain("src.payments.service.process")

        # Incremental update after changes
        cm.update()
    """

    def __init__(
        self,
        root: str | Path,
        *,
        db_path: str | Path | None = None,
        workers: int | None = None,
        max_file_size_kb: int = 500,
        use_rust: bool | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self._db_path = Path(db_path) if db_path else self.root / ".codemap" / "codemap.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self.graph = SQLiteSymbolGraph(self._db_path)
        self.tier_manager = TierManager(self.graph)
        self.engine = ParallelParserEngine(
            root=self.root,
            workers=workers,
            max_file_size_kb=max_file_size_kb,
        )
        self.unstructured_engine = UnstructuredParserEngine(
            root=self.root,
            max_file_size_kb=max_file_size_kb * 20,  # 10MB for documents
        )
        # Rust backend: auto-detect unless explicitly set
        self._use_rust = _HAS_RUST if use_rust is None else (use_rust and _HAS_RUST)
        self._max_file_size_kb = max_file_size_kb

    # -------------------------------------------------------------------
    # Scout: Initial fast index (T0 + T1)
    # -------------------------------------------------------------------

    def scout(self, *, force: bool = False, skip_tier1: bool = False) -> dict[str, Any]:
        """
        Initial scout of the codebase. Builds the structural map.

        Phase 1 (Tier 0): Inventory — walk + hash every file. ~50s for 500K.
        Phase 2 (Tier 1): Skeleton — parallel tree-sitter parse. ~2.5 min for 300K.
        Phase 3: Auto-promote entry points to Tier 2.

        Args:
            force: Re-scout even if DB already has data.
            skip_tier1: Only do T0 inventory (skip parsing).

        Returns stats about the indexing run.
        """
        # Skip if already indexed (unless forced)
        existing_stats = self.graph.get_stats()
        if existing_stats["files"] > 0 and not force:
            logger.info(
                "Database already contains %d files. Use force=True to re-scout.",
                existing_stats["files"],
            )
            existing_stats["skipped"] = True
            return existing_stats

        t_start = time.monotonic()
        stats: dict[str, Any] = {"phases": {}, "skipped": False}

        if self._use_rust:
            stats = self._scout_rust(stats, t_start, skip_tier1)
        else:
            stats = self._scout_python(stats, t_start, skip_tier1)

        # Phase 3: Auto-promote entry points (shared between backends)
        if not skip_tier1:
            self._auto_promote_entry_points(stats)

        total_elapsed = time.monotonic() - t_start
        db_stats = self.graph.get_stats()
        stats.update({
            "total_elapsed": round(total_elapsed, 1),
            **db_stats,
        })

        logger.info(
            "Scout complete: %d files, %d symbols, %d relations in %.1fs (%.1fMB)",
            db_stats["files"],
            db_stats["symbols"],
            db_stats["relations"],
            total_elapsed,
            db_stats["db_size_mb"],
        )

        return stats

    def _scout_rust(
        self, stats: dict[str, Any], t_start: float, skip_tier1: bool
    ) -> dict[str, Any]:
        """Scout using Rust backend for T0 and T1."""
        # Phase 1: Tier 0 via Rust
        logger.info("Phase 1: Tier 0 inventory scan (Rust)...")
        t0_start = time.monotonic()

        results = codemap_rs.scan_inventory(str(self.root), self._max_file_size_kb)
        # Batch insert
        batch_size = 5000
        t0_files = len(results)
        for i in range(0, len(results), batch_size):
            self.graph.upsert_files_batch(results[i : i + batch_size])

        t0_elapsed = time.monotonic() - t0_start
        stats["phases"]["tier0"] = {
            "files": t0_files,
            "elapsed_seconds": round(t0_elapsed, 1),
            "rate": round(t0_files / max(t0_elapsed, 0.01)),
            "backend": "rust",
        }
        logger.info(
            "  T0 complete: %d files in %.1fs (%d files/sec)",
            t0_files,
            t0_elapsed,
            t0_files / max(t0_elapsed, 0.01),
        )

        if skip_tier1:
            stats["total_elapsed"] = round(time.monotonic() - t_start, 1)
            return stats

        # Phase 2: Tier 1 via Rust
        logger.info("Phase 2: Tier 1 skeleton parse (Rust)...")
        t1_start = time.monotonic()

        results = codemap_rs.parse_skeleton(str(self.root), None, self._max_file_size_kb)
        t1_files = len(results)
        t1_symbols = 0

        for i in range(0, len(results), batch_size):
            batch = results[i : i + batch_size]
            file_dicts = []
            symbol_dicts = []
            for r in batch:
                file_dicts.append({
                    k: r[k]
                    for k in ("path", "language", "content_hash", "size_bytes", "line_count")
                })
                file_dicts[-1]["tier"] = 1
                for sym in r.get("symbols", []):
                    symbol_dicts.append(sym)
            self.graph.upsert_files_batch(file_dicts)
            self.graph.upsert_symbols_batch(symbol_dicts)
            t1_symbols += len(symbol_dicts)

        t1_elapsed = time.monotonic() - t1_start
        stats["phases"]["tier1"] = {
            "files": t1_files,
            "symbols": t1_symbols,
            "elapsed_seconds": round(t1_elapsed, 1),
            "rate": round(t1_files / max(t1_elapsed, 0.01)),
            "backend": "rust",
        }
        logger.info(
            "  T1 complete: %d files, %d symbols in %.1fs",
            t1_files,
            t1_symbols,
            t1_elapsed,
        )

        return stats

    def _scout_python(
        self, stats: dict[str, Any], t_start: float, skip_tier1: bool
    ) -> dict[str, Any]:
        """Scout using Python multiprocessing backend."""
        # Phase 1: Tier 0 Inventory
        logger.info("Phase 1: Tier 0 inventory scan (Python)...")
        t0_start = time.monotonic()
        t0_files = 0

        for batch in self.engine.scan_tier0():
            self.graph.upsert_files_batch(batch)
            t0_files += len(batch)
            if t0_files % 50000 == 0:
                logger.info("  T0: %d files inventoried...", t0_files)

        t0_elapsed = time.monotonic() - t0_start
        stats["phases"]["tier0"] = {
            "files": t0_files,
            "elapsed_seconds": round(t0_elapsed, 1),
            "rate": round(t0_files / max(t0_elapsed, 0.01)),
            "backend": "python",
        }
        logger.info(
            "  T0 complete: %d files in %.1fs (%d files/sec)",
            t0_files,
            t0_elapsed,
            t0_files / max(t0_elapsed, 0.01),
        )

        if skip_tier1:
            stats["total_elapsed"] = round(time.monotonic() - t_start, 1)
            return stats

        # Phase 2: Tier 1 Skeleton Parse (parallel)
        logger.info("Phase 2: Tier 1 skeleton parse (%d workers)...", self.engine.workers)
        t1_start = time.monotonic()
        t1_files = 0
        t1_symbols = 0

        for batch in self.engine.parse_tier1():
            self.graph.upsert_files_batch(batch["files"])
            self.graph.upsert_symbols_batch(batch["symbols"])
            t1_files += len(batch["files"])
            t1_symbols += len(batch["symbols"])
            if t1_files % 10000 == 0:
                logger.info("  T1: %d files parsed, %d symbols...", t1_files, t1_symbols)

        t1_elapsed = time.monotonic() - t1_start
        stats["phases"]["tier1"] = {
            "files": t1_files,
            "symbols": t1_symbols,
            "elapsed_seconds": round(t1_elapsed, 1),
            "rate": round(t1_files / max(t1_elapsed, 0.01)),
            "backend": "python",
        }
        logger.info(
            "  T1 complete: %d files, %d symbols in %.1fs",
            t1_files,
            t1_symbols,
            t1_elapsed,
        )

        return stats

    def _auto_promote_entry_points(self, stats: dict[str, Any]) -> None:
        """Phase 3: Auto-promote entry points to Tier 2."""
        all_paths = [
            row["path"]
            for row in self.graph._get_conn()
            .execute("SELECT path FROM files")
            .fetchall()
        ]
        promotions = self.tier_manager.classify_for_auto_promotion(all_paths)
        tier2_candidates = promotions.get(2, [])

        if tier2_candidates:
            t2_start = time.monotonic()
            t2_paths = [self.root / fp for fp in tier2_candidates]
            t2_symbols = 0
            t2_relations = 0

            for batch in self.engine.parse_tier2(t2_paths):
                self.graph.upsert_files_batch(batch["files"])
                self.graph.upsert_symbols_batch(batch["symbols"])
                self.graph.upsert_relations_batch(batch["relations"])
                t2_symbols += len(batch["symbols"])
                t2_relations += len(batch["relations"])

            t2_elapsed = time.monotonic() - t2_start
            stats["phases"]["tier2_auto"] = {
                "files": len(tier2_candidates),
                "symbols": t2_symbols,
                "relations": t2_relations,
                "elapsed_seconds": round(t2_elapsed, 1),
            }
            logger.info(
                "  T2 auto-promote: %d entry point files in %.1fs",
                len(tier2_candidates),
                t2_elapsed,
            )

    # -------------------------------------------------------------------
    # Focus: On-demand deep analysis of a region
    # -------------------------------------------------------------------

    def focus(self, directory: str, *, tier: int = 2) -> dict[str, Any]:
        """
        Deep-analyze a specific directory/region of the codebase.

        Called when an agent needs full intelligence on an area:
        parameters, docstrings, call relations, type info.

        Args:
            directory: Relative directory path (e.g., "src/payments/")
            tier: Target tier (2=full extract, 3=+ embeddings)
        """
        t_start = time.monotonic()

        # Find files needing promotion
        candidates = self.tier_manager.promote_directory(directory, tier)
        if not candidates:
            return {"promoted": 0, "already_at_tier": tier}

        full_paths = [self.root / fp for fp in candidates]
        total_symbols = 0
        total_relations = 0

        for batch in self.engine.parse_tier2(full_paths):
            self.graph.upsert_files_batch(batch["files"])
            self.graph.upsert_symbols_batch(batch["symbols"])
            self.graph.upsert_relations_batch(batch["relations"])
            total_symbols += len(batch["symbols"])
            total_relations += len(batch["relations"])

        elapsed = time.monotonic() - t_start
        return {
            "directory": directory,
            "promoted": len(candidates),
            "symbols": total_symbols,
            "relations": total_relations,
            "elapsed_seconds": round(elapsed, 1),
        }

    def focus_file(self, file_path: str) -> dict[str, Any]:
        """Deep-analyze a single file. Used when agent drills into a specific file."""
        self.tier_manager.record_query(file_path)
        full_path = self.root / file_path

        if not full_path.exists():
            return {"error": f"File not found: {file_path}"}

        results = list(self.engine.parse_tier2([full_path]))
        if not results:
            return {"error": f"Could not parse: {file_path}"}

        for batch in results:
            self.graph.upsert_files_batch(batch["files"])
            self.graph.upsert_symbols_batch(batch["symbols"])
            self.graph.upsert_relations_batch(batch["relations"])

        return {
            "file": file_path,
            "symbols": sum(len(b["symbols"]) for b in results),
            "relations": sum(len(b["relations"]) for b in results),
            "tier": 2,
        }

    # -------------------------------------------------------------------
    # Incremental Update
    # -------------------------------------------------------------------

    def update(self) -> dict[str, Any]:
        """
        Incremental update: detect changed files and re-index only those.

        Uses content hashing to identify changes. Re-indexes at the
        file's current tier (preserves T2 data for T2 files).
        """
        t_start = time.monotonic()

        # Get stored hashes and tiers
        stored_hashes = self.graph.get_file_hashes()

        if self._use_rust:
            changes = codemap_rs.detect_changes(str(self.root), stored_hashes)
            changed_paths = changes["added"] + changes["modified"]
            deleted = changes["deleted"]
        else:
            changed_paths, deleted = self._detect_changes_python(stored_hashes)

        # Remove deleted files
        if deleted:
            self.graph.remove_files_batch(deleted)

        # Group changed files by their stored tier for re-parsing at correct level
        conn = self.graph._get_conn()
        t1_files = []
        t2_files = []
        for rel_path in changed_paths:
            row = conn.execute(
                "SELECT tier FROM files WHERE path = ?", (rel_path,)
            ).fetchone()
            current_tier = row["tier"] if row else 1
            full_path = self.root / rel_path
            if current_tier >= 2:
                t2_files.append(full_path)
            else:
                t1_files.append(full_path)

        # Re-parse T1 files at T1
        t1_symbols = 0
        if t1_files:
            for batch in self.engine.parse_tier1(t1_files):
                self.graph.upsert_files_batch(batch["files"])
                self.graph.upsert_symbols_batch(batch["symbols"])
                t1_symbols += len(batch["symbols"])

        # Re-parse T2 files at T2 (preserving params, docstrings, relations)
        t2_symbols = 0
        t2_relations = 0
        if t2_files:
            for batch in self.engine.parse_tier2(t2_files):
                self.graph.upsert_files_batch(batch["files"])
                self.graph.upsert_symbols_batch(batch["symbols"])
                self.graph.upsert_relations_batch(batch["relations"])
                t2_symbols += len(batch["symbols"])
                t2_relations += len(batch["relations"])

        elapsed = time.monotonic() - t_start
        return {
            "changed": len(changed_paths),
            "deleted": len(deleted),
            "t1_reparsed": len(t1_files),
            "t2_reparsed": len(t2_files),
            "symbols_updated": t1_symbols + t2_symbols,
            "relations_updated": t2_relations,
            "elapsed_seconds": round(elapsed, 1),
        }

    def _detect_changes_python(
        self, stored_hashes: dict[str, str]
    ) -> tuple[list[str], list[str]]:
        """Detect changes using Python (fallback when Rust not available)."""
        import xxhash

        current_files = self.engine.walk_files()
        current_paths: set[str] = set()
        changed: list[str] = []

        for path in current_files:
            try:
                rel = str(path.relative_to(self.root))
            except ValueError:
                continue
            current_paths.add(rel)

            try:
                current_hash = xxhash.xxh64(path.read_bytes()).hexdigest()
            except OSError:
                continue

            if rel not in stored_hashes or stored_hashes[rel] != current_hash:
                changed.append(rel)

        deleted = [fp for fp in stored_hashes if fp not in current_paths]
        return changed, deleted

    # -------------------------------------------------------------------
    # Unstructured Document Ingestion
    # -------------------------------------------------------------------

    def ingest_documents(
        self,
        directory: str | None = None,
        *,
        detect_references: bool = True,
    ) -> dict[str, Any]:
        """
        Scan and parse unstructured documents (PDFs, Office, markdown, etc.).

        Walks the specified directory (or repo root) for document files,
        parses them using LiteParse or native parsers, and stores results
        in the graph. Optionally detects cross-references to code symbols.

        Args:
            directory: Subdirectory to scan (None = entire repo root)
            detect_references: Whether to detect doc→code references

        Returns:
            Stats about ingested documents.
        """
        t_start = time.monotonic()

        # Walk for documents
        if directory:
            scan_root = self.root / directory
            doc_paths = [
                p for p in self.unstructured_engine.walk_documents()
                if str(p).startswith(str(scan_root))
            ]
        else:
            doc_paths = self.unstructured_engine.walk_documents()

        if not doc_paths:
            return {"documents": 0, "symbols": 0, "elapsed_seconds": 0}

        logger.info("Ingesting %d documents...", len(doc_paths))

        total_files = 0
        total_symbols = 0
        total_relations = 0

        for batch in self.unstructured_engine.parse_documents(doc_paths):
            self.graph.upsert_files_batch(batch["files"])
            self.graph.upsert_symbols_batch(batch["symbols"])
            if batch.get("relations"):
                self.graph.upsert_relations_batch(batch["relations"])
            total_files += len(batch["files"])
            total_symbols += len(batch["symbols"])
            total_relations += len(batch.get("relations", []))

        # Detect doc→code cross-references
        if detect_references:
            code_symbols = self.graph._get_conn().execute(
                "SELECT fqn, name, kind FROM symbols WHERE language != 'document'"
            ).fetchall()
            code_sym_list = [dict(r) for r in code_symbols]

            for batch in self.unstructured_engine.parse_documents(doc_paths):
                for f in batch["files"]:
                    full_text = None
                    # Re-parse for full_text (it's not stored in graph)
                    full_path = self.root / f["path"]
                    if full_path.suffix.lower() in (".md", ".rst", ".txt"):
                        try:
                            full_text = full_path.read_text(errors="replace")
                        except OSError:
                            pass

                    if full_text and code_sym_list:
                        refs = detect_doc_code_references(
                            full_text, f["path"], code_sym_list
                        )
                        if refs:
                            self.graph.upsert_relations_batch(refs)
                            total_relations += len(refs)

        elapsed = time.monotonic() - t_start
        logger.info(
            "Document ingestion: %d files, %d symbols, %d refs in %.1fs",
            total_files, total_symbols, total_relations, elapsed,
        )

        return {
            "documents": total_files,
            "symbols": total_symbols,
            "references": total_relations,
            "liteparse_available": self.unstructured_engine.liteparse_available,
            "elapsed_seconds": round(elapsed, 1),
        }

    # -------------------------------------------------------------------
    # Community Detection (Graphify-inspired)
    # -------------------------------------------------------------------

    def detect_communities(
        self, resolution: float = 1.0, min_size: int = 2
    ) -> dict[str, Any]:
        """
        Run community detection on the code graph.

        Uses Leiden algorithm when graspologic is available,
        falls back to connected components or directory grouping.

        Returns community detection results and stores them in DB.
        """
        t_start = time.monotonic()
        conn = self.graph._get_conn()

        detector = CommunityDetector(conn)
        communities = detector.detect_communities(
            resolution=resolution, min_community_size=min_size
        )

        # Store in DB
        stored = self.graph.store_communities(communities)

        elapsed = time.monotonic() - t_start
        return {
            "communities": len(communities),
            "total_members_stored": stored,
            "god_nodes": detect_god_nodes(conn, top_n=10),
            "elapsed_seconds": round(elapsed, 1),
        }

    # -------------------------------------------------------------------
    # Wiki Layer (T3 - LLM Wiki Pattern)
    # -------------------------------------------------------------------

    @property
    def wiki(self) -> WikiEngine:
        """Get the wiki engine instance."""
        if not hasattr(self, "_wiki"):
            self._wiki = WikiEngine(self.graph._get_conn(), self._root_path)
        return self._wiki

    @property
    def _root_path(self) -> Path:
        return self.root

    def build_wiki(
        self,
        *,
        summary_fn: Any | None = None,
        include_overview: bool = True,
        include_communities: bool = True,
    ) -> dict[str, Any]:
        """
        Build or update the wiki layer from current graph data.

        This is the T3 operation — synthesizes knowledge from T2 data
        into cross-referenced wiki pages.

        Args:
            summary_fn: Optional LLM summarization function
            include_overview: Generate overview page
            include_communities: Include community info in overview

        Returns:
            Stats about wiki generation.
        """
        t_start = time.monotonic()
        conn = self.graph._get_conn()
        results: dict[str, Any] = {"pages_created": 0, "pages_updated": 0, "pages_unchanged": 0}

        # Generate per-module wiki pages for T2 files
        t2_files = conn.execute(
            "SELECT path FROM files WHERE tier >= 2"
        ).fetchall()

        for file_row in t2_files:
            fp = file_row["path"]
            symbols = self.graph.get_file_symbols(fp)
            if not symbols:
                continue

            result = self.wiki.ingest_module(fp, symbols, summary_fn=summary_fn)
            action = result.get("action", "unchanged")
            results[f"pages_{action}"] = results.get(f"pages_{action}", 0) + 1

        # Generate directory-level pages
        dirs: dict[str, dict[str, list]] = {}
        for file_row in t2_files:
            fp = file_row["path"]
            parts = fp.rsplit("/", 1)
            dir_path = parts[0] if len(parts) > 1 else "."
            if dir_path not in dirs:
                dirs[dir_path] = {}
            dirs[dir_path][fp] = self.graph.get_file_symbols(fp)

        for dir_path, files_syms in dirs.items():
            result = self.wiki.ingest_directory(
                dir_path, files_syms, summary_fn=summary_fn
            )
            action = result.get("action", "unchanged")
            results[f"pages_{action}"] = results.get(f"pages_{action}", 0) + 1

        # Generate overview page
        if include_overview:
            stats = self.graph.get_stats()
            hotspots = self.graph.get_hotspots(top_n=15)
            communities = None
            if include_communities:
                communities = self.graph.get_communities()

            result = self.wiki.ingest_overview(
                stats, hotspots, communities, summary_fn=summary_fn
            )
            action = result.get("action", "unchanged")
            results[f"pages_{action}"] = results.get(f"pages_{action}", 0) + 1

        elapsed = time.monotonic() - t_start
        results["elapsed_seconds"] = round(elapsed, 1)
        results["wiki_stats"] = self.wiki.get_stats()
        return results

    # -------------------------------------------------------------------
    # Agent Tools
    # -------------------------------------------------------------------

    @property
    def tools(self) -> ScaledCodeMapTools:
        """Get the agent-facing tools interface."""
        if not hasattr(self, "_tools"):
            self._tools = ScaledCodeMapTools(self.graph, self.tier_manager, self)
        return self._tools

    # -------------------------------------------------------------------
    # Semantic Index (Natural Language Search)
    # -------------------------------------------------------------------

    @property
    def semantic(self):
        """Get the semantic index instance."""
        if not hasattr(self, "_semantic"):
            from codemap_scale.index.semantic_engine import SemanticIndex
            self._semantic = SemanticIndex(self.graph._get_conn())
        return self._semantic

    def build_semantic_index(self, *, force: bool = False, progress_fn=None) -> dict[str, Any]:
        """
        Build the semantic vector index from all symbols in the graph.

        This enables natural language queries like "what handles authentication?"
        Requires: pip install 'codemap-scale[embeddings-local]'
        """
        symbols = self.graph.get_all_symbols()
        return self.semantic.build(symbols, force=force, progress_fn=progress_fn)

    def ask(self, question: str, top_k: int = 10) -> dict[str, Any]:
        """
        Ask a natural language question about the codebase.

        Examples:
            cm.ask("what handles authentication?")
            cm.ask("where is the database connection configured?")
            cm.ask("find error handling middleware")
        """
        results = self.semantic.search(question, top_k=top_k)
        # Enrich results with full symbol info from graph
        enriched = []
        for r in results:
            sym = self.graph.get_symbol(r["fqn"])
            if sym:
                enriched.append({
                    "fqn": r["fqn"],
                    "name": sym.get("name", ""),
                    "kind": sym.get("kind", ""),
                    "file_path": sym.get("file_path", ""),
                    "start_line": sym.get("start_line"),
                    "end_line": sym.get("end_line"),
                    "score": r["score"],
                    "context": r["text_repr"],
                })
            else:
                enriched.append(r)
        return {"query": question, "results": enriched, "total": len(enriched)}

    def close(self) -> None:
        """Close database connections."""
        self.graph.close()


class ScaledCodeMapTools:
    """
    Agent-facing tools interface for scaled codebases.

    Adds tier-awareness: if an agent queries something at Tier 1,
    the tool auto-promotes it to Tier 2 before returning results.
    """

    def __init__(
        self,
        graph: SQLiteSymbolGraph,
        tier_manager: TierManager,
        orchestrator: CodeMapScale,
    ) -> None:
        self.graph = graph
        self.tier_manager = tier_manager
        self._orchestrator = orchestrator

    def search_structural(
        self, name_pattern: str, kind: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """
        Structural symbol search. Works at Tier 1+ (no embeddings needed).
        Instant results. Use for "find class *Payment*" type queries.
        """
        results = self.graph.find_symbols(name_pattern, kind=kind, limit=limit)
        return {
            "pattern": name_pattern,
            "kind_filter": kind,
            "matches": results,
            "total": len(results),
        }

    def get_symbol(self, fqn: str) -> dict[str, Any]:
        """Get full symbol details. Auto-promotes to Tier 2 if needed."""
        sym = self.graph.get_symbol(fqn)
        if sym is None:
            return {"error": f"Symbol not found: {fqn}"}

        # If symbol is at Tier 1, promote its file for full details
        if sym.get("tier", 1) < 2:
            file_path = sym.get("file_path")
            if file_path:
                self._orchestrator.focus_file(file_path)
                sym = self.graph.get_symbol(fqn)
                if sym is None:
                    return {"error": f"Symbol lost after promotion: {fqn}"}

        return sym

    def get_call_chain(self, fqn: str, depth: int = 3) -> dict[str, Any]:
        """Get downstream call chain. Requires Tier 2 on the starting file."""
        sym = self.graph.get_symbol(fqn)
        if sym and sym.get("tier", 1) < 2 and sym.get("file_path"):
            self._orchestrator.focus_file(sym["file_path"])

        return {"call_chain": self.graph.get_call_chain(fqn, depth=depth)}

    def get_callers(self, fqn: str, depth: int = 3) -> dict[str, Any]:
        """Impact analysis: what calls this symbol."""
        return {"impact": self.graph.get_reverse_call_chain(fqn, depth=depth)}

    def get_file_map(self, file_path: str) -> dict[str, Any]:
        """Get structured map of a file. Auto-promotes to Tier 2."""
        self._orchestrator.focus_file(file_path)
        self.tier_manager.record_query(file_path)

        symbols = self.graph.get_file_symbols(file_path)
        deps = self.graph.get_module_dependencies(file_path)
        dependents = self.graph.get_module_dependents(file_path)

        return {
            "file": file_path,
            "symbols": symbols,
            "dependencies": deps,
            "dependents": dependents,
        }

    def get_overview(self) -> dict[str, Any]:
        """High-level project overview. Works at Tier 0+."""
        stats = self.graph.get_stats()
        hotspots = self.graph.get_hotspots(top_n=15)
        return {**stats, "hotspots": hotspots}

    def analyze_impact(self, file_path: str) -> dict[str, Any]:
        """Analyze the blast radius of changing a file."""
        direct = self.graph.get_module_dependents(file_path)
        transitive: set[str] = set()
        for dep in direct:
            transitive.update(self.graph.get_module_dependents(dep))
        transitive -= set(direct)
        transitive.discard(file_path)

        return {
            "file": file_path,
            "direct_dependents": direct,
            "transitive_dependents": sorted(transitive),
            "total_impact_radius": len(set(direct) | transitive),
        }

    def focus_area(self, directory: str) -> dict[str, Any]:
        """
        Tell the agent to focus on a directory. Promotes all files
        in that directory to Tier 2 for deep analysis.
        """
        return self._orchestrator.focus(directory)

    # -------------------------------------------------------------------
    # Document Tools
    # -------------------------------------------------------------------

    def search_documents(
        self, name_pattern: str, limit: int = 50
    ) -> dict[str, Any]:
        """Search for document sections (headings, tables, etc.)."""
        results = self.graph.find_symbols(name_pattern, limit=limit)
        doc_results = [r for r in results if r.get("language") == "document"]
        return {
            "pattern": name_pattern,
            "matches": doc_results,
            "total": len(doc_results),
        }

    def ingest_documents(
        self, directory: str | None = None
    ) -> dict[str, Any]:
        """Scan and parse documents from a folder or the entire repo."""
        return self._orchestrator.ingest_documents(directory)

    # -------------------------------------------------------------------
    # Community / Semantic Tools
    # -------------------------------------------------------------------

    def get_communities(self) -> dict[str, Any]:
        """Get detected module communities."""
        communities = self.graph.get_communities()
        return {"communities": communities, "total": len(communities)}

    def get_god_nodes(self, top_n: int = 20) -> dict[str, Any]:
        """Get god nodes — the most connected symbols in the codebase."""
        nodes = self.graph.get_god_nodes(top_n=top_n)
        return {"god_nodes": nodes, "total": len(nodes)}

    def get_symbol_community(self, fqn: str) -> dict[str, Any]:
        """Get which community a symbol belongs to."""
        comm = self.graph.get_community_for_symbol(fqn)
        if comm is None:
            return {"error": f"No community found for {fqn}"}
        return comm

    def detect_communities(self, resolution: float = 1.0) -> dict[str, Any]:
        """Run community detection on the code graph."""
        return self._orchestrator.detect_communities(resolution=resolution)

    # -------------------------------------------------------------------
    # Wiki Tools
    # -------------------------------------------------------------------

    def search_wiki(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Full-text search across wiki pages."""
        results = self._orchestrator.wiki.search(query, limit=limit)
        return {"query": query, "results": results, "total": len(results)}

    def get_wiki_page(self, page_id: str) -> dict[str, Any]:
        """Get a wiki page by ID."""
        page = self._orchestrator.wiki.get_page(page_id)
        if page is None:
            return {"error": f"Wiki page not found: {page_id}"}
        return page

    def get_wiki_overview(self) -> dict[str, Any]:
        """Get wiki statistics and page listing."""
        stats = self._orchestrator.wiki.get_stats()
        pages = self._orchestrator.wiki.get_all_pages()
        return {**stats, "pages": pages}

    def build_wiki(self) -> dict[str, Any]:
        """Build or update wiki pages from current graph data."""
        return self._orchestrator.build_wiki()

    def lint_wiki(self) -> dict[str, Any]:
        """Run health checks on the wiki."""
        return self._orchestrator.wiki.lint()

    # -------------------------------------------------------------------
    # Semantic Search (Natural Language)
    # -------------------------------------------------------------------

    def semantic_search(self, query: str, top_k: int = 10) -> dict[str, Any]:
        """
        Ask a natural language question about the codebase.

        Unlike search_structural (which matches symbol names), this finds
        semantically relevant code. Requires build_semantic_index() first.

        Examples:
            "what handles user authentication?"
            "where is the database connection pool configured?"
            "find the request validation logic"
        """
        return self._orchestrator.ask(query, top_k=top_k)

    def build_semantic_index(self, force: bool = False) -> dict[str, Any]:
        """Build the semantic vector index for natural language search."""
        return self._orchestrator.build_semantic_index(force=force)
