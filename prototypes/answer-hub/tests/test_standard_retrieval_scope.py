import json
import socket
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from answer_hub import audit, auto_review, cz_integration, workflow
from answer_hub.cz_integration import CzIntegrationConfig
from answer_hub.mimo import MimoConfig, MimoLabelResult


STANDARD_ID = "TEST-HQ-EARPHONE"
STANDARD_BODY = "耳机无法插入耳机接口时，按耳机接口功能异常处理。"
SIBLING_MODEL = "2019款16寸MacBook Pro"


def _result(candidate):
    return MimoLabelResult(candidate=candidate, request_audit={}, response_audit={})


@pytest.fixture
def local_standard_run(tmp_path, monkeypatch):
    requests = []
    model_inputs = []

    def reject_network(*args, **kwargs):
        pytest.fail("标准检索范围测试不得访问网络")

    def reject_dotenv(*args, **kwargs):
        pytest.fail("标准检索范围测试不得读取 dotenv")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    for module in (audit, auto_review, cz_integration):
        monkeypatch.setattr(module, "load_dotenv", reject_dotenv)
    monkeypatch.setattr(
        audit.AuditStore, "from_env",
        classmethod(lambda cls, path=None: cls(path)),
    )
    monkeypatch.setattr(
        auto_review.AutoReviewPolicy, "from_env",
        classmethod(lambda cls: cls()),
    )
    monkeypatch.setattr(
        CzIntegrationConfig, "from_env",
        classmethod(lambda cls: cls(
            "https://cz.invalid", "", retrieval_key="offline-test-only",
            max_retries=0,
        )),
    )

    def respond_to_cz(request, **kwargs):
        assert request.get_method() == "POST"
        assert urlparse(request.full_url).path == "/api/v1/integration/standard-search"
        requests.append(json.loads(request.data.decode("utf-8")))
        return BytesIO(json.dumps({
            "status": "success",
            "knowledgeVersion": "test-published-v1",
            "retrievalMode": "semantic",
            "candidates": [{
                "id": STANDARD_ID, "title": "耳机接口功能异常",
                "text": STANDARD_BODY, "status": "published",
                "knowledgeOrigin": "headquarters_standard",
                "businessType": "self_operated", "productType": "笔记本",
                "score": 0.98, "finalScore": 0.98,
                "keywords": ["耳机接口", "无法插入"],
            }],
        }, ensure_ascii=False).encode("utf-8"))

    monkeypatch.setattr(cz_integration, "urlopen", respond_to_cz)

    def analyze_topic_signal(row, matches, images, **kwargs):
        return _result({
            "intent": "故障判定", "subject": "耳机接口",
            "phenomenon": "耳机无法插入", "resolution_mode": "按耳机接口功能异常处理",
            "confidence": 0.98, "requires_images": False,
        })

    def classify_topic_stage(topic):
        return _result({
            "topic_stage": "质检标准", "knowledge_value": "值得沉淀",
            "stage_reason": "已核验当前原子问题的故障答复。",
            "value_reason": "包含明确的功能异常处理结论。",
            "reusable_knowledge": STANDARD_BODY, "confidence": 0.98,
            "needs_human_review": False,
        })

    def label_topic(topic, matches, use_standard_references=True):
        model_inputs.append((topic, matches))
        return _result({
            "title": "笔记本耳机无法插入如何判定", "subtitles": [],
            "content": STANDARD_BODY, "recommended_reply": STANDARD_BODY,
            "knowledge_form": "具体判定", "content_type": "定义型",
            "applicable_scope": "笔记本", "applicable_brands": [],
            "applicable_models": [], "standard_refs": [STANDARD_ID],
            "confidence": 0.98, "needs_human_review": True,
        })

    client = SimpleNamespace(
        config=MimoConfig("offline-test-only", "https://model.invalid", "test"),
        analyze_topic_signal=analyze_topic_signal,
        classify_topic_stage=classify_topic_stage,
        label_topic=label_topic,
        metrics_snapshot=lambda: {},
    )
    monkeypatch.setattr(
        workflow.MimoClient, "from_env", classmethod(lambda cls: client),
    )

    def run(current_model="", answered=True):
        question = f"笔记本｜耳机接口｜{current_model}耳机无法插入"
        row = {
            "数据ID": "700001", "工单ID": "700001", "产品类型": "笔记本",
            "核心问题": question, "判定结论": STANDARD_BODY if answered else "",
            "对象/部位": "耳机接口", "异常现象": "耳机无法插入",
            "适用机型": SIBLING_MODEL, "机型": SIBLING_MODEL,
            "_原子机型范围": current_model or "通用",
            "_原子知识ID": "700001-U2", "_原子来源问题数": 2,
            "_原子问答配对状态": "paired",
            "_原子实际回复": STANDARD_BODY if answered else "",
            "聊天内容": (
                f"回收师：{SIBLING_MODEL}耳机孔规格是多少\n答疑：3.5的\n"
                f"回收师：另一台{current_model}笔记本耳机无法插入\n"
                + (f"答疑：{STANDARD_BODY}" if answered else "答疑：稍等")
            ),
        }
        source = tmp_path / "source.xlsx"
        workflow.write_rows_to_workbook({"source": (list(row), [row])}, source)
        summary = workflow.initial_label_from_workbook(
            source, None, tmp_path / "output",
            use_mimo=True, audit_db_path=tmp_path / "audit.db",
            clustering_mode="rule", use_standard_references=True,
            enforce_cluster_admission=False,
        )
        _, topics = workflow.read_workbook_rows(
            summary["topic_review_file"], sheet_name="topic_review_queue",
        )
        return requests, model_inputs, topics, summary

    return run


@pytest.mark.parametrize("current_model", ["", "ThinkPad T14"])
def test_workbook_standard_request_uses_only_current_atomic_model(
    local_standard_run, current_model,
):
    requests, model_inputs, topics, summary = local_standard_run(current_model)

    assert requests, "公开工作簿流程必须实际走到 CZ 请求边界"
    assert all(request["model"] == current_model for request in requests)
    assert all(request["orderInfo"]["model"] == current_model for request in requests)
    assert SIBLING_MODEL not in json.dumps(requests, ensure_ascii=False)
    assert model_inputs and topics
    assert summary["cz_headquarters_standard_retrieval_enabled"] is True


def test_workbook_passes_published_headquarters_body_to_transcription(
    local_standard_run,
):
    requests, model_inputs, topics, _ = local_standard_run()

    assert requests and model_inputs and topics
    for topic, matches in model_inputs:
        assert topic["标准依据来源"] == "headquarters_standard"
        assert len(matches) == 1
        standard, _score = matches[0]
        assert standard.standard_id == STANDARD_ID
        assert standard.knowledge_type == "总部标准"
        assert standard.response_snippet == STANDARD_BODY
        assert standard.status == "published"
        assert standard.version == "test-published-v1"


def test_workbook_does_not_use_standard_or_sibling_answer_for_unanswered_atom(
    local_standard_run,
):
    requests, model_inputs, topics, summary = local_standard_run(answered=False)

    assert summary["cz_headquarters_standard_retrieval_enabled"] is True
    assert summary["evidence_gap_rows"] >= 1
    assert not requests
    assert not model_inputs
    assert not topics
