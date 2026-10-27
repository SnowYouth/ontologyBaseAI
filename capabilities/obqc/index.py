"""capabilities/obqc/index.py — 从本体查询态建 OBQC 校验用的内存索引 (per-source)。

spec §3.2 C-1: 物理表名→entity 必须按 source 分组 (多库同名表不串)。
spec §3.7: 每次从 get_ontology() 现建 (walking skeleton 保守取舍, 回避在线编辑器
改本体后缓存过期误判; 计划二再引入跟编辑器失效的缓存)。
"""
from dataclasses import dataclass

from core.observability import log_event


@dataclass
class ObqcIndex:
    table_to_entity: dict   # {source: {物理表名(大写): entity_id}}
    edges: dict             # {frozenset({eA, eB}): [{source_col, target_col, source, target, via}]}


def build_obqc_index(onto) -> ObqcIndex:
    """把 Ontology 查询态建成 OBQC 索引。表名统一大写 (SQL 表名大小写不敏感归一)。"""
    table_to_entity: dict = {}
    for eid, ent in onto.entities.items():
        source = ent.get("data_source")
        if not source:
            continue  # 无 data_source 的 entity 无法定位库, 跳过 (不进反向索引)
        bucket = table_to_entity.setdefault(source, {})
        for t in ent.get("tables") or []:
            if not t:
                continue  # 防 None/空串元素 .upper() 炸
            key = t.upper()
            # 表冲突检测: 同 source 下两 entity 声明同一物理表则静默 last-write-wins,
            # 将来本体自演化引入冲突会让 JOIN 校验无诊断错判。walking skeleton 保守:
            # 只落可观测告警, 不硬拦不抛异常, 覆盖行为保持不变 (spec §3.2 C-1)。
            if key in bucket and bucket[key] != eid:
                log_event("obqc.table_collision", {
                    "source": source, "table": key,
                    "existing": bucket[key], "incoming": eid,
                })
            bucket[key] = eid

    edges: dict = {}
    seen = set()  # 去重: adjacency 双向都记, 同一条边会出现两次
    # 归档继承边 (core.ontology_view._build_archive_edges) 也进校验索引: 否则
    # get_join_path 给了归档表的路, agent 照着写 JOIN 反被 no_edge 拦
    all_rels = [*onto.adjacency.items(), *getattr(onto, "archive_edges", {}).items()]
    for eid, rels in all_rels:
        for rel in rels:
            rid = id(rel)
            if rid in seen:
                continue
            seen.add(rid)
            a, b = rel.get("source"), rel.get("target")
            if not a or not b:
                continue
            key = frozenset({a, b})
            edges.setdefault(key, []).append({
                "source_col": rel.get("source_col"),
                "target_col": rel.get("target_col"),
                "source": a, "target": b,
                # ★ 自演化 (spec §3.3): 边身份是 (source, target, via) 三元组,
                # 丢了 via 则 used_edges 带不出去, relation_health 主键不完整
                "via": rel.get("via"),
                "inherited": "inherited_from" in rel,
            })
    return ObqcIndex(table_to_entity=table_to_entity, edges=edges)
