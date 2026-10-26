"""agent/tools/cross_source_planner.py — 跨源分段计划器 (纯函数)。

吃"边视图顺序序列 + 实体→source 映射", 单条边 to 端跨源即切段
(跨库边=接缝, 同时归属前后两段); 段间用连接列 (跨库边 target_col)
做 exports/needs 接力。
不碰走图内部、不碰 DB —— 是读图产物的下游纯变换, 可单测。
偷 Apollo Federation 心智: 分片键接力, 不跨库 join。
"""


def _src_of(entity_id, entity_source):
    """缺键兜底空串 = "未知源"。

    ★ 2026-10-15: 原写死真库名。这里**刻意不接 settings** —— 本模块零 import
    是刻意设计(本文件 docstring: 读图产物的下游纯变换, 可单测), 接 settings
    要新增依赖就破掉这个属性。
    而兜底本是第二层冗余: 两个调用方(ontology_join.py:83 / ontology_view.py:374)
    构造 entity_source 时已各自兜底过一次。
    空串与任何真实源名都不等 -> 会切成独立段; 宁可多切段也不要误判同源。
    """
    return entity_source.get(entity_id, "")


def plan_cross_source(edges, entity_source):
    """把连通有序边序列切成跨源分段计划 (Model A)。

    前置契约: `edges` 须是**连通有序**边序列 (e[i].to == e[i+1].from),
    通常来自 find_join_paths 逐跳拼接。段 source 由每段首边定,不复核后续边
    from 端——乱序/断裂输入不防御 (调用方保证连通)。
    """
    if not edges:
        return {"segments": []}
    segments = []
    cur = None
    for e in edges:
        src_from = _src_of(e["from"], entity_source)
        src_to = _src_of(e["to"], entity_source)
        if cur is None:
            cur = {"source": src_from, "edges": [], "exports": [], "needs": []}
            segments.append(cur)
        cur["edges"].append(e)
        if src_to != cur["source"]:
            # 跨库边: 前段导出连接列 (target_col), 起新段接力该列;
            # 跨库边同时归属新段 (它的入口), 故新段先收下这条边。
            cur["exports"] = [e["target_col"]]
            cur = {"source": src_to, "edges": [e],
                   "exports": [], "needs": [e["target_col"]]}
            segments.append(cur)
    return {"segments": segments}
