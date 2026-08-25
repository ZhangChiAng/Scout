"""Minimal optional LLM entry point reserved for future personalization.

V1 never calls a model: the data flow is collection, digest parsing, and
Feishu delivery only.  This module keeps the config contract and client
construction so a future personalization layer has a validated seam:

- ``models.toml`` is optional.  It is loaded and validated only when the
  file exists AND ``SCOUT_LLM_API_KEY`` is present; a missing file or key
  is not an error.
- The only provided client uses the project's fixed settings: a 60 second
  timeout, SDK retries disabled, and ``store=False`` on future requests.
  There are no prompts, schemas, or business calls here.
"""

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .config import ConfigError, _nonempty_string, _safe_endpoint

LLM_API_KEY_ENV = "SCOUT_LLM_API_KEY"
MODEL_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    protocol: str
    base_url: str
    api_key_env: str


class LLMError(RuntimeError):
    """Raised when the optional model client cannot be constructed."""


def load_optional_model_config(
    path: str | Path = "models.toml",
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> ModelConfig | None:
    """Load and validate models.toml only when both it and the key exist.

    A missing config file or missing API key simply disables the optional
    LLM seam and returns ``None``; a present but invalid config still fails
    fast.
    """

    source = os.environ if environ is None else environ
    config_path = Path(path)
    if not config_path.exists() or not source.get(LLM_API_KEY_ENV):
        return None
    config = load_models_config(config_path)
    resolve_api_key(config, environ=source)
    return config


def load_models_config(path: str | Path = "models.toml") -> ModelConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"cannot load models config {config_path}: {type(exc).__name__}"
        ) from exc

    if set(raw) != {"models"}:
        raise ConfigError("models config must contain only the models array")
    models = raw.get("models")
    if (
        not isinstance(models, list)
        or len(models) != 1
        or not isinstance(models[0], dict)
    ):
        raise ConfigError("models config must contain exactly one model")

    model_raw = models[0]
    expected = {"model", "protocol", "base_url", "api_key_env"}
    if set(model_raw) != expected:
        raise ConfigError("model config must contain exactly four supported fields")

    model = _nonempty_string(model_raw["model"], "models.model")
    protocol = _nonempty_string(model_raw["protocol"], "models.protocol")
    if protocol != "openai_responses":
        raise ConfigError("models.protocol must be openai_responses")
    base_url = _safe_endpoint(model_raw["base_url"], "models.base_url")
    api_key_env = _nonempty_string(model_raw["api_key_env"], "models.api_key_env")
    if api_key_env != LLM_API_KEY_ENV:
        raise ConfigError(f"models.api_key_env must be {LLM_API_KEY_ENV}")
    return ModelConfig(model, protocol, base_url, api_key_env)


def resolve_api_key(
    config: ModelConfig,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    value = source.get(config.api_key_env)
    if not value:
        raise ConfigError(f"{config.api_key_env} is required")
    return value


def build_client(
    config: ModelConfig,
    api_key: str,
    *,
    client_factory: Callable[..., Any] = AsyncOpenAI,
) -> AsyncOpenAI:
    """Construct the Responses-compatible async client with fixed settings.

    Timeout is 60 seconds and SDK retries are disabled.  Future request
    callers must pass ``store=False`` so model traffic never persists server
    side.
    """

    try:
        return client_factory(
            api_key=api_key,
            base_url=config.base_url,
            timeout=MODEL_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except Exception as exc:
        raise LLMError(
            f"model client initialization failed: {type(exc).__name__}"
        ) from exc
