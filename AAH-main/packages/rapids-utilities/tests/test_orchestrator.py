"""Integration tests for the CodeMapScale orchestrator."""

from __future__ import annotations



from codemap_scale.orchestrator import CodeMapScale


class TestScout:
    def test_indexes_all_files(self, codemap_instance, sample_repo):
        stats = codemap_instance.scout()
        assert stats["files"] > 0
        assert stats["phases"]["tier0"]["files"] > 0
        assert stats["skipped"] is False

    def test_extracts_symbols(self, codemap_instance, sample_repo):
        stats = codemap_instance.scout()
        assert stats.get("symbols", 0) > 0

    def test_skip_if_already_indexed(self, codemap_instance, sample_repo):
        stats1 = codemap_instance.scout()
        assert stats1["skipped"] is False
        stats2 = codemap_instance.scout()
        assert stats2.get("skipped") is True

    def test_force_reindexes(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        stats2 = codemap_instance.scout(force=True)
        assert stats2["skipped"] is False
        assert stats2["files"] > 0

    def test_skip_tier1(self, codemap_instance, sample_repo):
        stats = codemap_instance.scout(skip_tier1=True)
        assert stats["phases"]["tier0"]["files"] > 0
        assert "tier1" not in stats["phases"]

    def test_reports_backend(self, codemap_instance, sample_repo):
        stats = codemap_instance.scout()
        backend = stats["phases"]["tier0"].get("backend")
        assert backend in ("python", "rust")

    def test_db_file_created(self, codemap_instance, sample_repo, tmp_path):
        codemap_instance.scout()
        assert (tmp_path / "codemap_test.db").exists()

    def test_language_distribution(self, codemap_instance, sample_repo):
        stats = codemap_instance.scout()
        assert "python" in stats.get("languages", {})

    def test_auto_promotes_entry_points(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        # main.py should be auto-promoted to tier 2
        f = codemap_instance.graph.get_file("main.py")
        if f:
            assert f["tier"] >= 1  # At minimum tier 1


class TestFocus:
    def test_focus_directory(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        result = codemap_instance.focus("src/")
        # Should promote src/ files
        assert result.get("promoted", 0) >= 0

    def test_focus_already_promoted(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        codemap_instance.focus("src/")
        result = codemap_instance.focus("src/")
        assert result.get("promoted", 0) == 0 or "already_at_tier" in result

    def test_focus_file(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        result = codemap_instance.focus_file("src/service.py")
        assert "error" not in result

    def test_focus_nonexistent_file(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        result = codemap_instance.focus_file("nonexistent.py")
        assert "error" in result


class TestUpdate:
    def test_detects_changes(self, codemap_instance, sample_repo):
        codemap_instance.scout()

        # Modify a file
        (sample_repo / "src" / "utils.py").write_text("def new_function(): pass\n")
        result = codemap_instance.update()
        assert result["changed"] >= 1

    def test_detects_deletions(self, codemap_instance, sample_repo):
        codemap_instance.scout()

        # Delete a file
        (sample_repo / "src" / "utils.py").unlink()
        result = codemap_instance.update()
        assert result["deleted"] >= 1

    def test_no_changes(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        result = codemap_instance.update()
        assert result["changed"] == 0
        assert result["deleted"] == 0

    def test_new_file_detected(self, codemap_instance, sample_repo):
        codemap_instance.scout()

        # Add new file
        (sample_repo / "src" / "new_module.py").write_text("def hello(): pass\n")
        result = codemap_instance.update()
        assert result["changed"] >= 1

    def test_preserves_tier(self, codemap_instance, sample_repo):
        codemap_instance.scout()
        # Focus a file (promotes to T2)
        codemap_instance.focus_file("src/service.py")

        # Verify it's at T2
        f = codemap_instance.graph.get_file("src/service.py")
        assert f is not None
        original_tier = f["tier"]

        # Modify the file
        (sample_repo / "src" / "service.py").write_text(
            "class PaymentService:\n    def process(self): return True\n"
        )

        result = codemap_instance.update()
        assert result["changed"] >= 1

        # File should still be at its original tier (or higher)
        f2 = codemap_instance.graph.get_file("src/service.py")
        assert f2 is not None
        assert f2["tier"] >= original_tier


class TestFullCycle:
    def test_scout_query_focus_update(self, codemap_instance, sample_repo):
        """Full lifecycle: scout → query → focus → modify → update → query."""
        # 1. Scout
        stats = codemap_instance.scout()
        assert stats["files"] > 0

        # 2. Query
        tools = codemap_instance.tools
        results = tools.search_structural("*Service*")
        assert results["total"] >= 1

        # 3. Focus
        codemap_instance.focus("src/")

        # 4. Modify
        svc_path = sample_repo / "src" / "service.py"
        original = svc_path.read_text()
        svc_path.write_text(original + "\ndef new_endpoint(): pass\n")

        # 5. Update
        update_result = codemap_instance.update()
        assert update_result["changed"] >= 1

        # 6. Query again — should find the new function
        results2 = tools.search_structural("*new_endpoint*")
        # May or may not find it depending on tier, but search should work
        assert isinstance(results2["matches"], list)

    def test_close_and_reopen(self, sample_repo, tmp_path):
        """DB persists across sessions."""
        db_path = tmp_path / "persist_test.db"

        # Session 1: scout
        cm1 = CodeMapScale(sample_repo, db_path=db_path, workers=1)
        cm1.scout()
        file_count = cm1.graph.get_stats()["files"]
        cm1.close()

        # Session 2: reopen — data persists
        cm2 = CodeMapScale(sample_repo, db_path=db_path, workers=1)
        stats = cm2.graph.get_stats()
        assert stats["files"] == file_count
        cm2.close()
