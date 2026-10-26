"""agent/tools/ontology_concept.py — 把概念详情拼装包成 agent 工具 (渐进披露第零环)。

薄封装: 拼装逻辑在 core/ontology.py (build_concept_detail), 这里只做
"取单例 → 调 build_concept_detail → log"。

Note: 不用 `from __future__ import annotations` (同 tools/ontology_cards.py)。
"""
from core.observability import log_event
from core.ontology_view import build_concept_detail, get_ontology
from agent.tools.base import register_tool


@register_tool(category="serving")
def get_concept_detail(concept_id: str) -> dict:
    """按概念 id 拿派生业务概念的判定条件 + 涉及实体集 (查这类业务术语前必调)。

    先看 system prompt 里的概念目录点名要查的概念 id, 再调本工具拿详情
    (判定条件 col/op/value + involved_entities)。返回不含 SQL/JOIN: 跨表时拿
    involved_entities 两两调 get_join_path 拿精确 JOIN, 再自己组装 SQL。

    Args:
        concept_id: 概念 id (如 "active_member"), 从概念目录里选
    """
    onto = get_ontology()
    if onto is None:
        return {"error": "ontology not initialized"}
    result = build_concept_detail(onto, concept_id)
    log_event("ontology.concept_detail", {
        "concept_id": concept_id,
        "ok": "error" not in result,
        "n_conditions": len(result.get("conditions", [])),
        "n_involved": len(result.get("involved_entities", [])),
    })
    return result
