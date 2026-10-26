"""agent/tools/ontology_cards.py — 把本体卡片拼装包成 agent 工具.

薄封装: 拼装逻辑全在 core/ontology.py (build_cards), 这里只做
"取单例 -> 过滤 unknown_ids -> 调 build_cards -> 附加 unknown_ids"。

Note: 不用 `from __future__ import annotations`, 理由同 tools/rag_query.py
(base._type_to_json_schema 用 annotation 直接比对类型对象)。
"""
from core.observability import log_event
from core.ontology_view import build_cards, get_ontology
from agent.tools.base import register_tool


@register_tool(category="serving")
def get_entity_cards(ids: list[str]) -> dict:
    """按本体实体 id 拉取四层语义轻卡 (字段/召回边界/语义关联方向)。

    先看 system prompt 里的静态本体目录点名要查的实体 id, 再调本工具拿详情
    (字段/JOIN路径/值集翻译/断腿护栏), 然后再写 SQL。

    Args:
        ids: 本体实体 id 列表 (如 ["lib_loan", "title"]), 从目录里选
    """
    onto = get_ontology()
    if onto is None:
        return {"error": "ontology not initialized"}
    known = [i for i in ids if i in onto.entities]
    unknown = [i for i in ids if i not in onto.entities]
    result = build_cards(onto, known)
    if unknown:
        result["unknown_ids"] = unknown
    _log_cards(ids, known, unknown, result)
    return result


def _log_cards(requested: list, known: list, unknown: list, result: dict) -> None:
    """落 ontology.cards 埋点 (照 sql.query 哲学: 只落排查信号, 不落全量卡片).

    轻卡重构: 记"点了哪些 id / 命中拆分 / 卡片数 / 语义邻居总数 / notes 数",
    卡片全量不落 (agent.tool_call 已记 latency)。
    """
    n_related = sum(len(c.get("related", [])) for c in result["cards"])
    log_event("ontology.cards", {
        "requested": requested,
        "known": known,
        "unknown": unknown,
        "n_cards": len(result["cards"]),
        "n_related": n_related,
        "n_notes": len(result["notes"]),
    })
