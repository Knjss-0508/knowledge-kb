import unittest
from pathlib import Path


class ModelConfigurationFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")

    def test_managed_origin_has_fallback_labels(self):
        self.assertGreaterEqual(
            self.html.count(
                "{value:'model_configuration', label:'机型配置信息'}"
            ),
            2,
        )
        self.assertIn(
            "model_configuration:'机型配置信息'",
            self.html,
        )

    def test_managed_origin_is_not_offered_for_manual_assignment(self):
        self.assertIn(
            "assignableKnowledgeOrigins(candidateReviews.form.knowledge_origin)",
            self.html,
        )
        self.assertIn(
            "assignableKnowledgeOrigins(fm.knowledgeOrigin)",
            self.html,
        )
        self.assertIn(
            "key !== 'model_configuration' || currentKey === key",
            self.html,
        )
        self.assertIn(
            "item.knowledge_origin === 'model_configuration'",
            self.html,
        )
        self.assertIn(
            "r.knowledge_origin === 'model_configuration'",
            self.html,
        )
        self.assertNotIn(
            '<option v-for="origin in knowledgeOrigins"',
            self.html,
        )

    def test_managed_origin_remains_available_in_list_filter(self):
        self.assertIn(
            'v-for="origin in knowledgeOrigins"',
            self.html,
        )
        self.assertIn(
            "@click=\"setFilter('knowledge_origin', origin.value)\"",
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
