import unittest

from pydantic import ValidationError

from app.schemas.integration import IntegrationProcessing
from app.services.candidate_review import (
    evaluate_review_status,
    normalize_human_review,
)


class CandidateReviewServiceTests(unittest.TestCase):
    def test_pending_review_waits_for_human_confirmation(self):
        status, eligible, reason = evaluate_review_status(
            {"eligible": False},
            {"knowledge_value": "pending", "usability": "pending"},
        )

        self.assertEqual(status, "pending")
        self.assertFalse(eligible)
        self.assertIn("等待人工确认", reason)

    def test_worthy_and_usable_review_is_ready(self):
        status, eligible, reason = evaluate_review_status(
            {"eligible": False},
            {"knowledge_value": "是", "usability": "可用"},
        )

        self.assertEqual(status, "ready")
        self.assertTrue(eligible)
        self.assertIn("可提交", reason)

    def test_rejection_overrides_upstream_eligible_gate(self):
        status, eligible, reason = evaluate_review_status(
            {"eligible": True},
            {"decision": "驳回", "knowledge_value": "是", "usability": "是"},
        )

        self.assertEqual(status, "rejected")
        self.assertFalse(eligible)
        self.assertIn("驳回", reason)

    def test_localized_review_values_are_normalized(self):
        review = normalize_human_review(
            {
                "knowledge_value": "值得沉淀",
                "usability": "通过",
                "decision": "修改后通过",
                "modification_notes": None,
            }
        )

        self.assertEqual(review["knowledge_value"], "worthy")
        self.assertEqual(review["usability"], "usable")
        self.assertEqual(review["decision"], "approved_with_changes")
        self.assertEqual(review["modification_notes"], "")


class IntegrationProcessingContractTests(unittest.TestCase):
    def test_plugin_contract_is_the_primary_processing_contract(self):
        processing = IntegrationProcessing.model_validate(
            {
                "summary_version": "summary-v1",
                "label_model": "label-v2",
                "plugin_name": "answer-hub-topic-transcription",
                "plugin_version": "2026-07-23",
            }
        )

        self.assertEqual(processing.plugin_name, "answer-hub-topic-transcription")
        self.assertIsNone(processing.skill_name)

    def test_legacy_skill_pair_remains_compatible(self):
        processing = IntegrationProcessing.model_validate(
            {
                "summary_version": "summary-v1",
                "label_model": "label-v2",
                "skill_name": "legacy-knowledge-rewriter",
                "skill_version": "2026-07-11",
            }
        )

        self.assertEqual(processing.skill_name, "legacy-knowledge-rewriter")
        self.assertIsNone(processing.plugin_name)

    def test_processing_extension_name_and_version_must_be_complete(self):
        with self.assertRaises(ValidationError):
            IntegrationProcessing.model_validate(
                {
                    "summary_version": "summary-v1",
                    "label_model": "label-v2",
                    "plugin_name": "answer-hub-topic-transcription",
                }
            )

        with self.assertRaises(ValidationError):
            IntegrationProcessing.model_validate(
                {
                    "summary_version": "summary-v1",
                    "label_model": "label-v2",
                }
            )


if __name__ == "__main__":
    unittest.main()
