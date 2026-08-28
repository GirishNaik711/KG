"""
Wiki Engine: LLM-powered persistent knowledge layer (T3).

Implements Karpathy's "LLM Wiki" pattern:
- Raw sources (code + docs) are immutable, human-curated
- Wiki layer is LLM-generated markdown with cross-references
- Knowledge compounds over time through incremental maintenance

Three core operations:
- Ingest: Process symbols/docs, generate/update wiki pages
- Query: Search wiki for relevant pages, synthesize answers
- Lint: Health-check for contradictions, orphaned pages, gaps

The wiki is stored in SQLite alongside the code graph, and can
optionally be exported as Obsidian-compatible markdown files.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class WikiEngine:
    """
    LLM-powered wiki that incrementally builds and maintains
    synthesized knowledge from the codebase.

    The wiki layer sits at T3 — built on top of T2 extracted data.
    Each wiki page covers a module, component, or cross-cutting concern.
    Pages include cross-references to code symbols and other pages.
    """

    def __init__(self, conn: Any, root: Path | None = None) -> None:
        """
        Args:
            conn: SQLite connection (from SQLiteSymbolGraph)
            root: Repository root path (for export)
        """
        self._conn = conn
        self._root = root
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create wiki tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS wiki_pages (
                page_id         TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                content         TEXT NOT NULL,
                summary         TEXT,
                page_type       TEXT NOT NULL DEFAULT 'module',
                source_files    TEXT,  -- JSON array of file paths that contributed
                source_symbols  TEXT,  -- JSON array of FQNs that contributed
                cross_refs      TEXT,  -- JSON array of other page_ids referenced
                tags            TEXT,  -- JSON array of tags
                version         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                metadata_json   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_wiki_type ON wiki_pages(page_type);
            CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_pages(updated_at);

            -- Full-text search on wiki content
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                page_id, title, content, summary, tags,
                content=wiki_pages,
                content_rowid=rowid
            );

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS wiki_fts_insert AFTER INSERT ON wiki_pages BEGIN
                INSERT INTO wiki_fts(rowid, page_id, title, content, summary, tags)
                VALUES (new.rowid, new.page_id, new.title, new.content, new.summary, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS wiki_fts_update AFTER UPDATE ON wiki_pages BEGIN
                INSERT INTO wiki_fts(wiki_fts, rowid, page_id, title, content, summary, tags)
                VALUES ('delete', old.rowid, old.page_id, old.title, old.content, old.summary, old.tags);
                INSERT INTO wiki_fts(rowid, page_id, title, content, summary, tags)
                VALUES (new.rowid, new.page_id, new.title, new.content, new.summary, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS wiki_fts_delete AFTER DELETE ON wiki_pages BEGIN
                INSERT INTO wiki_fts(wiki_fts, rowid, page_id, title, content, summary, tags)
                VALUES ('delete', old.rowid, old.page_id, old.title, old.content, old.summary, old.tags);
            END;
        """)
        self._conn.commit()

    # -------------------------------------------------------------------
    # Ingest: Generate wiki pages from code/doc data
    # -------------------------------------------------------------------

    def ingest_module(
        self,
        module_path: str,
        symbols: list[dict[str, Any]],
        *,
        summary_fn: Any | None = None,
    ) -> dict[str, Any]:
        """
        Generate or update a wiki page for a module (file or directory).

        Args:
            module_path: File path or directory path
            symbols: List of symbol dicts from the graph
            summary_fn: Optional callable(symbols, module_path) -> str
                        for LLM-powered summarization. If None, uses
                        template-based generation.

        Returns:
            Dict with page_id, title, action (created/updated/unchanged)
        """
        page_id = self._path_to_page_id(module_path)
        title = self._generate_title(module_path)

        if summary_fn:
            content = summary_fn(symbols, module_path)
        else:
            content = self._generate_page_content(module_path, symbols)

        summary = self._extract_summary(content)
        source_symbols = [s["fqn"] for s in symbols]
        cross_refs = self._extract_cross_refs(content)
        tags = self._auto_tag(module_path, symbols)

        existing = self._conn.execute(
            "SELECT version, content FROM wiki_pages WHERE page_id = ?",
            (page_id,),
        ).fetchone()

        if existing:
            if existing["content"] == content:
                return {"page_id": page_id, "title": title, "action": "unchanged"}
            version = existing["version"] + 1
            self._conn.execute(
                """UPDATE wiki_pages SET
                     title=?, content=?, summary=?, source_files=?, source_symbols=?,
                     cross_refs=?, tags=?, version=?, updated_at=datetime('now')
                   WHERE page_id=?""",
                (
                    title, content, summary,
                    json.dumps([module_path]), json.dumps(source_symbols),
                    json.dumps(cross_refs), json.dumps(tags),
                    version, page_id,
                ),
            )
            self._conn.commit()
            return {"page_id": page_id, "title": title, "action": "updated", "version": version}
        else:
            self._conn.execute(
                """INSERT INTO wiki_pages
                     (page_id, title, content, summary, page_type,
                      source_files, source_symbols, cross_refs, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    page_id, title, content, summary, "module",
                    json.dumps([module_path]), json.dumps(source_symbols),
                    json.dumps(cross_refs), json.dumps(tags),
                ),
            )
            self._conn.commit()
            return {"page_id": page_id, "title": title, "action": "created", "version": 1}

    def ingest_overview(
        self,
        stats: dict[str, Any],
        hotspots: list[dict[str, Any]],
        communities: list[dict[str, Any]] | None = None,
        *,
        summary_fn: Any | None = None,
    ) -> dict[str, Any]:
        """
        Generate or update the codebase overview wiki page.
        """
        page_id = "_overview"
        title = "Codebase Overview"

        if summary_fn:
            content = summary_fn(stats, hotspots, communities)
        else:
            content = self._generate_overview_content(stats, hotspots, communities)

        summary = self._extract_summary(content)

        existing = self._conn.execute(
            "SELECT version FROM wiki_pages WHERE page_id = ?", (page_id,)
        ).fetchone()

        version = (existing["version"] + 1) if existing else 1
        action = "updated" if existing else "created"

        self._conn.execute(
            """INSERT OR REPLACE INTO wiki_pages
                 (page_id, title, content, summary, page_type, tags, version, updated_at)
               VALUES (?, ?, ?, ?, 'overview', ?, ?, datetime('now'))""",
            (page_id, title, content, summary, json.dumps(["overview"]), version),
        )
        self._conn.commit()

        return {"page_id": page_id, "title": title, "action": action, "version": version}

    def ingest_directory(
        self,
        dir_path: str,
        files_and_symbols: dict[str, list[dict[str, Any]]],
        *,
        summary_fn: Any | None = None,
    ) -> dict[str, Any]:
        """
        Generate a wiki page summarizing a directory / component.
        """
        page_id = self._path_to_page_id(dir_path)
        title = self._generate_title(dir_path)

        if summary_fn:
            content = summary_fn(dir_path, files_and_symbols)
        else:
            content = self._generate_directory_content(dir_path, files_and_symbols)

        summary = self._extract_summary(content)
        source_files = list(files_and_symbols.keys())
        all_symbols = []
        for syms in files_and_symbols.values():
            all_symbols.extend(s["fqn"] for s in syms)

        cross_refs = self._extract_cross_refs(content)
        tags = self._auto_tag(dir_path, [])

        existing = self._conn.execute(
            "SELECT version, content FROM wiki_pages WHERE page_id = ?", (page_id,)
        ).fetchone()

        if existing and existing["content"] == content:
            return {"page_id": page_id, "title": title, "action": "unchanged"}

        version = (existing["version"] + 1) if existing else 1
        action = "updated" if existing else "created"

        self._conn.execute(
            """INSERT OR REPLACE INTO wiki_pages
                 (page_id, title, content, summary, page_type,
                  source_files, source_symbols, cross_refs, tags, version, updated_at)
               VALUES (?, ?, ?, ?, 'directory', ?, ?, ?, ?, ?, datetime('now'))""",
            (
                page_id, title, content, summary,
                json.dumps(source_files), json.dumps(all_symbols),
                json.dumps(cross_refs), json.dumps(tags), version,
            ),
        )
        self._conn.commit()

        return {"page_id": page_id, "title": title, "action": action, "version": version}

    # -------------------------------------------------------------------
    # Query: Search and retrieve wiki pages
    # -------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Full-text search across wiki pages.

        Uses SQLite FTS5 for efficient text search with ranking.
        """
        rows = self._conn.execute(
            """SELECT p.page_id, p.title, p.summary, p.page_type, p.tags,
                      p.updated_at, p.version,
                      rank
               FROM wiki_fts f
               JOIN wiki_pages p ON f.page_id = p.page_id
               WHERE wiki_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()

        return [
            {
                "page_id": row["page_id"],
                "title": row["title"],
                "summary": row["summary"],
                "page_type": row["page_type"],
                "tags": json.loads(row["tags"]) if row["tags"] else [],
                "updated_at": row["updated_at"],
                "version": row["version"],
                "relevance_rank": row["rank"],
            }
            for row in rows
        ]

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        """Retrieve a full wiki page by ID."""
        row = self._conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id = ?", (page_id,)
        ).fetchone()

        if row is None:
            return None

        d = dict(row)
        for key in ("source_files", "source_symbols", "cross_refs", "tags"):
            if d.get(key):
                d[key] = json.loads(d[key])
            else:
                d[key] = []
        return d

    def get_all_pages(self, page_type: str | None = None) -> list[dict[str, Any]]:
        """List all wiki pages, optionally filtered by type."""
        if page_type:
            rows = self._conn.execute(
                "SELECT page_id, title, summary, page_type, tags, updated_at, version "
                "FROM wiki_pages WHERE page_type = ? ORDER BY updated_at DESC",
                (page_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT page_id, title, summary, page_type, tags, updated_at, version "
                "FROM wiki_pages ORDER BY updated_at DESC"
            ).fetchall()

        return [
            {
                **dict(row),
                "tags": json.loads(row["tags"]) if row["tags"] else [],
            }
            for row in rows
        ]

    def get_stats(self) -> dict[str, Any]:
        """Get wiki statistics."""
        total = self._conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        by_type = self._conn.execute(
            "SELECT page_type, COUNT(*) as cnt FROM wiki_pages GROUP BY page_type"
        ).fetchall()

        return {
            "total_pages": total,
            "by_type": {row["page_type"]: row["cnt"] for row in by_type},
        }

    # -------------------------------------------------------------------
    # Lint: Health checks
    # -------------------------------------------------------------------

    def lint(self) -> dict[str, Any]:
        """
        Run health checks on the wiki.

        Checks for:
        - Orphaned pages (no source files exist anymore)
        - Broken cross-references (referenced pages don't exist)
        - Stale pages (source files changed since last update)
        - Empty pages (no meaningful content)
        """
        issues: list[dict[str, Any]] = []

        # Check for broken cross-references
        pages = self._conn.execute(
            "SELECT page_id, cross_refs FROM wiki_pages WHERE cross_refs IS NOT NULL"
        ).fetchall()

        all_page_ids = {
            row["page_id"]
            for row in self._conn.execute("SELECT page_id FROM wiki_pages").fetchall()
        }

        for page in pages:
            refs = json.loads(page["cross_refs"]) if page["cross_refs"] else []
            for ref in refs:
                if ref not in all_page_ids:
                    issues.append({
                        "type": "broken_cross_ref",
                        "page_id": page["page_id"],
                        "missing_ref": ref,
                        "severity": "warning",
                    })

        # Check for orphaned pages (source files no longer exist)
        pages_with_sources = self._conn.execute(
            "SELECT page_id, source_files FROM wiki_pages WHERE source_files IS NOT NULL"
        ).fetchall()

        existing_files = {
            row["path"]
            for row in self._conn.execute("SELECT path FROM files").fetchall()
        }

        for page in pages_with_sources:
            sources = json.loads(page["source_files"]) if page["source_files"] else []
            if sources and not any(s in existing_files for s in sources):
                issues.append({
                    "type": "orphaned_page",
                    "page_id": page["page_id"],
                    "missing_sources": sources,
                    "severity": "info",
                })

        # Check for empty pages
        empty = self._conn.execute(
            "SELECT page_id FROM wiki_pages WHERE length(content) < 50"
        ).fetchall()
        for page in empty:
            issues.append({
                "type": "empty_page",
                "page_id": page["page_id"],
                "severity": "warning",
            })

        # Coverage: files without wiki pages
        all_files = self._conn.execute(
            "SELECT path FROM files WHERE tier >= 2"
        ).fetchall()

        covered_files: set[str] = set()
        for page in pages_with_sources:
            sources = json.loads(page["source_files"]) if page["source_files"] else []
            covered_files.update(sources)

        uncovered = [
            row["path"] for row in all_files
            if row["path"] not in covered_files
        ]

        return {
            "issues": issues,
            "issue_count": len(issues),
            "coverage": {
                "t2_files": len(all_files),
                "covered_files": len(covered_files),
                "uncovered_files": len(uncovered),
                "uncovered": uncovered[:20],  # cap for readability
            },
            "total_pages": len(all_page_ids),
        }

    # -------------------------------------------------------------------
    # Export: Obsidian-compatible markdown
    # -------------------------------------------------------------------

    def export_markdown(self, output_dir: str | Path) -> dict[str, Any]:
        """
        Export wiki pages as Obsidian-compatible markdown files.

        Creates one .md file per wiki page with YAML frontmatter and
        [[wikilinks]] for cross-references.
        """
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)

        pages = self._conn.execute("SELECT * FROM wiki_pages").fetchall()
        exported = 0

        for page in pages:
            page_dict = dict(page)
            filename = f"{page_dict['page_id']}.md"
            filepath = output / filename

            tags = json.loads(page_dict["tags"]) if page_dict.get("tags") else []
            cross_refs = json.loads(page_dict["cross_refs"]) if page_dict.get("cross_refs") else []

            # YAML frontmatter
            frontmatter = [
                "---",
                f"title: {page_dict['title']}",
                f"type: {page_dict['page_type']}",
                f"version: {page_dict['version']}",
                f"updated: {page_dict['updated_at']}",
            ]
            if tags:
                frontmatter.append(f"tags: [{', '.join(tags)}]")
            frontmatter.append("---")

            # Convert cross-refs to wikilinks in content
            content = page_dict["content"]
            for ref in cross_refs:
                content = content.replace(f"[{ref}]", f"[[{ref}]]")

            md_content = "\n".join(frontmatter) + "\n\n" + content

            filepath.write_text(md_content)
            exported += 1

        return {"exported": exported, "output_dir": str(output)}

    # -------------------------------------------------------------------
    # Delete
    # -------------------------------------------------------------------

    def delete_page(self, page_id: str) -> bool:
        """Delete a wiki page."""
        cursor = self._conn.execute(
            "DELETE FROM wiki_pages WHERE page_id = ?", (page_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_all(self) -> int:
        """Delete all wiki pages."""
        cursor = self._conn.execute("DELETE FROM wiki_pages")
        self._conn.commit()
        return cursor.rowcount

    # -------------------------------------------------------------------
    # Content Generation (Template-based, no LLM required)
    # -------------------------------------------------------------------

    def _generate_page_content(
        self, module_path: str, symbols: list[dict[str, Any]]
    ) -> str:
        """Generate wiki page content from symbols (template-based)."""
        lines = [f"# {self._generate_title(module_path)}", ""]

        # Classify symbols
        classes = [s for s in symbols if s.get("kind") == "class"]
        functions = [s for s in symbols if s.get("kind") == "function"]
        methods = [s for s in symbols if s.get("kind") == "method"]
        others = [s for s in symbols if s.get("kind") not in ("class", "function", "method")]

        if classes:
            lines.append("## Classes")
            lines.append("")
            for cls in classes:
                lines.append(f"### {cls['name']}")
                if cls.get("docstring"):
                    lines.append(f"\n{cls['docstring']}\n")
                lines.append(f"- Defined at `{module_path}:{cls.get('start_line', '?')}`")
                # Find methods belonging to this class
                cls_methods = [
                    m for m in methods
                    if m["fqn"].startswith(cls["fqn"] + ".")
                ]
                if cls_methods:
                    lines.append("- Methods:")
                    for m in cls_methods:
                        sig = m.get("signature", m["name"])
                        lines.append(f"  - `{sig}`")
                lines.append("")

        if functions:
            lines.append("## Functions")
            lines.append("")
            for fn in functions:
                sig = fn.get("signature") or fn["name"]
                lines.append(f"- `{sig}` (line {fn.get('start_line', '?')})")
                if fn.get("docstring"):
                    lines.append(f"  - {fn['docstring'][:200]}")
            lines.append("")

        if others:
            lines.append("## Other Symbols")
            lines.append("")
            for sym in others:
                lines.append(f"- {sym.get('kind', '?')} `{sym['name']}` (line {sym.get('start_line', '?')})")
            lines.append("")

        return "\n".join(lines)

    def _generate_overview_content(
        self,
        stats: dict[str, Any],
        hotspots: list[dict[str, Any]],
        communities: list[dict[str, Any]] | None = None,
    ) -> str:
        """Generate codebase overview wiki page."""
        lines = ["# Codebase Overview", ""]

        lines.append("## Statistics")
        lines.append("")
        lines.append(f"- **Files**: {stats.get('files', 0)}")
        lines.append(f"- **Symbols**: {stats.get('symbols', 0)}")
        lines.append(f"- **Relations**: {stats.get('relations', 0)}")
        lines.append(f"- **DB Size**: {stats.get('db_size_mb', 0)} MB")

        langs = stats.get("languages", {})
        if langs:
            lines.append("")
            lines.append("### Languages")
            for lang, count in sorted(langs.items(), key=lambda x: -x[1]):
                lines.append(f"- {lang}: {count} files")

        if hotspots:
            lines.append("")
            lines.append("## Hotspots (Most Referenced Symbols)")
            lines.append("")
            for h in hotspots[:10]:
                lines.append(
                    f"- `{h.get('name', h.get('fqn', '?'))}` "
                    f"({h.get('ref_count', 0)} refs) — {h.get('file_path', '?')}"
                )

        if communities:
            lines.append("")
            lines.append("## Module Communities")
            lines.append("")
            for comm in communities[:10]:
                lines.append(
                    f"- **{comm['label']}** ({comm['member_count']} symbols, "
                    f"density={comm.get('density', 0):.2f})"
                )
                for f in comm.get("files", [])[:5]:
                    lines.append(f"  - {f}")

        lines.append("")
        return "\n".join(lines)

    def _generate_directory_content(
        self,
        dir_path: str,
        files_and_symbols: dict[str, list[dict[str, Any]]],
    ) -> str:
        """Generate wiki page for a directory."""
        lines = [f"# {self._generate_title(dir_path)}", ""]
        lines.append(f"Directory: `{dir_path}`")
        lines.append(f"Files: {len(files_and_symbols)}")
        lines.append("")

        total_symbols = sum(len(syms) for syms in files_and_symbols.values())
        lines.append(f"Total symbols: {total_symbols}")
        lines.append("")

        lines.append("## Files")
        lines.append("")

        for fp, symbols in sorted(files_and_symbols.items()):
            lines.append(f"### {fp}")
            classes = [s for s in symbols if s.get("kind") == "class"]
            functions = [s for s in symbols if s.get("kind") in ("function", "method")]

            if classes:
                for cls in classes:
                    lines.append(f"- class `{cls['name']}`")
            if functions:
                for fn in functions[:10]:
                    lines.append(f"- {fn.get('kind', 'function')} `{fn['name']}`")
                if len(functions) > 10:
                    lines.append(f"  - ... and {len(functions) - 10} more")
            lines.append("")

        return "\n".join(lines)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _path_to_page_id(path: str) -> str:
        """Convert a file/directory path to a wiki page ID."""
        page_id = path.replace("/", "_").replace("\\", "_").replace(".", "_")
        return page_id.strip("_")

    @staticmethod
    def _generate_title(path: str) -> str:
        """Generate a human-readable title from a path."""
        name = Path(path).stem if "." in Path(path).name else Path(path).name
        # Convert snake_case / camelCase to title
        title = re.sub(r"[_\-]", " ", name)
        title = re.sub(r"([a-z])([A-Z])", r"\1 \2", title)
        return title.title()

    @staticmethod
    def _extract_summary(content: str) -> str:
        """Extract first meaningful paragraph as summary."""
        lines = content.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and len(stripped) > 20:
                return stripped[:300]
        return content[:300]

    @staticmethod
    def _extract_cross_refs(content: str) -> list[str]:
        """Extract wiki-style cross-references from content."""
        # Match [[page_id]] or [page_id] patterns
        refs = re.findall(r"\[\[([^\]]+)\]\]|\[([^\]]+)\]", content)
        return list({ref[0] or ref[1] for ref in refs if ref[0] or ref[1]})

    @staticmethod
    def _auto_tag(path: str, symbols: list[dict[str, Any]]) -> list[str]:
        """Auto-generate tags based on path and symbol kinds."""
        tags: set[str] = set()

        # Path-based tags
        parts = path.lower().split("/")
        for part in parts:
            if part in ("api", "routes", "handlers", "controllers", "endpoints"):
                tags.add("api")
            elif part in ("models", "schema", "entities"):
                tags.add("data-model")
            elif part in ("tests", "test", "__tests__", "spec"):
                tags.add("testing")
            elif part in ("utils", "helpers", "lib", "common"):
                tags.add("utilities")
            elif part in ("config", "settings"):
                tags.add("configuration")
            elif part in ("auth", "security"):
                tags.add("security")
            elif part in ("db", "database", "migrations"):
                tags.add("database")
            elif part in ("docs", "documentation"):
                tags.add("documentation")

        # Symbol-based tags
        kinds = {s.get("kind") for s in symbols}
        if "class" in kinds:
            tags.add("oop")
        if any(s.get("is_async") for s in symbols):
            tags.add("async")

        return sorted(tags)
