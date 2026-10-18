from pathlib import Path
import unittest


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")
START = '<div class="mn" v-if="view===\'highFrequency\' && can(\'knowledge:view\')">'
END = '<div class="mn" v-if="view===\'changeLogs\'">'
start = FRONTEND.index(START)
end = FRONTEND.index(END, start)
DASHBOARD = FRONTEND[start:end]


class HighFrequencyFrontendTests(unittest.TestCase):
    def test_high_frequency_dashboard_uses_rank_first_layout(self) -> None:
        expected_columns = (
            "<th>排名</th>",
            "<th>品类</th>",
            "<th>AI 高频问题（自动归并）</th>",
            "<th>出现次数</th>",
            "<th>统计周期</th>",
            "<th>来源</th>",
        )
        positions = [DASHBOARD.index(column) for column in expected_columns]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("筛选后工单数", DASHBOARD)
        self.assertIn("出现 2 次及以上的问题", DASHBOARD)
        self.assertIn("最高出现次数", DASHBOARD)
        self.assertIn("highFrequencyMaxCount()", DASHBOARD)

    def test_high_frequency_source_details_show_ai_evidence(self) -> None:
        self.assertIn("查看来源", DASHBOARD)
        self.assertIn("AI 分析问题（计入本行频次）", DASHBOARD)
        self.assertIn("原始AI核心问题", DASHBOARD)
        self.assertIn("AI 判定结论", DASHBOARD)
        self.assertNotIn("判定依据", DASHBOARD)
        self.assertNotIn("原始会话", DASHBOARD)

    def test_high_frequency_does_not_require_human_annotation(self) -> None:
        self.assertIn("人工标题", DASHBOARD)
        self.assertIn("无需人工标注", DASHBOARD)
        self.assertNotIn("待人工拆分", DASHBOARD)
        self.assertNotIn("人工核对", DASHBOARD)
        self.assertNotIn("请在下方核对", DASHBOARD)

    def test_high_frequency_filters_include_date_category_and_period(self) -> None:
        self.assertIn("highFrequency.filters.from_date", DASHBOARD)
        self.assertIn("highFrequency.filters.to_date", DASHBOARD)
        self.assertIn("highFrequency.filters.category", DASHBOARD)
        self.assertIn('value="week"', DASHBOARD)
        self.assertIn('value="month"', DASHBOARD)

    def test_high_frequency_deep_link_sets_date_and_reads_only_dashboard(self) -> None:
        self.assertIn("openHighFrequencyFromUrl", FRONTEND)
        self.assertIn("params.get('view') !== 'highFrequency'", FRONTEND)
        self.assertIn("params.get('from_date') || ''", FRONTEND)
        self.assertIn("params.get('to_date') || ''", FRONTEND)
        self.assertIn("this.loadHighFrequency()", FRONTEND)
        self.assertIn("self.openHighFrequencyFromUrl()", FRONTEND)
        self.assertNotIn("self.syncHighFrequency()", FRONTEND)

    def test_high_frequency_sync_is_explicit_and_query_stays_read_only(self) -> None:
        self.assertIn("同步数据", DASHBOARD)
        self.assertIn("检查数据源", DASHBOARD)
        self.assertIn("导出数据", DASHBOARD)
        self.assertIn("syncHighFrequency", DASHBOARD)
        self.assertIn("highFrequencyExportDisabled", FRONTEND)
        self.assertIn("fetch(API+'/high-frequency/export?'+self.highFrequencyQuery()", FRONTEND)
        self.assertIn("headers:self.authHeaders()", FRONTEND)
        self.assertIn("API+'/high-frequency/sync?'+query", FRONTEND)
        self.assertIn("dry_run:'true'", FRONTEND)
        self.assertIn("method:'POST'", FRONTEND)
        self.assertNotIn("retry=true", FRONTEND)

    def test_full_shadow_comparison_is_displayed_with_auditable_sources(self) -> None:
        self.assertIn("161 条全量影子对比", DASHBOARD)
        self.assertIn("/high-frequency/shadow-comparison", FRONTEND)
        self.assertIn("comparison_rows", DASHBOARD)
        self.assertIn("成员完全一致", DASHBOARD)
        self.assertIn("AI 分析问题", DASHBOARD)
        self.assertIn("原始AI核心问题", DASHBOARD)
        self.assertIn("AI 判定结论", DASHBOARD)
        self.assertIn("不写入业务数据", DASHBOARD)
        self.assertIn("查看系统自动排除来源", DASHBOARD)

    def test_shadow_comparison_is_not_loaded_or_shown_by_default(self) -> None:
        self.assertIn("showShadowComparison:false", FRONTEND)
        self.assertIn("toggleHighFrequencyShadowComparison", FRONTEND)
        self.assertNotIn(
            "this.loadHighFrequency(); this.loadHighFrequencyShadowComparison();",
            FRONTEND,
        )
        self.assertIn(
            "can('knowledge:submit') && highFrequency.showShadowComparison",
            FRONTEND,
        )


if __name__ == "__main__":
    unittest.main()
