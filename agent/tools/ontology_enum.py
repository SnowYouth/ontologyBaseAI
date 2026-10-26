"""agent/tools/ontology_enum.py — 按 (entity,field) 拉枚举码表 (渐进披露: 轻卡看到 hasEnum 后单拉).

薄封装: 命中逻辑在 core.ontology_view.lookup_enum, 这里做取单例 + 遍历 fields + 局部报错。

Note: 不用 `from __future__ import annotations` (同 tools/ontology_cards.py)。
"""
from core.observability import log_event
from core.ontology_view import get_ontology, lookup_enum
from agent.tools.base import register_tool


@register_tool(category="serving")
def get_enum_values(entity: str, fields: list[str]) -> dict:
    """拉某实体若干枚举字段的码值翻译表 (轻卡里字段标了 hasEnum 才需拉)。

    轻卡不内联枚举码值以省上下文; 你在卡片看到某字段 hasEnum=true 且这次查询要
    翻译/过滤它的值时, 用该实体 id + 字段名列表调本工具拿码表。同名字段跨实体
    码表不同, 所以按实体拉。要拉别的实体的枚举, 再调一次。

    Args:
        entity: 实体 id (如 "lib_loan")
        fields: 该实体的枚举字段名列表 (如 ["REC_STATUS", "TYPE_CD"])
    """
    onto = get_ontology()
    if onto is None:
        return {"error": "ontology not initialized"}
    results = [_lookup_one(onto, entity, f) for f in fields]
    log_event("ontology.enum", {
        "entity": entity, "fields": fields,
        "n_hit": sum(1 for r in results if "codes" in r),
        "n_miss": sum(1 for r in results if "error" in r),
    })
    return {"results": results}


def _lookup_one(onto, entity: str, field: str) -> dict:
    """单字段查找; 未登记返回 {entity,field,error}, 命中返回 {entity,field,name,spec_ref,codes}。"""
    ev = lookup_enum(onto, entity, field)
    if ev is None:
        return {"entity": entity, "field": field, "error": "no enum registered"}
    return {
        "entity": entity, "field": field,
        "name": ev.get("name"), "spec_ref": ev.get("spec_ref"),
        "codes": ev.get("codes", []),
    }
