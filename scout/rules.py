"""Plain text recall and weighted filtering; no network or persistence."""

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ConfigError(f"{label} must be a finite number")
    return value


@dataclass(frozen=True)
class TextArticle:
    title: str
    summary: str
    body: str


def load_rules(path: str | Path) -> Rules:
    with Path(path).open("rb") as file:
        raw = tomllib.load(file)
    if set(raw) != {"rules"}:
        raise ConfigError("独立规则文件只允许 [rules]")
    return Rules.load(raw["rules"])


def _excerpt(value: str, start: int, end: int) -> dict:
    left, right = max(0, start - 30), min(len(value), end + 30)
    text = value[left:right]
    truncated = len(text) > 240
    if truncated:
        text = text[:120] + "…[片段截断]…" + text[-120:]
    return {"start": start, "end": end, "text": text, "truncated": truncated}


def _literal_match(value: str, needle: str) -> dict | None:
    start = value.casefold().find(needle.casefold())
    if start < 0:
        return None
    # casefold may expand Unicode characters; keep offsets in original text.
    offsets = [i for i, char in enumerate(value) for _ in char.casefold()]
    end = start + len(needle.casefold())
    return _excerpt(value, offsets[start], offsets[end - 1] + 1)


@dataclass(frozen=True)
class Rules:
    regex: tuple[str, ...]
    keywords: tuple[tuple[str, int | float], ...]
    threshold: int | float
    fields: tuple[str, ...] = ("title", "summary", "body")

    @classmethod
    def load(cls, raw):
        required = {"regex", "keywords", "threshold"}
        if (
            not isinstance(raw, dict)
            or not required <= set(raw)
            or set(raw) - required - {"fields"}
        ):
            raise ConfigError(
                "缺少或无效的 [rules] 配置：需要 regex、keywords、threshold，可选 fields"
            )
        fields = raw.get("fields", ["title", "summary", "body"])
        if (
            not isinstance(fields, list)
            or not fields
            or any(
                not isinstance(field, str) or field not in {"title", "summary", "body"}
                for field in fields
            )
            or len(set(fields)) != len(fields)
        ):
            raise ConfigError(
                "rules.fields must be a non-empty unique list of title, summary, body"
            )
        patterns = raw["regex"]
        if not isinstance(patterns, list) or not patterns:
            raise ConfigError("rules.regex must be a non-empty list")
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ConfigError("rules.regex entries must be non-empty strings")
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(f"invalid rules.regex: {exc}") from exc
        if not isinstance(raw["keywords"], list):
            raise ConfigError("rules.keywords must be an array")
        keywords = []
        seen_keywords = set()
        for rule in raw["keywords"]:
            if not isinstance(rule, dict) or set(rule) != {"text", "weight"}:
                raise ConfigError("each keyword needs text and weight")
            text = rule["text"]
            if not isinstance(text, str) or not text.strip():
                raise ConfigError("keyword text must not be empty")
            if text.casefold() in seen_keywords:
                raise ConfigError("rules.keywords contains duplicate literal text")
            seen_keywords.add(text.casefold())
            keywords.append((text, _number(rule["weight"], "keyword weight")))
        return cls(
            tuple(patterns),
            tuple(keywords),
            _number(raw["threshold"], "threshold"),
            tuple(fields),
        )

    def evaluate(self, article: TextArticle):
        fields = {k: getattr(article, k) for k in self.fields}
        regex = []
        for pattern in self.regex:
            matches = {}
            for name, value in fields.items():
                if match := re.search(pattern, value, re.IGNORECASE):
                    matches[name] = _excerpt(value, *match.span())
            if matches:
                regex.append(
                    {"pattern": pattern, "fields": list(matches), "evidence": matches}
                )
        hits = []
        if regex:
            for text, weight in self.keywords:
                matched = {
                    k: match
                    for k, v in fields.items()
                    if (match := _literal_match(v, text)) is not None
                }
                if matched:
                    hits.append(
                        {
                            "text": text,
                            "weight": weight,
                            "fields": list(matched),
                            "evidence": matched,
                        }
                    )
        score = sum(m["weight"] for m in hits)
        _number(score, "total score")
        return {
            "regex_hits": regex,
            "keyword_hits": hits,
            "score": score,
            "threshold": self.threshold,
            "recalled": bool(regex),
            "retained": bool(regex) and score >= self.threshold,
        }

    def snapshot(self):
        return {
            "fields": list(self.fields),
            "regex": list(self.regex),
            "keywords": [{"text": t, "weight": w} for t, w in self.keywords],
            "threshold": self.threshold,
        }
