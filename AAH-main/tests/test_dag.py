"""Tests for aah.core.common.dag."""

import pytest

from aah.core.common.dag import (
    _split_by_file_scope,
    build_dag_from_features,
    compute_execution_waves,
    dag_from_json,
    dag_to_json,
    get_dependencies,
    get_dependents,
    get_execution_frontier,
    get_feature_depth,
    load_features_from_yamls,
    validate_dag,
)
from aah.core.common.io_utils import write_yaml


class TestBuildDag:
    def test_simple_chain(self):
        features = [
            {"id": "A", "dependencies": []},
            {"id": "B", "dependencies": ["A"]},
            {"id": "C", "dependencies": ["B"]},
        ]
        G = build_dag_from_features(features)
        assert G.number_of_nodes() == 3
        assert G.number_of_edges() == 2

    def test_diamond(self, sample_features):
        G = build_dag_from_features(sample_features)
        assert G.number_of_nodes() == 5
        # F001->F003, F001->F004, F002->F004, F003->F005, F004->F005
        assert G.number_of_edges() == 5

    def test_unknown_dependency_raises(self):
        features = [
            {"id": "A", "dependencies": ["NONEXISTENT"]},
        ]
        with pytest.raises(ValueError, match="unknown feature"):
            build_dag_from_features(features)

    def test_isolated_nodes(self):
        features = [
            {"id": "A", "dependencies": []},
            {"id": "B", "dependencies": []},
        ]
        G = build_dag_from_features(features)
        assert G.number_of_nodes() == 2
        assert G.number_of_edges() == 0


class TestValidateDag:
    def test_valid_dag(self, sample_features):
        G = build_dag_from_features(sample_features)
        errors = validate_dag(G)
        assert errors == []

    def test_cyclic_graph(self):
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("B", "C"), ("C", "A")])
        errors = validate_dag(G)
        assert len(errors) > 0
        assert any("cycle" in e.lower() for e in errors)


class TestComputeWaves:
    def test_simple_chain(self):
        features = [
            {"id": "A", "dependencies": []},
            {"id": "B", "dependencies": ["A"]},
            {"id": "C", "dependencies": ["B"]},
        ]
        G = build_dag_from_features(features)
        waves = compute_execution_waves(G)
        assert len(waves) == 3
        assert waves[0] == ["A"]
        assert waves[1] == ["B"]
        assert waves[2] == ["C"]

    def test_parallel_roots(self):
        features = [
            {"id": "A", "dependencies": []},
            {"id": "B", "dependencies": []},
            {"id": "C", "dependencies": ["A", "B"]},
        ]
        G = build_dag_from_features(features)
        waves = compute_execution_waves(G)
        assert len(waves) == 2
        assert set(waves[0]) == {"A", "B"}
        assert waves[1] == ["C"]

    def test_diamond_pattern(self, sample_features):
        G = build_dag_from_features(sample_features)
        waves = compute_execution_waves(G)
        # Wave 0: F001, F002 (no deps)
        # Wave 1: F003, F004 (depend on wave 0)
        # Wave 2: F005 (depends on wave 1)
        assert len(waves) == 3
        assert set(waves[0]) == {"F001", "F002"}
        assert set(waves[1]) == {"F003", "F004"}
        assert waves[2] == ["F005"]

    def test_cyclic_raises(self):
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("B", "A")])
        with pytest.raises(ValueError, match="cycles"):
            compute_execution_waves(G)


class TestExecutionFrontier:
    def test_initial_frontier(self, sample_features):
        G = build_dag_from_features(sample_features)
        frontier = get_execution_frontier(G, set())
        assert set(frontier) == {"F001", "F002"}

    def test_after_wave_0(self, sample_features):
        G = build_dag_from_features(sample_features)
        frontier = get_execution_frontier(G, {"F001", "F002"})
        assert set(frontier) == {"F003", "F004"}

    def test_partial_completion(self, sample_features):
        G = build_dag_from_features(sample_features)
        # Only F001 done — F003 is available (depends only on F001)
        # F004 is NOT available (depends on F001 AND F002)
        frontier = get_execution_frontier(G, {"F001"})
        assert "F003" in frontier
        assert "F004" not in frontier

    def test_all_complete(self, sample_features):
        G = build_dag_from_features(sample_features)
        frontier = get_execution_frontier(G, {"F001", "F002", "F003", "F004", "F005"})
        assert frontier == []


class TestFeatureDepth:
    def test_root_depth(self, sample_features):
        G = build_dag_from_features(sample_features)
        assert get_feature_depth(G, "F001") == 0
        assert get_feature_depth(G, "F002") == 0

    def test_mid_depth(self, sample_features):
        G = build_dag_from_features(sample_features)
        assert get_feature_depth(G, "F003") == 1
        assert get_feature_depth(G, "F004") == 1

    def test_leaf_depth(self, sample_features):
        G = build_dag_from_features(sample_features)
        assert get_feature_depth(G, "F005") == 2

    def test_unknown_feature(self, sample_features):
        G = build_dag_from_features(sample_features)
        with pytest.raises(ValueError):
            get_feature_depth(G, "F999")


class TestDependencies:
    def test_get_dependents(self, sample_features):
        G = build_dag_from_features(sample_features)
        deps = get_dependents(G, "F001")
        assert set(deps) == {"F003", "F004", "F005"}

    def test_get_dependencies(self, sample_features):
        G = build_dag_from_features(sample_features)
        deps = get_dependencies(G, "F005")
        assert set(deps) == {"F001", "F002", "F003", "F004"}

    def test_leaf_has_no_dependents(self, sample_features):
        G = build_dag_from_features(sample_features)
        assert get_dependents(G, "F005") == []

    def test_root_has_no_dependencies(self, sample_features):
        G = build_dag_from_features(sample_features)
        assert get_dependencies(G, "F001") == []


class TestSerialization:
    def test_round_trip(self, sample_features):
        G = build_dag_from_features(sample_features)
        data = dag_to_json(G)
        G2 = dag_from_json(data)
        assert set(G.nodes) == set(G2.nodes)
        assert set(G.edges) == set(G2.edges)

    def test_json_structure(self, sample_features):
        G = build_dag_from_features(sample_features)
        data = dag_to_json(G)
        assert "nodes" in data
        assert "edges" in data
        assert "stats" in data
        assert data["stats"]["total_nodes"] == 5
        assert data["stats"]["is_dag"] is True


class TestLoadFeaturesFromYamls:
    def test_loads_yamls(self, tmp_path, sample_features):
        features_dir = tmp_path / "features"
        features_dir.mkdir()
        for f in sample_features:
            write_yaml(f, features_dir / f"{f['id']}.yaml")
        loaded = load_features_from_yamls(features_dir)
        assert len(loaded) == 5
        ids = {f["id"] for f in loaded}
        assert ids == {"F001", "F002", "F003", "F004", "F005"}

    def test_empty_dir(self, tmp_path):
        features_dir = tmp_path / "empty"
        features_dir.mkdir()
        assert load_features_from_yamls(features_dir) == []

    def test_nonexistent_dir(self, tmp_path):
        assert load_features_from_yamls(tmp_path / "nope") == []


class TestFileScopeSplitting:
    def test_split_no_file_scope(self):
        """Features without file_scope stay as a single wave (backward compat)."""
        features = [
            {"id": "A", "dependencies": []},
            {"id": "B", "dependencies": []},
            {"id": "C", "dependencies": []},
        ]
        G = build_dag_from_features(features)
        result = _split_by_file_scope(G, ["A", "B", "C"])
        assert result == [["A", "B", "C"]]

    def test_split_no_overlap(self):
        """Features with disjoint file_scope stay in one wave."""
        features = [
            {"id": "A", "dependencies": [], "file_scope": ["src/a.py"]},
            {"id": "B", "dependencies": [], "file_scope": ["src/b.py"]},
            {"id": "C", "dependencies": [], "file_scope": ["src/c.py"]},
        ]
        G = build_dag_from_features(features)
        result = _split_by_file_scope(G, ["A", "B", "C"])
        assert result == [["A", "B", "C"]]

    def test_split_pairwise_overlap(self):
        """Two features sharing a file land in different sub-waves."""
        features = [
            {"id": "A", "dependencies": [], "file_scope": ["src/routes.py"]},
            {"id": "B", "dependencies": [], "file_scope": ["src/routes.py", "src/models.py"]},
            {"id": "C", "dependencies": [], "file_scope": ["src/utils.py"]},
        ]
        G = build_dag_from_features(features)
        result = _split_by_file_scope(G, ["A", "B", "C"])
        # A and B overlap on routes.py → different sub-waves
        # C has no overlap → groups with A
        assert len(result) == 2
        assert "A" in result[0] and "C" in result[0]
        assert result[1] == ["B"]

    def test_split_chain_overlap(self):
        """A touches X, B touches X+Y, C touches Y → A+C together, B alone."""
        features = [
            {"id": "A", "dependencies": [], "file_scope": ["x.py"]},
            {"id": "B", "dependencies": [], "file_scope": ["x.py", "y.py"]},
            {"id": "C", "dependencies": [], "file_scope": ["y.py"]},
        ]
        G = build_dag_from_features(features)
        result = _split_by_file_scope(G, ["A", "B", "C"])
        # A placed first → bucket 1 with {x.py}
        # B overlaps with A on x.py → new bucket 2 with {x.py, y.py}
        # C overlaps with B on y.py, but NOT with A → bucket 1 with {x.py, y.py}
        assert len(result) == 2
        assert "A" in result[0] and "C" in result[0]
        assert result[1] == ["B"]

    def test_split_mixed_scoped_and_unscoped(self):
        """Unscoped features group freely (no conflicts possible)."""
        features = [
            {"id": "A", "dependencies": [], "file_scope": ["src/routes.py"]},
            {"id": "B", "dependencies": []},  # no file_scope
            {"id": "C", "dependencies": [], "file_scope": ["src/routes.py"]},
        ]
        G = build_dag_from_features(features)
        result = _split_by_file_scope(G, ["A", "B", "C"])
        # A in bucket 1, B has no scope → goes in bucket 1 (no overlap with anything)
        # C overlaps with A → new bucket 2
        assert len(result) == 2
        assert "A" in result[0] and "B" in result[0]
        assert result[1] == ["C"]

    def test_compute_waves_with_file_scope_end_to_end(self):
        """End-to-end: DAG with file_scope splits generation into sub-waves."""
        features = [
            {"id": "F001", "dependencies": [], "file_scope": ["src/main.py"]},
            {"id": "F002", "dependencies": [], "file_scope": ["src/main.py"]},
            {"id": "F003", "dependencies": [], "file_scope": ["src/other.py"]},
            {"id": "F004", "dependencies": ["F001"]},
        ]
        G = build_dag_from_features(features)
        waves = compute_execution_waves(G)
        # Generation 0: F001, F002, F003 — F001 and F002 overlap on main.py
        # → sub-wave 1: [F001, F003], sub-wave 2: [F002]
        # Generation 1: F004 — no split
        assert len(waves) == 3
        assert "F001" in waves[0] and "F003" in waves[0]
        assert waves[1] == ["F002"]
        assert waves[2] == ["F004"]
