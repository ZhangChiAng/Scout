"""Feishu cards for the owner's Zhihu semantic scans and feedback."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date, datetime

from .datetime_utils import BEIJING_TIMEZONE
from .notifier import _check_card_size, _link_url, _markdown, _md_escape

FILTERED_PER_PAGE = 6
FILTER_REASONS = {
    "not_recommended": "语义判断：不推荐",
    "reject": "语义判断：不推荐",
    "irrelevant": "相关性判断：与话题无关",
    "preference_rejected": "内容评价：明确违背已生效偏好",
    "date_expired": "发布日期超出本轮近 30 天窗口",
    "date_unknown": "发布日期缺失或无法识别",
    "date_missing": "发布日期缺失",
    "date_invalid": "发布日期无法识别",
    "date_future": "发布日期晚于本次扫描开始时间",
    "body_incomplete": "未获取完整正文，暂不能判断",
    "body_failed": "正文读取失败",
    "model_failed": "模型判断失败，尚无推荐结论",
    "content_failed": "内容评价失败，本篇未发送",
    "historical_delivered": "此前已送达",
    "delivery_reserved": "已在投递队列中",
    "not_evaluated": "尚未判断",
}
DECISIONS = {
    "recommend": "推荐",
    "recommended": "推荐",
    "not_recommended": "不推荐",
    "reject": "不推荐",
    "uncertain": "不确定",
    "推荐": "推荐",
    "不推荐": "不推荐",
    "不确定": "不确定",
}


def _button(label: str, action: str, *, style: str = "default", **value) -> dict:
    return {
        "tag": "button",
        "type": style,
        "text": {"tag": "plain_text", "content": label},
        "value": {"action": f"zhihu_{action}", **value},
    }


def _actions(*buttons: dict) -> dict:
    return {"tag": "action", "actions": list(buttons)}


def _card(title: str, elements: list[dict], max_payload_bytes: int) -> dict:
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title[:120]},
        },
        "elements": elements,
    }
    _check_card_size(
        {
            "receive_id": "x" * 64,
            "msg_type": "interactive",
            "uuid": "x" * 36,
            "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
        },
        max_payload_bytes,
    )
    return card


def _input(
    name: str, label: str, placeholder: str, *, value: str = "", length: int = 500
) -> dict:
    return {
        "tag": "input",
        "name": name,
        "required": True,
        "max_length": length,
        "default_value": value,
        "label": {"tag": "plain_text", "content": label},
        "label_position": "top",
        "placeholder": {"tag": "plain_text", "content": placeholder},
    }


def _submit(label: str, action: str, **value) -> dict:
    return {
        **_button(label, action, style="primary", **value),
        "name": f"submit_{action}",
        "action_type": "form_submit",
    }


def _published_at(value: str) -> str:
    """Display source timestamps in Beijing time without inventing precision."""
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value).isoformat()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(BEIJING_TIMEZONE).strftime(
                "%Y-%m-%d %H:%M:%S（北京时间）"
            )
    except ValueError, OverflowError:
        pass
    return "未知"


def body_excerpt(body: str, *, title: str = "") -> str:
    """Skip repeated title lines, then keep three original sentences/paragraphs."""
    lines = [" ".join(line.split()) for line in body.splitlines()]
    title = " ".join(title.split())
    start = 0
    while start < len(lines):
        line = lines[start]
        heading = re.sub(r"^#{1,6}\s+", "", line)
        if line and (not title or heading != title):
            break
        start += 1
    body = "\n".join(lines[start:]).strip()
    if not body:
        return "（未获取正文）"
    start = count = 0
    end = len(body)
    for boundary in re.finditer(r"""[。！？!?]+[”’」』"']*|\.(?=\s|$)|\n+""", body):
        if body[start : boundary.end()].strip():
            count += 1
        start = boundary.end()
        if count == 3:
            end = boundary.end()
            break
    excerpt = " ".join(body[:end].split())
    return excerpt if len(excerpt) <= 300 else excerpt[:299].rstrip() + "…"


def _content_elements(article: dict) -> list[dict]:
    title = _md_escape(str(article.get("title") or "知乎内容")[:200])
    url = str(article.get("url") or "")
    evaluation = article.get("semantic") or {}
    decision = evaluation.get("decision")
    explanation = evaluation.get("reason")
    votes = article.get("voteup_count")
    elements = [
        _markdown(f"**{title}**"),
        _markdown(
            f"作者：{_md_escape(str(article.get('author') or '未知')[:100])}\n"
            f"发布日期：{_published_at(article.get('published_at') or '')}\n"
            f"采集时赞同：{votes if type(votes) is int and votes >= 0 else '未知'}"
        ),
    ]
    elements.append(
        _markdown(
            "**正文摘录**\n"
            + _md_escape(
                body_excerpt(
                    article.get("body") or "", title=article.get("title") or ""
                )
            )
        )
    )
    if decision:
        text = f"**推送判断**：{_md_escape(DECISIONS.get(decision, str(decision)))}"
        if explanation:
            text += f"\n{_md_escape(str(explanation)[:60])}"
        elements.append(_markdown(text))
    saved = (article.get("stages") or {}).get("content") or {}
    if saved.get("status") == "failed":
        elements.append(
            _markdown(
                "**内容评价失败**："
                + _md_escape(str(saved.get("error") or "本篇未发送")[:500])
            )
        )
    if url:
        elements.append(_markdown(f"[查看知乎原文]({_link_url(url)})"))
    return elements


def build_content_card(
    article: dict,
    *,
    snapshot_id: int,
    max_payload_bytes: int = 30 * 1024,
    feedback: dict | None = None,
) -> dict:
    elements = _content_elements(article)
    if feedback:
        label = (
            "喜欢"
            if feedback.get("label", feedback.get("sentiment")) == "like"
            else "不喜欢"
        )
        text = f"**已记录反馈**：{label}"
        if feedback.get("reason"):
            text += f"\n原因：{_md_escape(str(feedback['reason'])[:1500])}"
        elements.extend(
            [
                _markdown(text),
                _actions(_button("修改反馈", "feedback_edit", snapshot_id=snapshot_id)),
            ]
        )
    else:
        elements.append(
            _actions(
                _button("喜欢", "like", style="primary", snapshot_id=snapshot_id),
                _button("不喜欢", "dislike", style="danger", snapshot_id=snapshot_id),
            )
        )
    return _card(
        f"Scout · {article.get('title') or '知乎内容'}", elements, max_payload_bytes
    )


def build_feedback_form_card(
    article: dict,
    *,
    label: str,
    snapshot_id: int,
    feedback: dict | None = None,
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    if label not in {"like", "dislike"}:
        raise ValueError("未知反馈类型")
    sentiment = "喜欢" if label == "like" else "不喜欢"
    previous_reason = (
        str(feedback.get("reason") or "")
        if feedback and feedback.get("label") == label
        else ""
    )
    elements = _content_elements(article)
    elements.extend(
        [
            _markdown(
                f"请说明{sentiment}这篇内容的原因，填写后再提交。可说明是否切题、哪里符合或不符合兴趣。"
            ),
            {
                "tag": "form",
                "name": f"zhihu_{label}",
                "elements": [
                    _input(
                        "reason",
                        f"{sentiment}的原因（必填）",
                        "请用自己的话说明原因",
                        value=previous_reason,
                        length=1000,
                    ),
                    _submit(
                        f"提交{sentiment}", f"{label}_submit", snapshot_id=snapshot_id
                    ),
                ],
            },
            _actions(_button("重新选择反馈", "feedback_edit", snapshot_id=snapshot_id)),
        ]
    )
    return _card(f"知乎 · {sentiment}反馈", elements, max_payload_bytes)


def build_filtered_card(
    rows: Sequence[dict],
    *,
    offset: int = 0,
    topic_id: int | None = None,
    scan_id: str,
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    reference = {"topic_id": topic_id} if topic_id is not None else {}
    reference["scan_id"] = scan_id
    elements = [
        _markdown(
            "范围：当前轮次。查看不推荐、基础筛选及异常记录。有完整正文的内容可以补充或修改反馈，影响后续搜索。"
        )
    ]
    if not rows:
        elements.append(_markdown("暂无可查看的结果。"))
    for row in rows[:FILTERED_PER_PAGE]:
        article = row.get("article") or row
        snapshot_id = row.get("snapshot_id") or article.get("snapshot_id")
        title = str(article.get("title") or row.get("content_key") or "知乎内容")
        reason = str(row.get("reason") or row.get("filter_reason") or "尚未判断")
        elements.append(
            _markdown(
                f"**{_md_escape(title[:160])}**\n"
                + _md_escape(FILTER_REASONS.get(reason, reason)[:300])
            )
        )
        if snapshot_id:
            elements.append(
                _actions(
                    _button(
                        "查看内容并反馈",
                        "review",
                        snapshot_id=snapshot_id,
                        reason=reason,
                        scan_id=scan_id,
                    )
                )
            )
    navigation = []
    if offset:
        navigation.append(
            _button(
                "上一页",
                "filtered",
                offset=max(0, offset - FILTERED_PER_PAGE),
                **reference,
            )
        )
    if len(rows) > FILTERED_PER_PAGE:
        navigation.append(
            _button(
                "下一页", "filtered", offset=offset + FILTERED_PER_PAGE, **reference
            )
        )
    navigation.append(_button("返回本轮状态", "scan_status", scan_id=scan_id))
    elements.append(_actions(*navigation))
    return _card(
        "知乎 · 当前轮次结果复核",
        elements,
        max_payload_bytes,
    )


def _model_usage_text(usage: dict) -> str:
    lines = []
    for model, metrics in usage.items():
        effort = "/".join(metrics.get("reasoning_efforts") or [])
        tiers = [
            "Fast" if tier == "priority" else str(tier)
            for tier in metrics.get("service_tiers") or []
        ]
        settings = " · ".join(value for value in (effort, "/".join(tiers)) if value)
        lines.append(
            f"**{_md_escape(str(model))}**"
            + (f" · {_md_escape(settings)}" if settings else "")
            + f"\n调用 {metrics.get('calls', 0)} 次 · 累计请求耗时 {metrics.get('duration_seconds', 0):.1f} 秒"
        )
        stages = metrics.get("stages", {})
        lines.append(
            f"内容评价 {stages.get('content', 0)} 次 · 搜索词提取 {stages.get('rewrite', 0)} 次"
        )
        usage_tokens = metrics.get("token_usage")
        if usage_tokens is not None:
            token_labels = {
                "input_tokens": "输入",
                "cached_input_tokens": "缓存输入",
                "output_tokens": "输出",
                "reasoning_output_tokens": "推理输出",
                "total_tokens": "总计",
            }
            values = " · ".join(
                f"{label} {usage_tokens[name]}"
                for name, label in token_labels.items()
                if usage_tokens.get(name) is not None
            )
            lines.append("token：" + (values or "未提供"))
        else:
            lines.append("token：未提供")
        if metrics.get("usage_missing_calls"):
            lines.append(
                f"其中 {metrics['usage_missing_calls']} 次调用未提供 token 用量"
            )
    return "\n".join(lines)


def build_pending_card(
    scan: dict, *, offset: int = 0, max_payload_bytes: int = 30 * 1024
) -> dict:
    """Show pending candidates in discovery order."""
    pending = [
        (key, value["article"])
        for key, value in scan.get("candidates", {}).items()
        if value.get("status") == "pending"
    ]

    elements = [
        _markdown(
            f"本轮保存了 {len(pending)} 篇未处理候选，按发现顺序排列，页内保留知乎搜索排名。"
        )
    ]
    for key, article in pending[offset : offset + FILTERED_PER_PAGE]:
        votes = article.get("voteup_count")
        text = f"**{_md_escape(str(article.get('title') or key)[:180])}**\n赞同：{votes if type(votes) is int and votes >= 0 else '未知'} · 发布：{_md_escape(str(article.get('published_at') or '未知'))}"
        if article.get("url"):
            text += f"\n[查看知乎原文]({_link_url(article['url'])})"
        elements.append(_markdown(text))
    navigation = []
    if offset:
        navigation.append(
            _button(
                "上一页",
                "scan_pending",
                scan_id=scan["id"],
                offset=max(0, offset - FILTERED_PER_PAGE),
            )
        )
    if len(pending) > offset + FILTERED_PER_PAGE:
        navigation.append(
            _button(
                "下一页",
                "scan_pending",
                scan_id=scan["id"],
                offset=offset + FILTERED_PER_PAGE,
            )
        )
    navigation.append(_button("返回本轮状态", "scan_status", scan_id=scan["id"]))
    elements.append(_actions(*navigation))
    return _card("知乎 · 未处理候选", elements, max_payload_bytes)


def build_stream_card(scan: dict, *, max_payload_bytes: int = 30 * 1024) -> dict:
    request = scan.get("request", {})
    counts = scan.get("candidate_counts", {})
    labels = {
        "running": "正在查找",
        "waiting_user": "已达到目标，等待继续",
        "stopped": "已停止",
        "stopping": "正在停止",
        "exhausted": "搜索结果已耗尽",
        "delivery_failed": "发送失败，等待重试",
        "waiting_login": "等待知乎登录",
        "collector_failed": "采集异常",
        "model_failed": "模型异常",
        "model_busy": "模型繁忙",
        "processing_failed": "处理异常",
    }
    elements = [
        _markdown(
            f"**{_md_escape(scan['topic']['name'])}**\n{labels.get(scan['status'], scan['status'])}\n"
            f"已送达 **{request.get('qualified', 0)}/{request.get('quantity') or 5}** · 待发送 {request.get('pending', 0)} · 发送失败 {request.get('failed', 0)}"
        ),
        _markdown(
            f"已搜索 {scan.get('metrics', {}).get('search_pages', 0)} 页 · 发现 {len(scan.get('candidates', {}))} 篇\n"
            f"待处理 {counts.get('pending', 0)} · 推荐 {counts.get('recommend', 0)} · 不确定 {counts.get('uncertain', 0)} · 不推荐 {counts.get('reject', 0)}\n"
            f"单篇失败 {sum(counts.get(k, 0) for k in ('body_failed', 'content_failed'))}"
        ),
        _markdown(
            "搜索词：" + "；".join(_md_escape(t) for t in scan.get("query_plan", []))
        ),
    ]
    if scan.get("model_usage"):
        elements.append(
            _markdown("**模型开销**\n" + _model_usage_text(scan["model_usage"]))
        )
    if scan["status"] == "exhausted":
        elements.append(
            _markdown(
                "各搜索词的综合和最新分页均已结束，当前候选已处理完；单篇失败内容未自动重试。"
            )
        )
    elements.extend(
        [
            _actions(
                _button(
                    "继续查找", "scan_continue", style="primary", scan_id=scan["id"]
                ),
                _button("停止", "scan_stop", scan_id=scan["id"]),
            ),
            _actions(
                _button("刷新状态", "scan_status", scan_id=scan["id"]),
                _button("未处理候选", "scan_pending", scan_id=scan["id"]),
                _button(
                    "不推荐及异常",
                    "filtered",
                    scan_id=scan["id"],
                    topic_id=scan["topic"]["id"],
                ),
            ),
        ]
    )
    return _card("Scout · 知乎搜索进度", elements, max_payload_bytes)
