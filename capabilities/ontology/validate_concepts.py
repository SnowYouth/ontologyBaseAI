"""capabilities/ontology/validate_concepts.py — concept 4 类校验 (spec §3.5)。

纯逻辑, 真库 I/O 全靠注入隔离 (单测不连真库):
- table_cols_map: {entity_id: 该实体物理表列名集合} (调用方从真库 DESCRIBE 得到)
- join_path:      回调 (from_entity, to_entity) -> paths 列表 (复用 get_join_path);
                  返回非空 = 连通。

校验器只产标记 {ok, checks}, 不写库。回写 concept.valid 由调用方拿结果调
set_concept_valid (对齐 filter_rule 校验器: 标记式, 不阻塞本体加载)。
connectable 只验"边存在", 不验"边拼出的 JOIN 真能跑"(后者由 smoke 的 execute_sql 兜)。
"""
from __future__ import annotations

# 聚合断言白名单 (校验器自带一份, 不 import store 避免耦合)。
_AGG_FNS = {"COUNT", "SUM", "AVG", "MAX", "MIN"}
_HAVING_OPS = {">", ">=", "<", "<=", "=", "!="}


def _involved(concept: dict) -> list:
    """anchor + 各 condition.entity 去重保序。"""
    out: list = []
    for eid in [concept["anchor_entity"], *(c["entity"] for c in concept["conditions"])]:
        if eid not in out:
            out.append(eid)
    return out


def _check_entity_exists(concept: dict, onto) -> dict:
    missing = [e for e in _involved(concept) if e not in onto.entities]
    ok = not missing
    return {"name": "entity_exists", "ok": ok,
            "detail": "" if ok else f"实体不存在: {missing}"}


def _check_column_exists(concept: dict, table_cols_map: dict) -> dict:
    bad = []
    for c in concept["conditions"]:
        if c.get("kind", "column") != "column":  # agg 条件列检查归 agg_valid
            continue
        # 列名大小写归一比对 (真库 list_table_columns 返回 MySQL 大写列名;
        # concept.col 约定大写, 但两侧都 upper() 消除脆性, 见 plan-review 发现4)
        cols = {x.upper() for x in table_cols_map.get(c["entity"], set())}
        if c["col"].upper() not in cols:
            bad.append(f"{c['entity']}.{c['col']}")
    ok = not bad
    return {"name": "column_exists", "ok": ok,
            "detail": "" if ok else f"列不在物理表: {bad}"}


def _values(cond: dict) -> list:
    v = cond.get("value")
    if v is None or v == "":
        return []
    if cond.get("op", "").upper() == "IN":
        return [x.strip() for x in v.split(",") if x.strip()]
    return [v]


def _check_value_in_enum(concept: dict, onto) -> dict:
    bad = []
    for c in concept["conditions"]:
        if c.get("kind", "column") != "column":  # agg 条件不查 column 枚举
            continue
        ev = onto.value_sets.get((c["entity"], c["col"]))
        if ev is None:                       # 该字段无枚举登记 → 跳过 (不是错)
            continue
        codes = {code["code"] for code in ev.get("codes", [])}
        bad += [v for v in _values(c) if v not in codes]
    ok = not bad
    return {"name": "value_in_enum", "ok": ok,
            "detail": "" if ok else f"值不在枚举码值内: {bad}"}


def _col_ok(entity: str, col: str, table_cols_map: dict) -> bool:
    """物理列存在性 (大小写归一)。"""
    cols = {x.upper() for x in table_cols_map.get(entity, set())}
    return col.upper() in cols


def _bad_agg_target(entity: str, fn: str, target: str, table_cols_map: dict) -> list:
    """agg_target 校验: COUNT 允许 '*' 或物理列; 其余聚合必须物理列。"""
    if fn == "COUNT":
        if target != "*" and target and not _col_ok(entity, target, table_cols_map):
            return [f"COUNT 目标列不在物理表: {entity}.{target}"]
    elif fn in _AGG_FNS:  # SUM/AVG/MAX/MIN
        if not target or not _col_ok(entity, target, table_cols_map):
            return [f"{fn} 目标列不在物理表: {entity}.{target or '(空)'}"]
    return []


def _bad_agg_having(hop: str, hval: str) -> list:
    """HAVING 校验: 比较符白名单 + 值非空。"""
    bad = []
    if hop not in _HAVING_OPS:
        bad.append(f"HAVING 比较符非法: {hop or '(空)'}")
    if hval == "":
        bad.append("HAVING 值为空 (聚合断言必须有比较)")
    return bad


def _bad_agg_groupby(entity: str, gb: str, table_cols_map: dict) -> list:
    """group_by_col 必须非空且是物理列。"""
    if not gb or not _col_ok(entity, gb, table_cols_map):
        return [f"分组列不在物理表: {entity}.{gb or '(空)'}"]
    return []


def _bad_agg_filter(entity: str, fcol: str, table_cols_map: dict) -> list:
    """前置过滤列 (col 非空才查物理列)。"""
    if fcol and not _col_ok(entity, fcol, table_cols_map):
        return [f"前置过滤列不在物理表: {entity}.{fcol}"]
    return []


def _check_one_agg(c: dict, table_cols_map: dict) -> list:
    """单条 agg 条件的 5 项校验, 返回错误消息列表 (空 = 全通过)。"""
    entity = c["entity"]
    fn = (c.get("agg_fn") or "").upper()
    bad = []
    # 1. agg_fn 白名单
    if fn not in _AGG_FNS:
        bad.append(f"聚合函数非法: {fn or '(空)'}")
    # 2~5: 目标列 / 分组列 / 前置过滤列 / HAVING (逐项抽 helper, 控圈复杂度)
    bad += _bad_agg_target(entity, fn, c.get("agg_target") or "", table_cols_map)
    bad += _bad_agg_groupby(entity, c.get("group_by_col") or "", table_cols_map)
    bad += _bad_agg_filter(entity, c.get("col") or "", table_cols_map)
    bad += _bad_agg_having(c.get("having_op") or "", c.get("having_value") or "")
    return bad


def _check_agg(concept: dict, table_cols_map: dict) -> dict:
    """kind='agg' 条件的 5 项确定性校验 (spec §5.2)。无 agg 条件 → 直接 ok。"""
    bad = []
    for c in concept["conditions"]:
        if c.get("kind", "column") != "agg":
            continue
        bad += _check_one_agg(c, table_cols_map)
    ok = not bad
    return {"name": "agg_valid", "ok": ok,
            "detail": "" if ok else "; ".join(bad)}


def _check_connectable(concept: dict, join_path) -> dict:
    ents = _involved(concept)
    if len(ents) <= 1:                       # 单表概念不需 JOIN
        return {"name": "connectable", "ok": True, "detail": ""}
    anchor = ents[0]
    unreachable = []
    for other in ents[1:]:
        if not join_path(anchor, other):
            unreachable.append(f"{anchor}->{other}")
    ok = not unreachable
    return {"name": "connectable", "ok": ok,
            "detail": "" if ok else f"表间无 relation 边 (断腿): {unreachable}"}


def validate_concept(concept: dict, onto, table_cols_map: dict, join_path) -> dict:
    """校验单个概念, 返回 {id, checks:[...], ok}。

    4 类检查 (spec §3.5): 实体存在 / 列存在 / 值在枚举内 / 涉及实体两两可连通。
    ok = 四项全 ok。校验失败不抛 (标记式, 不拖垮本体加载)。
    """
    checks = [
        _check_entity_exists(concept, onto),
        _check_column_exists(concept, table_cols_map),
        _check_value_in_enum(concept, onto),
        _check_agg(concept, table_cols_map),
        _check_connectable(concept, join_path),
    ]
    return {"id": concept.get("id"), "checks": checks,
            "ok": all(c["ok"] for c in checks)}
