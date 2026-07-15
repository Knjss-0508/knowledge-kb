from __future__ import annotations

import unittest

from answer_hub.cz_integration import CzIntegrationAdapter, CzIntegrationConfig


class CzIntegrationTests(unittest.TestCase):
    def test_phone_pilot_exposes_readiness_without_network_submission(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        readiness = adapter.readiness()

        self.assertTrue(readiness["configured"])
        self.assertEqual(readiness["status"], "待联调（本期不发送 API 请求）")
        self.assertEqual(readiness["taxonomy_endpoint"], "/api/v1/integration/taxonomy")

    def test_unmapped_category_blocks_local_payload(self) -> None:
        adapter = CzIntegrationAdapter(CzIntegrationConfig("https://kb.example", "test-key"))
        candidate = {"主题ID": "TOP-001", "主标题": "机型查询流程", "知识内容": "查询步骤", "知识分类": "检测方法"}

        with self.assertRaisesRegex(ValueError, "未映射 category_id"):
            adapter.build_batch_payload([candidate], {})


if __name__ == "__main__":
    unittest.main()
