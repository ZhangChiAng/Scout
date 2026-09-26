"""Score saved Zhihu text and prepare an immutable delivery snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError, load_config, load_dotenv
from .locking import RunLockedError
from .notifier import NotificationError
from .rules import Rules, TextArticle, load_rules

STATUSES = {
    "body": "正文",
    "partial_body": "部分正文",
    "summary_only": "仅摘要",
    "failed": "失败",
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
    eligible = article["status"] == "body" and evaluation["retained"]
    return {
        "article": article,
        **evaluation,
        "rule_retained": evaluation["retained"],
        "retained": eligible,
        "eligible": eligible,
    }


def score(input_path: Path, rules_path: Path) -> dict:
    rules = load_rules(rules_path)  # Always validate before consuming records.
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
    hashes = {
        name: digest(evidence_file(input_path.parent, name).read_bytes())
        for article in unique.values()
        for name in article["raw_files"]
    }
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
        "rules_sha256": digest(rules_path.read_bytes()),
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


def preview(
    run_dir: Path, report_name: str, article_key: str, chat_id: str, limit: int
) -> dict:
    from .rule_test import build_card, new_preview

    report = read_json(run_file(run_dir, report_name))
    rules = Rules.load(report["rules"])
    result = next(
        (r for r in report["results"] if r["article"]["article_key"] == article_key),
        None,
    )
    if result is None:
        raise ConfigError("报告中找不到指定文章")
    article = validate_article(result["article"])
    if evaluate_article(article, rules) != result or not result["eligible"]:
        raise ConfigError("文章不是通过筛选的完整正文，或评分记录已变动")
    evidence = []
    for name in article["raw_files"]:
        raw = evidence_file(run_dir, name).read_bytes()
        sha = digest(raw)
        if sha != report["raw_sha256"].get(name):
            raise ConfigError(f"原始采集证据已变动：{name}")
        evidence.append({"path": name, "sha256": sha, "content": raw.decode("utf-8")})
    return new_preview(
        result, rules.snapshot(), evidence, build_card(result, limit), chat_id, limit
    )


def legacy_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scoring = commands.add_parser("score", help="本地评分，不联系模型/飞书或 SQLite")
    scoring.add_argument("--input", type=Path, required=True)
    scoring.add_argument("--rules", type=Path, required=True)
    scoring.add_argument("--output-prefix", default="scores")
    showing = commands.add_parser("preview", help="生成具体文章与目标群预览")
    showing.add_argument("--run-dir", type=Path, required=True)
    showing.add_argument("--report", default="scores.json")
    showing.add_argument("--article-key", required=True)
    showing.add_argument("--chat-id", help="默认读取 .env 中的目标 chat_id")
    showing.add_argument("--output", default="preview.json")
    showing.add_argument("--config", default="config.toml")
    sending = commands.add_parser("send-test", help="授权发送指定预览，或按文章键恢复")
    sending.add_argument("--article-key", required=True)
    sending.add_argument(
        "--preview", type=Path, help="首次发送需要；恢复仅用数据库快照"
    )
    sending.add_argument("--approve", help="已明确授权的预览 SHA-256；省略则交互确认")
    sending.add_argument("--database", type=Path)
    sending.add_argument("--config", default="config.toml")
    args = parser.parse_args(argv)
    try:
        if args.command == "score":
            report = score(args.input, args.rules)
            json_path = run_file(args.input.parent, args.output_prefix + ".json")
            md_path = run_file(args.input.parent, args.output_prefix + ".md")
            protected = {args.input.resolve(), args.rules.resolve()}
            if json_path in protected or md_path in protected:
                raise ConfigError("输出不能覆盖采集输入或规则")
            write_json(json_path, report)
            md_path.write_text(render_report(report), encoding="utf-8")
            print(json.dumps(report["summary"], ensure_ascii=False))
            # Incomplete collection is explicitly distinguishable from no match.
            return (
                2
                if report["summary"]["body"] < report["summary"]["candidates"]
                or report["collection_status"] not in {"completed", "historical"}
                else 0
            )
        from .rule_test import approval_digest, preview_display, send_test

        load_dotenv()
        config = load_config(args.config)
        if args.command == "preview":
            chat_id = args.chat_id
            if chat_id is None:
                if os.environ.get("FEISHU_RECEIVE_ID_TYPE") != "chat_id":
                    raise ConfigError("预览需要 --chat-id 或 .env 中的群 chat_id")
                chat_id = os.environ.get("FEISHU_RECEIVE_ID", "")
            payload = preview(
                args.run_dir,
                args.report,
                args.article_key,
                chat_id,
                config.feishu.max_payload_bytes,
            )
            output_path = run_file(args.run_dir, args.output)
            if output_path == run_file(args.run_dir, args.report):
                raise ConfigError("预览不能覆盖评分报告")
            write_json(output_path, payload)
            print(json.dumps(preview_display(payload), ensure_ascii=False, indent=2))
            print("授权摘要：" + approval_digest(payload))
            return 0
        return send_test(
            database_path=args.database
            or Path(os.environ.get("SCOUT_DB_PATH", "data/scout.sqlite3")),
            article_key=args.article_key,
            preview_path=args.preview,
            approval=args.approve,
            config=config,
            output=sys.stdout,
        )
    except (
        ConfigError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        RunLockedError,
        NotificationError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    from .zhihu import main as cli

    return cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
