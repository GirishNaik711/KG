"""
Semantic search engine for CodeMap-Scale.

Embeds symbol signatures, docstrings, and structural context into vectors,
enabling natural language queries like "what handles authentication?" or
"find the database connection pooling logic".

Uses sentence-transformers for local embedding (no API key required).
Vectors stored in SQLite alongside the symbol graph.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Any

_HAS_EMBEDDINGS = False
_MODEL = None

try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    _HAS_EMBEDDINGS = True
except ImportError:
    pass

DEFAULT_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension
BATCH_SIZE = 256


def _get_model(model_name: str = DEFAULT_MODEL) -> Any:
    """Lazy-load the embedding model."""
    global _MODEL
    if _MODEL is None:
        if not _HAS_EMBEDDINGS:
            raise ImportError(
                "Semantic search requires sentence-transformers. "
                "Install with: pip install 'codemap-scale[embeddings-local]'"
            )
        _MODEL = SentenceTransformer(model_name)
    return _MODEL


def _serialize_vector(vec) -> bytes:
    """Serialize numpy vector to bytes for SQLite storage."""
    return struct.pack(f"{len(vec)}f", *vec.tolist())


def _deserialize_vector(data: bytes):
    """Deserialize bytes back to numpy vector."""
    import numpy as np
    count = len(data) // 4
    return np.array(struct.unpack(f"{count}f", data), dtype=np.float32)


def _symbol_to_text(symbol: dict) -> str:
    """
    Convert a symbol dict to a searchable text representation.
    Combines name, kind, signature, docstring, file path, and params
    into a natural-language-like description.
    """
    parts = []

    kind = symbol.get("kind", "symbol")
    name = symbol.get("name", "")
    file_path = symbol.get("file_path", "")

    # Natural language description of what this symbol is
    if kind == "class":
        parts.append(f"class {name}")
    elif kind in ("function", "method"):
        parts.append(f"function {name}")
    else:
        parts.append(f"{kind} {name}")

    # File context gives domain hints
    if file_path:
        # Convert path to readable context: "src/auth/middleware.py" -> "in auth middleware"
        path_parts = Path(file_path).with_suffix("").parts
        # Skip common prefixes like src/, lib/
        meaningful = [p for p in path_parts if p not in ("src", "lib", "Lib", "pkg", "cmd")]
        if meaningful:
            parts.append(f"in {'/'.join(meaningful[-3:])}")

    # Signature has parameter types and return type
    sig = symbol.get("signature", "")
    if sig:
        parts.append(sig)

    # Docstring is the most semantically rich
    docstring = symbol.get("docstring", "")
    if docstring:
        # Take first 200 chars of docstring
        parts.append(docstring[:200])

    # Parameter names carry semantic meaning
    params = symbol.get("params", [])
    if params:
        param_names = [p["name"] for p in params if isinstance(p, dict) and p.get("name")]
        if param_names:
            parts.append(f"parameters: {', '.join(param_names[:10])}")

    return " | ".join(parts)


class SemanticIndex:
    """
    Vector index for semantic code search.

    Stores embeddings in the same SQLite DB as the symbol graph,
    in a dedicated `embeddings` table.
    """

    def __init__(self, db_conn, model_name: str = DEFAULT_MODEL):
        self.conn = db_conn
        self.model_name = model_name
        self._ensure_table()

    def _ensure_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                fqn TEXT PRIMARY KEY,
                text_repr TEXT NOT NULL,
                vector BLOB NOT NULL,
                model TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self.conn.commit()

    @property
    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return row[0]

    def build(
        self, symbols: list[dict], force: bool = False, progress_fn=None
    ) -> dict[str, Any]:
        """
        Build or update the semantic index from symbols.

        Args:
            symbols: List of symbol dicts from the graph
            force: Re-embed all symbols even if already indexed
            progress_fn: Optional callback(embedded_count, total)
        """
        t_start = time.monotonic()
        model = _get_model(self.model_name)

        # Filter to symbols worth indexing (skip tiny/unnamed ones)
        indexable = [s for s in symbols if s.get("name") and len(s.get("name", "")) > 1]

        if not force:
            # Only embed symbols not already in the index
            existing = set()
            rows = self.conn.execute("SELECT fqn FROM embeddings WHERE model = ?", (self.model_name,)).fetchall()
            existing = {row[0] for row in rows}
            to_embed = [s for s in indexable if s.get("fqn") not in existing]
        else:
            to_embed = indexable

        if not to_embed:
            return {
                "embedded": 0,
                "total_indexed": self.count,
                "elapsed_seconds": round(time.monotonic() - t_start, 2),
            }

        # Generate text representations
        texts = [_symbol_to_text(s) for s in to_embed]
        fqns = [s["fqn"] for s in to_embed]

        # Batch embed
        total_embedded = 0
        for i in range(0, len(texts), BATCH_SIZE):
            batch_texts = texts[i:i + BATCH_SIZE]
            batch_fqns = fqns[i:i + BATCH_SIZE]

            vectors = model.encode(batch_texts, normalize_embeddings=True, show_progress_bar=False)

            # Store in DB
            now = time.time()
            rows_to_insert = []
            for fqn, text, vec in zip(batch_fqns, batch_texts, vectors):
                rows_to_insert.append((fqn, text, _serialize_vector(vec), self.model_name, now))

            self.conn.executemany(
                """INSERT OR REPLACE INTO embeddings (fqn, text_repr, vector, model, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                rows_to_insert,
            )
            self.conn.commit()

            total_embedded += len(batch_texts)
            if progress_fn:
                progress_fn(total_embedded, len(to_embed))

        elapsed = time.monotonic() - t_start
        return {
            "embedded": total_embedded,
            "total_indexed": self.count,
            "elapsed_seconds": round(elapsed, 2),
            "symbols_per_sec": round(total_embedded / elapsed) if elapsed > 0 else 0,
        }

    def search(self, query: str, top_k: int = 10, min_score: float = 0.2) -> list[dict[str, Any]]:
        """
        Semantic search: find symbols most relevant to a natural language query.

        Args:
            query: Natural language question (e.g., "what handles authentication?")
            top_k: Number of results to return
            min_score: Minimum cosine similarity threshold

        Returns:
            List of {fqn, name, kind, file_path, score, text_repr} sorted by relevance
        """
        model = _get_model(self.model_name)

        # Embed the query
        query_vec = model.encode([query], normalize_embeddings=True)[0]

        # Load all vectors and compute cosine similarity
        # For codebases up to ~500K symbols, this brute-force approach
        # takes <50ms with numpy. No need for ANN until >1M symbols.
        rows = self.conn.execute(
            "SELECT fqn, text_repr, vector FROM embeddings WHERE model = ?",
            (self.model_name,),
        ).fetchall()

        if not rows:
            return []

        import numpy as np

        fqns = []
        texts = []
        vectors = []
        for row in rows:
            fqns.append(row[0])
            texts.append(row[1])
            vectors.append(_deserialize_vector(row[2]))

        # Vectorized cosine similarity (vectors are already normalized)
        matrix = np.vstack(vectors)
        scores = matrix @ query_vec

        # Get top_k results above threshold
        top_indices = np.argsort(scores)[::-1][:top_k * 2]  # over-fetch then filter

        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score < min_score:
                break
            results.append({
                "fqn": fqns[idx],
                "score": round(score, 4),
                "text_repr": texts[idx],
            })
            if len(results) >= top_k:
                break

        return results

    def remove_stale(self, valid_fqns: set[str]) -> int:
        """Remove embeddings for symbols that no longer exist."""
        all_fqns = {row[0] for row in self.conn.execute("SELECT fqn FROM embeddings").fetchall()}
        stale = all_fqns - valid_fqns
        if stale:
            placeholders = ",".join("?" * len(stale))
            self.conn.execute(f"DELETE FROM embeddings WHERE fqn IN ({placeholders})", list(stale))
            self.conn.commit()
        return len(stale)
