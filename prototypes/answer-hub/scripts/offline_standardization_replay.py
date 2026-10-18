"""Replay workbook plumbing with real Prompt/JSON validation and fake model HTTP.

This is a contract test, not a model-quality evaluation. No credentials or
remote media are loaded. Outputs belong to an isolated, newly created directory.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
from unittest.mock import patch

from answer_hub.excel_io import read_workbook_rows
from answer_hub.images import ImageEvidence, split_image_urls
from answer_hub.mimo import MimoClient, MimoConfig
from answer_hub.workflow import (
    _fallback_topic_signal,
    initial_label_from_workbook,
    preprocess_source_rows,
)


def _prompt_input(prompt: str, marker: str):
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1].lstrip())[0]


class OfflineModelTransport:
    """Return source-derived fixtures at the HTTP boundary of the real client."""

    def __init__(self, rows: list[dict]) -> None:
        self.by_ticket = {str(row["工单ID"]): row for row in rows}
        self.calls: Counter = Counter()
        self.prompt_checks: Counter = Counter()

    def _signal(self, row: dict) -> dict:
        signal = _fallback_topic_signal(row, [], "隔离回放未下载媒体", use_standard_references=False)
        signal["intent"] = {
            "边界判定": "标准判定", "痕迹核验": "检测核验",
            "功能核验": "检测核验", "异常核验": "检测核验",
        }.get(signal["intent"], signal["intent"])
        if signal["intent"] not in {"标准判定", "检测核验", "信息查询", "流程操作", "其他待确认"}:
            signal["intent"] = "其他待确认"
        allowed_categories = {
            "基本情况", "成色与回收标准", "外观问题", "显示问题",
            "功能问题", "拆修问题", "信息查询", "流程操作", "其他待确认",
        }
        if signal["category_l1"] not in allowed_categories:
            signal["category_l1"] = "其他待确认"
        signal["category_l2"] = signal["category_l2"] or "其他待确认"
        text = " ".join(str(row.get(field) or "") for field in ("核心问题", "判定结论"))
        if any(word in text for word in (
            "色斑", "颜色异常", "偏绿", "偏蓝", "闪屏", "闪烁", "亮线",
            "绿线", "坏点", "漏液", "显示异常",
        )) and not any(word in text for word in ("拆修", "维修", "更换", "非原装", "第三方", "弹窗")):
            signal["category_l1"] = "显示问题"
        signal.update(
            confidence=0.95,
            needs_human_review=(
                signal["intent"] == "其他待确认"
                or signal["category_l1"] == "其他待确认"
                or signal["category_l2"] == "其他待确认"
                or row["产品类型"] == "待确认"
            ),
        )
        signal["topic_tags"] = [
            "意图:" + signal["intent"], "对象:" + signal["subject"],
            "现象:" + signal["phenomenon"],
        ]
        return signal

    def _atomic(self, source: dict) -> dict:
        row = self.by_ticket[source["work_order_id"]]
        signal = self._signal(row)
        issue = str(row.get("核心问题") or "待确认问题")[:80]
        topic = {
            "normalized_issue": issue,
            "standard_question": issue,
            "product_category": row["产品类型"],
            "scope_type": "品类专用",
            "platform": "通用", "brand": "通用", "model_scope": "通用",
            "category_l1": signal["category_l1"],
            "category_l2": signal["category_l2"],
            "intent": signal["intent"],
            "subject": signal["subject"],
            "phenomenon": signal["phenomenon"],
            "judgment_target": issue,
            "resolution_mode": signal["resolution_mode"],
            "standard_path": "无明确标准路径",
            "threshold_or_exception": "无明确阈值",
            "evidence_summary": str(row.get("判定结论") or issue)[:300],
            "confidence": 0.95,
            "requires_review": (
                row["产品类型"] == "待确认"
                or signal["intent"] == "其他待确认"
                or signal["category_l1"] == "其他待确认"
            ),
        }
        return {
            "conversation_type": "single_topic",
            "reason": "隔离回放固定单主题；不评估真实拆分质量。",
            "topics": [topic],
        }

    def __call__(self, payload: dict) -> dict:
        system = payload["messages"][0]["content"]
        content = payload["messages"][1]["content"]
        prompt = content if isinstance(content, str) else content[0]["text"]
        assert "人工标题" not in prompt
        self.calls[system] += 1
        if "会话语义标注助手" in system:
            source = _prompt_input(prompt, "【会话输入】")["primary_evidence"]
            row = self.by_ticket[source["work_order_id"]]
            candidate = self._signal(row)
        elif "问题单元批量提取器" in system:
            records = _prompt_input(prompt, "【输入】")["records"]
            assert "standard_question" in prompt
            self.prompt_checks["atomic_standard_question"] += 1
            candidate = {"results": [
                {"record_id": record["record_id"], **self._atomic(record)}
                for record in records
            ]}
        elif "问题单元提取器" in system:
            assert "standard_question" in prompt
            self.prompt_checks["atomic_standard_question"] += 1
            candidate = self._atomic(_prompt_input(prompt, "【输入】"))
        elif "硬业务规则的知识主题聚类专家" in system:
            units = _prompt_input(prompt, "【待聚类原子知识点】")
            assert "标准问题相同只是值得比较的辅助信号" in prompt
            assert all("standardization_version" in unit for unit in units)
            self.prompt_checks["evidence_final_decision"] += 1
            groups = defaultdict(list)
            for unit in units:
                # Exact fixture groups deliberately make no semantic-quality claim.
                groups[(unit["product_category"], unit["normalized_issue"])].append(unit)
            candidate = {
                "clusters": [{
                    "cluster_id": f"C{index:03d}",
                    "theme_name": group[0]["normalized_issue"],
                    "member_atomic_ids": [unit["atomic_id"] for unit in group],
                    "shared_knowledge_definition": group[0]["normalized_issue"],
                    "merge_basis": "仅按同品类完全相同问题构造测试夹具。",
                    "scope_consistent": True, "object_consistent": True,
                    "judgment_target_consistent": True,
                    "standard_path_consistent": True,
                    "threshold_exception_consistent": True,
                    "confidence": 0.95, "requires_review": False,
                } for index, group in enumerate(groups.values(), 1)],
                "split_requests": [], "review_requests": [],
            }
        elif "环节与知识沉淀价值" in system:
            candidate = {
                "topic_stage": "案例解析", "knowledge_value": "值得沉淀",
                "stage_reason": "隔离夹具覆盖分类状态流。",
                "value_reason": "仅用于测试转写调用，实际价值仍由现有门禁复核。",
                "reusable_knowledge": "仅复述来源记录，不能外推适用范围。",
                "confidence": 0.95, "needs_human_review": True,
            }
        elif "主题知识标注助手" in system:
            topic = _prompt_input(prompt, "【主题输入】")
            facts = topic["evidence_package"]["representative_facts"]
            fact = facts[0]
            features = topic["features"]
            title = str(fact.get("human_core_problem") or fact["atomic_question"])[:100]
            title = re.sub(r"[｜|/／;；、]+", " ", title).strip("：:，, ")
            text = str(
                fact.get("human_judgment_conclusion")
                or fact.get("historical_actual_reply")
                or fact["atomic_question"]
            )[:700]
            candidate = {
                "title": title, "subtitles": [], "content": "1. " + text,
                "content_type": "定义型", "category_l1": "其他问题",
                "category_l2": "案例解析", "layer": "L2",
                "knowledge_form": "具体判定", "standard_refs": [],
                "applicable_scope": features["产品类型"],
                "applicable_brands": [], "applicable_models": [],
                "recommended_reply": str(fact.get("historical_actual_reply") or text),
                "confidence": 0.9, "reasoning_summary": "只复制来源事实的测试夹具。",
                "needs_human_review": True, "requires_images": False,
                "image_evidence_summary": "媒体未下载，不声称可见事实。",
            }
        elif "主题知识初审员" in system:
            candidate = {
                "decision": "需修改", "knowledge_value": "值得沉淀",
                "error_type": "隔离测试待人工复核",
                "reason": "mock 输出不代表真实模型结论。",
                "standard_consistency": "无可信标准",
                "evidence_sufficiency": "部分充分", "content_consistency": "部分一致",
                "image_necessity": "不需要", "title_quality": "清晰",
                "confidence": 0.9, "priority_review": True,
            }
        elif "知识主题聚类标注员" in system:
            candidate = {
                "decision": "不同主题", "topic_label": "隔离回放",
                "reason": "没有真实模型证据，不做额外语义合并。",
                "key_difference": "未评估语义关系", "confidence": 0.95,
            }
        else:
            raise AssertionError(f"未覆盖的模型调用：{system}")
        return {"choices": [{"message": {"content": json.dumps(candidate, ensure_ascii=False)}}]}


def run_replay(source: Path, output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    _, raw_rows = read_workbook_rows(source)
    rows = preprocess_source_rows(raw_rows)
    transport = OfflineModelTransport(rows)
    model = MimoClient(MimoConfig(
        api_key="offline-test-placeholder", base_url="https://example.invalid/v1",
        model="deepseek-mock-contract", cluster_media_policy="never",
    ))
    model._post = transport

    def no_network(*_args, **_kwargs):
        raise AssertionError("隔离回放禁止网络访问")

    def unavailable_images(_downloader, links):
        return [ImageEvidence(url=url, status="failed", error="隔离回放未下载")
                for url in split_image_urls(links)]

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "KB_BASE_URL": "", "KB_INTEGRATION_KEY": "",
            "AUTO_REVIEW_ENABLED": "false", "ANSWER_HUB_DIRECT_ATOMIC_BATCH_SIZE": "4",
        }, clear=True))
        for module in ("answer_hub.mimo", "answer_hub.audit",
                       "answer_hub.auto_review", "answer_hub.cz_integration"):
            stack.enter_context(patch(module + ".load_dotenv", return_value=None))
        stack.enter_context(patch("socket.create_connection", side_effect=no_network))
        stack.enter_context(patch("socket.socket.connect", side_effect=no_network))
        stack.enter_context(patch("answer_hub.workflow.MimoClient.from_env", return_value=model))
        stack.enter_context(patch("answer_hub.images.ImageDownloader.fetch", unavailable_images))
        summary = initial_label_from_workbook(
            source, None, output, use_mimo=True, clustering_mode="direct_mimo",
            use_standard_references=False, cluster_media_policy="never",
            audit_db_path=output / "audit.db",
        )
    _, topics = read_workbook_rows(output / "topic_review_queue.xlsx", "topic_review_queue")
    _, formal_candidates = read_workbook_rows(output / "candidate_knowledge.xlsx", "候选知识")
    media = [item for topic in topics for item in json.loads(topic.get("媒体证据包") or "[]")]
    report = {
        "verification_type": "offline_mock_contract_not_real_model_quality",
        "input_count": len(raw_rows),
        "manual_title_used": False, "real_network_calls": 0,
        "atomic_count": summary.get("atomic_unit_count"),
        "topic_count": len(topics),
        "singleton_count": sum(int(topic.get("主题样本数") or 0) == 1 for topic in topics),
        "admitted_topics": summary.get("cluster_admission_admitted_topics"),
        "pending_topics": summary.get("cluster_admission_pending_topics"),
        "provisional_topics": summary.get("cluster_admission_provisional_topics"),
        "standardization_ready": summary.get("question_standardization_ready"),
        "standardization_needs_review": summary.get("question_standardization_needs_review"),
        "transcription_statuses": dict(Counter(topic.get("主题转写状态") for topic in topics)),
        "transcription_validation_blocked_count": sum(
            topic.get("主题转写状态") == "topic_model_validation_failed" for topic in topics
        ),
        "transcription_schema_failures": sum(
            topic.get("主题转写状态") == "topic_model_validation_failed"
            and "JSON 校验失败" in str(topic.get("模型初标原因") or "")
            for topic in topics
        ),
        "no_standard_reference_blocked_count": sum(
            "无标准引用模式检测到" in str(topic.get("模型初标原因") or "")
            for topic in topics
        ),
        "review_decisions": dict(Counter(topic.get("模型初标结论") for topic in topics)),
        "admission_statuses": dict(Counter(topic.get("聚类准入状态") for topic in topics)),
        "semantic_model_rows": summary.get("topic_signal_labeled_rows"),
        "semantic_fallback_rows": summary.get("topic_signal_fallback_rows"),
        "manual_reply_review_rows": sum(
            topic.get("推荐回复来源类型") == "human_review_required" for topic in topics
        ),
        "reply_nonempty": sum(bool(topic.get("推荐回复")) for topic in topics),
        "reply_sources": dict(Counter(topic.get("推荐回复来源类型") or "未提供" for topic in topics)),
        "duplicate_title_extras": sum(count - 1 for count in Counter(
            topic.get("主标题") for topic in topics if topic.get("主标题")
        ).values()),
        "media_risk_count": sum(bool(item.get("审计风险")) for item in media),
        "model_fixture_calls": dict(transport.calls),
        "prompt_checks": dict(transport.prompt_checks),
        "atomic_failures": summary.get("atomic_extraction_failed"),
        "cluster_failures": summary.get("direct_cluster_failed"),
        "candidate_artifact_exists": (output / "candidate_knowledge.xlsx").is_file(),
        "formal_candidate_count": len(formal_candidates),
        "transcription_blocked_count": sum(
            topic.get("知识草稿状态") == "blocked" for topic in topics
        ),
        "review_skipped_count": sum(
            str(topic.get("模型初标状态") or "").startswith("topic_initial_review_skipped")
            for topic in topics
        ),
    }
    (output / "offline_replay_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    assert summary["eligible_rows"] == len(rows)
    assert summary["clustering_effective_mode"] == "direct_mimo"
    assert transport.prompt_checks["atomic_standard_question"] > 0
    assert transport.prompt_checks["evidence_final_decision"] > 0
    assert not summary.get("atomic_extraction_failed")
    assert not summary.get("direct_cluster_failed")
    assert summary["question_standardization_version"]
    assert report["candidate_artifact_exists"]
    assert report["transcription_schema_failures"] == 0
    assert report["transcription_validation_blocked_count"] == report["no_standard_reference_blocked_count"]
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_replay(args.source, args.output), ensure_ascii=False, indent=2))
