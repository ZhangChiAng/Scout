"""Validate saved Zhihu evidence and produce local scoring reports."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError

STATUSES = {"body", "partial_body", "summary_only", "failed"}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def evidence_file(run_dir: Path, name: str) -> Path:
    path = (run_dir / name).resolve()
    if not path.is_relative_to((run_dir / "raw").resolve()) or not path.is_file():
        raise ConfigError(f"原始采集证据必须是运行目录 raw/ 内的文件：{name}")
    return path


def validate_article(article: dict) -> dict:
    if not isinstance(article, dict):
        raise ConfigError("每篇采集记录必须是对象")
    fields = (
        "content_id",
        "content_type",
        "title",
        "author",
        "url",
        "published_at",
        "updated_at",
        "summary",
        "body",
        "fetched_at",
        "status",
        "read_error",
    )
    if any(not isinstance(article.get(k), str) for k in fields):
        raise ConfigError("采集记录缺少字符串字段；缺失作者和日期请填空字符串")
    url = urlsplit(article["url"])
    article_match = re.fullmatch(r"/p/([0-9]+)/?", url.path)
    answer_match = re.fullmatch(
        r"/(?:question/[0-9]+/|en/)?answer/([0-9]+)/?", url.path
    )
    match = article_match if article["content_type"] == "article" else answer_match
    host = "zhuanlan.zhihu.com" if article_match else "www.zhihu.com"
    if (
        article["content_type"] not in {"article", "answer"}
        or not match
        or match[1] != article["content_id"]
        or url.scheme != "https"
        or url.netloc != host
    ):
        raise ConfigError("只允许内容 ID 一致的知乎单篇回答/专栏链接，问题页不能评分")
    if article["status"] not in STATUSES or not article["title"].strip():
        raise ConfigError("无效的获取状态或空标题")
    question_id = article.get("question_id", "")
    if question_id and (
        not isinstance(question_id, str)
        or not question_id.isascii()
        or not question_id.isdigit()
    ):
        raise ConfigError("question_id 必须为数字字符串")
    question_match = re.match(r"/question/([0-9]+)/answer/", url.path)
    if question_match:
        if question_id and question_id != question_match[1]:
            raise ConfigError("question_id 与回答链接不一致")
        question_id = question_match[1]
    stamp = datetime.fromisoformat(article["fetched_at"])
    if stamp.tzinfo is None:
        raise ConfigError("fetched_at 必须带时区")
    body_status = article["status"] in {"body", "partial_body"}
    if body_status != bool(article["body"].strip()):
        raise ConfigError("正文/部分正文必须有文本，仅摘要/失败必须留空正文")
    if article["status"] == "summary_only" and not article["summary"].strip():
        raise ConfigError("仅摘要记录必须有摘要")
    if article["status"] == "failed" and not article["read_error"].strip():
        raise ConfigError("失败记录必须说明原因")
    discovery = article.get("first_discovery")
    if not isinstance(discovery, dict):
        raise ConfigError("缺少首次发现位置 first_discovery")
    for name in ("query_index", "result_index"):
        if type(discovery.get(name)) is not int or discovery[name] < 1:
            raise ConfigError("发现位置必须是从 1 开始的整数")
    if not isinstance(discovery.get("query"), str) or not discovery["query"].strip():
        raise ConfigError("首次发现查询不能为空")
    files = article.get("raw_files")
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(name, str) or not name for name in files)
    ):
        raise ConfigError("缺少 raw_files 原始采集证据路径")
    return {
        **article,
        "question_id": question_id if article["content_type"] == "answer" else "",
        "article_key": f"zhihu:{article['content_type']}:{article['content_id']}",
        "source": "知乎",
    }


def complete_body(article):
    basis = article.get("completeness")
    return (
        article["status"] == "body"
        and isinstance(basis, dict)
        and bool(basis.get("basis"))
        and basis.get("verified") is True
        and basis.get("target_id") == article["content_id"]
        and basis.get("target_type") == article["content_type"]
        and basis.get("target_id_verified") is True
        and basis.get("content_field_present") is True
        and basis.get("limitations") == []
    )


def validate_evidence(directory, article):
    validate_article(article)
    hashes = {}
    references = {entry["sha256"] for entry in article.get("evidence", [])}
    for name in article["raw_files"]:
        sha = digest(evidence_file(directory, name).read_bytes())
        if references and sha not in references:
            raise ConfigError("原始采集证据哈希不符")
        if (
            re.fullmatch(r"[0-9a-f]{64}\.json", Path(name).name)
            and Path(name).stem != sha
        ):
            raise ConfigError("原始采集证据文件名与哈希不符")
        hashes[name] = sha
    if references and not references <= set(hashes.values()):
        raise ConfigError("原始采集证据不完整")
    return hashes
