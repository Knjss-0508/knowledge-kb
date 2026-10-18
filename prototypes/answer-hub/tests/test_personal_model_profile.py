import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "isolated_real_audit", Path(__file__).parents[1] / "scripts/run_isolated_real_audit.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_personal_profile_cannot_fall_back_to_company_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_PERSONAL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_PERSONAL_MODEL", raising=False)
    monkeypatch.setenv("GROUP_LLM_API_KEY", "test-company-placeholder")
    with pytest.raises(RuntimeError, match="DEEPSEEK_PERSONAL_API_KEY"):
        module.model_profile("personal")


def test_personal_profile_uses_only_explicit_personal_settings(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_PERSONAL_API_KEY", "test-personal-placeholder")
    monkeypatch.setenv("DEEPSEEK_PERSONAL_MODEL", "test-personal-model")
    monkeypatch.setenv("DEEPSEEK_PERSONAL_BASE_URL", "https://api.deepseek.com/v1")
    assert module.model_profile("personal") == (
        "test-personal-placeholder", "https://api.deepseek.com/v1", "test-personal-model",
    )


@pytest.mark.parametrize("base", [
    "http://api.deepseek.com/v1", "https://company.example/v1",
    "https://api.deepseek.com@company.example/v1",
])
def test_personal_profile_rejects_company_or_unencrypted_endpoint(monkeypatch, base):
    monkeypatch.setenv("DEEPSEEK_PERSONAL_BASE_URL", base)
    with pytest.raises(RuntimeError, match="official HTTPS"):
        module.model_profile("personal")


def test_group_profile_rejects_credentials_in_endpoint(monkeypatch):
    monkeypatch.setenv("GROUP_LLM_API_KEY", "test-placeholder")
    monkeypatch.setenv("GROUP_LLM_MODEL", "test")
    monkeypatch.setenv("GROUP_LLM_BASE_URL", "https://user:test-password@company.example/v1")
    with pytest.raises(RuntimeError, match="must not contain credentials"):
        module.model_profile("group")
