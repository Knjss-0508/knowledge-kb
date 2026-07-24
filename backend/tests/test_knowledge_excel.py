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
                name="操作流程",
                parent_id="cat-parent",
                level=2,
                sort_order=20,
            ),
            SimpleNamespace(
                id="cat-qc-standard",
                name="质检标准",
                parent_id=None,
                level=1,
                sort_order=30,
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
        self.assertIn("标题（必填）", headers)
        self.assertIn("知识分类（必填）", headers)
        self.assertIn("正文（必填）", headers)
        self.assertIn("副标题（选填）", headers)
        self.assertIn("场景标签（选填）", headers)
        self.assertIn("适用类目（选填）", headers)
        self.assertIn("适用品牌（选填）", headers)
        self.assertIn("适用机型（选填）", headers)
        self.assertNotIn("知识层级", headers)
        self.assertNotIn("适用业务", headers)
        self.assertNotIn("机型个性化", headers)
        dictionary_rows = list(
            workbook["分类字典"].iter_rows(min_row=2, values_only=True)
        )
        self.assertIn(("cat-process", "操作流程", "质检/操作流程"), dictionary_rows)

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
            [["流程说明", "质检/操作流程", "按流程逐项检查。"]],
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

    def test_parse_accepts_governed_qc_workbook_format(self):
        payload = self.workbook_bytes(
            ["主标题", "副标题", "知识内容", "知识分类", "适用范围", "生效状态"],
            [
                [
                    "苹果设备外观判定",
                    "苹果设备外观怎么检查\n苹果外观如何判定",
                    "按质检标准逐项检查。\n"
                    "[img:https://cdn.example.com/qc/apple.png]",
                    "场景判定",
                    "苹果",
                    "生效中",
                ],
                [
                    "安卓设备检测方法",
                    "安卓设备怎么检测",
                    "按步骤执行检测。",
                    "检测方法",
                    "通用",
                    "待审核",
                ],
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].is_valid)
        self.assertEqual(rows[0].category_id, "cat-qc-standard")
        self.assertEqual(
            rows[0].subtitles,
            ["苹果设备外观怎么检查", "苹果外观如何判定"],
        )
        self.assertEqual(
            rows[0].content,
            {
                "blocks": [
                    {"type": "text", "value": "按质检标准逐项检查。"},
                    {
                        "type": "image",
                        "external_url": "https://cdn.example.com/qc/apple.png",
                        "alt": "",
                        "caption": "",
                    },
                ]
            },
        )
        self.assertEqual(rows[0].applicable_scenes, ["适用范围：苹果"])
        self.assertEqual(rows[0].source_status, "生效中")
        self.assertEqual(rows[0].source_scope, "苹果")
        self.assertEqual(rows[1].error_code, "SOURCE_STATUS_NOT_IMPORTABLE")
        self.assertIn("不会上传", rows[1].error_message)

    def test_parse_maps_qc_category_values_to_system_categories(self):
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容"],
            [
                ["标准定义示例", "标准定义", "标准正文。"],
                ["检测方法示例", "检测方法", "检测步骤。"],
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(rows[0].category_id, "cat-qc-standard")
        self.assertEqual(rows[1].category_id, "cat-process")

    def test_parse_only_promotes_prefixed_media_tokens(self):
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容"],
            [
                [
                    "链接与图片示例",
                    "标准定义",
                    "帮助地址：https://example.com/help\n"
                    "[img:https://cdn.example.com/image-resource?version=2]\n"
                    "后续说明。",
                ]
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(
            rows[0].content,
            {
                "blocks": [
                    {
                        "type": "text",
                        "value": "帮助地址：https://example.com/help",
                    },
                    {
                        "type": "image",
                        "external_url": "https://cdn.example.com/image-resource?version=2",
                        "alt": "",
                        "caption": "",
                    },
                    {"type": "text", "value": "后续说明。"},
                ]
            },
        )

    def test_parse_prefixed_media_token_preserves_inline_position(self):
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容"],
            [
                [
                    "行内媒体示例",
                    "标准定义",
                    "图片前文[img:https://cdn.example.com/image-resource]图片后文",
                ]
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(
            rows[0].content,
            {
                "blocks": [
                    {"type": "text", "value": "图片前文"},
                    {
                        "type": "image",
                        "external_url": "https://cdn.example.com/image-resource",
                        "alt": "",
                        "caption": "",
                    },
                    {"type": "text", "value": "图片后文"},
                ]
            },
        )

    def test_parse_preserves_external_media_order_and_duplicate_references(self):
        repeated_image = "https://cdn.example.com/psn-resource"
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容"],
            [
                [
                    "序列号查看说明",
                    "标准定义",
                    "【苹果】补充：\n"
                    "[img:https://cdn.example.com/apple-resource]\n"
                    "【安卓】补充：\n"
                    "[video:https://cdn.example.com/android-stream?version=2]\n"
                    "【小米/红米】PSN码查看：\n"
                    f"[img:{repeated_image}]\n"
                    "【小米/红米】补充：\n"
                    f"[img:{repeated_image}]",
                ]
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(
            rows[0].content,
            {
                "blocks": [
                    {"type": "text", "value": "【苹果】补充："},
                    {
                        "type": "image",
                        "external_url": "https://cdn.example.com/apple-resource",
                        "alt": "",
                        "caption": "",
                    },
                    {"type": "text", "value": "【安卓】补充："},
                    {
                        "type": "video",
                        "external_url": "https://cdn.example.com/android-stream?version=2",
                        "alt": "",
                        "caption": "",
                    },
                    {"type": "text", "value": "【小米/红米】PSN码查看："},
                    {
                        "type": "image",
                        "external_url": repeated_image,
                        "alt": "",
                        "caption": "",
                    },
                    {"type": "text", "value": "【小米/红米】补充："},
                    {
                        "type": "image",
                        "external_url": repeated_image,
                        "alt": "",
                        "caption": "",
                    },
                ]
            },
        )

    def test_parse_keeps_unprefixed_urls_as_plain_text(self):
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容"],
            [
                [
                    "原有链接示例",
                    "标准定义",
                    "官网：https://example.com\n"
                    "https://cdn.example.com/raw-image.png\n"
                    "https://cdn.example.com/raw-video.mp4",
                ]
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(
            rows[0].content,
            "官网：https://example.com\n"
            "https://cdn.example.com/raw-image.png\n"
            "https://cdn.example.com/raw-video.mp4",
        )

    def test_parse_keeps_unsafe_prefixed_url_as_plain_text(self):
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容"],
            [
                [
                    "不安全媒体标记",
                    "标准定义",
                    "[img:https://user@example.com/private-resource]",
                ]
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(
            rows[0].content,
            "[img:https://user@example.com/private-resource]",
        )

    def test_parse_accepts_195_rows(self):
        payload = self.workbook_bytes(
            ["主标题", "知识分类", "知识内容", "生效状态"],
            [
                [f"知识 {index}", "标准定义", f"正文 {index}", "生效中"]
                for index in range(195)
            ],
        )

        rows = parse_knowledge_workbook(payload, self.categories)

        self.assertEqual(len(rows), 195)
        self.assertTrue(all(row.is_valid for row in rows))

    def test_parse_rejects_more_than_500_rows(self):
        payload = self.workbook_bytes(
            ["标题", "知识分类", "正文"],
            [
                [f"知识 {index}", "cat-process", f"正文 {index}"]
                for index in range(501)
            ],
        )

        with self.assertRaisesRegex(KnowledgeExcelError, "单次最多导入 500 条"):
            parse_knowledge_workbook(payload, self.categories)

    def test_missing_required_headers_rejects_workbook(self):
        payload = self.workbook_bytes(
            ["标题", "知识分类"],
            [["缺少正文列", "cat-process"]],
        )

        with self.assertRaisesRegex(KnowledgeExcelError, "缺少必填列"):
            parse_knowledge_workbook(payload, self.categories)


if __name__ == "__main__":
    unittest.main()
