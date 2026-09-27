"""Feishu cards for the owner's Zhihu semantic scans and feedback."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from .notifier import _check_card_size, _link_url, _markdown, _md_escape

TOPICS_PER_PAGE = 6
FILTERED_PER_PAGE = 6
TIME_RANGES = {"30d": "近 30 天"}
TRIAL_PARAMETERS = {
    "evaluation_batch_size": ("每批评价候选数", 8),
    "search_latest_pages_per_advance": ("每词最新搜索推进页数", 1),
    "search_general_pages_per_advance": ("每词综合搜索推进页数", 1),
    "question_pages_per_advance": ("每题回答推进页数", 1),
}
FILTER_REASONS = {
    "not_recommended": "语义判断：不推荐",
    "reject": "语义判断：不推荐",
    "irrelevant": "Luna 判断：与话题无关",
    "preference_rejected": "Sol 判断：不符合已生效偏好",
    "date_expired": "发布日期超出本轮近 30 天窗口",
    "date_unknown": "发布日期缺失或无法识别",
    "date_missing": "发布日期缺失",
    "date_invalid": "发布日期无法识别",
    "date_future": "发布日期晚于本次扫描开始时间",
    "body_incomplete": "未获取完整正文，暂不能判断",
    "body_failed": "正文读取失败",
    "model_failed": "模型判断失败，尚无推荐结论",
    "relevance_failed": "Luna 相关性判断失败，尚无相关性结论",
    "evaluation_failed": "Sol 摘要／偏好评价失败，已保存的相关性结论保留",
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
            "\n尚无有效偏好档案，先等已提交反馈归纳后再开始下一批。"
            if has_feedback
            else "\n当前偏好为空，只判断话题相关性；相关内容直接推荐，质量标准等待真实反馈建立。"
        )
    training_status = learning.get("training_status")
    if training_status == "busy":
        text += "\n偏好更新正在等待模型空闲，最新反馈生效后再开始下一批。"
    elif training_status == "failed" or learning.get("training_error"):
        text += "\n偏好更新失败，上一有效版本保留。明确继续可重试更新；成功后再次明确继续，才开始下一批。"
    elif learning.get("training_pending") or training_status in {
        "pending",
        "running",
        "waiting",
    }:
        text += "\n已提交反馈正在等待归纳；更新成功后请明确继续下一批。"
    elif learning.get("ready"):
        text += "\n当前有效反馈已归纳，明确继续后下一批使用此版本。"
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
            f"主动开始扫描 · 每轮固定近 30 天\n每批完成后等待反馈与明确继续，首批最多评价 8 篇。\n{_progress(learning)}"
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
            "**发布时间：近 30 天**\n描述你关注的内容。每批默认处理 8 篇，可调整至 32 篇，先按完整正文判断相关性，再为准备发送的内容生成摘要；本批完成后等待你反馈并明确继续。"
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
    evaluation = (
        article.get("evaluation")
        or article.get("semantic_result")
        or article.get("semantic")
        or {}
    )
    relevance = article.get("relevance") or {}
    summary = str(evaluation.get("summary") or article.get("summary") or "").strip()
    decision = article.get("decision") or evaluation.get("decision")
    explanation = article.get("decision_reason") or evaluation.get("reason")
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
        verdict = relevance.get("relevance", "")
        text = f"**Luna 相关性**：{_md_escape(labels.get(verdict, str(verdict)))}"
        if relevance.get("reason"):
            text += f"\n{_md_escape(str(relevance['reason'])[:1500])}"
        elements.append(_markdown(text))
    if decision and (not relevance or article.get("evaluation")):
        label = "Sol 摘要／偏好评价" if relevance else "语义判断"
        text = f"**{label}**：{_md_escape(DECISIONS.get(decision, str(decision)))}"
        if explanation:
            text += f"\n{_md_escape(str(explanation)[:1500])}"
        elements.append(_markdown(text))
    for stage, label in (
        ("relevance", "Luna 相关性"),
        ("evaluation", "Sol 摘要／偏好评价"),
    ):
        saved = (article.get("stages") or {}).get(stage) or {}
        if saved.get("status") == "failed":
            attempts = saved.get("attempts") or []
            error = (attempts[-1].get("error") if attempts else "") or "等待重试"
            elements.append(
                _markdown(f"**{label}失败**：{_md_escape(str(error)[:500])}")
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
    elements.append(_actions(_button("知乎话题管理", "manage", separate=True)))
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
                f"请说明{sentiment}这篇内容的原因，填写后再提交。反馈用于归纳后续批次的语义偏好。"
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
            f"范围：{scope}。查看不推荐、基础筛选及异常记录。有完整正文的内容可以补充或修改反馈，影响后续批次。"
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
                "填写希望新增的合格内容下限。每次只处理一批，本批结束后等待你反馈并明确继续；当前批次多出的推荐及不确定内容一起发送，实际耗尽时报告缺口。"
            ),
            {
                "tag": "form",
                "name": "zhihu_append",
                "elements": [
                    _input(
                        "quantity", "本次新增合格内容至少多少篇", "例如：5", length=5
                    ),
                    _submit(
                        "保存目标并处理一批", "scan_append_submit", scan_id=scan_id
                    ),
                ],
            },
            _actions(_button("返回本轮状态", "scan_status", scan_id=scan_id)),
        ],
        max_payload_bytes,
    )


def build_parameter_form_card(
    scan: dict, *, max_payload_bytes: int = 30 * 1024
) -> dict:
    params = scan.get("params") or {}
    current = "\n".join(
        f"{label}：{params.get(name, initial)}"
        for name, (label, initial) in TRIAL_PARAMETERS.items()
    )
    return _card(
        "知乎 · 调整下一批参数",
        [
            _markdown(
                "一次只修改一项，便于比较前后批次。先等待最新反馈完成归纳，再应用参数并明确继续一批。\n\n**当前参数**\n"
                + current
                + "\n工程初值：每批 8 篇，可调整至 32 篇，每词最新、综合各 1 页，每题回答 1 页。"
            ),
            _markdown(
                "调整搜索或同题页数时，下一批会先按调整后的规模推进一次采集，再合并排序。调整评价篇数时，使用当前候选池；候选不足再推进采集。"
            ),
            {
                "tag": "form",
                "name": "zhihu_parameters",
                "elements": [
                    {
                        "tag": "select_static",
                        "name": "parameter_name",
                        "required": True,
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "选择本次唯一要修改的参数",
                        },
                        "options": [
                            {
                                "text": {"tag": "plain_text", "content": label},
                                "value": name,
                            }
                            for name, (label, _) in TRIAL_PARAMETERS.items()
                        ],
                    },
                    _input(
                        "parameter_value",
                        "新的参数值（评价篇数 1–32，分页 1–100）",
                        "填写一个正整数",
                        length=3,
                    ),
                    _submit(
                        "应用这一项并继续一批",
                        "scan_parameters_submit",
                        scan_id=scan["id"],
                    ),
                ],
            },
            _actions(_button("返回本轮状态", "scan_status", scan_id=scan["id"])),
        ],
        max_payload_bytes,
    )


def _rate(numerator: int, denominator: int, *, no_feedback: bool = False) -> str:
    if denominator <= 0 or no_feedback:
        return "暂无数据"
    return f"{numerator / denominator:.1%}（{numerator}/{denominator}）"


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
            + f"\n调用 {metrics.get('calls', 0)} 次 · 耗时 {metrics.get('duration_seconds', 0):.1f} 秒"
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


def build_batch_summary_card(
    scan: dict,
    batch: dict | None = None,
    *,
    latest_learning: dict | None = None,
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    """Durable summary shared by completed batches and on-demand scan status."""
    batch = batch or {}
    counts = dict(batch.get("stats") or batch.get("counts") or {})
    for decision in (
        "recommend",
        "uncertain",
        "reject",
        "body_failed",
        "model_failed",
        "relevance_failed",
        "evaluation_failed",
    ):
        counts[decision] = (
            sum(
                result.get("decision", result.get("error")) == decision
                for result in batch.get("results", {}).values()
            )
            if batch.get("results")
            else counts.get(decision, 0)
        )
    preference = batch.get("preference") or {}
    learning = latest_learning if latest_learning is not None else scan.get("learning")
    status_labels = {
        "running": "进行中",
        "waiting_user": "等待本批反馈与明确继续",
        "waiting_preference": "等待最新反馈生效",
        "stopped": "本轮已结束",
        "exhausted": "结果实际耗尽",
        "waiting_login": "等待知乎登录",
        "waiting_model": "等待模型空闲",
        "model_busy": "等待模型空闲",
        "stopping": "正在停止，在途结果会保留",
        "model_failed": "模型失败",
        "collector_failed": "采集失败",
        "processing_failed": "处理异常",
        "delivery_failed": "发送失败",
        "paused": "已暂停",
        "completed": "已完成",
        "failed": "失败",
    }
    stop_labels = {
        "user_stop": "用户主动结束",
        "stopped_by_user": "用户主动结束",
        "batch_complete": "本批已完成，等待决定是否继续",
        "exhausted": "搜索和已发现问题的分页均已实际耗尽",
        "target_reached": "本次追加已达到指定下限",
        "batch_completed": "本批已完成，等待决定是否继续",
        "results_exhausted": "搜索和已发现问题的分页均已实际耗尽",
        "results_exhausted_with_errors": "当前可处理结果已用完，仍有异常内容待复核或重试",
        "append_pending": "尚未达到追加下限，等待本批反馈与明确继续",
        "append_waiting_user": "尚未达到追加下限，等待本批反馈与明确继续",
        "feedback_update_pending": "等待最新反馈归纳成功后明确继续",
        "feedback_required": "等待本批真实反馈与明确继续",
        "preference_updated_continue_required": "最新反馈已生效，等待再次明确继续",
        "preference_pending": "等待最新反馈完成归纳后明确继续",
        "preference_failed": "偏好更新失败，恢复成功后再次明确继续",
        "delivery_failed": "原发送队列有失败，继续时沿原队列重试",
        "processing_failed": "处理异常，已有结果保留，修复后可继续",
        "collector_failed": "采集失败，已有候选和分页进度保留",
        "model_failed": "模型判断失败，已有结果保留",
        "model_busy": "等待模型空闲，已有进度保留",
        "waiting_login": "等待完成知乎登录，已有进度保留",
    }
    candidates = scan.get("candidates") or {}
    candidate_rows = candidates.values() if isinstance(candidates, dict) else candidates
    pending_candidates = sum(
        row.get("status") in {"pending", "ready", "discovered"}
        for row in candidate_rows
    )
    remaining_searches = sum(
        not row.get("is_end", False) for row in scan.get("queries", [])
    )
    remaining_questions = sum(
        not row.get("is_end", False) for row in (scan.get("questions") or {}).values()
    )
    coverage = scan.get("coverage_summary") or {}
    topic = scan.get("topic") or scan.get("topic_snapshot") or {}
    plan = scan.get("query_plan") or []
    if isinstance(plan, dict):
        plan = plan.get("search_terms") or plan.get("queries") or []
    terms = list(
        dict.fromkeys(
            str(value.get("query") if isinstance(value, dict) else value)
            for value in plan
        )
    )
    terms_text = "，".join(terms)
    if len(terms_text) > 2000:
        terms_text = terms_text[:2000] + "…（完整查询计划已保存到本轮报告）"
    batch_id = batch.get("number", batch.get("id", ""))
    elements = [
        _markdown(
            f"**{_md_escape(topic.get('name') or '知乎话题')}** · 本轮近 30 天\n"
            f"扫描状态：{_md_escape(str(scan.get('status_label') or status_labels.get(scan.get('status'), scan.get('status')) or '进行中'))}\n"
            f"实际搜索词（{len(terms)} 个）：{_md_escape(terms_text or '等待转写')}"
        )
    ]
    if batch:
        elements.append(
            _markdown(
                f"**本批 {batch_id}** · {preference.get('label') or ('偏好 v' + str(preference.get('version') or batch.get('preference_version') or 0))}\n"
                f"推荐 {counts.get('recommended', counts.get('recommend', 0))} · 不确定 {counts.get('uncertain', 0)} · 不推荐 {counts.get('not_recommended', counts.get('reject', 0))}\n"
                f"正文异常 {counts.get('body_failed', 0)} · Luna 异常 {counts.get('relevance_failed', 0)} · Sol 异常 {counts.get('evaluation_failed', 0)}"
                + "\n"
                f"已发送 {counts.get('sent', counts.get('delivered', 0))} · 待发送 {counts.get('pending', counts.get('delivery_pending', 0))} · 发送失败 {counts.get('failed', counts.get('delivery_failed', 0))}"
            )
        )
    if batch:
        recommend = counts.get("recommend", counts.get("recommended", 0))
        uncertain = counts.get("uncertain", 0)
        reject = counts.get("reject", counts.get("not_recommended", 0))
        evaluated = recommend + uncertain + reject
        likes = counts.get("feedback_likes", 0)
        dislikes = counts.get("feedback_dislikes", 0)
        feedback_count = likes + dislikes
        sent = counts.get("sent", counts.get("delivered", 0))
        metrics = (
            f"**本批反馈与效果**\n模型放行率：{_rate(recommend + uncertain, evaluated)}\n"
            f"用户通过率：{_rate(likes, feedback_count)}\n"
            f"反馈覆盖率：{_rate(feedback_count, sent, no_feedback=feedback_count == 0)}\n"
            f"自动推送反馈：喜欢 {likes} · 不喜欢 {dislikes} · 实际送达 {sent}\n"
            f"过滤复核反馈：喜欢 {counts.get('feedback_review_likes', 0)} · 不喜欢 {counts.get('feedback_review_dislikes', 0)}"
        )
        elements.append(_markdown(metrics))
        elements.append(
            _markdown(
                "模型放行率按评价成功内容计算；用户通过率按已自动送达内容的当前反馈计算，反馈覆盖率按已反馈篇数除以实际送达篇数计算。过滤复核另列，未反馈不代表不喜欢。"
            )
        )
        params = batch.get("params") or scan.get("params") or {}
        elements.append(
            _markdown(
                "**本批实际参数**\n"
                + " · ".join(
                    f"{label} {params.get(name, initial)}"
                    for name, (label, initial) in TRIAL_PARAMETERS.items()
                )
            )
        )
    model_usage = batch.get("model_usage") if batch else scan.get("model_usage")
    if model_usage:
        elements.append(
            _markdown("**模型实际开销**\n" + _model_usage_text(model_usage))
        )
    if learning is not None:
        elements.append(_markdown(_progress(learning)))
    elements.append(
        _markdown(
            "本批完成后先查看并反馈内容，等反馈更新成功，再明确选择继续一批。追加目标尚未达到也会逐批等待。"
        )
    )
    elements.append(
        _markdown(
            f"池中未处理：{coverage.get('pending_candidates', scan.get('pending_candidates', pending_candidates))} 篇\n"
            f"尚可继续的搜索：{coverage.get('remaining_searches', remaining_searches)} 路 · 问题：{coverage.get('remaining_questions', remaining_questions)} 个\n"
            "排序覆盖当前已发现候选，未覆盖全部知乎内容。"
        )
    )
    if scan.get("stop_reason"):
        elements.append(
            _markdown(
                f"本轮停止原因：{_md_escape(str(stop_labels.get(scan['stop_reason'], scan['stop_reason']))[:500])}"
            )
        )
    if scan.get("last_error"):
        error = scan["last_error"]
        if isinstance(error, dict):
            error_labels = {
                "processing": "处理异常",
                "processing_failed": "处理异常",
                "protocol": "采集响应或完整性校验失败",
                "network": "采集连接失败",
                "http_error": "采集接口失败",
                "collection": "采集任务失败",
                "login": "知乎登录失效",
                "authentication": "采集器认证失败",
                "conflict": "采集请求参数冲突",
                "missing": "采集任务不存在",
                "model_busy": "模型繁忙",
                "model_failed": "模型判断失败",
                "relevance_failed": "Luna 相关性判断失败",
                "evaluation_failed": "Sol 摘要／偏好评价失败",
                "delivery_failed": "发送失败",
                "preference_failed": "偏好更新失败",
                "preference_pending": "等待最新反馈生效",
                "preference_update": "偏好更新尚未完成",
            }
            label = error_labels.get(error.get("kind"), "处理异常")
            message = error.get("message")
            error = f"{label}：{message}" if message else label
        elements.append(_markdown(f"当前异常：{_md_escape(str(error)[:500])}"))
    append = scan.get("append_progress") or {}
    request = scan.get("request") or {}
    if not append and request.get("action") == "append":
        append = {
            "target": request.get("quantity", 0),
            "qualified": request.get("qualified", 0),
        }
    if append:
        elements.append(
            _markdown(
                f"本次追加下限 {append.get('target', 0)} 篇 · 已新增合格 {append.get('qualified', 0)} 篇 · 缺口 {max(0, append.get('target', 0) - append.get('qualified', 0))} 篇"
            )
        )
    scan_id = scan["id"]
    elements.append(
        _actions(
            _button("继续一批", "scan_continue", style="primary", scan_id=scan_id),
            _button("指定追加数量", "scan_append", scan_id=scan_id),
            _button("调整下一批参数", "scan_parameters", scan_id=scan_id),
            _button("本轮够了", "scan_stop", scan_id=scan_id),
        )
    )
    elements.append(
        _actions(
            _button("刷新本轮状态", "scan_status", scan_id=scan_id),
            _button("查看未处理候选", "scan_pending", scan_id=scan_id),
            _button(
                "查看本轮不推荐及异常",
                "filtered",
                scan_id=scan_id,
                **({"topic_id": topic["id"]} if topic.get("id") else {}),
            ),
            _button("话题管理", "manage"),
        )
    )
    return _card("Scout · 知乎本批结果", elements, max_payload_bytes)


def build_pending_card(
    scan: dict, *, offset: int = 0, max_payload_bytes: int = 30 * 1024
) -> dict:
    """Show the saved pool in the same vote/date/identity order as evaluation."""
    pending = [
        (key, value["article"])
        for key, value in scan.get("candidates", {}).items()
        if value.get("status") == "pending"
    ]

    def order(row):
        key, article = row
        votes = article.get("voteup_count")
        if type(votes) is not int or votes < 0:
            votes = None
        try:
            stamp = datetime.fromisoformat(
                article.get("published_at") or ""
            ).timestamp()
        except ValueError, TypeError, OverflowError:
            stamp = 0
        return (votes is None, -(votes or 0), -stamp, key)

    pending.sort(key=order)
    elements = [
        _markdown(
            f"本轮保存了 {len(pending)} 篇未处理候选，按赞同数、发布时间和稳定身份排序。新候选加入后，下一评价批次会重新排序。"
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
