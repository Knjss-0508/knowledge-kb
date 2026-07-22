import unittest
from io import BytesIO
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook

from app.services.knowledge_excel import (
    KnowledgeExcelError,
    build_knowledge_import_template,
    parse_knowledge_workbook,
)


class KnowledgeExcelTests(unittest.TestCase):
    def setUp(self):
        self.categories = [
            SimpleNamespace(
                id="cat-parent",
                name="质检",
                parent_id=None,
                level=1,
                sort_order=10,
            ),
            SimpleNamespace(
                id="cat-process",
                name="质检流程",
                parent_id="cat-parent",
                level=2,
                sort_order=20,
            ),
        ]

    @staticmethod
    def workbook_bytes(headers, rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "知识导入"
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_template_contains_category_dictionary_and_no_layer_column(self):
        payload = build_knowledge_import_template(self.categories)
        workbook = load_workbook(BytesIO(payload), read_only=True)

        self.assertEqual(
            workbook.sheetnames,
            ["知识导入", "分类字典", "填写说明"],
        )
        headers = [
            cell.value
            for cell in next(workbook["知识导入"].iter_rows(max_row=1))
        ]
        self.assertIn("知识分类", headers)
        self.assertNotIn("知识层级", headers)
        self.assertNotIn("适用业务", headers)
        self.assertNotIn("机型个性化", headers)
        dictionary_rows = list(
            workbook["分类字典"].iter_rows(min_row=2, values_only=True)
        )
        self.assertIn(("cat-process", "质检流程", "质检/质检流程"), dictionary_rows)

    def test_parse_accepts_category_id_and_splits_multi_value_fields(self):
        payload = self.workbook_bytes(
            ["标题", "知识分类", "正文", "副标题", "场景标签"],
            [
                [
                    "设备无法开机",
                    "cat-process",
                    "先检查电量，再执行强制重启。",
                    "黑屏怎么办；无法启动",
                    "无法开机；售后咨询",
                ]
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_valid)
        self.assertEqual(rows[0].category_id, "cat-process")
        self.assertEqual(rows[0].subtitles, ["黑屏怎么办", "无法启动"])
        self.assertEqual(rows[0].applicable_scenes, ["无法开机", "售后咨询"])

    def test_parse_accepts_full_category_path(self):
        payload = self.workbook_bytes(
            ["标题", "知识分类", "正文"],
            [["流程说明", "质检/质检流程", "按流程逐项检查。"]],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertTrue(rows[0].is_valid)
        self.assertEqual(rows[0].category_id, "cat-process")

    def test_invalid_rows_are_reported_without_hiding_valid_rows(self):
        payload = self.workbook_bytes(
            ["标题", "知识分类", "正文"],
            [
                ["有效知识", "cat-process", "有效正文内容。"],
                ["分类错误", "不存在的分类", "正文内容。"],
                ["", "cat-process", "正文内容。"],
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertTrue(rows[0].is_valid)
        self.assertEqual(rows[1].error_code, "CATEGORY_NOT_FOUND")
        self.assertEqual(rows[2].error_code, "TITLE_REQUIRED")

    def test_missing_required_headers_rejects_workbook(self):
        payload = self.workbook_bytes(
            ["标题", "知识分类"],
            [["缺少正文列", "cat-process"]],
        )

        with self.assertRaisesRegex(KnowledgeExcelError, "缺少必填列"):
            parse_knowledge_workbook(payload, self.categories)


if __name__ == "__main__":
    unittest.main()
