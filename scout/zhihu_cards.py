"""Feishu cards for the owner's Zhihu topics and literal-keyword feedback."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .notifier import _check_card_size, _link_url, _markdown, _md_escape

TOPICS_PER_PAGE = 6
FILTERED_PER_PAGE = 6
TIME_RANGES = {"7d": "近 7 天", "30d": "近 30 天", "all": "不限"}
FILTER_REASONS = {
    "keyword_filtered": "正文关键词评分低于当前偏好阈值",
    "date_expired": "发布日期超出话题的时间范围",
    "date_unknown": "发布日期缺失或无法识别",
    "date_future": "发布日期晚于本次扫描开始时间",
    "body_incomplete": "未获取完整正文，暂不能评分",
    "body_failed": "正文读取失败",
    "historical_delivered": "此前已送达",
    "delivery_reserved": "已在投递队列中",
    "not_evaluated": "采集未完成，尚未评分",
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
    # Include the outer send request, whose escaped content is larger than the card.
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
    counts = (
        f"喜欢 {progress.get('likes', 0)} 篇 · 不喜欢 {progress.get('dislikes', 0)} 篇"
    )
    if learning.get("ready"):
        pending = (
            "\n新反馈正在学习，完成后用于后续扫描。"
            if learning.get("training_pending")
            else ""
        )
        error = (
            "\n最近一次学习失败，已保留上一有效偏好。"
            if learning.get("training_error")
            else ""
        )
        return f"偏好学习 v{learning.get('version', 0)} · {counts}{pending}{error}"
    if progress.get("likes") and progress.get("dislikes"):
        status = (
            "最近一次偏好学习失败"
            if learning.get("training_error")
            else "样本已齐备，正在学习"
        )
        return f"{status} · {counts}"
    return f"待校准 · {counts}\n各收集至少 1 篇喜欢和不喜欢内容后开始学习。"


def build_management_card(
    topics: Sequence[dict],
    schedule: dict,
    learning: dict | None = None,
    *,
    offset: int = 0,
    recent_activity: Sequence[str] = (),
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    offset = max(0, offset)
    schedule_text = (
        f"每天 {schedule['time_of_day']}（{schedule.get('timezone', 'Asia/Shanghai')}）"
        if schedule.get("enabled")
        else "未启用"
    )
    elements = [
        _markdown(f"**每日扫描**：{schedule_text}\n{_progress(learning)}"),
        _actions(
            _button("新增话题", "topic_new", style="primary"),
            _button("立即扫描全部", "scan"),
            _button("每日扫描设置", "schedule"),
        ),
        _actions(
            _button("查看过滤结果并补标", "filtered"),
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
        elements.append(_markdown("尚未配置话题。新增话题后可以立即扫描。"))
    for topic in topics[offset : offset + TOPICS_PER_PAGE]:
        topic_id = topic["id"]
        enabled = bool(topic["enabled"])
        terms = "，".join(topic["search_terms"])
        elements.extend(
            [
                {"tag": "hr"},
                _markdown(
                    f"**{_md_escape(topic['name'])}** · {'启用' if enabled else '停用'}\n"
                    f"搜索词：{_md_escape(terms[:500])}\n"
                    f"发布时间：{TIME_RANGES[topic['time_range']]}"
                ),
                _actions(
                    _button("修改", "topic_edit", topic_id=topic_id),
                    _button(
                        "停用" if enabled else "启用",
                        "topic_toggle",
                        topic_id=topic_id,
                        enabled=not enabled,
                    ),
                    _button("立即扫描", "scan", topic_id=topic_id),
                ),
            ]
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
            "搜索词忽略大小写，重复词自动合并。每个词分别搜索最新与综合结果，并扩展回答所属问题默认排序的前 20 个回答。"
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
                    "search_terms",
                    "搜索词",
                    "多个搜索词使用中文或英文逗号分隔",
                    value="，".join(topic.get("search_terms", [])),
                    length=1000,
                ),
                {
                    "tag": "select_static",
                    "name": "time_range",
                    "required": True,
                    "initial_option": topic.get("time_range", "30d"),
                    "placeholder": {"tag": "plain_text", "content": "发布时间范围"},
                    "options": [
                        {
                            "text": {"tag": "plain_text", "content": label},
                            "value": value,
                        }
                        for value, label in TIME_RANGES.items()
                    ],
                },
                _submit("保存话题", "topic_save", **reference),
            ],
        },
        _actions(_button("返回话题管理", "manage")),
    ]
    return _card(
        "修改知乎话题" if topic else "新增知乎话题", elements, max_payload_bytes
    )


def build_schedule_card(schedule: dict, *, max_payload_bytes: int = 30 * 1024) -> dict:
    elements = [
        _markdown("启用后，每日按北京时间扫描全部启用的话题。请填写执行时刻。"),
        {
            "tag": "form",
            "name": "zhihu_schedule",
            "elements": [
                _input(
                    "time_of_day",
                    "每日执行时刻（北京时间）",
                    "24 小时制，格式 HH:MM",
                    value=schedule.get("time_of_day") or "",
                    length=5,
                ),
                _submit("保存并启用每日扫描", "schedule_save"),
            ],
        },
    ]
    if schedule.get("enabled"):
        elements.append(_actions(_button("停用每日扫描", "schedule_disable")))
    elements.append(_actions(_button("返回话题管理", "manage")))
    return _card("知乎每日扫描设置", elements, max_payload_bytes)


def _content_elements(article: dict, reason: str = "") -> list[dict]:
    title = _md_escape(str(article.get("title") or "知乎内容")[:200])
    url = str(article.get("url") or "")
    excerpt = str(
        article.get("body") or article.get("summary") or "（未获取正文）"
    ).strip()
    elements = [
        _markdown(f"**{title}**"),
        _markdown(
            f"作者：{_md_escape(str(article.get('author') or '未知')[:100])}\n"
            f"发布日期：{_md_escape(str(article.get('published_at') or '未知'))}"
        ),
        _markdown(_md_escape(excerpt[:1000]) + ("…" if len(excerpt) > 1000 else "")),
    ]
    if url:
        elements.append(_markdown(f"[查看知乎原文]({_link_url(url)})"))
    if reason:
        elements.append(
            _markdown(
                f"**筛选结果**：{_md_escape(FILTER_REASONS.get(reason, reason)[:500])}"
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
        keywords = feedback.get("keywords") or []
        text = f"**已记录反馈**：{label}"
        if keywords:
            text += f"\n降权关键词：{_md_escape('，'.join(keywords))}"
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


def build_dislike_form_card(
    article: dict,
    *,
    snapshot_id: int,
    feedback: dict | None = None,
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    elements = _content_elements(article)
    elements.extend(
        [
            _markdown(
                "填写该篇正文中实际出现的降权关键词，多个词用中文或英文逗号分隔，忽略大小写。"
            ),
            {
                "tag": "form",
                "name": "zhihu_dislike",
                "elements": [
                    _input(
                        "keywords",
                        "降权关键词（必填）",
                        "例如：广告，推广",
                        value="，".join((feedback or {}).get("keywords") or []),
                        length=1000,
                    ),
                    _submit("提交不喜欢", "dislike_submit", snapshot_id=snapshot_id),
                ],
            },
            _actions(_button("重新选择反馈", "feedback_edit", snapshot_id=snapshot_id)),
        ]
    )
    return _card("知乎 · 不喜欢反馈", elements, max_payload_bytes)


def build_filtered_card(
    rows: Sequence[dict],
    *,
    offset: int = 0,
    topic_id: int | None = None,
    max_payload_bytes: int = 30 * 1024,
) -> dict:
    """Accept one extra row to detect the next page without a count query."""
    reference = {"topic_id": topic_id} if topic_id is not None else {}
    elements = [
        _markdown("查看已过滤内容，补充喜欢或不喜欢反馈，以纠正后续扫描的偏好判断。")
    ]
    if not rows:
        elements.append(_markdown("暂无可查看的过滤结果。"))
    for row in rows[:FILTERED_PER_PAGE]:
        article = row.get("article") or row
        snapshot_id = row.get("snapshot_id") or article.get("snapshot_id")
        title = str(article.get("title") or row.get("content_key") or "知乎内容")
        reason = str(row.get("reason") or row.get("filter_reason") or "被筛除")
        elements.append(
            _markdown(
                f"**{_md_escape(title[:160])}**\n{_md_escape(FILTER_REASONS.get(reason, reason)[:300])}"
            )
        )
        if snapshot_id:
            elements.append(
                _actions(
                    _button(
                        "查看内容并打标",
                        "review",
                        snapshot_id=snapshot_id,
                        reason=reason,
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
    navigation.append(_button("返回话题管理", "manage"))
    elements.append(_actions(*navigation))
    return _card("知乎过滤结果 · 补充打标", elements, max_payload_bytes)
