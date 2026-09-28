from __future__ import annotations

import json
from pathlib import Path

from answer_hub.mimo import MimoConfig
from answer_hub.automation_api import create_automation_api_app


def test_mimo_config_reads_group_model_from_local_json_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "local-model.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "group-internal",
                "base_url": "http://llm.intra.example/v1",
                "model": "group-qwen3",
                "media_model": "group-qwen3-vl",
                "api_key_env": "GROUP_LLM_API_KEY",
                "timeout_seconds": 45,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANSWER_HUB_LOCAL_MODEL_CONFIG", str(config_path))
    monkeypatch.setenv("GROUP_LLM_API_KEY", "test-group-key")
    for name in (
        "MIMO_API_KEY",
        "MIMO_API_KEYS",
        "MIMO_BASE_URL",
        "MIMO_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    config = MimoConfig.from_env()

    assert config is not None
    assert config.base_url == "http://llm.intra.example/v1"
    assert config.model == "group-qwen3"
    assert config.media_model == "group-qwen3-vl"
    assert config.api_key == "test-group-key"
    assert config.timeout_seconds == 45


def test_local_model_config_requires_api_key_from_declared_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "local-model.json"
    config_path.write_text(
        json.dumps(
            {
                "base_url": "http://llm.intra.example/v1",
                "model": "group-qwen3",
                "api_key_env": "GROUP_LLM_API_KEY",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANSWER_HUB_LOCAL_MODEL_CONFIG", str(config_path))
    monkeypatch.delenv("GROUP_LLM_API_KEY", raising=False)
    monkeypatch.setenv("MIMO_API_KEY", "old-personal-key")
    monkeypatch.setenv("MIMO_BASE_URL", "https://personal.example/v1")
    monkeypatch.setenv("MIMO_MODEL", "old-personal-model")
    assert MimoConfig.from_env() is None


def test_local_model_config_can_be_updated_through_authenticated_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "local-model.json"
    monkeypatch.setenv("ANSWER_HUB_LOCAL_MODEL_CONFIG", str(config_path))
    monkeypatch.setenv("GROUP_LLM_API_KEY", "test-group-key")
    app = create_automation_api_app(
        api_key="test-answer-hub-key",
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
    )
    client = app.test_client()

    response = client.put(
        "/api/v1/local/model-config",
        headers={"X-Answer-Hub-Key": "test-answer-hub-key"},
        json={
            "provider": "group-internal",
            "base_url": "http://llm.intra.example/v1",
            "model": "group-qwen3-next",
            "media_model": "group-qwen3-vl-next",
            "api_key_env": "GROUP_LLM_API_KEY",
            "timeout_seconds": 50,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "updated"
    assert payload["config"]["model"] == "group-qwen3-next"
    assert payload["config"]["api_key_configured"] is True
    assert "test-group-key" not in response.get_data(as_text=True)
    assert json.loads(config_path.read_text(encoding="utf-8"))["model"] == (
        "group-qwen3-next"
    )

    loaded = MimoConfig.from_env()
    assert loaded is not None
    assert loaded.model == "group-qwen3-next"
