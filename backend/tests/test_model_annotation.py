from types import SimpleNamespace

import pytest

from app.services import model_annotation, model_connection


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


def test_draft_revision_returns_revised_content_without_overwriting_input(monkeypatch):
    captured = {}
    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"revised_content": "修改后的正文", "revised_recommended_reply": "修改后的推荐回复", "change_summary": ["收紧结论范围"]}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    original = {"draft": {"content": "原正文", "recommended_reply": "原回复"}, "evidence_excerpt": "证据"}
    result = model_annotation.revise_transcribed_candidate_draft(original)
    assert result["revised_content"] == "修改后的正文"
    assert original["draft"]["content"] == "原正文"
    assert captured["purpose"] == "草稿修订"
    assert "不得补造" in captured["prompt"]


def test_draft_prompt_optimization_uses_human_edit_snapshot(monkeypatch):
    captured = {}
    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"candidate_prompt": "候选草稿 Prompt"}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    result = model_annotation.analyze_draft_generation_prompt({"representative_edits": [{"before": "旧", "after": "新"}]})
    assert result["candidate_prompt"] == "候选草稿 Prompt"
    assert captured["purpose"] == "知识草稿提示词优化分析"
    assert "人工最终稿" in captured["prompt"]


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


def test_connection_test_passes_unsaved_dialog_values_and_reports_the_route(monkeypatch):
    captured = {}

    def fake_call(prompt, *, purpose, config=None):
        captured.update(prompt=prompt, purpose=purpose, config=config)
        return {"status": "ok", "requested_model": "deepseek-flash"}

    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    result = model_annotation.test_deepseek_flash_connection(
        {"base_url": "http://form/v1", "api_key": "sk-form-secret-9999", "model": "form-model"}
    )
    assert captured["purpose"] == "连接测试"
    assert captured["config"]["base_url"] == "http://form/v1"
    assert result["status"] == "ok"
    assert result["base_url"] == "http://form/v1"
    assert result["model"] == "form-model"
    assert isinstance(result["latency_ms"], int)
    assert result["api_key_masked"].startswith("sk-f")
    assert "sk-form-secret-9999" not in str(result)


def test_connection_test_without_a_dialog_body_keeps_the_plain_call(monkeypatch):
    captured = {}

    def fake_call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {"status": "ok"}

    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", fake_call)
    assert model_annotation.test_deepseek_flash_connection()["status"] == "ok"
    assert "config" not in captured


def test_saved_model_override_is_used_by_model_calls(monkeypatch):
    calls = []

    class Response:
        def json(self):
            return {"choices": [{"message": {"content": '{"knowledge_value":"worthy"}'}}]}

    monkeypatch.setattr(
        model_annotation,
        "settings",
        SimpleNamespace(DEEPSEEK_BASE_URL="", DEEPSEEK_API_KEY="", GROUP_LLM_BASE_URL="", GROUP_LLM_API_KEY="", DEEPSEEK_MODEL="deepseek-flash", DEEPSEEK_TIMEOUT_SECONDS=5, DEEPSEEK_PROMPT_VERSION="v1"),
    )
    monkeypatch.setattr(
        model_connection,
        "_CACHED_OVERRIDES",
        {
            "base_url": "http://saved/v1",
            "api_key": "sk-saved",
            "model": "saved-model",
            "timeout_seconds": 21,
        },
    )
    monkeypatch.setattr(
        model_annotation.httpx,
        "post",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or Response()),
    )
    result = model_annotation.correct_with_deepseek_flash({"human_truth": {"knowledge_value": "worthy"}})
    assert calls[0][0][0] == "http://saved/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sk-saved"
    assert calls[0][1]["json"]["model"] == "saved-model"
    assert calls[0][1]["timeout"] == 21
    assert result["model_name"] == "saved-model"


def test_explicit_call_config_wins_over_the_saved_override(monkeypatch):
    calls = []

    class Response:
        def json(self):
            return {"choices": [{"message": {"content": '{"status":"ok"}'}}]}

    monkeypatch.setattr(
        model_annotation,
        "settings",
        SimpleNamespace(DEEPSEEK_BASE_URL="", DEEPSEEK_API_KEY="", GROUP_LLM_BASE_URL="", GROUP_LLM_API_KEY="", DEEPSEEK_MODEL="deepseek-flash", DEEPSEEK_TIMEOUT_SECONDS=5, DEEPSEEK_PROMPT_VERSION="v1"),
    )
    monkeypatch.setattr(
        model_connection,
        "_CACHED_OVERRIDES",
        {"base_url": "http://saved/v1", "api_key": "sk-saved"},
    )
    monkeypatch.setattr(
        model_annotation.httpx,
        "post",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or Response()),
    )
    result = model_annotation._call_deepseek_flash(
        "只返回 JSON",
        purpose="连接测试",
        config={"base_url": "http://form/v1", "api_key": "sk-form"},
    )
    assert calls[0][0][0] == "http://form/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sk-form"
    assert result["prompt_version"] == "v1"
