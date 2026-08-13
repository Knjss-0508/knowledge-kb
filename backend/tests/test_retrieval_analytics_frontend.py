import unittest
from pathlib import Path


class RetrievalAnalyticsFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")

    def test_time_filter_defaults_to_today_and_exposes_required_ranges(self):
        self.assertIn(
            "timeFilter:{mode:'today',appliedMode:'today'",
            self.html,
        )
        for value, label in (
            ("today", "当日"),
            ("7d", "近7天"),
            ("30d", "近30天"),
            ("all", "全部"),
            ("custom", "自定义日期"),
        ):
            self.assertIn(f"{{value:'{value}',label:'{label}'}}", self.html)

    def test_time_filter_controls_the_shared_analytics_request(self):
        self.assertIn(
            "params.set('start_at', bounds.start.toISOString())",
            self.html,
        )
        self.assertIn(
            "params.set('end_at', bounds.end.toISOString())",
            self.html,
        )
        self.assertIn(
            "this.analysis.pagination.page = 1;",
            self.html,
        )
        self.assertIn(
            "applyTimeFilter:true",
            self.html,
        )
        self.assertIn(
            "self.analysis.timeFilter.appliedMode = requestedFilter.mode",
            self.html,
        )
        self.assertIn(
            "当前仍显示上一次成功加载的时间范围",
            self.html,
        )
        self.assertIn(
            "self.analysis.pagination.page = self.analysis.pagination.appliedPage",
            self.html,
        )
        self.assertIn(
            "self.analysis.pagination.size = self.analysis.pagination.appliedSize",
            self.html,
        )
        self.assertIn(
            "self.analysis.timeFilter.startDate = "
            "self.analysis.timeFilter.appliedStartDate",
            self.html,
        )
        self.assertIn(
            "self.analysis.timeFilter.endDate = "
            "self.analysis.timeFilter.appliedEndDate",
            self.html,
        )

    def test_loading_blocks_conflicting_refresh_and_pagination_requests(self):
        self.assertIn(
            "if (self.analysis.loading) return Promise.resolve(null);",
            self.html,
        )
        self.assertIn(
            '<button class="btn bd" :disabled="analysis.loading" '
            '@click="loadSearchAnalysis">刷新</button>',
            self.html,
        )
        self.assertIn(
            ':disabled="analysis.loading || '
            "analysis.pagination.page<=1\"",
            self.html,
        )

    def test_quick_ranges_use_button_group_accessibility_semantics(self):
        self.assertIn(
            'class="retrieval-period-tabs" role="group"',
            self.html,
        )
        self.assertIn(
            ':aria-pressed="analysis.timeFilter.mode===option.value"',
            self.html,
        )
        self.assertNotIn(
            'class="retrieval-period-tabs" role="tablist"',
            self.html,
        )

    def test_custom_range_is_inclusive_by_advancing_the_end_date(self):
        self.assertIn(
            "end.setDate(end.getDate() + 1);",
            self.html,
        )
        self.assertIn(
            "alert('开始日期不能晚于结束日期')",
            self.html,
        )

    def test_near_threshold_rate_replaces_threshold_pass_rate(self):
        self.assertIn("<h3>临界候选占比</h3>", self.html)
        self.assertIn(
            "analysis.rates.near_threshold_rate",
            self.html,
        )
        self.assertIn(
            "临界候选 / 明显高于阈值 / 低于阈值",
            self.html,
        )
        self.assertIn(
            "analysis.summary.near_threshold",
            self.html,
        )
        self.assertIn(
            "analysis.summary.clear_threshold",
            self.html,
        )
        self.assertNotIn("阈值通过率", self.html)
        self.assertNotIn("threshold_pass_rate", self.html)
        self.assertNotIn("threshold_passed", self.html)


if __name__ == "__main__":
    unittest.main()
