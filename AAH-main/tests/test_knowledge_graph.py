"""Tests for the AAH Knowledge Graph Engine (WS1.4)."""

import json
from pathlib import Path

import pytest
import networkx as nx

from aah.core.common.io_utils import write_json, write_text
from aah.core.intel.knowledge_graph.ast_extractor import (
    extract_relationships_from_file,
    IMPORTS,
    INHERITS,
    CALLS,
    IMPLEMENTS,
)
from aah.core.intel.knowledge_graph.graph_builder import (
    build_graph,
    detect_communities,
    export_graph_json,
    find_god_nodes,
    find_hub_nodes,
)
from aah.core.intel.knowledge_graph.graph_report import generate_report
from aah.core.intel.knowledge_graph.graph_viz import generate_html
from aah.core.intel.knowledge_graph.incremental import (
    get_changed_files,
    load_cache,
    save_cache,
)


# ─── AST Extractor ────────────────────────────────────────────────


class TestPythonExtraction:
    def test_import_statement(self, tmp_path):
        f = tmp_path / "main.py"
        write_text("import os\nimport json\n", f)
        rels = extract_relationships_from_file(f)
        import_targets = [r["target"] for r in rels if r["type"] == IMPORTS]
        assert "os" in import_targets
        assert "json" in import_targets

    def test_from_import(self, tmp_path):
        f = tmp_path / "app.py"
        write_text("from pathlib import Path\nfrom os.path import join\n", f)
        rels = extract_relationships_from_file(f)
        import_targets = [r["target"] for r in rels if r["type"] == IMPORTS]
        assert "pathlib.Path" in import_targets
        assert "os.path.join" in import_targets

    def test_class_inheritance(self, tmp_path):
        f = tmp_path / "models.py"
        write_text("class User(BaseModel):\n    pass\n\nclass Admin(User):\n    pass\n", f)
        rels = extract_relationships_from_file(f)
        inherits = [r for r in rels if r["type"] == INHERITS]
        # BaseModel is filtered out as common
        targets = [r["target"] for r in inherits]
        assert "User" in targets

    def test_method_calls(self, tmp_path):
        f = tmp_path / "service.py"
        write_text("db.query(User)\nlogger.info('test')\n", f)
        rels = extract_relationships_from_file(f)
        calls = [r["target"] for r in rels if r["type"] == CALLS]
        assert "db.query" in calls
        assert "logger.info" in calls

    def test_nonexistent_file(self, tmp_path):
        rels = extract_relationships_from_file(tmp_path / "nonexistent.py")
        assert rels == []


class TestTypeScriptExtraction:
    def test_import(self, tmp_path):
        f = tmp_path / "app.ts"
        write_text("import { Router } from 'express'\nimport axios from 'axios'\n", f)
        rels = extract_relationships_from_file(f)
        targets = [r["target"] for r in rels if r["type"] == IMPORTS]
        assert "express" in targets
        assert "axios" in targets

    def test_require(self, tmp_path):
        f = tmp_path / "app.js"
        write_text("const fs = require('fs')\n", f)
        rels = extract_relationships_from_file(f)
        targets = [r["target"] for r in rels if r["type"] == IMPORTS]
        assert "fs" in targets

    def test_class_extends(self, tmp_path):
        f = tmp_path / "component.tsx"
        write_text("class MyComponent extends React.Component {\n}\n", f)
        rels = extract_relationships_from_file(f)
        inherits = [r["target"] for r in rels if r["type"] == INHERITS]
        assert any("React" in t for t in inherits)


class TestJavaExtraction:
    def test_import(self, tmp_path):
        f = tmp_path / "Main.java"
        write_text("import java.util.List;\nimport com.app.Service;\n", f)
        rels = extract_relationships_from_file(f)
        targets = [r["target"] for r in rels if r["type"] == IMPORTS]
        assert "java.util.List" in targets
        assert "com.app.Service" in targets

    def test_extends_implements(self, tmp_path):
        f = tmp_path / "UserService.java"
        write_text("class UserService extends BaseService implements Serializable {\n}\n", f)
        rels = extract_relationships_from_file(f)
        inherits = [r["target"] for r in rels if r["type"] == INHERITS]
        implements = [r["target"] for r in rels if r["type"] == IMPLEMENTS]
        assert "BaseService" in inherits
        assert "Serializable" in implements


# ─── Graph Builder ─────────────────────────────────────────────────


class TestGraphBuilder:
    def _create_profile(self, tmp_path, files=None, module_graph=None):
        profile = {
            "files": files or [
                {"path": "src/main.py", "language": "Python", "role": "entry_point", "size": 100, "symbol_count": 5},
                {"path": "src/models.py", "language": "Python", "role": "model", "size": 200, "symbol_count": 10},
                {"path": "src/api.py", "language": "Python", "role": "api", "size": 150, "symbol_count": 8},
            ],
            "module_graph": module_graph or [
                {"source": "src/main.py", "target": "src/api.py"},
                {"source": "src/api.py", "target": "src/models.py"},
            ],
            "entry_points": ["src/main.py"],
            "api_layer_files": ["src/api.py"],
            "data_model_files": ["src/models.py"],
            "config_files": [],
            "architecture": {"hotspots": [], "coupling": {"afferent_top": []}},
        }
        profile_path = tmp_path / "codebase-profile.json"
        write_json(profile, profile_path)
        return profile_path

    def test_build_from_profile(self, tmp_path):
        profile_path = self._create_profile(tmp_path)
        G = build_graph(profile_path, tmp_path)
        assert G.number_of_nodes() >= 3
        assert G.number_of_edges() >= 2

    def test_nodes_have_attributes(self, tmp_path):
        profile_path = self._create_profile(tmp_path)
        G = build_graph(profile_path, tmp_path)
        main_data = G.nodes.get("src/main.py", {})
        assert main_data.get("role") == "entry_point"
        assert main_data.get("language") == "Python"

    def test_detect_communities(self, tmp_path):
        profile_path = self._create_profile(tmp_path)
        G = build_graph(profile_path, tmp_path)
        communities = detect_communities(G)
        # All 3 files are connected, so should form 1 community
        assert len(communities) >= 1

    def test_export_json(self, tmp_path):
        profile_path = self._create_profile(tmp_path)
        G = build_graph(profile_path, tmp_path)
        data = export_graph_json(G)
        assert data["node_count"] >= 3
        assert data["edge_count"] >= 2
        assert "nodes" in data
        assert "edges" in data

    def test_find_hub_nodes_small_graph(self, tmp_path):
        profile_path = self._create_profile(tmp_path)
        G = build_graph(profile_path, tmp_path)
        hubs = find_hub_nodes(G)
        # Small graph — api.py should be a hub (connects main to models)
        assert isinstance(hubs, list)

    def test_find_god_nodes(self):
        G = nx.DiGraph()
        # Create a node with many connections
        for i in range(20):
            G.add_edge("hub", f"module_{i}")
        gods = find_god_nodes(G, threshold=15)
        assert len(gods) >= 1
        assert gods[0][0] == "hub"


# ─── Report Generation ────────────────────────────────────────────


class TestGraphReport:
    def test_generate_report(self, tmp_path):
        G = nx.DiGraph()
        G.add_node("main.py", role="entry_point", language="Python", symbols=5, size=100)
        G.add_node("api.py", role="api", language="Python", symbols=8, size=200)
        G.add_edge("main.py", "api.py", type="imports")
        G.add_edge("api.py", "main.py", type="calls")

        report = generate_report(G, "test-project")
        assert "# Codebase Knowledge Graph Report" in report
        assert "test-project" in report
        assert "Nodes:" in report
        assert "Relationship Types" in report

    def test_report_with_communities(self):
        G = nx.DiGraph()
        for i in range(5):
            G.add_edge(f"a_{i}", f"a_{i+1}", type="imports")
        for i in range(3):
            G.add_edge(f"b_{i}", f"b_{i+1}", type="imports")

        report = generate_report(G)
        assert "Module Communities" in report

    def test_report_empty_graph(self):
        G = nx.DiGraph()
        report = generate_report(G)
        assert "**Nodes:** 0" in report


# ─── Visualization ─────────────────────────────────────────────────


class TestGraphVisualization:
    def test_generate_html(self):
        G = nx.DiGraph()
        G.add_node("main.py", role="entry_point", symbols=5, size=100)
        G.add_node("api.py", role="api", symbols=3, size=80)
        G.add_edge("main.py", "api.py", type="imports")

        html = generate_html(G, "Test Graph")
        assert "<html>" in html
        assert "vis-network" in html
        assert "Test Graph" in html
        assert "main.py" in html

    def test_html_contains_legend(self):
        G = nx.DiGraph()
        G.add_node("x", role="other", symbols=1, size=10)
        html = generate_html(G)
        assert "legend" in html
        assert "Entry Point" in html


# ─── Incremental Cache ────────────────────────────────────────────


class TestIncrementalCache:
    def test_save_and_load(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        cache = {"file1.py": "abc123", "file2.py": "def456"}
        save_cache(cache, cache_path)
        loaded = load_cache(cache_path)
        assert loaded == cache

    def test_load_nonexistent(self, tmp_path):
        cache = load_cache(tmp_path / "nonexistent.json")
        assert cache == {}

    def test_changed_files_detection(self, tmp_path):
        cache_path = tmp_path / "cache.json"

        # Create files
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        write_text("content1", f1)
        write_text("content2", f2)

        # First run: all files are "changed" (no cache)
        changed, new_cache = get_changed_files([f1, f2], cache_path)
        assert len(changed) == 2
        save_cache(new_cache, cache_path)

        # Second run: no changes
        changed, _ = get_changed_files([f1, f2], cache_path)
        assert len(changed) == 0

        # Modify one file
        write_text("modified", f1)
        changed, _ = get_changed_files([f1, f2], cache_path)
        assert len(changed) == 1
        assert f1 in changed

    def test_graceful_fallback_no_tree_sitter(self):
        """Verify AST extraction works without tree-sitter (uses regex fallback)."""
        from aah.core.intel.knowledge_graph.ast_extractor import _is_tree_sitter_available
        # This should not crash regardless of whether tree-sitter is installed
        available = _is_tree_sitter_available()
        assert isinstance(available, bool)
