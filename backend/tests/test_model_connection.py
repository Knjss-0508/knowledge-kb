from types import SimpleNamespace

import pytest

from app.models.integration import ConfidenceTrainingSettingsRecord
from app.routes import confidence_training as confidence_training_routes
from app.schemas.confidence_training import ModelConnectionTest
from app.services import model_connection
from app.services.model_connection import (
    MODEL_CONNECTION_RECORD_ID,
    ModelConnectionError,
    mask_api_key,
    model_config_for_test,
    model_config_snapshot,
    update_model_config,
)


def _settings(**overrides):
    values = {
        "DEEPSEEK_BASE_URL": "",
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_TIMEOUT_SECONDS": 60.0,
        "DEEPSEEK_PROMPT_VERSION": "v1",
        "GROUP_LLM_BASE_URL": "",
        "GROUP_LLM_API_KEY": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeQuery:
    def __init__(self, db):
        self.db = db

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.db.record


class _FakeDb:
    """Only the session surface the model-connection service relies on."""

    def __init__(self):
        self.record = None

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self)

    def add(self, record):
        self.record = record

    def commit(self):
        return None

    def refresh(self, _record):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _isolate_model_connection_state(monkeypatch):
    monkeypatch.setattr(model_connection, "_CACHED_OVERRIDES", None)
    monkeypatch.setattr(model_connection, "settings", _settings())
    yield


def test_mask_api_key_never_reveals_the_full_key():
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "•" * 5
    masked = mask_api_key("sk-1234567890abcdef")
    assert masked != "sk-1234567890abcdef"
    assert masked.startswith("sk-1")
    assert masked.endswith("cdef")
    assert "4567890" not in masked


def test_environment_config_falls_back_to_the_group_llm_route(monkeypatch):
    monkeypatch.setattr(
        model_connection,
        "settings",
        _settings(
            GROUP_LLM_BASE_URL="http://group/v1",
            GROUP_LLM_API_KEY="group-secret",
        ),
    )
    env = model_connection.environment_model_config()
    assert env["base_url"] == "http://group/v1"
    assert env["api_key"] == "group-secret"
    snapshot = model_config_snapshot(None)
    assert snapshot["source"] == "environment"
    assert snapshot["configured"] is True
    assert snapshot["api_key_source"] == "environment"
    assert snapshot["api_key_masked"] == mask_api_key("group-secret")
    assert snapshot["overrides"] == {
        "base_url": False,
        "model": False,
        "timeout_seconds": False,
        "api_key": False,
    }


def test_update_model_config_stores_the_override_and_masks_the_key(monkeypatch):
    monkeypatch.setattr(
        model_connection,
        "settings",
        _settings(DEEPSEEK_BASE_URL="http://env/v1", DEEPSEEK_API_KEY="env-secret"),
    )
    db = _FakeDb()
    snapshot = update_model_config(
        db,
        {
            "base_url": "http://saved/v1/",
            "model": "deepseek-flash-internal",
            "timeout_seconds": 12,
            "api_key": "sk-saved-secret-1234",
        },
        updated_by="tester",
    )
    assert isinstance(db.record, ConfidenceTrainingSettingsRecord)
    assert db.record.id == MODEL_CONNECTION_RECORD_ID
    assert db.record.settings["api_key"] == "sk-saved-secret-1234"
    assert snapshot["source"] == "database"
    assert snapshot["base_url"] == "http://saved/v1"
    assert snapshot["model"] == "deepseek-flash-internal"
    assert snapshot["timeout_seconds"] == 12.0
    assert snapshot["api_key_source"] == "database"
    assert "sk-saved-secret-1234" not in str(snapshot)
    assert snapshot["overrides"]["api_key"] is True
    assert snapshot["updated_by"] == "tester"
    # The saved override is applied to live model calls without a restart.
    assert model_connection.cached_model_config()["base_url"] == "http://saved/v1"


def test_empty_values_keep_the_previous_route_and_clear_api_key(monkeypatch):
    monkeypatch.setattr(
        model_connection,
        "settings",
        _settings(DEEPSEEK_BASE_URL="http://env/v1", DEEPSEEK_API_KEY="env-secret"),
    )
    db = _FakeDb()
    update_model_config(
        db,
        {"base_url": "http://saved/v1", "model": "saved-model", "api_key": "sk-saved"},
        updated_by="tester",
    )
    kept = update_model_config(db, {"base_url": None, "api_key": ""}, updated_by="tester")
    assert kept["base_url"] == "http://saved/v1"
    assert kept["model"] == "saved-model"
    assert kept["api_key_source"] == "database"

    # An explicitly emptied field drops only that override and falls back.
    dropped_model = update_model_config(db, {"model": ""}, updated_by="tester")
    assert dropped_model["model"] == "deepseek-flash"
    assert dropped_model["base_url"] == "http://saved/v1"

    cleared = update_model_config(db, {"clear_api_key": True}, updated_by="tester")
    assert cleared["api_key_source"] == "environment"
    assert cleared["api_key_masked"] == mask_api_key("env-secret")
    assert "api_key" not in db.record.settings

    reset = update_model_config(db, {"reset_to_environment": True}, updated_by="tester")
    assert reset["source"] == "environment"
    assert reset["base_url"] == "http://env/v1"
    assert db.record.settings == {}
    assert model_connection.cached_model_config() is None


def test_invalid_base_url_and_timeout_are_rejected():
    db = _FakeDb()
    with pytest.raises(ModelConnectionError) as invalid_url:
        update_model_config(db, {"base_url": "internal:8080/v1"}, updated_by="tester")
    assert invalid_url.value.code == "MODEL_BASE_URL_INVALID"
    with pytest.raises(ModelConnectionError) as too_long:
        update_model_config(db, {"base_url": "http://host/" + "x" * 600}, updated_by="tester")
    assert too_long.value.code == "MODEL_BASE_URL_TOO_LONG"
    with pytest.raises(ModelConnectionError) as out_of_range:
        update_model_config(db, {"timeout_seconds": 0}, updated_by="tester")
    assert out_of_range.value.code == "MODEL_TIMEOUT_OUT_OF_RANGE"
    with pytest.raises(ModelConnectionError) as not_a_number:
        update_model_config(db, {"timeout_seconds": "sixty"}, updated_by="tester")
    assert not_a_number.value.code == "MODEL_TIMEOUT_INVALID"
    assert db.record is None or db.record.settings == {}


def test_model_config_for_test_layers_unsaved_values_over_the_saved_route(monkeypatch):
    monkeypatch.setattr(
        model_connection,
        "settings",
        _settings(DEEPSEEK_BASE_URL="http://env/v1", DEEPSEEK_API_KEY="env-secret"),
    )
    db = _FakeDb()
    update_model_config(
        db,
        {"base_url": "http://saved/v1", "api_key": "sk-saved", "timeout_seconds": 15},
        updated_by="tester",
    )
    inline = model_config_for_test(db, {"base_url": "http://typo/v1"})
    assert inline["base_url"] == "http://typo/v1"
    assert inline["api_key"] == "sk-saved"
    assert inline["timeout_seconds"] == 15.0
    # An empty inline field must not erase a saved value (the dialog sends the
    # key field empty whenever the operator is not replacing it).
    assert model_config_for_test(db, {"api_key": ""})["api_key"] == "sk-saved"


def test_model_connection_routes_expose_get_patch_and_optional_test_body(monkeypatch):
    paths = {(route.path, method) for route in confidence_training_routes.router.routes for method in getattr(route, "methods", set())}
    assert ("/confidence-training/model-connection", "GET") in paths
    assert ("/confidence-training/model-connection", "PATCH") in paths
    assert ("/confidence-training/model-connection-test", "POST") in paths

    captured = {}

    def fake_test(config=None):
        captured["config"] = config
        return {
            "status": "ok",
            "requested_model": "deepseek-flash",
            "resolved_model_version": "deepseek-flash-2026",
            "prompt_version": "v1",
            "base_url": config.get("base_url"),
            "model": config.get("model"),
            "timeout_seconds": config.get("timeout_seconds"),
            "api_key_masked": mask_api_key(config.get("api_key")),
            "latency_ms": 12,
        }

    monkeypatch.setattr(
        confidence_training_routes,
        "test_deepseek_flash_connection",
        fake_test,
    )
    db = _FakeDb()
    update_model_config(
        db,
        {"base_url": "http://saved/v1", "api_key": "sk-saved"},
        updated_by="tester",
    )
    payload = confidence_training_routes.test_confidence_training_model_connection(
        body=ModelConnectionTest(base_url="http://form/v1", model="form-model"),
        db=db,
        _=None,
    )
    assert captured["config"]["base_url"] == "http://form/v1"
    assert captured["config"]["model"] == "form-model"
    assert captured["config"]["api_key"] == "sk-saved"
    assert payload["status"] == "connected"
    assert payload["base_url"] == "http://form/v1"
    assert payload["latency_ms"] == 12
    assert "sk-saved" not in str(payload)

    # The quick toolbar test keeps working without a body.
    payload = confidence_training_routes.test_confidence_training_model_connection(
        body=None,
        db=db,
        _=None,
    )
    assert captured["config"]["base_url"] == "http://saved/v1"
    assert payload["status"] == "connected"
