"""CLI for codemap-scale: code intelligence for large codebases."""

from __future__ import annotations

import json
import logging

import click

from codemap_scale.orchestrator import CodeMapScale


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


def _get_cm(path: str, db_path: str | None, workers: int | None) -> CodeMapScale:
    return CodeMapScale(path, db_path=db_path, workers=workers)


def _output(data: dict, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        _print_table(data)


def _print_table(data: dict, indent: int = 0) -> None:
    prefix = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            click.echo(f"{prefix}{key}:")
            _print_table(value, indent + 1)
        elif isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict):
            click.echo(f"{prefix}{key}: ({len(value)} items)")
            for i, item in enumerate(value[:20]):
                click.echo(f"{prefix}  [{i}] {_summarize(item)}")
            if len(value) > 20:
                click.echo(f"{prefix}  ... and {len(value) - 20} more")
        else:
            click.echo(f"{prefix}{key}: {value}")


def _summarize(item: dict) -> str:
    if "fqn" in item:
        return f"{item.get('kind', '?')} {item['fqn']} ({item.get('file_path', item.get('file', '?'))}:{item.get('start_line', '?')})"
    return str(item)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """codemap-scale: Code intelligence for large codebases."""
    _setup_logging(verbose)


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Re-scout even if DB exists")
@click.option("--skip-tier1", is_flag=True, help="Only do T0 inventory")
@click.option("--workers", "-w", type=int, default=None, help="Number of worker processes")
@click.option("--db-path", type=click.Path(), default=None, help="Override DB path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def scout(path: str, force: bool, skip_tier1: bool, workers: int | None, db_path: str | None, as_json: bool) -> None:
    """Index a codebase (T0 inventory + T1 skeleton parse)."""
    cm = _get_cm(path, db_path, workers)
    try:
        stats = cm.scout(force=force, skip_tier1=skip_tier1)
        _output(stats, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.argument("pattern")
@click.option("--kind", "-k", type=click.Choice(["function", "class", "method", "interface", "enum"]), default=None)
@click.option("--limit", "-l", type=int, default=50)
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def query(path: str, pattern: str, kind: str | None, limit: int, db_path: str | None, as_json: bool) -> None:
    """Search for symbols by name pattern (supports * wildcards)."""
    cm = _get_cm(path, db_path, None)
    try:
        results = cm.tools.search_structural(pattern, kind=kind, limit=limit)
        _output(results, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.argument("fqn")
@click.option("--depth", "-d", type=int, default=3)
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def chain(path: str, fqn: str, depth: int, db_path: str | None, as_json: bool) -> None:
    """Show downstream call chain for a symbol (by FQN)."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.get_call_chain(fqn, depth=depth)
        _output(result, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.argument("file_path")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def impact(path: str, file_path: str, db_path: str | None, as_json: bool) -> None:
    """Analyze the blast radius of changing a file."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.analyze_impact(file_path)
        _output(result, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def stats(path: str, db_path: str | None, as_json: bool) -> None:
    """Show project statistics and overview."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.get_overview()
        _output(result, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.argument("directory")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def focus(path: str, directory: str, db_path: str | None, as_json: bool) -> None:
    """Deep-analyze a directory (promote to Tier 2)."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.focus(directory)
        _output(result, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def update(path: str, db_path: str | None, as_json: bool) -> None:
    """Incremental update: re-index changed files only."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.update()
        _output(result, as_json)
    finally:
        cm.close()


# -------------------------------------------------------------------
# Phase 1: Document Ingestion Commands
# -------------------------------------------------------------------

@main.command("ingest-docs")
@click.argument("path", type=click.Path(exists=True))
@click.option("--directory", "-d", default=None, help="Subdirectory to scan (default: entire repo)")
@click.option("--no-refs", is_flag=True, help="Skip doc→code reference detection")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def ingest_docs(path: str, directory: str | None, no_refs: bool, db_path: str | None, as_json: bool) -> None:
    """Ingest unstructured documents (PDFs, Office docs, markdown, images)."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.ingest_documents(directory, detect_references=not no_refs)
        _output(result, as_json)
    finally:
        cm.close()


# -------------------------------------------------------------------
# Phase 2: Community Detection Commands
# -------------------------------------------------------------------

@main.command("communities")
@click.argument("path", type=click.Path(exists=True))
@click.option("--resolution", "-r", type=float, default=1.0, help="Leiden resolution (higher = more communities)")
@click.option("--min-size", type=int, default=2, help="Minimum community size")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def communities(path: str, resolution: float, min_size: int, db_path: str | None, as_json: bool) -> None:
    """Detect module communities in the code graph."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.detect_communities(resolution=resolution, min_size=min_size)
        _output(result, as_json)
    finally:
        cm.close()


@main.command("god-nodes")
@click.argument("path", type=click.Path(exists=True))
@click.option("--top", "-n", type=int, default=20, help="Number of god nodes to show")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def god_nodes(path: str, top: int, db_path: str | None, as_json: bool) -> None:
    """Find god nodes — the most connected symbols in the codebase."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.get_god_nodes(top_n=top)
        _output(result, as_json)
    finally:
        cm.close()


# -------------------------------------------------------------------
# Phase 3: Wiki Commands
# -------------------------------------------------------------------

@main.command("wiki-build")
@click.argument("path", type=click.Path(exists=True))
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def wiki_build(path: str, db_path: str | None, as_json: bool) -> None:
    """Build or update the wiki layer from current graph data (T3)."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.build_wiki()
        _output(result, as_json)
    finally:
        cm.close()


@main.command("wiki-search")
@click.argument("path", type=click.Path(exists=True))
@click.argument("query")
@click.option("--limit", "-l", type=int, default=10)
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def wiki_search(path: str, query: str, limit: int, db_path: str | None, as_json: bool) -> None:
    """Search the wiki for relevant pages."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.search_wiki(query, limit=limit)
        _output(result, as_json)
    finally:
        cm.close()


@main.command("wiki-page")
@click.argument("path", type=click.Path(exists=True))
@click.argument("page_id")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def wiki_page(path: str, page_id: str, db_path: str | None, as_json: bool) -> None:
    """View a specific wiki page."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.get_wiki_page(page_id)
        _output(result, as_json)
    finally:
        cm.close()


@main.command("wiki-lint")
@click.argument("path", type=click.Path(exists=True))
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def wiki_lint(path: str, db_path: str | None, as_json: bool) -> None:
    """Health-check the wiki for contradictions, orphans, and gaps."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.tools.lint_wiki()
        _output(result, as_json)
    finally:
        cm.close()


@main.command("wiki-export")
@click.argument("path", type=click.Path(exists=True))
@click.option("--output-dir", "-o", required=True, help="Directory for exported markdown files")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def wiki_export(path: str, output_dir: str, db_path: str | None, as_json: bool) -> None:
    """Export wiki as Obsidian-compatible markdown files."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.wiki.export_markdown(output_dir)
        _output(result, as_json)
    finally:
        cm.close()


@main.command("list-tools")
@click.option("--json", "as_json", is_flag=True, default=True, help="Output as JSON")
def list_tools(as_json: bool) -> None:
    """List all available codemap tools with their arguments."""
    tools = []
    for name, cmd in sorted(main.commands.items()):
        if name == "list-tools":
            continue
        tool_info: dict = {
            "name": name,
            "description": cmd.help or "",
            "arguments": [],
        }
        for param in cmd.params:
            arg_info: dict = {
                "name": param.name,
                "type": param.type.name if hasattr(param.type, "name") else str(param.type),
                "required": param.required,
                "is_flag": isinstance(param, click.Option) and param.is_flag,
            }
            if hasattr(param, "help") and param.help:
                arg_info["description"] = param.help
            if hasattr(param, "default") and param.default is not None:
                arg_info["default"] = param.default
            tool_info["arguments"].append(arg_info)
        tools.append(tool_info)

    manifest = {
        "version": "0.1.0",
        "tool_count": len(tools),
        "tools": tools,
    }
    click.echo(json.dumps(manifest, indent=2))


# -------------------------------------------------------------------
# Semantic Search
# -------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Re-embed all symbols")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def embed(path: str, force: bool, db_path: str | None, as_json: bool) -> None:
    """Build the semantic vector index for natural language search."""
    cm = _get_cm(path, db_path, None)
    try:
        def progress(done, total):
            if not as_json:
                click.echo(f"\r  Embedding: {done}/{total} symbols", nl=False)

        result = cm.build_semantic_index(force=force, progress_fn=progress)
        if not as_json:
            click.echo("")  # newline after progress
        _output(result, as_json)
    finally:
        cm.close()


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.argument("question")
@click.option("--top", "-k", default=10, help="Number of results")
@click.option("--db-path", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True)
def ask(path: str, question: str, top: int, db_path: str | None, as_json: bool) -> None:
    """Ask a natural language question about the codebase."""
    cm = _get_cm(path, db_path, None)
    try:
        result = cm.ask(question, top_k=top)
        if as_json:
            _output(result, True)
        else:
            click.echo(f"\nResults for: \"{question}\"\n")
            for i, r in enumerate(result["results"], 1):
                score = r.get("score", 0)
                name = r.get("name", r.get("fqn", ""))
                kind = r.get("kind", "")
                file_path = r.get("file_path", "")
                line = r.get("start_line", "")
                loc = f"{file_path}:{line}" if line else file_path
                click.echo(f"  {i}. [{score:.3f}] {kind} {name}")
                click.echo(f"     {loc}")
                click.echo("")
    finally:
        cm.close()


if __name__ == "__main__":
    main()
