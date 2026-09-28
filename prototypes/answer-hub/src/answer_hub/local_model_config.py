"""Local, file-backed configuration for an OpenAI-compatible group model."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


DEFAULT_CONFIG_RELATIVE_PATH = Path("config") / "local-model.json"
DEFAULT_API_KEY_ENV = "MIMO_API_KEY"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_config_path(path: str | Path | None = None) -> Path:
    configured = path if path is not None else os.getenv("ANSWER_HUB_LOCAL_MODEL_CONFIG")
    candidate = Path(configured) if configured else DEFAULT_CONFIG_RELATIVE_PATH
    return candidate if candidate.is_absolute() else project_root() / candidate


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _valid_base_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _positive_int(value: Any, default: int | None = None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _read_payload(config_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class LocalModelConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    api_keys: tuple[str, ...]
    media_model: str = ""
    timeout_seconds: int | None = None
    response_read_timeout_seconds: float | None = None
    max_completion_tokens: int | None = None
    max_retries: int | None = None
    max_requests_per_second: float | None = None
    thinking_type: str | None = None
    input_cost_per_million_tokens: float | None = None
    output_cost_per_million_tokens: float | None = None

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "LocalModelConfig | None":
        config_path = resolve_config_path(path)
        payload = _read_payload(config_path)
        if payload is None or payload.get("enabled", True) is False:
            return None

        base_url = _text(payload.get("base_url"))
        model = _text(payload.get("model"))
        if not base_url or not model or not _valid_base_url(base_url):
            return None

        api_key_env = _text(payload.get("api_key_env")) or DEFAULT_API_KEY_ENV
        if not _ENV_NAME_RE.fullmatch(api_key_env):
            return None
        api_key = os.getenv(api_key_env, "").strip()
        extra_key_env = _text(payload.get("api_keys_env"))
        extra_key_value = os.getenv(extra_key_env, "") if extra_key_env else ""
        extra_keys = tuple(
            item.strip()
            for item in extra_key_value.replace(";", ",").split(",")
            if item.strip()
        ) if extra_key_value else ()
        media_model = _text(payload.get("media_model")) or model
        thinking_type = _text(payload.get("thinking_type")).lower() or None
        if thinking_type not in {None, "enabled", "disabled"}:
            thinking_type = None

        def _non_negative_float(value: Any) -> float | None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None

        return cls(
            provider=_text(payload.get("provider")) or "group-internal",
            base_url=base_url,
            model=model,
            api_key=api_key,
            api_keys=extra_keys,
            media_model=media_model,
            timeout_seconds=_positive_int(payload.get("timeout_seconds")),
            response_read_timeout_seconds=_non_negative_float(
                payload.get("response_read_timeout_seconds")
            ),
            max_completion_tokens=_positive_int(payload.get("max_completion_tokens")),
            max_retries=_positive_int(payload.get("max_retries"), 0),
            max_requests_per_second=_non_negative_float(
                payload.get("max_requests_per_second")
            ),
            thinking_type=thinking_type,
            input_cost_per_million_tokens=_non_negative_float(
                payload.get("input_cost_per_million_tokens")
            ),
            output_cost_per_million_tokens=_non_negative_float(
                payload.get("output_cost_per_million_tokens")
            ),
        ) if api_key else None


def write_local_model_config(
    values: dict[str, Any],
    path: str | Path | None = None,
) -> Path:
    """Atomically persist non-secret model settings for subsequent runs."""
    config_path = resolve_config_path(path)
    base_url = _text(values.get("base_url"))
    model = _text(values.get("model"))
    if not _valid_base_url(base_url):
        raise ValueError("base_url 必须是 http 或 https 地址。")
    if not model:
        raise ValueError("model 不能为空。")
    if "api_key" in values or "api_keys" in values:
        raise ValueError("不能通过接口写入 API Key；请设置 api_key_env 指向的环境变量。")

    api_key_env = _text(values.get("api_key_env")) or DEFAULT_API_KEY_ENV
    if not _ENV_NAME_RE.fullmatch(api_key_env):
        raise ValueError("api_key_env 必须是合法的环境变量名称。")
    api_keys_env = _text(values.get("api_keys_env"))
    if api_keys_env and not _ENV_NAME_RE.fullmatch(api_keys_env):
        raise ValueError("api_keys_env 必须是合法的环境变量名称。")

    payload: dict[str, Any] = {
        "enabled": bool(values.get("enabled", True)),
        "provider": _text(values.get("provider")) or "group-internal",
        "base_url": base_url,
        "model": model,
        "media_model": _text(values.get("media_model")) or model,
        "api_key_env": api_key_env,
    }
    for key in (
        "timeout_seconds",
        "response_read_timeout_seconds",
        "max_completion_tokens",
        "max_retries",
        "max_requests_per_second",
        "thinking_type",
        "input_cost_per_million_tokens",
        "output_cost_per_million_tokens",
    ):
        if key in values and values[key] not in (None, ""):
            payload[key] = values[key]
    if api_keys_env:
        payload["api_keys_env"] = api_keys_env

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(config_path)
    return config_path


def public_local_model_config(
    path: str | Path | None = None,
) -> dict[str, Any]:
    config_path = resolve_config_path(path)
    payload = _read_payload(config_path)
    if payload is None or payload.get("enabled", True) is False:
        return {
            "configured": False,
            "provider": "",
            "base_url": "",
            "model": "",
            "media_model": "",
            "api_key_configured": False,
        }
    base_url = _text(payload.get("base_url"))
    model = _text(payload.get("model"))
    api_key_env = _text(payload.get("api_key_env")) or DEFAULT_API_KEY_ENV
    if not base_url or not model or not _valid_base_url(base_url):
        return {
            "configured": False,
            "provider": "",
            "base_url": "",
            "model": "",
            "media_model": "",
            "api_key_configured": False,
        }
    return {
        "configured": True,
        "provider": _text(payload.get("provider")) or "group-internal",
        "base_url": base_url,
        "model": model,
        "media_model": _text(payload.get("media_model")) or model,
        "api_key_configured": bool(os.getenv(api_key_env, "").strip()),
    }
