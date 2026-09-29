from __future__ import annotations

from answer_hub import local_model_config


def test_group_model_key_default_is_not_personal_mimo_key() -> None:
    assert local_model_config.DEFAULT_API_KEY_ENV == "GROUP_LLM_API_KEY"
