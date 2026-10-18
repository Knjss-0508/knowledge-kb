import unittest

from pydantic import ValidationError

from app.schemas.integration import IntegrationProcessing
from app.services.candidate_review import (
    build_quick_human_review,
    draft_blocking_reasons,
    evaluate_candidate_state,
    evaluate_review_status,
    model_priority_reasons,
    normalize_human_review,
)


class CandidateReviewServiceTests(unittest.TestCase):
    def test_quick_review_populates_legacy_gate_fields_once(self):
        review = build_quick_human_review(
            "unworthy",
            include_in_training=True,
            notes="模型失败批次，无需沉淀",
        )

        self.assertEqual(review["knowledge_value"], "unworthy")
        self.assertEqual(review["usability"], "unusable")
        self.assertEqual(review["decision"], "rejected")
        self.assertEqual(review["training_eligible"], "是")
        self.assertEqual(review["notes"], "模型失败批次，无需沉淀")

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
            {"suggested_action": "submit_for_human_review"},
        )

        self.assertEqual(status, "ready")
        self.assertTrue(eligible)
        self.assertIn("可提交", reason)

    def test_worthy_and_usable_review_is_ready_without_model_annotation(self):
        status, eligible, reason = evaluate_review_status(
            {"eligible": False},
            {"knowledge_value": "值得沉淀", "usability": "可用"},
            {},
        )

        self.assertEqual(status, "ready")
        self.assertTrue(eligible)
        self.assertIn("可提交", reason)

    def test_valuable_topic_with_revision_required_stays_out_of_submission_queue(self):
        status, eligible, reason = evaluate_review_status(
            {"eligible": False},
            {
                "knowledge_value": "worthy",
                "usability": "pending",
            },
            {"suggested_action": "return_for_revision"},
        )

        self.assertEqual(status, "revision_required")
        self.assertFalse(eligible)
        self.assertIn("退回转写", reason)

    def test_model_draft_action_controls_gate_without_human_draft_field(self):
        for action, expected_status in (
            ("submit_for_human_review", "ready"),
            ("return_for_revision", "revision_required"),
            ("hold_for_evidence", "hold_for_evidence"),
        ):
            status, eligible, _ = evaluate_review_status(
                {"eligible": False},
                {"knowledge_value": "worthy", "usability": "usable"},
                {"suggested_action": action},
            )
            self.assertEqual(status, expected_status)
            self.assertEqual(eligible, expected_status == "ready")

    def test_draft_completeness_requires_title_body_and_reply(self):
        self.assertEqual(
            draft_blocking_reasons(
                {
                    "title": " ",
                    "content": {"blocks": [{"type": "image", "caption": "照片"}]},
                    "recommended_reply": "",
                    "evidence_excerpt": None,
                }
            ),
            ["知识标题为空", "知识正文为空", "推荐回复为空"],
        )
        self.assertEqual(
            draft_blocking_reasons(
                {
                    "title": "候选标题",
                    "content": {"blocks": [{"type": "text", "value": "有效正文"}]},
                    "recommended_reply": "可直接发送的回复",
                    "evidence_excerpt": "",
                }
            ),
            [],
        )

    def test_model_actions_produce_distinct_candidate_states(self):
        draft = {
            "title": "候选标题",
            "content": "有效正文",
            "recommended_reply": "可直接发送的回复",
            "evidence_excerpt": "来源原文摘要",
        }
        expected = {
            "submit_for_human_review": "pending",
            "return_for_revision": "revision_required",
            "hold_for_evidence": "hold_for_evidence",
        }
        for action, expected_status in expected.items():
            state = evaluate_candidate_state(
                {"eligible": False},
                {"knowledge_value": "pending"},
                {"suggested_action": action},
                knowledge=draft,
                model_annotation={"status": "completed", "draft_version": 2},
                draft_version=2,
            )
            self.assertEqual(state["review_status"], expected_status)
            self.assertFalse(state["eligible"])
            self.assertTrue(state["blocking_reasons"])
            self.assertEqual(state["draft_version"], 2)

    def test_stale_model_annotation_blocks_submission_after_draft_change(self):
        state = evaluate_candidate_state(
            {"eligible": False},
            {"knowledge_value": "worthy", "usability": "usable"},
            {"suggested_action": "submit_for_human_review"},
            knowledge={
                "title": "候选标题",
                "content": "有效正文",
                "recommended_reply": "可直接发送的回复",
                "evidence_excerpt": "来源原文摘要",
            },
            model_annotation={"status": "completed", "draft_version": 1},
            draft_version=2,
        )

        self.assertEqual(state["review_status"], "pending_model_annotation")
        self.assertFalse(state["eligible"])
        self.assertEqual(state["next_action"], "annotate_model")

    def test_explicit_model_evidence_risk_blocks_submission_even_if_action_conflicts(self):
        state = evaluate_candidate_state(
            {"eligible": False},
            {"knowledge_value": "worthy", "usability": "usable"},
            {
                "suggested_action": "submit_for_human_review",
                "evidence_sufficiency": "insufficient",
            },
            knowledge={
                "title": "候选标题",
                "content": "有效正文",
                "recommended_reply": "可直接发送的回复",
                "evidence_excerpt": "有摘要，但模型判断证据不足",
            },
            model_annotation={"status": "completed", "draft_version": 1},
            draft_version=1,
        )

        self.assertEqual(state["review_status"], "hold_for_evidence")
        self.assertFalse(state["eligible"])
        self.assertEqual(state["next_action"], "add_source_evidence")
        self.assertTrue(state["priority_review"])

    def test_model_priority_requires_explicit_risk_not_a_pending_review(self):
        self.assertEqual(
            model_priority_reasons(
                {"priority_review": True, "suggested_action": "submit_for_human_review"}
            ),
            [],
        )
        self.assertEqual(
            model_priority_reasons(
                {"risk_flags": ["source_conflict"], "priority_review": False}
            ),
            ["模型风险：source_conflict"],
        )
        self.assertTrue(
            model_priority_reasons({"risk_flags": ["evidence_insufficient"]})
        )

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
