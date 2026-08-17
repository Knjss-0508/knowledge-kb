import unittest
from pathlib import Path


class RetrievalConfigFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")

    def test_two_knowledge_pools_have_independent_top_k_inputs(self):
        self.assertIn('class="embedding-config-grid retrieval-settings"', self.html)
        for input_id, field_name in (
            (
                "retrieval-headquarters-top-k",
                "retrieval_headquarters_standard_top_k",
            ),
            (
                "retrieval-business-top-k",
                "retrieval_business_accumulation_top_k",
            ),
        ):
            self.assertIn(f'for="{input_id}"', self.html)
            self.assertIn(f'id="{input_id}"', self.html)
            self.assertIn(
                f'v-model.number="embedding.configForm.{field_name}"',
                self.html,
            )
        self.assertGreaterEqual(
            self.html.count('type="number" min="1" max="10" step="1"'),
            2,
        )

    def test_top_k_payload_and_validation_cover_both_pools(self):
        for field_name, message in (
            (
                "retrieval_headquarters_standard_top_k",
                "总部标准返回数量必须是 1 到 10 的整数",
            ),
            (
                "retrieval_business_accumulation_top_k",
                "业务沉淀返回数量必须是 1 到 10 的整数",
            ),
        ):
            self.assertIn(f"{field_name}:Number(", self.html)
            self.assertIn(message, self.html)
        self.assertIn(
            "两个知识池独立生效，可分别设置 1 - 10 条",
            self.html,
        )
        self.assertIn(
            "达到阈值的知识不足设置数量时按实际数量返回",
            self.html,
        )

    def test_active_status_and_review_labels_follow_both_top_k_values(self):
        self.assertIn(
            "this.retrievalConfigTopK(item, "
            "'retrieval_headquarters_standard_top_k')",
            self.html,
        )
        self.assertIn(
            "this.retrievalConfigTopK(item, "
            "'retrieval_business_accumulation_top_k')",
            self.html,
        )
        self.assertIn("label:'总部标准',items:headquarters", self.html)
        self.assertIn("label:'业务沉淀',items:business", self.html)
        self.assertNotIn(
            "候选知识（总部标准 TOP{{normalizeRetrievalTopK",
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
