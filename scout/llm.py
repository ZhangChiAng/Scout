"""Strict Responses-API personalization for Scout articles and preferences."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .config import ConfigError, _nonempty_string, _safe_endpoint
from .model import DigestArticle, PersonalizedEvaluation, PreferenceProfile
from .storage import FeedbackEvidence

LLM_API_KEY_ENV = "SCOUT_LLM_API_KEY"
MODEL_TIMEOUT_SECONDS = 60.0
EVALUATION_BATCH_SIZE = 8


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    protocol: str
    base_url: str
    api_key_env: str


class LLMError(RuntimeError):
    """Raised when the Responses endpoint violates Scout's strict contract."""


def load_required_model_config(
    path: str | Path = "models.toml",
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> ModelConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"models config is required: {config_path}")
    config = load_models_config(config_path)
    resolve_api_key(config, environ=environ)
    return config


def load_optional_model_config(
    path: str | Path = "models.toml",
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> ModelConfig | None:
    """Compatibility seam for non-personalized commands."""

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


class PersonalizationLLM:
    def __init__(self, config: ModelConfig, api_key: str) -> None:
        self.config = config
        self.client = build_client(config, api_key)

    async def close(self) -> None:
        await self.client.close()

    async def summarize_preferences(
        self,
        evidence: Sequence[FeedbackEvidence],
        previous: PreferenceProfile | None,
        *,
        next_version: int,
        last_feedback_revision_id: int,
    ) -> PreferenceProfile:
        expected_ids = [item.revision_id for item in evidence]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "like_rules": _string_array_schema(),
                "dislike_rules": _string_array_schema(),
                "tradeoffs": _string_array_schema(),
                "uncertainties": _string_array_schema(),
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": len(expected_ids),
                    "maxItems": len(expected_ids),
                },
                "change_summary": {"type": "string", "minLength": 1, "maxLength": 300},
            },
            "required": [
                "like_rules",
                "dislike_rules",
                "tradeoffs",
                "uncertainties",
                "evidence_ids",
                "change_summary",
            ],
        }
        payload = {
            "previous_profile": previous.as_prompt_dict() if previous else None,
            "current_feedback": [_feedback_dict(item) for item in evidence],
            "required_evidence_ids": expected_ids,
        }
        data = await self._request_json(
            name="scout_preference_profile",
            schema=schema,
            instructions=(
                "你是 Scout 的偏好归纳器，只服务一个固定 owner。根据带原因的喜欢/"
                "不喜欢反馈，提炼稳定、可执行的中文规则。不要发明反馈中没有的偏好，"
                "不要给兴趣打分。权衡项记录可能冲突的判断，不确定项记录证据仍不足的"
                "地方。evidence_ids 必须逐一且仅包含输入要求的 ID。"
            ),
            payload=payload,
            max_output_tokens=5000,
        )
        ids = data.get("evidence_ids")
        if not isinstance(ids, list) or sorted(ids) != sorted(expected_ids):
            raise LLMError("preference response evidence IDs are incomplete or invalid")
        return PreferenceProfile(
            version=next_version,
            like_rules=_string_tuple(data, "like_rules"),
            dislike_rules=_string_tuple(data, "dislike_rules"),
            tradeoffs=_string_tuple(data, "tradeoffs"),
            uncertainties=_string_tuple(data, "uncertainties"),
            evidence_ids=tuple(int(value) for value in ids),
            change_summary=_required_string(data, "change_summary"),
            last_feedback_revision_id=last_feedback_revision_id,
        )

    async def evaluate_articles(
        self,
        articles: Sequence[DigestArticle],
        profile: PreferenceProfile,
        recent_likes: Sequence[FeedbackEvidence],
        recent_dislikes: Sequence[FeedbackEvidence],
    ) -> tuple[PersonalizedEvaluation, ...]:
        if not 1 <= len(articles) <= EVALUATION_BATCH_SIZE:
            raise ValueError("evaluation batch must contain 1–8 articles")
        expected_keys = [article.article_key for article in articles]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "evaluations": {
                    "type": "array",
                    "minItems": len(articles),
                    "maxItems": len(articles),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "article_key": {"type": "string", "enum": expected_keys},
                            "verdict": {
                                "type": "string",
                                "enum": ["推荐", "不推荐", "不确定"],
                            },
                            "reason": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 180,
                            },
                        },
                        "required": ["article_key", "verdict", "reason"],
                    },
                }
            },
            "required": ["evaluations"],
        }
        payload = {
            "preference_profile": profile.as_prompt_dict(),
            "recent_likes": [_feedback_dict(item) for item in recent_likes[:4]],
            "recent_dislikes": [_feedback_dict(item) for item in recent_dislikes[:4]],
            "articles_in_required_order": [_article_dict(item) for item in articles],
        }
        data = await self._request_json(
            name="scout_article_evaluations",
            schema=schema,
            instructions=(
                "你是 Scout 的条目评价器。只能使用橘鸦给出的分类、标题、人工摘要、"
                "详情和相关链接信息，以及 owner 偏好；不要假装读过原文。按输入顺序"
                "逐条输出，只能选择推荐、不推荐、不确定。理由必须是具体、简短、可理解"
                "的中文个性化理由，不使用分数，不过滤或重排条目。"
            ),
            payload=payload,
            max_output_tokens=5000,
        )
        raw = data.get("evaluations")
        if not isinstance(raw, list):
            raise LLMError("evaluation response is missing evaluations")
        evaluations: list[PersonalizedEvaluation] = []
        for value in raw:
            if not isinstance(value, dict):
                raise LLMError("evaluation response contains a non-object")
            evaluations.append(
                PersonalizedEvaluation(
                    article_key=_required_string(value, "article_key"),
                    verdict=_required_string(value, "verdict"),
                    reason=_required_string(value, "reason"),
                )
            )
        actual_keys = [item.article_key for item in evaluations]
        if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != set(
            expected_keys
        ):
            raise LLMError(
                "evaluation response IDs are incomplete, duplicated, or invalid"
            )
        by_key = {item.article_key: item for item in evaluations}
        return tuple(by_key[key] for key in expected_keys)

    async def _request_json(
        self,
        *,
        name: str,
        schema: dict[str, object],
        instructions: str,
        payload: dict[str, object],
        max_output_tokens: int,
    ) -> dict[str, object]:
        try:
            response = await self.client.responses.create(
                model=self.config.model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": name,
                        "strict": True,
                        "schema": schema,
                    }
                },
                max_output_tokens=max_output_tokens,
                store=False,
            )
        except Exception as exc:
            raise LLMError(f"Responses request failed: {type(exc).__name__}") from exc
        status = getattr(response, "status", None)
        if status != "completed":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) if details is not None else None
            suffix = f" ({reason})" if isinstance(reason, str) and reason else ""
            raise LLMError(f"Responses request did not complete{suffix}")
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise LLMError("Responses result has no output_text")
        try:
            data = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise LLMError("Responses output_text is not valid JSON") from exc
        if not isinstance(data, dict):
            raise LLMError("Responses structured output is not an object")
        return data


def _string_array_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 200},
    }


def _required_string(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise LLMError(f"structured response field {name} must be a non-empty string")
    return value.strip()


def _string_tuple(data: dict[str, object], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise LLMError(f"structured response field {name} must be a string array")
    return tuple(item.strip() for item in value)


def _feedback_dict(item: FeedbackEvidence) -> dict[str, object]:
    return {
        "revision_id": item.revision_id,
        "sentiment": item.sentiment,
        "reason": item.reason,
        "article": {
            "article_key": item.article_key,
            "category": item.category,
            "title": item.title,
            "summary": item.summary,
            "detail": item.detail,
        },
    }


def _article_dict(article: DigestArticle) -> dict[str, object]:
    return {
        "article_key": article.article_key,
        "position": article.position,
        "number": article.number,
        "category": article.category,
        "title": article.title,
        "article_url": article.article_url,
        "summary": article.summary,
        "detail": article.detail,
        "related_links": [
            {"text": text, "url": url} for text, url in article.related_links
        ],
    }
