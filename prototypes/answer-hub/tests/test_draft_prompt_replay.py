from __future__ import annotations

from types import SimpleNamespace

from answer_hub.draft_prompt_replay import (
    replay_draft_prompts,
    topic_review_rows_to_replay_samples,
)
from answer_hub.mimo import MimoError


class _ReplayClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.baseline_max_attempts: int | None = None
        self.config = SimpleNamespace(model="deepseek-flash")

    def label_topic(self, topic, _matches, **_kwargs):
        self.calls.append(("baseline", str(topic["topic_id"])))
        self.baseline_max_attempts = _kwargs.get("max_attempts")
        return SimpleNamespace(
            candidate={
                "title": "基线标题",
                "content": "1. 基线正文。",
                "recommended_reply": "基线回复。",
            },
            request_audit={"prompt_version": "answer-hub-topic-transcription-v1"},
            response_audit={},
        )

    def replay_topic_draft(self, topic, *, draft_prompt, prompt_version):
        self.calls.append((prompt_version, str(topic["topic_id"])))
        assert draft_prompt == "候选草稿 Prompt"
        return SimpleNamespace(
            candidate={
                "title": "候选标题",
                "content": "1. 候选正文。",
                "recommended_reply": "候选回复。",
            },
            request_audit={"prompt_version": prompt_version},
            response_audit={},
        )


def test_replay_uses_answer_hub_transcription_for_baseline_and_candidate():
    client = _ReplayClient()

    result = replay_draft_prompts(
        client=client,
        samples=[
            {
                "id": "sample-1",
                "topic": {
                    "topic_id": "TOP-1",
                    "产品类型": "笔记本",
                    "主题事实证据包": {"facts": [{"fact_id": "F01"}]},
                },
                "human_final_draft": {
                    "title": "人工标题",
                    "content": "人工正文",
                    "recommended_reply": "人工回复",
                },
            }
        ],
        candidate_prompt="候选草稿 Prompt",
        candidate_prompt_version="draft-v1-candidate",
    )

    assert client.calls == [
        ("baseline", "TOP-1"),
        ("draft-v1-candidate", "TOP-1"),
    ]
    assert client.baseline_max_attempts == 2
    assert result["completed_count"] == 1
    assert result["skipped_count"] == 0
    row = result["rows"][0]
    assert row["baseline"]["title"] == "基线标题"
    assert row["candidate"]["recommended_reply"] == "候选回复。"
    assert row["human_final_draft"]["title"] == "人工标题"
    assert row["candidate_prompt_version"] == "draft-v1-candidate"


def test_replay_skips_incomplete_topic_evidence_without_fabricating_input():
    client = _ReplayClient()

    result = replay_draft_prompts(
        client=client,
        samples=[
            {
                "id": "missing-topic",
                "topic": {},
                "human_final_draft": {"title": "人工标题"},
            }
        ],
        candidate_prompt="候选草稿 Prompt",
        candidate_prompt_version="draft-v1-candidate",
    )

    assert client.calls == []
    assert result["completed_count"] == 0
    assert result["skipped_count"] == 1
    assert result["skipped_samples"][0]["reason"] == "缺少完整 Answer Hub 主题证据包。"


def test_topic_review_rows_keep_full_evidence_and_do_not_claim_human_truth():
    result = topic_review_rows_to_replay_samples([
        {
            "主题ID": "TOP-1",
            "适用范围": "笔记本",
            "主题问题意图": "判断屏幕色斑",
            "主题事实证据包": (
                '{"representative_facts":'
                '[{"fact_id":"F01","source_record_id":"record-1"}]}'
            ),
            "主标题": "模型标题",
            "知识内容": "模型正文",
            "推荐回复": "模型推荐回复",
        },
        {
            "主题ID": "TOP-2",
            "适用范围": "手机",
            "主题事实证据包": '{"facts":[]}',
        },
    ])

    assert len(result["samples"]) == 1
    sample = result["samples"][0]
    assert sample["topic"]["topic_id"] == "TOP-1"
    assert (
        sample["topic"]["主题事实证据包"]["representative_facts"][0]["fact_id"]
        == "F01"
    )
    assert sample["human_final_draft"] == {}
    assert result["skipped_samples"] == [
        {
            "id": "TOP-2",
            "reason": "主题事实证据包不含 facts 或 representative_facts。",
        }
    ]


def test_replay_reports_whether_baseline_or_candidate_failed():
    class CandidateFailureClient(_ReplayClient):
        def replay_topic_draft(self, *_args, **_kwargs):
            raise MimoError("候选结构不合格")

    result = replay_draft_prompts(
        client=CandidateFailureClient(),
        samples=[
            {
                "id": "sample-1",
                "topic": {
                    "topic_id": "TOP-1",
                    "产品类型": "笔记本",
                    "主题事实证据包": {
                        "representative_facts": [{"fact_id": "F01"}]
                    },
                },
            }
        ],
        candidate_prompt="候选草稿 Prompt",
        candidate_prompt_version="draft-v1-candidate",
    )

    assert result["failed_samples"] == [
        {
            "id": "sample-1",
            "stage": "candidate",
            "reason": "候选结构不合格",
        }
    ]
