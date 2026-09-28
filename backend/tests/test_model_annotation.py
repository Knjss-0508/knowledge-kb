from types import SimpleNamespace

import pytest

from app.services import model_annotation


def test_correction_requires_internal_endpoint(monkeypatch):
    monkeypatch.setattr(
        model_annotation,
        "settings",
        SimpleNamespace(DEEPSEEK_BASE_URL="", DEEPSEEK_API_KEY="", GROUP_LLM_BASE_URL="", GROUP_LLM_API_KEY="", DEEPSEEK_MODEL="deepseek-flash", DEEPSEEK_TIMEOUT_SECONDS=5, DEEPSEEK_PROMPT_VERSION="v1"),
    )
    with pytest.raises(model_annotation.ModelAnnotationError, match="尚未配置"):
        model_annotation.correct_with_deepseek_flash({"human_truth": {"knowledge_value": "worthy"}})


def test_correction_preserves_structured_model_result(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"knowledge_value":"worthy","confidence":0.97}'}}]}

    calls = []
    monkeypatch.setattr(
        model_annotation,
        "settings",
        SimpleNamespace(DEEPSEEK_BASE_URL="http://internal/v1", DEEPSEEK_API_KEY="secret", GROUP_LLM_BASE_URL="", GROUP_LLM_API_KEY="", DEEPSEEK_MODEL="deepseek-flash", DEEPSEEK_TIMEOUT_SECONDS=5, DEEPSEEK_PROMPT_VERSION="v1"),
    )
    monkeypatch.setattr(model_annotation.httpx, "post", lambda *args, **kwargs: (calls.append((args, kwargs)) or Response()))
    result = model_annotation.correct_with_deepseek_flash({"human_truth": {"knowledge_value": "worthy"}})
    assert result["knowledge_value"] == "worthy"
    assert result["model_name"] == "deepseek-flash"
    assert calls[0][0][0] == "http://internal/v1/chat/completions"


def test_correction_reuses_group_model_configuration(monkeypatch):
    class Response:
        def raise_for_status(self): return None
        def json(self): return {"model": "deepseek-flash-internal", "choices": [{"message": {"content": '{"knowledge_value":"worthy"}'}}]}

    monkeypatch.setattr(
        model_annotation,
        "settings",
        SimpleNamespace(DEEPSEEK_BASE_URL="", DEEPSEEK_API_KEY="", GROUP_LLM_BASE_URL="http://group/v1", GROUP_LLM_API_KEY="group-secret", DEEPSEEK_MODEL="deepseek-flash", DEEPSEEK_TIMEOUT_SECONDS=5, DEEPSEEK_PROMPT_VERSION="v1"),
    )
    monkeypatch.setattr(model_annotation.httpx, "post", lambda *args, **kwargs: Response())
    result = model_annotation.correct_with_deepseek_flash({"human_truth": {"knowledge_value": "worthy"}})
    assert result["resolved_model_version"] == "deepseek-flash-internal"


def test_connection_test_uses_fixed_non_business_prompt(monkeypatch):
    captured = {}
    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"status": "ok"}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    assert model_annotation.test_deepseek_flash_connection()["status"] == "ok"
    assert captured == {"prompt": '请只返回 JSON：{"status":"ok"}', "purpose": "连接测试"}


def test_draft_annotation_includes_draft_and_evidence(monkeypatch):
    captured = {}
    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"knowledge_value": "worthy"}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    model_annotation.annotate_transcribed_candidate({"draft": {"title": "草稿标题", "content": {"blocks": []}}, "evidence_excerpt": "证据摘要"})
    assert captured["purpose"] == "草稿标注"
    assert "草稿标题" in captured["prompt"]
    assert "证据摘要" in captured["prompt"]


def test_prompt_optimization_analysis_uses_shadow_snapshot(monkeypatch):
    captured = {}
    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"candidate_prompt": "候选提示词"}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    result = model_annotation.analyze_confidence_training_prompt({"evaluation_scope": "shadow_only"})
    assert result["candidate_prompt"] == "候选提示词"
    assert captured["purpose"] == "提示词优化分析"
    assert "shadow_only" in captured["prompt"]


def test_shadow_rerun_uses_requested_prompt_version(monkeypatch):
    captured = {}
    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"knowledge_value": "worthy", "draft_disposition": "approved"}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    result = model_annotation.shadow_rerun_transcribed_candidate(
        review_prompt="候选规则",
        candidate={"title": "样本"},
        prompt_version="candidate-v1",
    )
    assert result["prompt_version"] == "candidate-v1"
    assert captured["purpose"] == "候选提示词影子复跑"
    assert "候选规则" in captured["prompt"]


def test_http_error_keeps_retry_classification(monkeypatch):
    class Response:
        status_code = 429
        def json(self): return {"error": "rate limited"}
    monkeypatch.setattr(
        model_annotation,
        "settings",
        SimpleNamespace(DEEPSEEK_BASE_URL="http://internal/v1", DEEPSEEK_API_KEY="secret", GROUP_LLM_BASE_URL="", GROUP_LLM_API_KEY="", DEEPSEEK_MODEL="deepseek-flash", DEEPSEEK_TIMEOUT_SECONDS=5, DEEPSEEK_PROMPT_VERSION="v1"),
    )
    monkeypatch.setattr(model_annotation.httpx, "post", lambda *args, **kwargs: Response())
    with pytest.raises(model_annotation.ModelAnnotationError) as error:
        model_annotation.correct_with_deepseek_flash({"human_truth": {"knowledge_value": "worthy"}})
    assert error.value.error_code == "HTTP_429"
    assert error.value.retryable is True
