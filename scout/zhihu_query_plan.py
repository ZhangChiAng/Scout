"""Finite, case-sensitive query plans."""

import copy
from datetime import UTC, datetime

from .config import ConfigError

EVALUATION_POLICY = "relevance_v2"


def install_query_plan(state, terms):
    """Freeze every actual term; casing is part of the search identity."""
    if state.get("query_plan_frozen"):
        raise ConfigError("本轮查询计划已固定，不能自动追加或替换搜索词")
    if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
        raise ConfigError("查询计划必须为有限的搜索词列表")
    plan = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    if not plan:
        raise ConfigError("查询计划不能为空")
    saved = {
        (row["query"], row["sort"]): row
        for row in state.get("reusable_queries", state.get("queries", []))
    }
    streams = []
    contributions = {}
    for term in plan:
        contributions[term] = {
            "new_candidates": 0,
            "duplicates": 0,
            "records": 0,
            "pages": 0,
            "reused_pages": 0,
        }
        for order in ("latest", "general"):
            previous = saved.get((term, order))
            stream = (
                copy.deepcopy(previous)
                if previous
                else {
                    "query": term,
                    "sort": order,
                    "cursor": None,
                    "is_end": False,
                    "pages": 0,
                }
            )
            stream["reused_pages"] = stream["pages"]
            stream["new_candidates"] = 0
            stream["duplicates"] = 0
            stream["records"] = 0
            contributions[term]["reused_pages"] += stream["pages"]
            streams.append(stream)
    state.update(
        query_plan=plan,
        queries=streams,
        query_contributions=contributions,
        query_plan_frozen=True,
        query_plan_generated_at=datetime.now(UTC).isoformat(),
    )
