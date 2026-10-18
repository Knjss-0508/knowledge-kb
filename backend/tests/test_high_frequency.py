import unittest
from unittest.mock import patch

from app.services.high_frequency import (
    build_dashboard,
    expand_laptop_component_verification_issues,
    normalize_product_category,
    parse_ai_result,
    parse_source_record,
    split_atomic_issues,
)
from app.services.high_frequency_classifier import (
    _cache_key,
    _consolidation_batches,
)


def _row(ticket, category, problem, analyzed="2026-10-01"):
    return {
        "工单ID": ticket,
        "分析时间": analyzed,
        "产品类型": category,
        "ai_result": {"核心问题": problem, "参考话术": "建议按标准核验"},
        "聊天内容": "用户原始问题",
    }


class HighFrequencyDashboardTests(unittest.TestCase):
    def test_product_category_aliases_use_one_dashboard_boundary(self):
        self.assertEqual(normalize_product_category("耳机/耳麦"), "耳机")
        self.assertEqual(normalize_product_category("耳麦"), "耳机")
        self.assertEqual(normalize_product_category("平板"), "平板电脑")
        self.assertEqual(normalize_product_category("笔记本电脑"), "笔记本")
        self.assertEqual(
            parse_source_record(
                {
                    "产品类型": "耳机/耳麦",
                    "ai_result": {"核心问题": "定位功能正常如何判定"},
                }
            )["category"],
            "耳机",
        )

    def test_laptop_memory_disk_verification_is_split_into_two_components(self):
        self.assertEqual(
            expand_laptop_component_verification_issues(
                "笔记本",
                "内存和硬盘是否为品牌、原装或第三方件",
            ),
            [
                "内存是否品牌、原装或第三方",
                "硬盘是否品牌、原装或第三方",
            ],
        )

    def test_same_issue_in_different_categories_is_never_merged(self):
        payload = build_dashboard(
            [
                _row("1", "手机", "屏幕异常怎么判断"),
                _row("2", "手机", "屏幕异常怎么判断"),
                _row("3", "笔记本", "屏幕异常怎么判断"),
                _row("4", "笔记本", "屏幕异常怎么判断"),
            ]
        )

        self.assertEqual(
            [(row["品类"], row["出现次数"]) for row in payload["rows"]],
            [("手机", 2), ("笔记本", 2)],
        )
        self.assertEqual(
            payload["meta"]["grouping_mode"],
            "same_category_standard_question",
        )

    def test_explicit_numbered_issues_are_split_before_frequency_counting(self):
        first = "回收师咨询两个问题：1. 硬盘是否为品牌件；2. 转轴异响是否异常。"
        second = "1. 硬盘是否为品牌件；2. 屏幕亮线是否异常。"
        payload = build_dashboard(
            [
                _row("1", "电脑", first),
                _row("2", "电脑", second),
            ],
            high_frequency_only=False,
        )

        hard_disk_row = next(
            row
            for row in payload["rows"]
            if row["原子问题"] == "硬盘是否品牌、原装或第三方"
        )
        self.assertEqual(hard_disk_row["出现次数"], 2)
        self.assertEqual(hard_disk_row["工单数"], 2)
        self.assertEqual(payload["summary"]["atomic_issue_count"], 4)
        self.assertEqual(hard_disk_row["案例"][0]["拆分状态"], "已拆分")

    def test_ambiguous_multiple_issue_text_stays_visible_for_manual_split(self):
        issues, status = split_atomic_issues("回收师咨询两个问题，但未提供可可靠拆分的边界。")

        self.assertEqual(status, "待拆分")
        self.assertEqual(len(issues), 1)

    def test_unlabelled_second_request_is_sent_for_model_split_review(self):
        issues, status = split_atomic_issues(
            "回收师询问定位功能如何判定，同时追问解绑流程应如何操作。"
        )

        self.assertEqual(status, "待拆分")
        self.assertEqual(issues, ["回收师询问定位功能如何判定，同时追问解绑流程应如何操作。"])

    def test_newline_numbered_issues_keep_their_split_boundary(self):
        issues, status = split_atomic_issues(
            "1. 后置摄像头画面有线条异常。\n2. 原彩显示开关效果异常。"
        )

        self.assertEqual(status, "已拆分")
        self.assertEqual(issues, ["后置摄像头画面有线条异常", "原彩显示开关效果异常"])

    def test_ai_result_parser_preserves_newline_split_boundaries(self):
        source = parse_source_record(
            {
                "ai_result": (
                    "【核心问题】\n"
                    "1. 电池状态如何核验\n"
                    "2. 屏幕偏光异常如何判定\n"
                    "【产品类型】手机\n"
                    "【判定结论】需要分别核验。"
                )
            }
        )

        issues, status = split_atomic_issues(source["core_problem"])
        self.assertEqual(status, "已拆分")
        self.assertEqual(
            issues,
            ["电池状态如何核验", "屏幕偏光异常如何判定"],
        )

    def test_inline_ai_result_section_markers_do_not_leak_into_core_problem(self):
        raw_ai_result = (
            "【核心问题】1. 电池工具读出异常如何判定；"
            "2. 双卡无法识别如何判定。 "
            "【产品类型】手机 "
            "【一级分类】基本情况 "
            "【二级分类】工具读出维修 "
            "【判定结论】应分别按对应质检项判定。 "
            "【参考话术】请按工具检测结果选择。"
        )

        parsed = parse_ai_result(raw_ai_result)
        source = parse_source_record({"ai_result": raw_ai_result})
        issues, status = split_atomic_issues(source["core_problem"])

        self.assertEqual(parsed["产品类型"], "手机")
        self.assertEqual(source["core_problem"], "1. 电池工具读出异常如何判定；2. 双卡无法识别如何判定。")
        self.assertEqual(status, "已拆分")
        self.assertEqual(
            issues,
            ["电池工具读出异常如何判定", "双卡无法识别如何判定"],
        )

    def test_manual_title_column_does_not_replace_the_ai_core_problem(self):
        record = _row("1", "手机", "AI 核心问题原文")
        record["人工标题"] = "人工手填的另一个标题"

        parsed = parse_source_record(record)

        self.assertEqual(parsed["core_problem"], "AI 核心问题原文")
        self.assertNotIn("人工标题", parsed)

    def test_same_category_model_standard_question_merges_different_wording(self):
        records = [
            _row("1", "手机", "关于本机提示电池为正品部件，如何判定"),
            _row("2", "手机", "关于本机弹窗显示电池正品部件应勾选什么"),
            _row("3", "手机", "关于本机提示电池为正品部件，如何判定"),
            _row("4", "手机", "关于本机弹窗显示电池正品部件应勾选什么"),
            _row("5", "笔记本", "关于本机提示电池为正品部件，如何判定"),
            _row("6", "笔记本", "关于本机提示电池为正品部件，如何判定"),
        ]

        def fake_classify(items):
            titles = {}
            for item in items:
                if item["category"] == "手机":
                    titles[
                        _cache_key(
                            item["category"],
                            item["issue"],
                            item["level1"],
                            item["level2"],
                        )
                    ] = "关于本机提示电池正品部件如何判定"
                else:
                    titles[
                        _cache_key(
                            item["category"],
                            item["issue"],
                            item["level1"],
                            item["level2"],
                        )
                    ] = item["issue"]
            return titles, None

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records)

        mobile = next(row for row in payload["rows"] if row["品类"] == "手机")
        notebook = next(row for row in payload["rows"] if row["品类"] == "笔记本")
        self.assertEqual(mobile["原子问题"], "关于本机提示电池正品部件如何判定")
        self.assertEqual(mobile["出现次数"], 4)
        self.assertEqual(notebook["出现次数"], 2)
        self.assertEqual(
            payload["meta"]["grouping_mode"],
            "same_category_standard_question",
        )

    def test_standard_question_punctuation_variants_are_merged_after_model_classification(self):
        records = [
            _row("1", "手机", "偏光检测异常应如何判定"),
            _row("2", "手机", "偏光检测异常应如何判定"),
            _row("3", "手机", "偏光检测异常如何判定"),
            _row("4", "手机", "偏光检测异常如何判定"),
        ]

        def fake_classify(items):
            return {
                _cache_key(
                    item["category"],
                    item["issue"],
                    item["level1"],
                    item["level2"],
                ): (
                    "偏光检测异常如何判定"
                    if "应如何" in item["issue"]
                    else "偏光检测异常如何判定？"
                )
                for item in items
            }, None

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records)

        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["出现次数"], 4)

    def test_same_standard_question_counts_at_most_once_per_work_order(self):
        records = [
            _row(
                "1",
                "手机",
                "1. 屏幕偏光检测异常如何判定；2. 屏幕偏光异常怎么判定。",
            ),
            _row("2", "手机", "屏幕偏光检测异常如何判定"),
        ]

        def fake_classify(items):
            return {
                _cache_key(
                    item["category"],
                    item["issue"],
                    item["level1"],
                    item["level2"],
                ): {
                    "business_theme": "屏幕检测",
                    "standard_question": "屏幕偏光检测异常如何判定",
                }
                for item in items
            }, None

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records, high_frequency_only=False)

        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["出现次数"], 2)
        self.assertEqual(payload["rows"][0]["工单数"], 2)
        self.assertEqual(len(payload["rows"][0]["案例"]), 2)
        same_ticket_case = next(
            case for case in payload["rows"][0]["案例"] if case["工单ID"] == "1"
        )
        self.assertIn("屏幕偏光异常怎么判定", same_ticket_case["AI分析问题"])
        self.assertIn("屏幕偏光检测异常如何判定", same_ticket_case["AI分析问题"])

    def test_all_problem_view_does_not_relabel_single_occurrence_as_high_frequency(self):
        payload = build_dashboard(
            [
                _row("1", "手机", "偏光检测异常如何判定"),
                _row("2", "手机", "偏光检测异常如何判定"),
                _row("3", "手机", "防水标签变红如何判定"),
            ],
            high_frequency_only=False,
        )

        self.assertEqual(payload["summary"]["standard_question_count"], 2)
        self.assertEqual(payload["summary"]["high_frequency_count"], 1)
        self.assertEqual(payload["summary"]["single_occurrence_count"], 1)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertFalse(payload["meta"]["high_frequency_only"])

    def test_camera_battery_variants_use_actual_case_count_after_model_clustering(self):
        records = [
            _row("1", "单电/微单机身", "富士 X-T30 电池是否为正品原装，如何判定"),
            _row("2", "单电/微单机身", "LP-E12 电池是否原装需要如何确认"),
            _row("3", "单电/微单机身", "提供电池图片确认是否为原装电池"),
            _row("4", "单电/微单机身", "相机序列号标签不匹配是否需要人工复核"),
        ]

        def fake_classify(items):
            titles = {}
            for item in items:
                titles[
                    _cache_key(
                        item["category"],
                        item["issue"],
                        item["level1"],
                        item["level2"],
                    )
                ] = (
                    "相机原装电池如何判定"
                    if "电池" in item["issue"]
                    else "相机序列号异常如何处理"
                )
            return titles, None

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records, high_frequency_only=False)

        battery = next(
            row for row in payload["rows"]
            if row["原子问题"] == "相机原装电池如何判定"
        )
        self.assertEqual(battery["出现次数"], 3)
        self.assertEqual(battery["工单数"], 3)
        self.assertEqual(len(battery["案例"]), 3)
        self.assertEqual(payload["summary"]["high_frequency_count"], 1)
        self.assertEqual(payload["summary"]["single_occurrence_count"], 1)

    def test_consolidation_batches_keep_same_subject_titles_together(self):
        items = [
            {
                "id": str(index),
                "category": "单电/微单机身",
                "standard_question": title,
            }
            for index, title in enumerate(
                [
                    "相机快门次数异常如何处理",
                    "相机原装电池如何判定",
                    "相机屏幕坏点如何判定",
                    "相机序列号标签不匹配如何处理",
                    "相机镜头镀膜异常如何判定",
                    "相机找不到序列号如何处理",
                    "相机电池健康度如何查看判定",
                    "相机序列号显示N/A如何处理",
                ],
                1,
            )
        ]

        batches = _consolidation_batches(items, batch_size=2)
        serial_number_batch = next(
            batch
            for batch in batches
            if any(item["id"] == "4" for item in batch)
        )

        self.assertEqual(
            {item["id"] for item in serial_number_batch},
            {"4", "6", "8"},
        )

    def test_business_theme_does_not_merge_different_camera_standard_questions(self):
        records = [
            _row("1", "单电/微单机身", "相机序列号在哪里查看"),
            _row("2", "单电/微单机身", "相机序列号标签与工单不匹配如何处理"),
            _row("3", "单电/微单机身", "相机序列号显示 N/A 如何处理"),
            _row("4", "单电/微单机身", "相机序列号标签磨损无法完整展示如何判定"),
            _row("5", "单电/微单机身", "电池是否为正品原装，如何确认"),
            _row("6", "单电/微单机身", "JJC 第三方电池是否属于品牌电池"),
            _row("7", "单电/微单机身", "更换非品牌电池应选择哪个质检项"),
            _row("8", "单电/微单机身", "相机序列号标签不匹配是否需要人工复核"),
            _row("9", "单电/微单机身", "富士原装电池外观特征如何查看"),
        ]

        def fake_classify(items):
            titles = {}
            for item in items:
                key = _cache_key(
                    item["category"],
                    item["issue"],
                    item["level1"],
                    item["level2"],
                )
                if "在哪里查看" in item["issue"]:
                    titles[key] = {
                        "business_theme": "序列号核验",
                        "standard_question": "相机序列号位置如何查看",
                    }
                elif "不匹配" in item["issue"]:
                    titles[key] = {
                        "business_theme": "序列号核验",
                        "standard_question": "相机序列号与工单信息不一致如何判定",
                    }
                elif "N/A" in item["issue"]:
                    titles[key] = {
                        "business_theme": "序列号核验",
                        "standard_question": "相机序列号读取/查询失败如何处理",
                    }
                elif "磨损" in item["issue"]:
                    titles[key] = {
                        "business_theme": "序列号核验",
                        "standard_question": "相机序列号标签异常如何判定",
                    }
                elif "第三方" in item["issue"]:
                    titles[key] = {
                        "business_theme": "电池核验",
                        "standard_question": "相机第三方品牌电池如何认定",
                    }
                elif "更换" in item["issue"]:
                    titles[key] = {
                        "business_theme": "电池核验",
                        "standard_question": "相机非品牌/更换电池应选择哪个质检项",
                    }
                else:
                    titles[key] = {
                        "business_theme": "电池核验",
                        "standard_question": "相机原装/正品电池如何核验",
                    }
            return titles, None

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records, high_frequency_only=False)

        rows = {
            row["原子问题"]: row
            for row in payload["rows"]
        }
        self.assertEqual(len(rows), 7)
        self.assertEqual(
            rows["相机序列号位置如何查看"]["业务主题"],
            "序列号核验",
        )
        self.assertEqual(
            rows["相机序列号读取/查询失败如何处理"]["业务主题"],
            "序列号核验",
        )
        self.assertEqual(
            rows["相机原装/正品电池如何核验"]["业务主题"],
            "电池核验",
        )
        self.assertEqual(
            rows["相机原装/正品电池如何核验"]["出现次数"],
            2,
        )
        self.assertEqual(
            rows["相机序列号与工单信息不一致如何判定"]["出现次数"],
            2,
        )
        self.assertEqual(
            rows["相机第三方品牌电池如何认定"]["业务主题"],
            "电池核验",
        )
        self.assertEqual(
            rows["相机序列号与工单信息不一致如何判定"]["工单数"],
            len(rows["相机序列号与工单信息不一致如何判定"]["案例"]),
        )

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            serial_only = build_dashboard(
                records,
                high_frequency_only=False,
                business_theme="序列号核验",
            )

        self.assertEqual(serial_only["summary"]["atomic_issue_count"], 5)
        self.assertEqual(serial_only["summary"]["total_records"], 5)
        self.assertEqual(
            {row["业务主题"] for row in serial_only["rows"]},
            {"序列号核验"},
        )

    def test_redundant_camera_noun_does_not_split_same_standard_question(self):
        records = [
            _row("1", "单电/微单机身", "相机序列号标签不匹配如何判定"),
            _row("2", "单电/微单机身", "机身序列号标签不匹配是否需要人工复核"),
        ]

        def fake_classify(items):
            return {
                _cache_key(
                    item["category"],
                    item["issue"],
                    item["level1"],
                    item["level2"],
                ): {
                    "business_theme": "序列号核验",
                    "standard_question": (
                        "相机机身序列号标签不匹配如何判定"
                        if "机身序列号" in item["issue"]
                        else "相机序列号标签不匹配如何判定"
                    ),
                }
                for item in items
            }, None

        with patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records, high_frequency_only=False)

        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["出现次数"], 2)
        self.assertEqual(
            payload["rows"][0]["原子问题"],
            "相机序列号标签不匹配如何判定",
        )

    def test_cached_only_query_returns_real_atomic_statistics_without_model_wait(self):
        records = [
            _row("1", "手机", "屏幕亮线如何判定"),
            _row("2", "手机", "屏幕亮线如何判定"),
            _row("3", "手机", "后盖划痕如何判定"),
        ]

        with patch(
            "app.services.high_frequency.get_cached_classifications",
            return_value=({}, 2),
        ), patch(
            "app.services.high_frequency.classify_questions",
        ) as classify:
            payload = build_dashboard(
                records,
                high_frequency_only=False,
                cached_only=True,
            )

        classify.assert_not_called()
        self.assertTrue(payload["meta"]["classification_pending"])
        self.assertEqual(payload["meta"]["uncached_classification_count"], 2)
        self.assertEqual(payload["summary"]["atomic_issue_count"], 3)
        self.assertEqual(
            next(row for row in payload["rows"] if row["出现次数"] == 2)[
                "原子问题"
            ],
            "屏幕亮线如何判定",
        )

    def test_deepseek_split_questions_are_counted_independently(self):
        records = [
            _row("1", "手机", "回收师咨询两个问题，但未提供可可靠拆分的边界。"),
            _row("2", "手机", "回收师咨询两个问题，但未提供可可靠拆分的边界。"),
        ]

        def fake_split(items):
            return {
                item["id"]: {
                    "questions": [
                        "电池正品状态如何核验",
                        "屏幕偏光检测异常如何判定",
                    ],
                    "needs_review": False,
                }
                for item in items
            }, None

        def fake_classify(items):
            return {
                _cache_key(
                    item["category"],
                    item["issue"],
                    item["level1"],
                    item["level2"],
                ): {
                    "business_theme": "部件核验",
                    "standard_question": item["issue"],
                }
                for item in items
            }, None

        with patch(
            "app.services.high_frequency.split_ambiguous_questions",
            side_effect=fake_split,
        ), patch(
            "app.services.high_frequency.classify_questions",
            side_effect=fake_classify,
        ):
            payload = build_dashboard(records, high_frequency_only=False)

        self.assertEqual(payload["summary"]["atomic_issue_count"], 4)
        self.assertEqual(payload["summary"]["pending_split_records"], 0)
        self.assertEqual(
            sorted(row["出现次数"] for row in payload["rows"]),
            [2, 2],
        )
        self.assertTrue(
            all(
                case["拆分状态"] == "DeepSeek已拆分"
                for row in payload["rows"]
                for case in row["案例"]
            )
        )

    def test_unresolved_split_is_excluded_and_exposes_ai_analysis_and_judgment(self):
        ambiguous = _row(
            "ambiguous",
            "手机",
            "回收师咨询两个问题，但未提供可可靠拆分的边界。",
        )
        ambiguous["ai_result"]["判定结论"] = "需要分项核验后再判断"

        def fake_split(items):
            return {
                items[0]["id"]: {
                    "questions": ["无法确认边界的问题"],
                    "needs_review": True,
                }
            }, None

        with patch(
            "app.services.high_frequency.split_ambiguous_questions",
            side_effect=fake_split,
        ):
            payload = build_dashboard([ambiguous], high_frequency_only=False)

        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["summary"]["atomic_issue_count"], 0)
        self.assertEqual(payload["summary"]["pending_split_records"], 1)
        self.assertEqual(payload["meta"]["quality_status"], "review")
        pending = payload["pending_cases"][0]
        self.assertEqual(pending["AI分析问题"], ambiguous["ai_result"]["核心问题"])
        self.assertEqual(pending["原始AI核心问题"], ambiguous["ai_result"]["核心问题"])
        self.assertEqual(pending["判定结论"], "需要分项核验后再判断")

    def test_model_can_confirm_a_flagged_text_is_one_question(self):
        record = _row(
            "1",
            "手机",
            "回收师询问定位功能如何判定，同时追问检测流程的设备状态如何影响该判定。",
        )

        def fake_split(items):
            return {
                items[0]["id"]: {
                    "questions": ["定位功能检测如何判定"],
                    "needs_review": False,
                }
            }, None

        with patch(
            "app.services.high_frequency.split_ambiguous_questions",
            side_effect=fake_split,
        ):
            payload = build_dashboard([record], high_frequency_only=False)

        self.assertEqual(payload["summary"]["atomic_issue_count"], 1)
        self.assertEqual(payload["summary"]["pending_split_records"], 0)
        self.assertEqual(payload["rows"][0]["案例"][0]["拆分状态"], "DeepSeek确认单问题")

    def test_cached_only_missing_split_is_pending_and_not_frequency(self):
        record = _row(
            "1",
            "手机",
            "回收师咨询两个问题，但未提供可可靠拆分的边界。",
        )
        with patch(
            "app.services.high_frequency.get_cached_atomic_splits",
            return_value=({}, 1),
        ), patch(
            "app.services.high_frequency.get_cached_classifications",
            return_value=({}, 0),
        ), patch(
            "app.services.high_frequency.split_ambiguous_questions",
        ) as split_live, patch(
            "app.services.high_frequency.classify_questions",
        ) as classify_live:
            payload = build_dashboard(
                [record],
                cached_only=True,
                high_frequency_only=False,
            )

        split_live.assert_not_called()
        classify_live.assert_not_called()
        self.assertTrue(payload["meta"]["classification_pending"])
        self.assertEqual(payload["meta"]["uncached_split_count"], 1)
        self.assertEqual(payload["summary"]["atomic_issue_count"], 0)
        self.assertEqual(payload["pending_cases"][0]["拆分状态"], "等待DeepSeek拆分")


if __name__ == "__main__":
    unittest.main()
