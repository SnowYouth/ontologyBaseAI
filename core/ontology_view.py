"""core/ontology.py — 本体加载 + 索引构建 + 卡片拼装 (阶段 6 OBDA).

设计:
1. Ontology 是纯数据容器 (dataclass), 三索引把"存储态"(三数组分开躺)
   变成"查询态"(O(1)/O(度数) 查找), 建一次复用
2. 邻接表双向记 (source 侧和 target 侧各记一次该边), 让"命中一个实体
   查它所有边"不用管这条边它是 source 还是 target
3. 纯内存操作, 不碰文件 I/O (加载文件是 set_ontology 的事), 方便单测喂 dict
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Ontology:
    """本体的查询态: 三索引 + domains 元数据 + filter_rules + concepts 索引."""
    entities: dict       # id -> 实体 dict (原始结构, 含 attributes)
    value_sets: dict      # (entity, field) -> {name, spec_ref, codes:[{code,name,desc}]}
    adjacency: dict       # id -> [边dict,...] (双向都记)
    domains: list         # 规范子域元数据 (分组/配色)
    filter_rules: dict    # entity id -> [{col,op,value,mode,origin},...] 默认过滤规则
    concepts: dict = field(default_factory=dict)   # id -> {..., conditions:[...]}


def build_indexes(raw: dict) -> Ontology:
    """把本体原始 dict (entities/relationships/valueSets 三数组) 建成三索引.

    Args:
        raw: 本体 JSON 反序列化后的 dict, 需含 entities/relationships/valueSets/domains 四键;
             filterRules 键可选 (老本体无此键时按空处理, 向后兼容)。
    """
    entities = {e["id"]: e for e in raw["entities"]}
    adjacency: dict = {eid: [] for eid in entities}
    for rel in raw["relationships"]:
        # 边端点可能是"影子实体"(如 lib_reader/account: 故意不建实体节点, 只留
        # ID 指针边, 教学"断腿"场景), 不假设 source/target 必在 entities 里
        adjacency.setdefault(rel["source"], []).append(rel)
        adjacency.setdefault(rel["target"], []).append(rel)
    filter_rules: dict = {}
    for fr in raw.get("filterRules", []):
        filter_rules.setdefault(fr["entity"], []).append(fr)
    concepts = {c["id"]: c for c in raw.get("concepts", [])}
    return Ontology(
        entities=entities,
        value_sets=raw["valueSets"],
        adjacency=adjacency,
        domains=raw.get("domains", []),
        filter_rules=filter_rules,
        concepts=concepts,
    )


# ============ 模块级单例 (main.py 启动期注入) ============

_ontology: Ontology | None = None
_ontology_path: str | None = None  # set_ontology 记录的运行时真实加载路径 (审计标签用)


def lookup_enum(onto: Ontology, entity: str, field: str) -> dict | None:
    """按 (entity,field) 查枚举码表; 未登记返回 None (工具层局部报错用)。"""
    return onto.value_sets.get((entity, field))


def find_join_paths(onto: Ontology, from_entity: str, to_entity: str) -> list[dict]:
    """a↔b 直连 JOIN 路径 (一跳); 语义边优先, _his/temp 噪音边过滤掉。

    第一版只做直连 (两实体间直接有边); 多跳中转留迭代。每条路径带完整 JOIN
    字段对供拼 SQL。plan-review 发现5: 按 via 去重(邻接表双向记可能重复) +
    过滤 _his/temp 噪音边(不只沉底, 直接不返回, 防噪音塞爆上下文) + 语义边优先排序。

    via 现由 store 层从 source_col/target_col 派生 (`{source}.{sc}={target}.{tc}`),
    不再手编。seen_via 去重仍按派生 via (唯一性更强), _is_noise_via 靠 via 含表名
    做子串匹配 (派生公式保留表名, 噪音过滤不受影响)。
    """
    seen_via: set = set()
    views: list[dict] = []
    for e in onto.adjacency.get(from_entity, []):
        if not _connects(e, from_entity, to_entity):
            continue
        via = e.get("via")
        if via in seen_via:
            continue
        seen_via.add(via)
        if _is_noise_edge(e):         # _his/temp 裸边直接过滤
            continue
        views.append(_join_edge_view(e, from_entity))
    views.sort(key=_join_sort_key)
    return views


def _is_noise_via(via) -> bool:
    """via 命中 _his/temp/j_temp → 噪音边 (历史/临时表, 非业务 JOIN)。

    via 现为派生格式 `{source}.{sc}={target}.{tc}`, 含 source/target 表名,
    子串匹配 _his/temp 仍能命中历史/临时表边, 不受派生影响。
    """
    v = (via or "").lower()
    return "_his" in v or "temp" in v or "j_temp" in v


def _is_noise_edge(edge: dict) -> bool:
    """边级噪音判定: via 命中 _his/temp **且没有 label** 才算噪音。

    ★ 2026-10-21 (CQ-16 连错两轮的根因): 子串规则写于"_his 表都是聚类脚本抽的
    裸边"的年代; 09-20 补登两个归档实体 (订单项/订单头的 _his)并人审上线
    一批带 label 的 his 边后, 这条规则把它们整条滤掉 —— get_join_path 到归档表
    永远 paths=[] / multi_hop_paths=[], agent 拿不到去归档表的路, 只能停在在用表。
    label 非空是人审过的"这是正路"信号 (同 _collect_related 只收带 label 的边)。
    """
    return not edge.get("label") and _is_noise_via(edge.get("via"))


def _connects(edge: dict, a: str, b: str) -> bool:
    return {edge["source"], edge["target"]} == {a, b}


def _join_edge_view(edge: dict, from_entity: str) -> dict:
    """把存储态边整成 JOIN 视图 (from 固定为查询发起实体方向)。

    source_col/target_col 按边的存储方向 (source→target) 固定。反向查询
    (from_entity == target 侧) 时交换两者, 使返回永远对齐 from.col = to.col,
    agent 拿来直接写 JOIN 不用自己换列。

    via 是边的稳定标识 (store 层从 col 派生, 按存储方向固定 `source.sc=target.tc`),
    反向查询时 **不交换** (只换 col)。agent 拿到的 via 永远是存储方向字符串,
    作去重 key + 噪音过滤依据, 不随查询方向变。
    """
    reversed_ = edge["source"] != from_entity
    src_col = edge.get("source_col")
    tgt_col = edge.get("target_col")
    return {
        "from": from_entity,
        "to": edge["target"] if edge["source"] == from_entity else edge["source"],
        "via": edge.get("via"),
        "source_col": tgt_col if reversed_ else src_col,
        "target_col": src_col if reversed_ else tgt_col,
        "type": edge.get("type"),
        "label": edge.get("label"),
        "note": edge.get("note"),
        "bridge_count": edge.get("bridge_count"),
    }


def _join_sort_key(p: dict) -> tuple:
    """排序: 语义边(有label/note)优先 → bridge_count 降序。噪音边已在 find_join_paths 过滤。"""
    has_semantic = 0 if (p.get("label") or p.get("note")) else 1
    return (has_semantic, -(p.get("bridge_count") or 0))


def _path_sort_key(p: dict) -> tuple:
    """路径排序 (spec §5.2): 跳数升 → 语义边数降 → 最小 bridge_count 降。

    语义边 = label/note 任一非空。一条路上标注齐全的边越多越可信 (清债时手标的
    label 就是"这是正路"的信号)。第三级取整条路径各边 bridge_count 的最小值
    (瓶颈边), 再按该值降序。
    """
    edges = p["edges"]
    n_semantic = sum(1 for e in edges if e.get("label") or e.get("note"))
    min_bridge = min((e.get("bridge_count") or 0) for e in edges) if edges else 0
    return (p["hops"], -n_semantic, -min_bridge)


def find_join_paths_multi(onto: Ontology, from_entity: str, to_entity: str,
                          max_hops: int = 2) -> list[dict]:
    """a→b 多跳 JOIN 路径 (BFS, 最短优先); 噪音边/环路过滤。

    与 find_join_paths (只做直连一跳) 并列, 老函数零改动 (它有 10 个调用点)。
    逐层扩展, 命中目标即收该层所有路径不再往下探 —— 跳数少一跳少绕一张表,
    也防 member_id 类分片键把全图连通导致路径爆炸。

    每跳复用 _join_edge_view(e, cur) 拼边视图: 它已处理反向时交换
    source_col/target_col, 保证 e[i].to == e[i+1].from 且 from.col=to.col 对齐,
    正好满足 plan_cross_source 的前置契约。

    返回 [{"hops": n, "edges": [...]}, ...] 按 _path_sort_key 排序; 无路径返 []。
    """
    if from_entity == to_entity:
        return []
    # BFS 队列项: (当前实体, 已走边视图列表, 路径内已访问实体集)
    queue: list = [(from_entity, [], {from_entity})]
    for _ in range(max_hops):
        found: list = []
        nxt: list = []
        for cur, path, visited in queue:
            seen_via: set = set()
            for e in onto.adjacency.get(cur, []):
                if e["source"] != cur and e["target"] != cur:
                    continue
                via = e.get("via")
                if via in seen_via:
                    continue
                seen_via.add(via)
                if _is_noise_edge(e):
                    continue
                view = _join_edge_view(e, cur)
                nb = view["to"]
                if nb in visited:
                    continue
                new_path = [*path, view]
                if nb == to_entity:
                    found.append({"hops": len(new_path), "edges": new_path})
                else:
                    nxt.append((nb, new_path, visited | {nb}))
        if found:
            found.sort(key=_path_sort_key)
            return found
        queue = nxt
        if not queue:
            break
    return []


def set_ontology(path: str) -> None:
    """main.py 启动期调一次: 加载本体 → build_indexes → 存单例.

    按后缀分流 (阶段C 引入 SQLite 权威源):
    - .db/.sqlite → capabilities.ontology.OntologyStore.load_raw() 组装 dict;
    - 其它 (.json) → 旧 json.load 逻辑 (向后兼容, 老测试零回归)。
    文件不存在/损坏/缺必需键 一律直接抛 (fail-fast, 本体是地基不带病运行)。
    """
    global _ontology, _ontology_path
    if path.endswith((".db", ".sqlite")):
        raw = _load_raw_from_store(path)
    else:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    _ontology = build_indexes(raw)
    _ontology_path = path


def _load_raw_from_store(path: str) -> dict:
    """从 SQLite 存储读并组装成 build_indexes 契约 dict."""
    from capabilities.ontology.ontology_store import OntologyStore

    store = OntologyStore(path)
    try:
        return store.load_raw()
    finally:
        store.close()


def get_ontology() -> Ontology | None:
    """取单例; 未调用过 set_ontology 时返回 None, 调用方 (工具层) 自行处理."""
    return _ontology


def get_ontology_path() -> str | None:
    """返回 set_ontology 记录的运行时真实加载路径; 未加载返回 None.
    供审计标签 (如 cq_runner.save_current 的 ontology_path) 用, 区别于 settings 声明值。"""
    return _ontology_path


def build_cards(onto: Ontology, ids: list[str]) -> dict:
    """命中 ids → 轻卡列表 + 断腿 note 汇总。

    渐进披露: 轻卡只给结构/边界/语义 JOIN 方向; 枚举码值走 get_enum_values,
    精确 JOIN 路径走 get_join_path。不再返回 subgraph 裸桥边 (噪音)。
    """
    cards = [_render_card(onto, onto.entities[eid]) for eid in ids]
    notes = _collect_notes_for(onto, ids)
    return {"cards": cards, "notes": notes}


def _anchor_pk(onto: Ontology, entity_id: str) -> str:
    """从实体 attributes 找 kind=='pk' 的列名; 找不到返 ''。"""
    ent = onto.entities.get(entity_id, {})
    for a in ent.get("attributes", []):
        if a.get("kind") == "pk":
            return a.get("name", "")
    return ""


def _derive_output_key(onto: Ontology, concept: dict) -> tuple[dict, str]:
    """概念对外 JOIN 把手 (spec §4.4)。返回 (output_key, note)。
    - 有 agg 条件: 取 seq 最小 agg 的 {entity, group_by_col}。
    - 纯 column: {anchor, anchor 主键}; 无 pk 则 col='' + note。
    """
    aggs = sorted((c for c in concept["conditions"] if c.get("kind") == "agg"),
                  key=lambda c: c.get("seq", 0))
    if aggs:
        a = aggs[0]
        return {"entity": a["entity"], "col": a.get("group_by_col", "")}, ""
    anchor = concept["anchor_entity"]
    pk = _anchor_pk(onto, anchor)
    if not pk:
        return ({"entity": anchor, "col": ""},
                f"anchor 实体 {anchor} 无主键标记, 请自行判断对外 JOIN 列")
    return {"entity": anchor, "col": pk}, ""


def build_concept_detail(onto: Ontology, concept_id: str) -> dict:
    """概念 → 结构化判定条件 + 涉及实体集 + 对外 JOIN 把手 (不含 SQL/JOIN)。

    involved_entities = anchor_entity + 各 condition.entity 去重保序; JOIN 让 LLM
    拿这个集合调 get_join_path 现查 (概念层零 SQL 模板, 见 spec §2.3)。
    output_key = 概念对外暴露的 JOIN 列 (组合场景, 见 spec §4.4)。
    断腿层次2: 概念不存在 / valid≠1 → 返回 {"error": ...} (响亮失败)。
    """
    c = onto.concepts.get(concept_id)
    if c is None:
        return {"error": f"概念不存在: {concept_id}"}
    if c.get("valid") != 1:
        return {"error": f"概念未通过校验 (断腿), 不可用: {concept_id}"}
    involved: list = []
    for eid in [c["anchor_entity"], *(cond["entity"] for cond in c["conditions"])]:
        if eid not in involved:
            involved.append(eid)
    output_key, note = _derive_output_key(onto, c)
    result = {
        "anchor_entity": c["anchor_entity"],
        "conditions": [{"entity": cond["entity"], "col": cond["col"], "op": cond["op"],
                        "value": cond["value"], "kind": cond.get("kind", "column"),
                        "agg_fn": cond.get("agg_fn", ""),
                        "agg_target": cond.get("agg_target", ""),
                        "group_by_col": cond.get("group_by_col", ""),
                        "having_op": cond.get("having_op", ""),
                        "having_value": cond.get("having_value", "")}
                       for cond in c["conditions"]],
        "involved_entities": involved,
        "output_key": output_key,
    }
    if note:
        result["output_key_note"] = note
    return result


_MAX_ALTERNATIVES = 5


def _assemble_join_result(base: dict, paths: list, entity_source: dict) -> dict:
    """把 find_join_paths_multi 的排序结果拼成返回体 (方案B: 最优一条带完整
    字段+跨库分段, 备选只给 hops+through 摘要, 上限 _MAX_ALTERNATIVES)。

    抽出来是为把 build_concept_join 圈复杂度压回 ≤11 (硬规则)。
    """
    from agent.tools.cross_source_planner import plan_cross_source  # 延迟导入断循环

    best, rest = paths[0], paths[1:]
    result = {
        **base,
        "path": {"hops": best["hops"], "edges": best["edges"],
                 "segments": plan_cross_source(best["edges"], entity_source)["segments"]},
        "alternatives": [{"hops": p["hops"],
                          "through": [e["to"] for e in p["edges"][:-1]]}
                         for p in rest[:_MAX_ALTERNATIVES]],
    }
    if len(rest) > _MAX_ALTERNATIVES:
        result["alternatives_truncated"] = len(rest) - _MAX_ALTERNATIVES
    return result


def build_concept_join(onto: Ontology, concept_id: str, target_entity: str,
                       max_hops: int = 2) -> dict:
    """概念 output_key → 目标实体的完整跨界 JOIN 路径 + 跨库分段 (spec §5)。

    补的是"缝合仍靠 LLM 自己拼"的债: get_concept_detail 只给 output_key
    (把手是哪列), 从把手走到目标实体仍要 LLM 自己调 get_join_path 多次。
    本函数一步给完整路径。只给路径不生成 SQL (概念层零 SQL 模板铁律)。

    断腿层次2 照 build_concept_detail 口径: 概念不存在/valid≠1 → {"error"}。
    max_hops 内无路径也响亮失败 (返 error 而非空 dict, 防 LLM 误以为不需 JOIN)。
    """
    c = onto.concepts.get(concept_id)
    if c is None:
        return {"error": f"概念不存在: {concept_id}"}
    if c.get("valid") != 1:
        return {"error": f"概念未通过校验 (断腿), 不可用: {concept_id}"}
    if target_entity not in onto.entities:
        return {"error": f"目标实体不存在: {target_entity}"}
    output_key, _note = _derive_output_key(onto, c)
    src = output_key["entity"]
    if not output_key["col"]:
        return {"error": f"概念 {concept_id} 的 anchor 实体 {src} 无主键标记, "
                         f"无对外 JOIN 把手无法缝合; 请调 get_concept_detail 自行判断"}
    base = {"concept_id": concept_id, "output_key": output_key,
            "target_entity": target_entity}
    if src == target_entity:
        return {**base, "path": {"hops": 0, "edges": [], "segments": []},
                "path_note": f"概念把手已在目标实体 {target_entity} 上, 无需 JOIN"}
    paths = find_join_paths_multi(onto, src, target_entity, max_hops)
    if not paths:
        return {"error": f"{src} 到 {target_entity} 在 {max_hops} 跳内无 JOIN 路径; "
                         f"可调大 max_hops 重试"}
    from core.config import default_source  # 延迟导入, 同 _assemble_join_result 里 plan_cross_source 惯例

    entity_source = {eid: (e.get("data_source") or default_source())
                     for eid, e in onto.entities.items()}
    return _assemble_join_result(base, paths, entity_source)


def _render_card(onto: "Ontology", entity: dict) -> dict:
    """单个实体 → 轻卡: 字段(标 hasEnum 不展码) + 召回边界 + 默认过滤 + 语义 JOIN 方向。

    related: 只给带 label 的语义邻居 {to, hint} (裸桥边不进轻卡, LLM 需精确 JOIN
    时调 get_join_path)。recallKeywords (正向召回信号) / disambiguateFrom (负向消歧
    边界) 有值透传, 缺失给空列表。
    filterRules: 从 filter_rule 表按 entity 取的结构化默认过滤 {col,op,value,mode},
    取代旧散文 query_hints (origin 是飞轮信任分级, 不进给 agent 的卡片)。
    """
    attributes = [_render_attribute(a) for a in entity["attributes"]]
    rules = onto.filter_rules.get(entity["id"], [])
    card = {
        "id": entity["id"],
        "displayName": entity["displayName"],
        "summary": entity.get("summary", ""),
        "maps_to": entity["maps_to"],
        "tables": entity["tables"],
        "attributes": attributes,
        "recallKeywords": entity.get("recallKeywords", []),
        "disambiguateFrom": entity.get("disambiguateFrom", []),
        "filterRules": [
            {"col": r["col"], "op": r["op"], "value": r["value"], "mode": r["mode"]}
            for r in rules
        ],
        "related": _collect_related(onto, entity["id"]),
    }
    card.update(_archive_pair(onto, entity["id"]))
    return card


def _archive_pair(onto: "Ontology", eid: str) -> dict:
    """在用实体 ↔ 归档实体 (`X` ↔ `X_his`) 互指; 两者都登记过才挂, 否则空 dict。

    ★ 2026-10-21 CQ-16 连错两轮: 在用订单项与其归档实体之间没有边, 卡片
    related 里也就没有它, agent 在在用表查到一条就断言"只有一个订单项" —— 而
    判据那张工单的订单项在归档表里。归档表是"同一业务对象的另一半", 不是关联
    实体, 不该靠边表达; 用命名约定配对, 但只认**本体里真登记了的**实体。
    """
    if eid.endswith("_his") and eid[:-4] in onto.entities:
        return {"live": eid[:-4]}
    if f"{eid}_his" in onto.entities:
        return {"archive": f"{eid}_his"}
    return {}


def _render_attribute(attr: dict) -> dict:
    """轻卡属性渲染: 不内联枚举码值, 只标 hasEnum (LLM 需码值时调 get_enum_values)。

    统一补齐 desc/desc_source/disambiguation (缺失给空串), 下游拿到统一结构不必判 get。
    """
    rendered = dict(attr)
    rendered.setdefault("desc", "")
    rendered.setdefault("desc_source", "")
    rendered.setdefault("disambiguation", "")
    rendered["hasEnum"] = bool(attr.get("hasEnum"))
    # valueSet/inSpec/note 已于 2026-08-05 从 AttributeIn 彻底废除, 无需再 pop
    return rendered


def _collect_related(onto: "Ontology", eid: str) -> list[dict]:
    """命中实体的语义邻居: 只收带 label 的边, 每个邻居一项 {to, hint}, 去重。

    裸桥边 (label 空) 不进轻卡 — 它们是聚类脚本从 mapper 自动抽的噪音, LLM 真要
    拼多表 SQL 时调 get_join_path 按需拉 (那里做语义优先排序)。
    """
    seen: dict = {}
    for edge in onto.adjacency.get(eid, []):
        label = edge.get("label")
        if not label:
            continue
        other = edge["target"] if edge["source"] == eid else edge["source"]
        if other == eid:  # 自反边跳过
            continue
        seen.setdefault(other, {"to": other, "hint": label})
    return list(seen.values())


def _collect_notes_for(onto: Ontology, ids: list[str]) -> list[str]:
    """扫命中实体的邻接边, 汇总带 note 的边 (断腿/避坑信号显眼), 去重。

    note = "走这条边要注意什么"的告警口径 (JOIN 陷阱/断腿/影子实体), 与 label
    (语义方向短语) 分工不同。跨域断腿只是 note 的一种场景, 不作硬门槛 —— 同域边
    的 JOIN 同名列陷阱同样要显眼喂给 agent。
    """
    notes: list[str] = []
    seen_edges: set = set()
    for eid in ids:
        for edge in onto.adjacency.get(eid, []):
            key = (edge["source"], edge["target"], edge.get("via"))
            if key in seen_edges:
                continue
            seen_edges.add(key)
            if edge.get("note"):
                notes.append(edge["note"])
    return notes


def build_catalog_text(onto: Ontology) -> str:
    """生成静态本体目录文本 (实体名 + summary + 三户归组), 追加进 system prompt.

    实体带 disambiguateFrom 时追加"不涉及"边界行 (步骤5): 让 LLM 在点名
    阶段就避开高相似实体召回错 (BSS 实体命名高度相似, 是召回错的主因)。
    """
    lines = ["## 本体实体目录 (LIB BSS 只读库, 查询前先从这里点名)"]
    for entity in onto.entities.values():
        lines.append(
            f"- `{entity['id']}` ({entity['displayName']}, 归 {entity['maps_to']}): "
            f"{entity.get('summary', '')}"
        )
        disambig = entity.get("disambiguateFrom", [])
        if disambig:
            lines.append(f"    ↳ 不涉及: {'; '.join(disambig)}")
    active_concepts = [c for c in onto.concepts.values() if c.get("valid") == 1]
    if active_concepts:
        lines.append("")
        lines.append("## 本体概念目录 (派生业务概念; 命中就调 get_concept_detail 拿判定条件)")
        for c in active_concepts:
            lines.append(f"- `{c['id']}` ({c['display_name']}): {c.get('summary', '')}")
    return "\n".join(lines)
