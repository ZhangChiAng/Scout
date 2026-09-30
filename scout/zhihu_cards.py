"""Feishu cards for the owner's Zhihu semantic scans and feedback."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .notifier import _check_card_size, _link_url, _markdown, _md_escape

TOPICS_PER_PAGE = 6
FILTERED_PER_PAGE = 6
TIME_RANGES = {"30d": "近 30 天"}
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


def _progress(learning: dict | None) -> str:
    learning = learning or {}
    progress = learning.get("progress") or {}
    text = (
        f"知乎语义偏好 v{learning.get('version') or 0} · "
        f"喜欢 {progress.get('likes', 0)} 篇 · 不喜欢 {progress.get('dislikes', 0)} 篇"
    )
    has_feedback = bool(
        learning.get("feedback_revision")
        or progress.get("likes")
        or progress.get("dislikes")
    )
    if not learning.get("ready"):
        text += (
            "\n尚无有效偏好档案，先按空偏好处理。"
            if has_feedback
            else "\n当前偏好为空，只判断话题相关性；相关内容直接推荐，质量标准等待真实反馈建立。"
        )
    training_status = learning.get("training_status")
    if training_status == "busy":
        text += "\n偏好更新正在等待模型空闲，当前使用已完成的偏好。"
    elif training_status == "failed" or learning.get("training_error"):
        text += "\n偏好更新失败，保留上一有效版本。"
    elif learning.get("training_pending") or training_status in {
        "pending",
        "running",
        "waiting",
    }:
        text += "\n已提交反馈正在等待归纳；继续使用当前有效偏好。"
    elif learning.get("ready"):
        text += "\n当前有效反馈已归纳，继续查找时使用最新已完成版本。"
    return text


def build_management_card(
    topics: Sequence[dict],
    schedule: dict | None = None,
    learning: dict | None = None,
    *,
    offset: int = 0,
    recent_activity: Sequence[str] = (),
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    offset = max(0, offset)
    elements = [
        _markdown(
            f"主动开始扫描 · 每轮固定近 30 天\n逐条推送，成功送达 5 条后暂停；继续无需先反馈。\n{_progress(learning)}"
        ),
        _actions(
            _button("新增话题", "topic_new", style="primary"),
            _button("开始全部话题新一轮", "scan"),
        ),
        _actions(
            _button("结果复核", "filtered"),
            _button("刷新状态", "manage", offset=offset),
        ),
    ]
    if recent_activity:
        elements.append(
            _markdown(
                "**最近状态（北京时间）**\n"
                + "\n".join(_md_escape(item) for item in recent_activity)
            )
        )
    if not topics:
        elements.append(_markdown("新增话题并描述你关注的内容，即可保存并开始扫描。"))
    for topic in topics[offset : offset + TOPICS_PER_PAGE]:
        topic_id = topic["id"]
        enabled = bool(topic["enabled"])
        description = topic.get("description") or "，".join(
            topic.get("search_terms") or []
        )
        plan = topic.get("latest_query_plan") or []
        if isinstance(plan, dict):
            plan = plan.get("search_terms") or plan.get("queries") or []
        terms = "，".join(
            str(term.get("query") if isinstance(term, dict) else term) for term in plan
        )
        details = (
            f"**{_md_escape(topic['name'])}** · {'启用' if enabled else '停用'}\n"
            f"关注描述：{_md_escape(description[:800])}\n发布时间：近 30 天"
        )
        if terms:
            details += f"\n最近一轮实际搜索词：{_md_escape(terms[:600])}"
        elements.extend(
            [
                {"tag": "hr"},
                _markdown(details),
                _actions(
                    _button("修改", "topic_edit", topic_id=topic_id),
                    _button(
                        "停用" if enabled else "启用",
                        "topic_toggle",
                        topic_id=topic_id,
                        enabled=not enabled,
                    ),
                    _button("开始新一轮", "scan", topic_id=topic_id),
                ),
            ]
        )
        if topic.get("latest_scan_id"):
            elements.append(
                _actions(
                    _button(
                        "查看本轮状态与继续",
                        "scan_status",
                        scan_id=topic["latest_scan_id"],
                    )
                )
            )
    navigation = []
    if offset:
        navigation.append(
            _button("上一页", "manage", offset=max(0, offset - TOPICS_PER_PAGE))
        )
    if len(topics) > offset + TOPICS_PER_PAGE:
        navigation.append(_button("下一页", "manage", offset=offset + TOPICS_PER_PAGE))
    if navigation:
        elements.append(_actions(*navigation))
    return _card("Scout · 知乎话题管理", elements, max_payload_bytes)


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


def build_topic_form_card(
    topic: dict | None = None, *, max_payload_bytes: int = 30 * 1024
) -> dict:
    topic = topic or {}
    reference = {"topic_id": topic["id"]} if topic else {}
    elements = [
        _markdown(
            "**发布时间：近 30 天**\n描述你关注的内容。逐篇判断相关性并生成摘要，送达 5 条后暂停，继续无需先反馈。"
        ),
        {
            "tag": "form",
            "name": "zhihu_topic",
            "elements": [
                _input(
                    "name",
                    "话题名称",
                    "例如：人工智能研究",
                    value=topic.get("name", ""),
                    length=100,
                ),
                _input(
                    "description",
                    "自然语言关注描述",
                    "例如：关注大模型推理能力的真实进展、研究方法和失败案例",
                    value=topic.get("description")
                    or "，".join(topic.get("search_terms") or []),
                    length=4000,
                ),
                _submit("保存并开始", "topic_save", **reference),
            ],
        },
        _actions(_button("返回话题管理", "manage")),
    ]
    return _card(
        "修改知乎话题" if topic else "新增知乎话题", elements, max_payload_bytes
    )


def build_schedule_card(schedule: dict, *, max_payload_bytes: int = 30 * 1024) -> dict:
    """Keep old imports working while making old schedule cards harmless."""
    return _card(
        "知乎扫描",
        [
            _markdown("每日扫描已停用，请在话题管理中主动开始新一轮。"),
            _actions(_button("话题管理", "manage")),
        ],
        max_payload_bytes,
    )


def _content_elements(article: dict, reason: str = "") -> list[dict]:
    title = _md_escape(str(article.get("title") or "知乎内容")[:200])
    url = str(article.get("url") or "")
    evaluation = article.get("semantic") or {}
    relevance = evaluation.get("relevance")
    summary = str(evaluation.get("summary") or "").strip()
    decision = evaluation.get("decision")
    explanation = evaluation.get("reason")
    votes = article.get("voteup_count")
    elements = [
        _markdown(f"**{title}**"),
        _markdown(
            f"作者：{_md_escape(str(article.get('author') or '未知')[:100])}\n"
            f"发布日期：{_md_escape(str(article.get('published_at') or '未知'))}\n"
            f"采集时赞同：{votes if type(votes) is int and votes >= 0 else '未知'}"
        ),
    ]
    if summary:
        elements.append(
            _markdown(
                "**摘要**\n"
                + _md_escape(summary[:3000])
                + ("…" if len(summary) > 3000 else "")
            )
        )
    elif not relevance and not article.get("stages"):
        # Unevaluated candidates show source text explicitly as an excerpt.
        excerpt = str(article.get("body") or "（未获取正文）").strip()
        elements.append(_markdown("**正文摘录**\n" + _md_escape(excerpt[:1000])))
    if relevance:
        labels = {
            "relevant": "相关",
            "irrelevant": "明确无关",
            "uncertain": "相关性存疑",
        }
        verdict = relevance
        text = f"**相关性**：{_md_escape(labels.get(verdict, str(verdict)))}"
        if evaluation.get("relevance_reason"):
            text += f"\n{_md_escape(str(evaluation['relevance_reason'])[:1500])}"
        elements.append(_markdown(text))
    if decision:
        label = "最终评价"
        text = f"**{label}**：{_md_escape(DECISIONS.get(decision, str(decision)))}"
        if explanation:
            text += f"\n{_md_escape(str(explanation)[:1500])}"
        elements.append(_markdown(text))
    saved = (article.get("stages") or {}).get("content") or {}
    if saved.get("status") == "failed":
        elements.append(
            _markdown(
                "**内容评价失败**："
                + _md_escape(str(saved.get("error") or "本篇未发送")[:500])
            )
        )
    metrics = saved.get("telemetry", {})
    if metrics:
        elements.append(
            _markdown(
                f"**内容评价开销**：{_md_escape(str(metrics.get('model') or '未知模型'))} · "
                f"{_md_escape(str(metrics.get('reasoning_effort') or ''))} · "
                f"调用 {metrics.get('calls', 0)} 次 · {metrics.get('duration_seconds', 0):.1f} 秒\n"
                + "token："
                + _md_escape(
                    json.dumps(metrics.get("token_usage"), ensure_ascii=False)
                    if metrics.get("token_usage") is not None
                    else "未提供"
                )
            )
        )
    if url:
        elements.append(_markdown(f"[查看知乎原文]({_link_url(url)})"))
    if reason:
        elements.append(
            _markdown(
                f"**处理记录**：{_md_escape(FILTER_REASONS.get(reason, reason)[:500])}"
            )
        )
    return elements


def build_content_card(
    article: dict,
    *,
    snapshot_id: int,
    max_payload_bytes: int = 30 * 1024,
    feedback: dict | None = None,
    learning: dict | None = None,
    reason: str = "",
    scan_id: str | None = None,
) -> dict:
    elements = _content_elements(article, reason)
    if learning is not None:
        elements.append(_markdown(_progress(learning)))
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
    if scan_id:
        elements.append(
            _actions(
                _button("查看搜索进度", "scan_status", scan_id=scan_id),
                _button("停止搜索", "scan_stop", scan_id=scan_id),
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
                f"请说明{sentiment}这篇内容的原因，填写后再提交。反馈用于归纳后续搜索的语义偏好。"
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
    scan_id: str | None = None,
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    reference = {"topic_id": topic_id} if topic_id is not None else {}
    if scan_id:
        reference["scan_id"] = scan_id
    scope = "当前轮次" if scan_id else "全部保留轮次"
    elements = [
        _markdown(
            f"范围：{scope}。查看不推荐、基础筛选及异常记录。有完整正文的内容可以补充或修改反馈，影响后续搜索。"
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
                        **({"scan_id": scan_id} if scan_id else {}),
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
    navigation.append(
        _button("返回本轮状态", "scan_status", scan_id=scan_id)
        if scan_id
        else _button("返回话题管理", "manage")
    )
    elements.append(_actions(*navigation))
    return _card(
        "知乎 · 当前轮次结果复核" if scan_id else "知乎 · 结果复核",
        elements,
        max_payload_bytes,
    )


def build_append_form_card(scan_id: str, *, max_payload_bytes: int = 30 * 1024) -> dict:
    return _card(
        "知乎 · 指定追加数量",
        [
            _markdown(
                "填写希望新增送达的数量。逐条推送，达到目标后暂停；搜索耗尽时报告实际送达数量。"
            ),
            {
                "tag": "form",
                "name": "zhihu_append",
                "elements": [
                    _input("quantity", "本次新增送达多少条", "例如：5", length=5),
                    _submit("保存目标并继续", "scan_append_submit", scan_id=scan_id),
                ],
            },
            _actions(_button("返回本轮状态", "scan_status", scan_id=scan_id)),
        ],
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
    if scan.get("schema_version") != 5:
        return _card(
            "知乎 · 历史扫描",
            [_markdown("旧扫描已终止，请重新发起搜索。")],
            max_payload_bytes,
        )
    request = scan.get("request", {})
    counts = scan.get("candidate_counts", {})
    relevance = scan.get("relevance_counts", {})
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
            f"相关 {relevance.get('relevant', 0)} · 无关 {relevance.get('irrelevant', 0)} · 相关性存疑 {relevance.get('uncertain', 0)}\n"
            f"单篇失败 {sum(counts.get(k, 0) for k in ('body_failed', 'content_failed'))}"
        ),
        _markdown(
            "搜索词：" + "；".join(_md_escape(t) for t in scan.get("query_plan", []))
        ),
        _markdown(
            "逐条推送，送达够数后暂停。最新和综合交替搜索，按搜索结果顺序处理；不确定内容会说明疑点。继续无需先反馈。"
        ),
    ]
    preference = scan.get("stream", {}).get("preference", {})
    elements.append(
        _markdown(
            f"本次固定偏好版本：{preference.get('version') or '空偏好'} · 同时评价 1 篇"
        )
    )
    if scan.get("learning"):
        elements.append(_markdown(_progress(scan["learning"])))
    if request.get("first_delivery_seconds") is not None:
        elements.append(
            _markdown(f"本次首条送达耗时 {request['first_delivery_seconds']:.1f} 秒")
        )
    if request.get("target_delivery_seconds") is not None:
        elements.append(
            _markdown(f"本次达到目标耗时 {request['target_delivery_seconds']:.1f} 秒")
        )
    if scan.get("last_error"):
        elements.append(
            _markdown(
                "当前异常："
                + _md_escape(str(scan["last_error"].get("message", ""))[:500])
            )
        )
    if scan.get("model_usage"):
        elements.append(
            _markdown("**模型开销**\n" + _model_usage_text(scan["model_usage"]))
        )
    if scan["status"] == "exhausted":
        elements.append(
            _markdown(
                "各搜索词的最新和综合分页均已结束，当前候选已处理完；单篇失败内容未自动重试。"
            )
        )
    elements.extend(
        [
            _actions(
                _button(
                    "继续查找", "scan_continue", style="primary", scan_id=scan["id"]
                ),
                _button("指定追加数量", "scan_append", scan_id=scan["id"]),
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
