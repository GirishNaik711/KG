"""
SQLite-backed symbol graph for large codebases (500K+ files).

Replaces NetworkX with SQLite in WAL mode for:
- Concurrent reads during writes (agent queries while indexing)
- Single-file persistence (~200MB for 500K files)
- Sub-5ms queries with proper indexes
- No memory pressure (disk-backed, OS page cache handles hot data)

Schema is designed for the exact query patterns agents use:
call chains, reverse impact, symbol lookup, file-level listing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
-- Files: one row per source file
CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    language        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    line_count      INTEGER NOT NULL,
    tier            INTEGER NOT NULL DEFAULT 1,
    summary         TEXT,
    imports_json    TEXT,  -- JSON array of raw import strings
    indexed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json   TEXT
);

-- Symbols: one row per code entity (function, class, method, etc.)
CREATE TABLE IF NOT EXISTS symbols (
    fqn             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    language        TEXT NOT NULL,
    file_path       TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    start_col       INTEGER NOT NULL DEFAULT 0,
    end_col         INTEGER NOT NULL DEFAULT 0,
    signature       TEXT,
    return_type     TEXT,
    docstring       TEXT,
    is_exported     INTEGER NOT NULL DEFAULT 1,
    is_async        INTEGER NOT NULL DEFAULT 0,
    tier            INTEGER NOT NULL DEFAULT 1,
    params_json     TEXT,  -- JSON array of {name, type, default}
    decorators_json TEXT,  -- JSON array of decorator strings
    raw_text        TEXT,  -- Source text (only at tier 2+)
    metadata_json   TEXT
);

-- Relations: directed edges between symbols
CREATE TABLE IF NOT EXISTS relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_fqn      TEXT NOT NULL,
    target_fqn      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    file_path       TEXT,  -- where the reference occurs
    line            INTEGER,
    confidence      REAL DEFAULT 1.0,       -- 0.0-1.0 confidence score
    provenance      TEXT DEFAULT 'EXTRACTED', -- EXTRACTED / INFERRED / AMBIGUOUS
    metadata_json   TEXT
);

-- Communities: Leiden/connected-component clusters
CREATE TABLE IF NOT EXISTS communities (
    fqn             TEXT PRIMARY KEY,
    community_id    INTEGER NOT NULL,
    community_label TEXT,
    detection_method TEXT DEFAULT 'leiden'
);

CREATE INDEX IF NOT EXISTS idx_communities_id ON communities(community_id);

-- Indexes for the query patterns agents actually use
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_kind_name ON symbols(kind, name);

CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_fqn);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_fqn);
CREATE INDEX IF NOT EXISTS idx_relations_kind ON relations(kind);
CREATE INDEX IF NOT EXISTS idx_relations_source_kind ON relations(source_fqn, kind);
CREATE INDEX IF NOT EXISTS idx_relations_target_kind ON relations(target_fqn, kind);
CREATE INDEX IF NOT EXISTS idx_relations_file ON relations(file_path);

-- Covering index for hotspot queries (GROUP BY target_fqn filtered by kind)
CREATE INDEX IF NOT EXISTS idx_relations_kind_target ON relations(kind, target_fqn);

CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_tier ON files(tier);
"""


class SQLiteSymbolGraph:
    """
    SQLite-backed symbol graph for 500K+ file codebases.

    Uses WAL mode for concurrent read access during writes.
    All queries are parameterized and use prepared indexes.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    # -------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.executescript(_SCHEMA)
            self._migrate_schema()
        return self._conn

    def _migrate_schema(self) -> None:
        """Add new columns to existing tables if they don't exist."""
        conn = self._conn
        # Check if relations table has confidence column
        cols = {row[1] for row in conn.execute("PRAGMA table_info(relations)").fetchall()}
        if "confidence" not in cols:
            conn.execute("ALTER TABLE relations ADD COLUMN confidence REAL DEFAULT 1.0")
        if "provenance" not in cols:
            conn.execute("ALTER TABLE relations ADD COLUMN provenance TEXT DEFAULT 'EXTRACTED'")
        conn.commit()

    @contextmanager
    def _transaction(self):
        """Context manager for batched writes with automatic commit/rollback."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def optimize(self) -> None:
        """Run ANALYZE after bulk loads to help the SQLite query planner."""
        self._get_conn().execute("ANALYZE")
        logger.info("ANALYZE complete — query planner statistics updated")

    # -------------------------------------------------------------------
    # Bulk Ingestion (Tier 0 + Tier 1)
    # -------------------------------------------------------------------

    def upsert_files_batch(self, files: list[dict[str, Any]]) -> int:
        """
        Bulk upsert file records. Used during Tier 0 inventory scan.

        Each dict: {path, language, content_hash, size_bytes, line_count, tier, ...}
        """
        with self._transaction() as conn:
            conn.executemany(
                """INSERT INTO files (path, language, content_hash, size_bytes, line_count, tier, imports_json, summary, metadata_json)
                   VALUES (:path, :language, :content_hash, :size_bytes, :line_count, :tier, :imports_json, :summary, :metadata_json)
                   ON CONFLICT(path) DO UPDATE SET
                     language=excluded.language, content_hash=excluded.content_hash,
                     size_bytes=excluded.size_bytes, line_count=excluded.line_count,
                     tier=MAX(files.tier, excluded.tier),
                     imports_json=COALESCE(excluded.imports_json, files.imports_json),
                     summary=COALESCE(excluded.summary, files.summary),
                     indexed_at=datetime('now'),
                     metadata_json=COALESCE(excluded.metadata_json, files.metadata_json)""",
                [
                    {
                        "path": f["path"],
                        "language": f["language"],
                        "content_hash": f["content_hash"],
                        "size_bytes": f["size_bytes"],
                        "line_count": f["line_count"],
                        "tier": f.get("tier", 0),
                        "imports_json": json.dumps(f.get("imports")) if f.get("imports") else None,
                        "summary": f.get("summary"),
                        "metadata_json": json.dumps(f.get("metadata")) if f.get("metadata") else None,
                    }
                    for f in files
                ],
            )
        return len(files)

    def upsert_symbols_batch(self, symbols: list[dict[str, Any]]) -> int:
        """
        Bulk upsert symbols. Used during Tier 1/2 parsing.

        Each dict must have: fqn, name, kind, language, file_path, start_line, end_line
        """
        with self._transaction() as conn:
            conn.executemany(
                """INSERT INTO symbols (fqn, name, kind, language, file_path,
                     start_line, end_line, start_col, end_col,
                     signature, return_type, docstring,
                     is_exported, is_async, tier,
                     params_json, decorators_json, raw_text, metadata_json)
                   VALUES (:fqn, :name, :kind, :language, :file_path,
                     :start_line, :end_line, :start_col, :end_col,
                     :signature, :return_type, :docstring,
                     :is_exported, :is_async, :tier,
                     :params_json, :decorators_json, :raw_text, :metadata_json)
                   ON CONFLICT(fqn) DO UPDATE SET
                     name=excluded.name, kind=excluded.kind,
                     signature=excluded.signature, return_type=excluded.return_type,
                     docstring=excluded.docstring, raw_text=excluded.raw_text,
                     tier=MAX(symbols.tier, excluded.tier),
                     params_json=COALESCE(excluded.params_json, symbols.params_json),
                     decorators_json=COALESCE(excluded.decorators_json, symbols.decorators_json)""",
                [
                    {
                        "fqn": s["fqn"],
                        "name": s["name"],
                        "kind": s["kind"],
                        "language": s["language"],
                        "file_path": s["file_path"],
                        "start_line": s["start_line"],
                        "end_line": s["end_line"],
                        "start_col": s.get("start_col", 0),
                        "end_col": s.get("end_col", 0),
                        "signature": s.get("signature"),
                        "return_type": s.get("return_type"),
                        "docstring": s.get("docstring"),
                        "is_exported": 1 if s.get("is_exported", True) else 0,
                        "is_async": 1 if s.get("is_async", False) else 0,
                        "tier": s.get("tier", 1),
                        "params_json": json.dumps(s["params"]) if s.get("params") else None,
                        "decorators_json": json.dumps(s["decorators"]) if s.get("decorators") else None,
                        "raw_text": s.get("raw_text"),
                        "metadata_json": json.dumps(s.get("metadata")) if s.get("metadata") else None,
                    }
                    for s in symbols
                ],
            )
        return len(symbols)

    def upsert_relations_batch(self, relations: list[dict[str, Any]]) -> int:
        """Bulk insert relations. Deletes existing relations for affected files first."""
        if not relations:
            return 0

        # Group by file to do per-file replacement
        files_affected = {r.get("file_path") for r in relations if r.get("file_path")}

        with self._transaction() as conn:
            for fp in files_affected:
                if fp:
                    conn.execute("DELETE FROM relations WHERE file_path = ?", (fp,))

            conn.executemany(
                """INSERT INTO relations (source_fqn, target_fqn, kind, file_path, line, confidence, provenance, metadata_json)
                   VALUES (:source_fqn, :target_fqn, :kind, :file_path, :line, :confidence, :provenance, :metadata_json)""",
                [
                    {
                        "source_fqn": r["source_fqn"],
                        "target_fqn": r["target_fqn"],
                        "kind": r["kind"],
                        "file_path": r.get("file_path"),
                        "line": r.get("line"),
                        "confidence": r.get("confidence", 1.0),
                        "provenance": r.get("provenance", "EXTRACTED"),
                        "metadata_json": json.dumps(r.get("metadata")) if r.get("metadata") else None,
                    }
                    for r in relations
                ],
            )
        return len(relations)

    def remove_file(self, file_path: str) -> None:
        """Remove a file and all its symbols/relations (CASCADE)."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM relations WHERE file_path = ?", (file_path,))
            conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
            conn.execute("DELETE FROM files WHERE path = ?", (file_path,))

    def remove_files_batch(self, file_paths: list[str]) -> int:
        """Bulk remove files."""
        if not file_paths:
            return 0
        with self._transaction() as conn:
            placeholders = ",".join("?" * len(file_paths))
            conn.execute(f"DELETE FROM relations WHERE file_path IN ({placeholders})", file_paths)
            conn.execute(f"DELETE FROM symbols WHERE file_path IN ({placeholders})", file_paths)
            conn.execute(f"DELETE FROM files WHERE path IN ({placeholders})", file_paths)
        return len(file_paths)

    # -------------------------------------------------------------------
    # Symbol Queries
    # -------------------------------------------------------------------

    def get_all_symbols(self) -> list[dict[str, Any]]:
        """Return all symbols. Used for building semantic index."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM symbols ORDER BY file_path, start_line"
        ).fetchall()
        return [self._row_to_symbol(row) for row in rows]

    def get_symbol(self, fqn: str) -> dict[str, Any] | None:
        """Look up a symbol by FQN. <5ms."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM symbols WHERE fqn = ?", (fqn,)).fetchone()
        if row is None:
            return None
        return self._row_to_symbol(row)

    def find_symbols(
        self,
        name_pattern: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Find symbols by name pattern (SQL LIKE). <10ms."""
        conn = self._get_conn()
        sql = "SELECT * FROM symbols WHERE name LIKE ?"
        params: list[Any] = [name_pattern.replace("*", "%")]

        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if file_path:
            sql += " AND file_path = ?"
            params.append(file_path)

        sql += " ORDER BY file_path, start_line LIMIT ?"
        params.append(limit)

        return [self._row_to_symbol(row) for row in conn.execute(sql, params)]

    def get_file_symbols(self, file_path: str) -> list[dict[str, Any]]:
        """Get all symbols in a file, ordered by line number. <5ms."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM symbols WHERE file_path = ? ORDER BY start_line",
            (file_path,),
        ).fetchall()
        return [self._row_to_symbol(row) for row in rows]

    # -------------------------------------------------------------------
    # Relation Queries (Call Chains, Impact)
    # -------------------------------------------------------------------

    def get_call_chain(self, fqn: str, depth: int = 3) -> dict[str, Any]:
        """
        Downstream call chain with cycle detection. <10ms for depth=3.

        Uses recursive CTE for efficient traversal in SQL.
        """
        # Get the root symbol info
        root = self.get_symbol(fqn)
        root_info = {
            "fqn": fqn,
            "name": root["name"] if root else fqn.rsplit(".", 1)[-1],
            "kind": root["kind"] if root else "unknown",
            "file": root["file_path"] if root else None,
        }

        # BFS traversal (Python-side for cycle detection)
        children = self._traverse_calls(fqn, depth, direction="outgoing", visited=set())
        if children:
            root_info["calls"] = children
        return root_info

    def get_reverse_call_chain(self, fqn: str, depth: int = 3) -> dict[str, Any]:
        """Everything that calls this symbol (impact analysis). <10ms."""
        root = self.get_symbol(fqn)
        root_info = {
            "fqn": fqn,
            "name": root["name"] if root else fqn.rsplit(".", 1)[-1],
            "kind": root["kind"] if root else "unknown",
            "file": root["file_path"] if root else None,
        }

        children = self._traverse_calls(fqn, depth, direction="incoming", visited=set())
        if children:
            root_info["called_by"] = children
        return root_info

    def _traverse_calls(
        self, fqn: str, depth: int, direction: str, visited: set[str]
    ) -> list[dict[str, Any]]:
        """BFS call chain traversal with cycle detection."""
        if depth <= 0 or fqn in visited:
            return []
        visited.add(fqn)

        conn = self._get_conn()
        if direction == "outgoing":
            rows = conn.execute(
                "SELECT DISTINCT target_fqn FROM relations WHERE source_fqn = ? AND kind IN ('calls', 'CALLS')",
                (fqn,),
            ).fetchall()
            next_fqns = [row["target_fqn"] for row in rows]
        else:
            rows = conn.execute(
                "SELECT DISTINCT source_fqn FROM relations WHERE target_fqn = ? AND kind IN ('calls', 'CALLS')",
                (fqn,),
            ).fetchall()
            next_fqns = [row["source_fqn"] for row in rows]

        results = []
        for next_fqn in next_fqns:
            sym = self.get_symbol(next_fqn)
            entry: dict[str, Any] = {
                "fqn": next_fqn,
                "name": sym["name"] if sym else next_fqn.rsplit(".", 1)[-1],
                "kind": sym["kind"] if sym else "unresolved",
                "file": sym["file_path"] if sym else None,
            }
            sub = self._traverse_calls(next_fqn, depth - 1, direction, visited)
            if sub:
                entry["calls" if direction == "outgoing" else "called_by"] = sub
            results.append(entry)

        return results

    def get_relations(
        self, fqn: str, kind: str | None = None, direction: str = "outgoing"
    ) -> list[dict[str, Any]]:
        """Get relations for a symbol."""
        conn = self._get_conn()
        if direction == "outgoing":
            sql = "SELECT * FROM relations WHERE source_fqn = ?"
        elif direction == "incoming":
            sql = "SELECT * FROM relations WHERE target_fqn = ?"
        else:
            sql = "SELECT * FROM relations WHERE source_fqn = ? OR target_fqn = ?"

        params: list[Any] = [fqn] if direction != "both" else [fqn, fqn]

        if kind:
            sql += " AND kind = ?"
            params.append(kind)

        return [dict(row) for row in conn.execute(sql, params)]

    # -------------------------------------------------------------------
    # File / Module Queries
    # -------------------------------------------------------------------

    def get_file(self, path: str) -> dict[str, Any] | None:
        """Get file metadata."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def get_file_hashes(self) -> dict[str, str]:
        """Get all file path → content_hash pairs for change detection. <100ms for 500K files."""
        conn = self._get_conn()
        rows = conn.execute("SELECT path, content_hash FROM files").fetchall()
        return {row["path"]: row["content_hash"] for row in rows}

    def get_module_dependents(self, file_path: str) -> list[str]:
        """Files that reference symbols in this file."""
        conn = self._get_conn()
        # Check both: relations targeting symbols in this file, and
        # IMPORTS relations where target_fqn starts with this module's prefix
        rows = conn.execute(
            """SELECT DISTINCT r.file_path
               FROM relations r
               JOIN symbols s ON r.target_fqn = s.fqn
               WHERE s.file_path = ? AND r.file_path != ?
               UNION
               SELECT DISTINCT r.file_path
               FROM relations r
               WHERE r.kind IN ('IMPORTS', 'imports')
                 AND r.target_fqn LIKE ? || '%'
                 AND r.file_path != ?""",
            (file_path, file_path,
             file_path.replace("/", ".").replace(".py", "").replace(".ts", ""),
             file_path),
        ).fetchall()
        return [row["file_path"] for row in rows if row["file_path"]]

    def get_module_dependencies(self, file_path: str) -> list[str]:
        """Files that this file's symbols reference."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT DISTINCT s.file_path
               FROM relations r
               JOIN symbols s ON r.target_fqn = s.fqn
               WHERE r.file_path = ? AND s.file_path != ?""",
            (file_path, file_path),
        ).fetchall()
        return [row["file_path"] for row in rows]

    # -------------------------------------------------------------------
    # Analytics
    # -------------------------------------------------------------------

    def get_hotspots(self, top_n: int = 20) -> list[dict[str, Any]]:
        """Symbols with the most incoming references. <50ms."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT r.target_fqn as fqn, COUNT(*) as ref_count,
                      s.name, s.kind, s.file_path
               FROM relations r
               LEFT JOIN symbols s ON r.target_fqn = s.fqn
               WHERE r.kind = 'calls'
               GROUP BY r.target_fqn
               ORDER BY ref_count DESC
               LIMIT ?""",
            (top_n,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, Any]:
        """Overall statistics. <10ms."""
        conn = self._get_conn()
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbol_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

        lang_rows = conn.execute(
            "SELECT language, COUNT(*) as cnt FROM files GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        languages = {row["language"]: row["cnt"] for row in lang_rows}

        tier_rows = conn.execute(
            "SELECT tier, COUNT(*) as cnt FROM files GROUP BY tier ORDER BY tier"
        ).fetchall()
        tiers = {f"tier_{row['tier']}": row["cnt"] for row in tier_rows}

        db_size = Path(self._db_path).stat().st_size if Path(self._db_path).exists() else 0

        return {
            "files": file_count,
            "symbols": symbol_count,
            "relations": relation_count,
            "languages": languages,
            "tiers": tiers,
            "db_size_mb": round(db_size / (1024 * 1024), 1),
        }

    # -------------------------------------------------------------------
    # Community Detection
    # -------------------------------------------------------------------

    def store_communities(self, communities: list[dict[str, Any]]) -> int:
        """Store community detection results."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM communities")
            count = 0
            for comm in communities:
                for member in comm.get("members", []):
                    fqn = member.get("fqn") if isinstance(member, dict) else member
                    conn.execute(
                        """INSERT OR REPLACE INTO communities (fqn, community_id, community_label, detection_method)
                           VALUES (?, ?, ?, ?)""",
                        (fqn, comm["id"], comm.get("label"), comm.get("detection_method", "leiden")),
                    )
                    count += 1
        return count

    def get_communities(self) -> list[dict[str, Any]]:
        """Get all communities with their members."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT community_id, community_label, detection_method,
                      GROUP_CONCAT(fqn) as members
               FROM communities
               GROUP BY community_id
               ORDER BY COUNT(*) DESC"""
        ).fetchall()

        return [
            {
                "id": row["community_id"],
                "label": row["community_label"],
                "detection_method": row["detection_method"],
                "members": row["members"].split(",") if row["members"] else [],
                "member_count": len(row["members"].split(",")) if row["members"] else 0,
            }
            for row in rows
        ]

    def get_community_for_symbol(self, fqn: str) -> dict[str, Any] | None:
        """Get the community a symbol belongs to."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM communities WHERE fqn = ?", (fqn,)
        ).fetchone()
        return dict(row) if row else None

    def get_god_nodes(self, top_n: int = 20) -> list[dict[str, Any]]:
        """Get god nodes — symbols with highest total degree."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT s.fqn, s.name, s.kind, s.file_path,
                      COALESCE(out_t.cnt, 0) as out_degree,
                      COALESCE(in_t.cnt, 0) as in_degree
               FROM symbols s
               LEFT JOIN (SELECT source_fqn, COUNT(*) as cnt FROM relations GROUP BY source_fqn) out_t
                    ON s.fqn = out_t.source_fqn
               LEFT JOIN (SELECT target_fqn, COUNT(*) as cnt FROM relations GROUP BY target_fqn) in_t
                    ON s.fqn = in_t.target_fqn
               WHERE COALESCE(out_t.cnt, 0) + COALESCE(in_t.cnt, 0) > 0
               ORDER BY COALESCE(out_t.cnt, 0) + COALESCE(in_t.cnt, 0) DESC
               LIMIT ?""",
            (top_n,),
        ).fetchall()

        return [
            {
                "fqn": row["fqn"],
                "name": row["name"],
                "kind": row["kind"],
                "file_path": row["file_path"],
                "in_degree": row["in_degree"],
                "out_degree": row["out_degree"],
                "total_degree": row["in_degree"] + row["out_degree"],
            }
            for row in rows
        ]

    def find_inferred_relations(self, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """Get all inferred (non-extracted) relations with optional confidence filter."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM relations
               WHERE provenance != 'EXTRACTED' AND confidence >= ?
               ORDER BY confidence DESC""",
            (min_confidence,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _row_to_symbol(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("params_json"):
            d["params"] = json.loads(d.pop("params_json"))
        else:
            d.pop("params_json", None)
            d["params"] = []
        if d.get("decorators_json"):
            d["decorators"] = json.loads(d.pop("decorators_json"))
        else:
            d.pop("decorators_json", None)
            d["decorators"] = []
        d.pop("metadata_json", None)
        d["is_exported"] = bool(d.get("is_exported", 1))
        d["is_async"] = bool(d.get("is_async", 0))
        return d
