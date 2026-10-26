"""agent/tools/ontology_join.py — 按需拉两实体间精确 JOIN 路径 (渐进披露: 轻卡只给 related 方向).

薄封装: 路径逻辑在 core.ontology_view (find_join_paths 一跳 / find_join_paths_multi 多跳),
这里取单例 + 调 + 一跳落空时回退多跳。

Note: 不用 `from __future__ import annotations` (同 tools/ontology_cards.py)。
"""
from core.observability import log_event
from core.ontology_view import (build_concept_join, find_join_paths,
                           find_join_paths_multi, get_ontology)
from capabilities.ontology.selfevolve.signals import log_failure_signal
from agent.tools.base import register_tool

_FALLBACK_MAX_HOPS = 2

# 已采集过燃料的实体对 (无序), 防 agent 重试把燃料库刷屏。
# 对齐热路径 tools/sql_exec._fuel_collected 的做法: 进程级, 冷路径另有
# 探针/去重/墓碑三道闸兜底。
_nopath_fuel_collected: set = set()


def _reset_nopath_fuel() -> None:
    """测试用: 清空 n_paths=0 燃料去重集。"""
    _nopath_fuel_collected.clear()


def _should_collect_nopath(onto, from_entity: str, to_entity: str) -> bool:
    """两端都登记 + 同库 + 这对还没采过 → 才值得采成燃料。

    三道门, 每道都有真实数据支撑:
    - **端点未登记 → 不采**: 那是实体缺口 (另一类债), 冷路径探针无表可探。
    - **★ 跨库 → 不采**: 真实 B 类 entity 对大多数是跨库的 (lib_reader/account 在
      lib2, 另一端在 lib), 相关提问也绝大部分落在这些对上。跨库物理上加不了边, 提议
      出来只会被人审 reject, 白烧探针的真库开销。
      (跨库提问本身仍在 log_event 的 cross_source 字段里可见, 不是丢掉。)
    - **同一对只采一次**: agent 会重试, 不去重会把燃料库刷屏。
    """
    ea, eb = onto.entities.get(from_entity), onto.entities.get(to_entity)
    if ea is None or eb is None:
        return False
    from core.config import default_source  # 延迟导入, 同本文件 plan_cross_source 惯例

    if (ea.get("data_source") or default_source()) != (eb.get("data_source") or default_source()):
        return False
    return frozenset((from_entity, to_entity)) not in _nopath_fuel_collected


def _collect_nopath_fuel(onto, from_entity: str, to_entity: str) -> None:
    """两端都登记、一跳无、多跳也无 → 图上真断开, 采成冷路径燃料 (热路径, 异常绝不外抛)。

    ★ 为什么值得单开一条采集渠道: 此前 n_paths=0 只 log_event 记一笔就丢了
    (冷路径只订 obqc 的 hard_block)。真实日志里 n_paths=0 的次数比 no_edge
    硬拦还多出一截, 而且**危害完全不可见** —— 那些调用之后 agent 几乎
    全自己绕过去了 (改走单表 / IN 字面量, 极少报错), 答案出来了没人知道
    它绕了路。obqc 那条渠道只逮得住"agent 硬写了错 JOIN", 逮不住"agent 问了
    本体、本体答不出、于是它绕开"。

    采不采的判据全在 _should_collect_nopath (抽出来是为把 CC 压回 ≤11 硬规则,
    顺带让"什么不采"能单独测)。
    """
    try:
        if not _should_collect_nopath(onto, from_entity, to_entity):
            return
        from core.config import default_source  # 延迟导入, 同本文件 plan_cross_source 惯例

        source = (onto.entities[from_entity].get("data_source") or default_source())
        log_failure_signal("obqc_circuit_break", {
            "origin": "get_join_path",     # 与 obqc 硬拦来的信号区分
            "violations": [],              # 没有 SQL, 不是违规, 是"答不出"
            "no_edge_signals": [{"entities": [from_entity, to_entity], "cols": []}],
        }, source=source)
        _nopath_fuel_collected.add(frozenset((from_entity, to_entity)))
    except Exception:
        pass  # 热路径: 埋点失败绝不影响 agent 拿路径


def _annotate_cross_source(onto, paths: list) -> list:
    """给多跳路径标跨库 + 附分段计划 (跨库才附, 同库一段是冗余)。

    多跳的中转实体可能落在另一个库 (真实案例 lib_loan(lib) → lib_reader(lib2)
    → account(lib2)), 物理上不能写一条 SQL JOIN。不标出来 = 把 agent 推去
    撞 execute_sql 的墙 —— 回退多跳之前它看到空路径还会自己绕开。
    分段逻辑复用 plan_cross_source (概念层缝合同款), 不另造一套。
    """
    from agent.tools.cross_source_planner import plan_cross_source  # 延迟导入, 同 core.ontology_view
    from core.config import default_source  # 延迟导入, 同上

    entity_source = {eid: (e.get("data_source") or default_source())
                     for eid, e in onto.entities.items()}
    out = []
    for p in paths:
        segments = plan_cross_source(p["edges"], entity_source)["segments"]
        crossed = len(segments) > 1
        item = {**p, "cross_source": crossed}
        if crossed:
            item["segments"] = segments
        out.append(item)
    return out


@register_tool(category="serving")
def get_join_path(from_entity: str, to_entity: str) -> dict:
    """拉两个实体之间的精确 JOIN 路径 (拼多表 SQL 时用)。

    轻卡 related 只告诉你"这个实体和谁有语义关系"(方向), 不含 JOIN 字段。你确定
    要拼多表查询时, 用两实体 id 调本工具拿精确的 source_col/target_col 对来写 JOIN。
    语义边优先返回, _his/temp 历史临时表边沉底。

    两个返回键, 按优先级看:
    - `paths`: **直连一跳**的边视图列表, 每条带 source_col/target_col, 直接拼
      `FROM 起点 JOIN 终点 ON source_col = target_col`。有就用这个。
    - `multi_hop_paths`: 直连没有时才给, 是**经中转实体的多跳路径**
      `[{"hops": n, "edges": [边视图, ...], "cross_source": bool}, ...]`, 按
      "跳数少/语义边多"排序。edges 是**有序连通**的 (前一条的 to 就是后一条的
      from), 逐条拼 JOIN 即可, 途经的中转表要一起写进 FROM。挑第一条就行,
      其余是备选。
      **`cross_source: true` 的那条不能写成一条 SQL** —— 途中跨了库, 必须按它
      带的 `segments` 分段查, 用前一段 `exports` 的列接力到后一段的 `needs`。

    两个都空 = 本体里这两个实体**真的连不上**, 别自己猜列名硬 JOIN, 按"知识边界"
    停下回问用户。

    Args:
        from_entity: 起点实体 id (如 "lib_loan")
        to_entity: 终点实体 id (如 "lib_loan_acct_rel")
    """
    onto = get_ontology()
    if onto is None:
        return {"error": "ontology not initialized"}
    paths = find_join_paths(onto, from_entity, to_entity)
    # 一跳落空才回退多跳: paths 的扁平边视图是 9 个调用方的硬契约
    # (validate_concepts 只判真假 / smoke 读 paths[0]["source_col"]), 多跳结构
    # 另开一个键, 不混进 paths。
    multi = ([] if paths
             else find_join_paths_multi(onto, from_entity, to_entity, _FALLBACK_MAX_HOPS))
    multi = _annotate_cross_source(onto, multi) if multi else []
    if not paths and not multi:
        _collect_nopath_fuel(onto, from_entity, to_entity)
    log_event("ontology.join", {
        "from": from_entity, "to": to_entity, "n_paths": len(paths),
        "n_multi": len(multi), "hops": multi[0]["hops"] if multi else None,
        "cross_source": multi[0]["cross_source"] if multi else None,
    })
    result = {"paths": paths}
    if multi:
        result["multi_hop_paths"] = multi
    return result


@register_tool(category="serving")
def get_concept_join(concept_id: str, target_entity: str,
                     max_hops: int = 2) -> dict:
    """拉「概念 → 目标实体」的完整 JOIN 路径 (把概念当积木嵌进外层查询时用)。

    get_concept_detail 只告诉你概念对外的把手是哪一列 (output_key), 从把手走到
    你要的目标实体还得自己拼 JOIN。本工具一步给完整路径: path.edges 是有序边序列
    (每条带 source_col/target_col, 可直接拼 JOIN), path.segments 是跨库分段计划
    (跨库不能写一条 SQL JOIN, 必须按段分别查、用 exports/needs 的列接力)。

    alternatives 是其余候选路径的摘要 (只给跳数 + 途经实体); 要用备选就带上中转
    实体自己调 get_join_path 分段拉。只给路径不给 SQL, SQL 你自己写。

    Args:
        concept_id: 概念 id (如 "frequent_reader")
        target_entity: 要缝合到的目标实体 id (如 "lib_book")
        max_hops: 最大跳数, 默认 2; 报"无 JOIN 路径"时可调大重试
    """
    onto = get_ontology()
    if onto is None:
        return {"error": "ontology not initialized"}
    result = build_concept_join(onto, concept_id, target_entity, max_hops)
    log_event("ontology.concept_join", {
        "concept_id": concept_id, "target": target_entity, "max_hops": max_hops,
        "error": result.get("error"),
        "hops": result.get("path", {}).get("hops"),
        "n_alternatives": len(result.get("alternatives", [])),
        "n_segments": len(result.get("path", {}).get("segments", [])),
    })
    return result
