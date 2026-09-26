"""Validate saved Zhihu evidence and produce local scoring reports."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError
from .rules import Rules, TextArticle

STATUSES = {
    "body": "正文",
    "partial_body": "部分正文",
    "summary_only": "仅摘要",
    "failed": "失败",
}


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run_file(run_dir: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ConfigError("输出/报告名称必须是运行目录内的文件名")
    path = (run_dir / name).resolve()
    if path.parent != run_dir.resolve():
        raise ConfigError("文件必须位于运行目录内")
    return path


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
        "article_key": f"zhihu:{article['content_type']}:{article['content_id']}",
        "source": "知乎",
    }


def evaluate_article(article: dict, rules: Rules) -> dict:
    if article["status"] == "failed":
        evaluation = {
            "regex_hits": [],
            "keyword_hits": [],
            "score": None,
            "threshold": rules.threshold,
            "recalled": None,
            "retained": False,
        }
    else:
        evaluation = rules.evaluate(
            TextArticle(*(article[name] for name in ("title", "summary", "body")))
        )
    eligible = complete_body(article) and evaluation["retained"]
    return {
        "article": article,
        **evaluation,
        "rule_retained": evaluation["retained"],
        "retained": eligible,
        "eligible": eligible,
    }


def score(input_path: Path, rules: Rules) -> dict:
    raw = input_path.read_bytes()
    collection = json.loads(raw)
    if (
        not isinstance(collection, dict)
        or collection.get("schema_version") != 1
        or not isinstance(collection.get("queries"), list)
        or not isinstance(collection.get("records"), list)
    ):
        raise ConfigError("采集 JSON 需要 schema_version=1、queries 和 records 数组")
    articles = [validate_article(a) for a in collection["records"]]
    for article in articles:
        discovery = article["first_discovery"]
        if (
            discovery["query_index"] > len(collection["queries"])
            or discovery["query"] != collection["queries"][discovery["query_index"] - 1]
        ):
            raise ConfigError("首次发现位置与采集查询序列不一致")
    articles.sort(
        key=lambda a: (
            a["first_discovery"]["query_index"],
            a["first_discovery"]["result_index"],
        )
    )
    unique = {}
    for article in articles:
        unique.setdefault(article["article_key"], article)
    hashes = {}
    for article in unique.values():
        hashes.update(validate_evidence(input_path.parent, article))
    results = [evaluate_article(a, rules) for a in unique.values()]
    complete = [r for r in results if r["article"]["status"] == "body"]
    counts = {
        status: sum(r["article"]["status"] == status for r in results)
        for status in STATUSES
    }
    return {
        "schema_version": 1,
        "scored_at": datetime.now(UTC).isoformat(),
        "queries": collection["queries"],
        "input_file": input_path.name,
        "input_sha256": digest(raw),
        "rules": rules.snapshot(),
        "rules_sha256": digest(
            json.dumps(rules.snapshot(), ensure_ascii=False, sort_keys=True).encode()
        ),
        "raw_sha256": hashes,
        "results": results,
        "collection_status": collection.get("status", "historical"),
        "coverage": collection.get("coverage", []),
        "errors": collection.get("errors", []),
        "summary": {
            "candidates": len(results),
            "duplicates_removed": len(articles) - len(results),
            **counts,
            "read_failures": sum(bool(r["article"]["read_error"]) for r in results),
            "body_recalled": sum(r["recalled"] for r in complete),
            "body_no_match": sum(not r["recalled"] for r in complete),
            "retained": sum(r["retained"] for r in results),
        },
        "scope": "仅本轮搜索发现且实际读取的知乎内容；不代表知乎全站。",
    }


def render_report(report: dict) -> str:
    lines = [
        "# 知乎单次规则验证",
        "",
        report["scope"],
        "",
        "正文以外的评分只供诊断，不能判定正文无命中或用于发送。",
        "",
        "```json",
        json.dumps(report["summary"], ensure_ascii=False, indent=2),
        "```",
        "",
        "规则：",
        "",
        "```json",
        json.dumps(report["rules"], ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    for complete, label in ((True, "已读取正文"), (False, "不完整或失败")):
        lines.extend(["## " + label, ""])
        for index, result in enumerate(report["results"], 1):
            article = result["article"]
            if (article["status"] == "body") != complete:
                continue
            lines.extend(
                [
                    f"### {index}. {article['title']}",
                    "",
                    f"原文：{article['url']}",
                    "",
                    (
                        f"键：`{article['article_key']}`；作者：{article['author'] or '未知'}；"
                        f"获取：{STATUSES[article['status']]}。"
                    ),
                    "",
                    (
                        f"发布时间：{article['published_at'] or '未提供'}；"
                        f"更新时间：{article['updated_at'] or '未提供'}。"
                    ),
                    "",
                    (
                        f"得分：{result['score']}；阈值：{result['threshold']}；"
                        f"正文保留：{result['retained']}。"
                    ),
                    "",
                ]
            )
            if article["read_error"]:
                lines.extend(["读取问题：" + article["read_error"], ""])
            for hit in result["regex_hits"] + result["keyword_hits"]:
                label = hit.get("pattern", hit.get("text"))
                weight = f"，权重 {hit['weight']}" if "weight" in hit else ""
                lines.extend([f"匹配 `{label}`{weight}：", ""])
                for field, evidence in hit["evidence"].items():
                    lines.extend(
                        [
                            f"- {field}[{evidence['start']}:{evidence['end']}]："
                            + json.dumps(evidence["text"], ensure_ascii=False),
                        ]
                    )
                lines.append("")
            lines.extend(["原始返回：" + "、".join(article["raw_files"]), ""])
    return "\n".join(lines)


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
