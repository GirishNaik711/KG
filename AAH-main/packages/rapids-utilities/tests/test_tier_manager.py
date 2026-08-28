"""Tests for the tier manager."""

from __future__ import annotations

import time


from codemap_scale.core.tier_manager import TierManager


class TestClassification:
    def test_entry_points_classified_tier2(self, populated_graph):
        tm = TierManager(populated_graph)
        result = tm.classify_for_auto_promotion([
            "main.py",
            "app.py",
            "server.py",
            "src/routes/api.py",
            "src/handlers/payment.py",
            "src/controllers/user.py",
        ])
        assert "main.py" in result[2]
        assert "app.py" in result[2]
        assert "server.py" in result[2]

    def test_test_files_stay_tier1(self, populated_graph):
        tm = TierManager(populated_graph)
        result = tm.classify_for_auto_promotion([
            "tests/test_service.py",
            "src/__tests__/test_api.js",
            "spec/models_spec.rb",
        ])
        assert all(fp in result[1] for fp in [
            "tests/test_service.py",
            "src/__tests__/test_api.js",
            "spec/models_spec.rb",
        ])
        assert len(result.get(2, [])) == 0

    def test_regular_files_tier1(self, populated_graph):
        tm = TierManager(populated_graph)
        result = tm.classify_for_auto_promotion([
            "src/service.py",
            "src/utils.py",
        ])
        assert "src/service.py" in result[1]
        assert "src/utils.py" in result[1]

    def test_mixed_classification(self, populated_graph):
        tm = TierManager(populated_graph)
        result = tm.classify_for_auto_promotion([
            "main.py",        # entry → T2
            "src/service.py", # regular → T1
            "tests/test_a.py", # test → T1
        ])
        assert "main.py" in result[2]
        assert "src/service.py" in result[1]
        assert "tests/test_a.py" in result[1]


class TestQueryTracking:
    def test_record_query_increments(self, populated_graph):
        tm = TierManager(populated_graph)
        tm.record_query("src/service.py")
        tm.record_query("src/service.py")
        tm.record_query("src/service.py")
        assert tm._query_counts["src/service.py"] == 3

    def test_promotion_candidates_at_threshold(self, populated_graph):
        tm = TierManager(populated_graph)
        tm.record_query("src/service.py")
        tm.record_query("src/service.py")
        # Below threshold
        candidates = tm.get_promotion_candidates(threshold=3)
        assert "src/service.py" not in candidates

        # At threshold
        tm.record_query("src/service.py")
        candidates = tm.get_promotion_candidates(threshold=3)
        assert "src/service.py" in candidates

    def test_promotion_candidates_multiple_files(self, populated_graph):
        tm = TierManager(populated_graph)
        for _ in range(5):
            tm.record_query("src/service.py")
        for _ in range(3):
            tm.record_query("src/validator.py")
        tm.record_query("src/utils.py")  # only once

        candidates = tm.get_promotion_candidates(threshold=3)
        assert "src/service.py" in candidates
        assert "src/validator.py" in candidates
        assert "src/utils.py" not in candidates


class TestDemotion:
    def test_demotion_candidates_inactive(self, populated_graph):
        tm = TierManager(populated_graph)
        # Record a query far in the past
        tm._last_queried["src/old_file.py"] = time.time() - (31 * 86400)
        tm._last_queried["src/recent_file.py"] = time.time()

        candidates = tm.get_demotion_candidates(days_inactive=30)
        assert "src/old_file.py" in candidates
        assert "src/recent_file.py" not in candidates

    def test_no_demotion_if_recent(self, populated_graph):
        tm = TierManager(populated_graph)
        tm.record_query("src/service.py")
        candidates = tm.get_demotion_candidates(days_inactive=30)
        assert "src/service.py" not in candidates


class TestPromotion:
    def test_promote_files_checks_current_tier(self, populated_graph):
        tm = TierManager(populated_graph)
        # main.py is at tier 2, src/service.py is at tier 1
        needs = tm.promote_files(["main.py", "src/service.py"], target_tier=2)
        assert "src/service.py" in needs
        assert "main.py" not in needs

    def test_promote_files_unknown_file(self, populated_graph):
        tm = TierManager(populated_graph)
        needs = tm.promote_files(["nonexistent.py"], target_tier=2)
        assert "nonexistent.py" in needs  # not in DB → needs promotion

    def test_promote_directory(self, populated_graph):
        tm = TierManager(populated_graph)
        # src/ files are at tier 1
        needs = tm.promote_directory("src/", 2)
        assert "src/service.py" in needs
        assert "src/validator.py" in needs
        assert "src/utils.py" in needs
        # main.py is at tier 2, not under src/
        assert "main.py" not in needs

    def test_promote_directory_already_at_tier(self, populated_graph):
        # Manually set all src files to tier 2
        conn = populated_graph._get_conn()
        conn.execute("UPDATE files SET tier = 2 WHERE path LIKE 'src/%'")
        conn.commit()

        tm = TierManager(populated_graph)
        needs = tm.promote_directory("src/", 2)
        assert len(needs) == 0


class TestTierSummary:
    def test_returns_distribution(self, populated_graph):
        tm = TierManager(populated_graph)
        summary = tm.get_tier_summary()
        assert isinstance(summary, dict)
