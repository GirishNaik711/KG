"""Tests for the ScaledCodeMapTools agent interface."""

from __future__ import annotations

from unittest.mock import MagicMock


from codemap_scale.core.tier_manager import TierManager
from codemap_scale.orchestrator import ScaledCodeMapTools


def _make_tools(graph):
    """Create tools with a mock orchestrator (focus_file is a no-op)."""
    tm = TierManager(graph)
    mock_orch = MagicMock()
    mock_orch.focus_file = MagicMock(return_value={"file": "mock", "symbols": 0, "relations": 0, "tier": 2})
    return ScaledCodeMapTools(graph, tm, mock_orch)


class TestSearchStructural:
    def test_finds_by_name(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.search_structural("*Service*")
        assert result["total"] >= 1
        names = [m["name"] for m in result["matches"]]
        assert "PaymentService" in names

    def test_filter_by_kind(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.search_structural("*validate*", kind="function")
        assert result["total"] >= 1
        for match in result["matches"]:
            assert match["kind"] == "function"

    def test_no_results(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.search_structural("*zzz_nonexistent_zzz*")
        assert result["total"] == 0
        assert result["matches"] == []


class TestGetSymbol:
    def test_get_existing_symbol(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.get_symbol("src.service.PaymentService")
        assert "error" not in result
        assert result["name"] == "PaymentService"
        assert result["kind"] == "class"

    def test_get_nonexistent_symbol(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.get_symbol("nonexistent.module.Symbol")
        assert "error" in result


class TestCallChain:
    def test_downstream_calls(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.get_call_chain("src.service.PaymentService.process")
        chain = result["call_chain"]
        assert chain["fqn"] == "src.service.PaymentService.process"

        # process() calls validate_card, format_amount, log_transaction
        if "calls" in chain:
            called_fqns = {c["fqn"] for c in chain["calls"]}
            assert "src.validator.validate_card" in called_fqns
            assert "src.utils.format_amount" in called_fqns

    def test_callers(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.get_callers("src.service.PaymentService.process")
        impact = result["impact"]
        # main.main calls process
        if "called_by" in impact:
            caller_fqns = {c["fqn"] for c in impact["called_by"]}
            assert "main.main" in caller_fqns


class TestGetFileMap:
    def test_returns_file_info(self, populated_graph):
        symbols = populated_graph.get_file_symbols("src/service.py")
        assert len(symbols) > 0
        names = {s["name"] for s in symbols}
        assert "PaymentService" in names

    def test_dependents(self, populated_graph):
        deps = populated_graph.get_module_dependents("src/service.py")
        assert "main.py" in deps

    def test_dependencies(self, populated_graph):
        deps = populated_graph.get_module_dependencies("main.py")
        assert "src/service.py" in deps


class TestAnalyzeImpact:
    def test_impact_analysis(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.analyze_impact("src/service.py")
        assert result["file"] == "src/service.py"
        assert isinstance(result["direct_dependents"], list)
        assert isinstance(result["transitive_dependents"], list)
        assert "main.py" in result["direct_dependents"]


class TestGetOverview:
    def test_returns_stats(self, populated_graph):
        tools = _make_tools(populated_graph)
        result = tools.get_overview()
        assert "files" in result
        assert "symbols" in result
        assert "relations" in result
        assert result["files"] == 5
        assert result["symbols"] == 9
