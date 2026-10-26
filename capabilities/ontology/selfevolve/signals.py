"""capabilities/ontology/selfevolve/signals.py — 热路径失败信号采集 + 冷路径调度.

热路径: log_failure_signal 直连 sqlite3 (不依赖 OntologyStore, 避免循环导入);
        rebuild_circuit_breaker_state 启动期重建熔断状态。
冷路径 (Task 5): process_pending_signals 扫信号→候选构造→探针→墓碑→提议;
        process_cq_regression 集合差归因 + mismatch 累计;
        compute_suspect_edges / extract_no_edge_signals 纯函数。
"""
from __future__ import annotations

import json
import sqlite3
import datetime
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# mismatch 定罪阈值: 同一错误列对在 >= N 个不同 CQ 出现 (spec §5.3/R-2)
MISMATCH_RETIRE_THRESHOLD = 3

# 默认 DB 路径（启动期可覆盖）
_default_db_path: str | None = None


def set_default_db_path(path: str) -> None:
    """启动期设定默认 DB 路径，供 log_failure_signal 零参数调用。"""
    global _default_db_path
    _default_db_path = path


def log_failure_signal(
    kind: str,
    payload: dict,
    source: str | None = None,
    db_path: str | None = None,
) -> None:
    """热路径：失败即插入 failure_signal 表，不阻塞调用方。

    异常静默（日志记录不能让查询炸）。直连 sqlite3，不依赖 OntologyStore。

    kind 是事件类型 ('obqc_circuit_break' / 'max_iters' / 'cq_fail')，
    不是 violation 类型。no_edge / join_col_mismatch 等 violation 从 CQ 结果的
    tool_events 里读取，不进 failure_signal 表。
    """
    try:
        path = db_path or _default_db_path
        if not path:
            logger.warning("log_failure_signal: no db_path configured, skipping")
            return
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT INTO failure_signal (kind, payload_json, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, json.dumps(payload, ensure_ascii=False), source,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # 热路径：任何异常静默，绝不让日志采集影响业务
        logger.debug("log_failure_signal failed silently", exc_info=True)


def rebuild_circuit_breaker_state(db_path: str) -> dict:
    """启动期从 failure_signal 表重建内存熔断状态。

    纯函数：返回 {sql_fingerprint: hit_count} 字典。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT payload_json FROM failure_signal "
            "WHERE kind='obqc_circuit_break' AND processed=0"
        ).fetchall()
    finally:
        conn.close()
    state: dict[str, int] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        fp = payload.get("sql_fingerprint")
        if fp:
            state[fp] = state.get(fp, 0) + 1
    return state


# ============ 冷路径 (Task 5, spec §3.5/§4.6/§5.2/§5.3/§5.7/§8) ============

def extract_no_edge_signals(verdict: dict) -> list[dict]:
    """从 obqc_check 返回值抽 no_edge 信号 [{entities, cols}] (spec §3.5)。

    纯函数。hints 里 kind=no_edge 的条目带 entities + cols (AI 首选猜测),
    violations 里同 entity 对有 no_edge 类型。冷路径燃料的规范化形态。
    """
    out = []
    for h in verdict.get("hints") or []:
        if h.get("kind") == "no_edge":
            out.append({"entities": h.get("entities") or [],
                        "cols": h.get("cols") or []})
    return out


def compute_suspect_edges(current_used_edges: set, previous_used_edges: set) -> set:
    """集合差归因纯函数 (spec §5.2): suspects = current - previous。

    边身份用 (source, target, via) 三元组 (spec §3.3, relation 表 UNIQUE)。
    上轮也在用的主干边不进嫌疑 → "高频边不被冤枉"。
    """
    return set(current_used_edges) - set(previous_used_edges)


def _edge_triples(used_edges: list) -> set:
    """[边dict] → {(source, target, via)} 集合。"""
    return {(e["source"], e["target"], e["via"])
            for e in used_edges or [] if e.get("via")}


def process_cq_regression(store) -> dict:
    """扫 CQ 结果: verdict 下降→集合差→suspect_count++; mismatch 累计 (spec §5.2/§5.3)。

    前置条件: 调用前已手动执行 cq_runner 全量跑 + rotate_current_to_previous。
    读全局 CQ 文件 (kg_private/cq_result_current.json + previous)。

    返回 {"suspects_updated": n, "mismatch_thresholds_hit": m,
          "retire_proposals_created": k}
    """
    from core.runtime.cq_runner import CUR_PATH, PREV_PATH, read_results

    cur = read_results(CUR_PATH) or {"results": []}
    prev = read_results(PREV_PATH) or {"results": []}
    prev_by_id = {r["id"]: r for r in prev["results"]}

    out = {"suspects_updated": 0, "mismatch_thresholds_hit": 0,
           "retire_proposals_created": 0}
    batch_id = datetime.datetime.now().isoformat(timespec="seconds")
    mismatches: dict[tuple, dict] = {}

    for r in cur["results"]:
        out["suspects_updated"] += _attribute_suspects(
            store, r, prev_by_id.get(r.get("id")), batch_id)
        _accumulate_mismatches(r, mismatches)

    for entry in mismatches.values():
        _persist_mismatch(store, entry, out)
    return out


def _attribute_suspects(store, result: dict, prev_result: dict | None,
                        batch_id: str) -> int:
    """verdict 下降 (上轮 pass 这轮 fail/partial) → 新用边 suspect_count++。

    返回更新的边数。suspects = edges(这轮) - edges(上轮) (spec §5.2),
    上轮集合从 cq_result_previous.json 里**同一条 CQ** 的 used_edges 取。

    ★ 2026-10-16: 此处原先传 `set()` 死值 → 差集退化成"这轮用过的边全是嫌疑",
    两轮用同样边的 CQ 会把 use_count 最高的主干边顶到人审面板最上面,
    正是 spec §5.2 "高频边不被冤枉" 要防的事。
    老结果文件 (无 used_edges 字段) 仍退化成全量嫌疑 —— 保守但不是正确归因。
    """
    if (prev_result or {}).get("verdict") != "pass" \
            or result.get("verdict") not in ("fail", "partial"):
        return 0
    used = _used_edges_of(result)
    suspects = compute_suspect_edges(used, _used_edges_of(prev_result))
    n = 0
    for (s, t, via) in suspects:
        store.upsert_relation_health(s, t, via, suspect_count_delta=1,
                                     batch_id=batch_id)
        n += 1
    return n


def _accumulate_mismatches(result: dict, mismatches: dict) -> None:
    """聚合一条 CQ result 里的 join_col_mismatch 证据 (按 entity 对 + 无序列对)。

    ★ 聚合 key 无序 (A=B 与 B=A 同一对), 但 entry 里留一份**原始有序**的
    (ents, cols) —— 错写的列对进 col_pair (展示用), 不用来派生 via。
    ★ 嫌疑人是 legal_edges (本体现有的候选边), 不是错写列对派生出的虚构边
      —— 后者按 mismatch 定义在 relation 表里必然不存在 (2026-09-18)。
    """
    cq_id = result.get("id")
    for v in _violations_of(result):
        if v.get("type") != "join_col_mismatch":
            continue
        cols = _mismatch_cols(v.get("hit") or "")
        if not cols:
            continue
        ents = _entities_of_violation(v)
        if not ents:
            continue
        entry = mismatches.setdefault(
            _mismatch_key(ents, cols),
            {"count": 0, "cq_ids": [], "ents": ents, "cols": cols,
             "legal_edges": _legal_edges_of(v)})
        entry["count"] += 1
        if cq_id not in entry["cq_ids"]:
            entry["cq_ids"].append(cq_id)


def _legal_edges_of(v: dict) -> list:
    """violation 里带出的本体现有候选边 (obqc/check.py 结构化写入)。

    老 CQ 结果文件无此字段 → 返回空列表, 下游据此只跳过定罪 (向后兼容)。
    """
    out = []
    for e in v.get("legal_edges") or []:
        if e.get("via") and e.get("source") and e.get("target"):
            out.append(e)
    return out


def _persist_mismatch(store, entry: dict, out: dict) -> None:
    """mismatch 落库: relation_health.mismatch_details + 达阈值生成 retire 提议。

    ★ 证据攒在**本体现有的候选边**上 (可能多条, 都攒); 定罪只在候选唯一时发生
    —— 多条边时"该淘汰哪条"有歧义, 宽进严出, 留给人在 health 面板判断。
    真库里有相当一部分 entity 对是多边的, 这条分支不是边角情况。
    """
    pair = _format_col_pair(entry["cols"])
    cq_ids = ",".join(map(str, entry["cq_ids"]))
    suspects = entry.get("legal_edges") or []
    if not suspects:
        # 无候选边 (老数据缺字段): 无从指认, 不攒也不定罪
        return
    for e in suspects:
        store.update_mismatch_details(e["source"], e["target"], e["via"], pair, cq_ids)
    if len(set(entry["cq_ids"])) < MISMATCH_RETIRE_THRESHOLD:
        return
    out["mismatch_thresholds_hit"] += 1
    if len(suspects) > 1:
        logger.info("mismatch 达阈值但 %s↔%s 有 %d 条候选边, 不自动定罪 (证据已攒)",
                    entry["ents"][0], entry["ents"][1], len(suspects))
        return
    s, t, via = suspects[0]["source"], suspects[0]["target"], suspects[0]["via"]
    # 已有同方向 retire 提议 pending 则不重复
    if not _has_pending_retire(store, s, t, via, pair):
        store.insert_proposal(
            "relation_col",
            {"source": s, "target": t, "via": via, "col_pair": pair,
             "source_col": suspects[0].get("source_col"),
             "target_col": suspects[0].get("target_col"),
             "cq_ids": entry["cq_ids"]},
            reason=f"LLM 在 {len(set(entry['cq_ids']))} 个不同 CQ 里错写列对 {pair} "
                   f"(阈值 {MISMATCH_RETIRE_THRESHOLD}) — 疑似本条边把它带偏",
            proposed_by="mismatch_accumulator", direction="retire")
        out["retire_proposals_created"] += 1


def _used_edges_of(result: dict | None) -> set:
    """一条 CQ result 的 used_edges 三元组集合 (从 tool_events 聚合)。

    result 为 None (上轮没跑过这条 CQ) → 空集。
    """
    triples = set()
    for te in (result or {}).get("tool_events") or []:
        triples |= _edge_triples(te.get("used_edges"))
    return triples


def _violations_of(result: dict) -> list:
    """一条 CQ result 的所有 obqc violations (从 tool_events 聚合)。"""
    out = []
    for te in result.get("tool_events") or []:
        out.extend(te.get("violations") or [])
    return out


def _entities_of_violation(v: dict) -> tuple | None:
    """violation 里抽 entity 对: 优先 entities 字段 (Task5 补强),
    兜底解析 no_edge 的 hit 'a JOIN b'。"""
    ents = v.get("entities")
    if isinstance(ents, list) and len(ents) == 2 and all(ents):
        return (ents[0], ents[1])
    hit = v.get("hit") or ""
    if " JOIN " in hit:
        parts = hit.split(" JOIN ")
        if len(parts) == 2 and parts[0] and parts[1]:
            return (parts[0].strip(), parts[1].strip())
    return None


def _mismatch_cols(hit: str) -> tuple | None:
    """join_col_mismatch 的 hit 'la.lcol = ra.rcol' → **原始有序** ('LCOL','RCOL')。

    位置对应 violation 的 entities: entities[0]↔lcol, entities[1]↔rcol
    (obqc/check.py:_check_one_join 里 eA/lcol 与 eB/rcol 同序传入)。
    via 必须按这个顺序拼, 不能用字典序归一后的顺序 (2026-09-18 bug)。
    """
    if " = " not in hit:
        return None
    left, _, right = hit.partition(" = ")
    lcol = left.partition(".")[2].strip().upper() if "." in left else left.strip().upper()
    rcol = right.partition(".")[2].strip().upper() if "." in right else right.strip().upper()
    if not lcol or not rcol:
        return None
    return (lcol, rcol)


def _mismatch_key(ents: tuple, cols: tuple) -> tuple:
    """聚合 key: 把 (entity, col) 绑成对再排序 → 方向无关。

    ★ 不能只排列名: entities 和 cols 是位置对应的, 单排列名会让
    (lib_loan,lib_reader)+(READER_ID,READER_ID) 和反向写法算成两组。
    绑对后排序, 同一条边的两种写法归到同一个 key。
    """
    return tuple(sorted([(ents[0], cols[0]), (ents[1], cols[1])]))


def _format_col_pair(cols: tuple) -> str:
    """列对的展示/去重形态 (无序, 字典序归一)。

    ★ 注意: 这是 LLM **错写**的那个列对, 只用于展示和去重, **不能**用它派生 via
    —— 按 mismatch 定义它不等于本体里任何一条边的列对, 派生出的 via 在 relation
    表必然查无此行 (2026-09-18 取证)。要定位的真边走 violation.legal_edges。
    """
    pair = sorted([cols[0], cols[1]])
    return f"{pair[0]}={pair[1]}"


def _has_pending_retire(store, source: str, target: str, via: str,
                        col_pair: str) -> bool:
    """同 (边, 列对) 已有 pending/approved 的 retire 提议 → 去重。"""
    for p in store.list_proposals():
        if p["direction"] != "retire" or p["review_status"] not in ("pending", "approved"):
            continue
        payload = json.loads(p["payload_json"] or "{}")
        if (payload.get("source") == source and payload.get("target") == target
                and payload.get("col_pair") == col_pair):
            return True
    return False


def process_pending_signals(
    store,
    run_sql: Callable,
    get_index_info: Callable,
    entities: dict,
    relations: list,
    limit: int = 50,
    probe_fn: Callable | None = None,
) -> dict:
    """冷路径主流程: 扫未处理信号 → 候选构造 → 探针 → 墓碑 → INSERT proposal。

    候选列对 (spec §3.5 + §4.6): {hint.cols} ∪ (orphan(eA)×PK(eB)) ∪ (PK(eA)×orphan(eB))。
    只处理 obqc_circuit_break 的 no_edge 生成新增提议; max_iters/cq_fail 标
    processed 不送队列 (spec §8)。

    返回 {"processed": n, "proposals_created": m, "skipped": k, "tombstone_hits": t}
    """
    from capabilities.ontology.selfevolve.probe import probe_relation, find_orphan_cols

    probe = probe_fn or probe_relation
    out = {"processed": 0, "proposals_created": 0, "skipped": 0,
           "tombstone_hits": 0}
    for signal in store.list_unprocessed_signals(limit):
        out["processed"] += 1
        payload = json.loads(signal["payload_json"] or "{}")
        for ne in payload.get("no_edge_signals") or []:
            ents = ne.get("entities") or []
            if len(ents) != 2:
                continue
            _process_one_no_edge(
                store, ents[0], ents[1], ne.get("cols") or [],
                entities, relations, find_orphan_cols, probe,
                run_sql, get_index_info, out)
        store.mark_signal_processed(signal["id"])
    return out


def _process_one_no_edge(store, eA, eB, hint_cols, entities, relations,
                         find_orphan_cols, probe, run_sql, get_index_info,
                         out: dict) -> None:
    """单个 no_edge 信号: 候选构造 → 探针 → pass 才 (墓碑+)提议。

    ★ 每对探完落一条 `selfevolve.probe_verdict` 埋点, 信号收尾落一条
    `selfevolve.no_edge_processed` 汇总。没有这两条时, fail/unknown 跑完即丢
    (只有 pass 留痕在 proposal.probe_result_json) → `proposals_created=0`
    在"逐对判过都不合格"与"压根没跑探针"两种情况下**返回值完全相同**,
    而后者是本项目栽过最多次的形态。详见 memory 里 2026-09-20e 那轮。
    """
    from core.observability import log_event

    probed = passed = 0
    for (sc, tc) in _candidate_pairs(eA, eB, hint_cols, entities, relations,
                                     find_orphan_cols):
        result = probe(eA, eB, sc, tc, run_sql, get_index_info)
        probed += 1
        log_event("selfevolve.probe_verdict", {
            "source": eA, "target": eB, "source_col": sc, "target_col": tc,
            "verdict": None if result is None else result.verdict,
            # evidence 带 inclusion_rate / null_rate / col_similarity —— 判死的
            # 理由要留下, 否则只知道"fail 了"复查不了是哪一层卡的。
            "evidence": {} if result is None else result.evidence,
        })
        if result is None or result.verdict != "pass":
            continue
        passed += 1
        _insert_proposal_with_tombstone(store, eA, eB, sc, tc, result, out)
    log_event("selfevolve.no_edge_processed", {
        "entities": [eA, eB], "probed": probed, "passed": passed})


# 候选预筛阈值: 复用探针第三层的命名 Jaccard 护栏, **不许另写一份字面量**。
# 候选预筛阈值: 复用探针第三层的命名 Jaccard 护栏, **不许另写一份字面量**。
# 两处分叉的后果是静默的 —— 预筛严于探针时, probe.py 的校准注释还写着
# "3 条真边降 unknown 可接受", 而实际已变成"连探都不探"。
# 模块级 import 安全: probe.py 不反向依赖 signals (它是纯函数 + 注入 I/O)。
from capabilities.ontology.selfevolve.probe import (  # noqa: E402
    NAME_SIM_THRESHOLD as _PREFILTER_SIM_THRESHOLD,
    _token_jaccard as _prefilter_jaccard,
)


def _candidate_pairs(eA, eB, hint_cols, entities, relations,
                     find_orphan_cols) -> list:
    """候选列对构造 (spec §3.5 + §4.6): hint ∪ 预筛后的 orphan×PK 两向。

    PK 取本体声明 (entity.attributes kind=pk), 不查物理库 (plan v3 决策)。
    去重 + 保持确定性顺序。筛不筛、为什么筛见 _prefilter_pairs。
    """
    hint = ([(hint_cols[0], hint_cols[1])]
            if len(hint_cols or []) == 2 else [])
    orph_a = find_orphan_cols(eA, entities, relations)
    orph_b = find_orphan_cols(eB, entities, relations)
    pk_a = sorted(_declared_pk_cols(entities.get(eA) or {}))
    pk_b = sorted(_declared_pk_cols(entities.get(eB) or {}))
    product = [(oc, pk) for oc in sorted(orph_a) for pk in pk_b]
    product += [(pk, oc) for pk in pk_a for oc in sorted(orph_b)]
    kept = _prefilter_pairs(product, hint, eA, eB)
    return list(dict.fromkeys(hint + kept))


def _prefilter_pairs(product: list, hint: list, eA: str, eB: str) -> list:
    """笛卡儿积按命名 Jaccard 预筛, 并把被筛掉的数量落成埋点。

    ★ 为什么笛卡儿积那部分要预筛 (2026-09-20f): n_paths=0 渠道采来的燃料
    `cols` 是**空的** (它不是"agent 写错了 JOIN"而是"agent 问了本体、本体
    答不出", 没有 SQL 可抄列名) → hint 项为空, 候选完全退化成
    `orphan(eA)×PK(eB) ∪ PK(eA)×orphan(eB)`。真实燃料产出**几十个**候选,
    里面 `REQUEST_DATE↔LIB_BRANCH_ID`、`REC_STATUS↔BASKET_ID` 这种零交集垃圾对
    占了大半, 每个都要连真库跑几条 COUNT。换成属性多的大表会爆。

    **零语义损失** (前提已用本地取证脚本验过, 脚本不随仓分发):
    探针第三层 `_layer3_guards` 本来就拿同一个阈值判, 不过 → unknown →
    只有 pass 才建提议 → 这些对本来永远产不出提议。预筛只是把已有判据前移。
    真实数据: 候选降到原先约三分之一, 而已登记真边的存活率远高于垃圾候选。

    **hint 不预筛**: 它来自 obqc 硬拦时抓的真实 JOIN 条件 = agent 的行为证据,
    再难看也要探; 预筛只针对我们自己构造的笛卡儿积。

    **被筛掉的必须留痕**: 否则这笔债 (省开销) 会把另一笔债 (判定不可观测)
    挖得更大 —— 候选无声消失比探完不留痕更难查。
    """
    from core.observability import log_event

    fresh = [p for p in dict.fromkeys(product) if p not in set(hint)]
    kept = [p for p in fresh
            if _prefilter_jaccard(p[0], p[1]) >= _PREFILTER_SIM_THRESHOLD]
    dropped = len(fresh) - len(kept)
    if dropped:
        log_event("selfevolve.candidates_filtered", {
            "entities": [eA, eB], "kept": len(hint) + len(kept),
            "dropped": dropped, "threshold": _PREFILTER_SIM_THRESHOLD})
    return kept


def _declared_pk_cols(entity: dict) -> list:
    """本体声明的 PK 列 (plan v3: 候选构造只看声明, 不调 get_index_info)。"""
    return [a["name"] for a in entity.get("attributes") or []
            if a.get("kind") == "pk" and a.get("name")]


def _derive_edge_via(source: str, source_col: str, target: str, target_col: str) -> str:
    """生成 via, 与 store._derive_via 同口径: `{source}.{sc}={target}.{tc}`。

    ★ plan v3 P1-12 写的是简化版 `{sc}={tc}`, 但真库 relation.via 全是带表名
    格式 (幂等键 + 墓碑匹配 + Task 6 upsert 都按它) → 实现侧以 store 口径为准。
    """
    return f"{source}.{source_col}={target}.{target_col}"


def _insert_proposal_with_tombstone(store, eA, eB, sc, tc,
                                    probe_result, out) -> int:
    """墓碑查询 (spec §5.7) + 去重 + INSERT proposal。"""
    via = _derive_edge_via(eA, sc, eB, tc)
    # 去重: 同列对已有 pending/approved 提议 → skip
    for p in store.list_proposals():
        if p["kind"] != "relation" or p["review_status"] not in ("pending", "approved"):
            continue
        payload = json.loads(p["payload_json"] or "{}")
        if (payload.get("source") == eA and payload.get("target") == eB
                and payload.get("source_col") == sc
                and payload.get("target_col") == tc):
            out["skipped"] += 1
            return -1
    tomb_hit, tomb_note = _tombstone_lookup(store, eA, eB, via)
    if tomb_hit:
        out["tombstone_hits"] += 1
    pid = store.insert_proposal(
        "relation",
        {"source": eA, "target": eB, "source_col": sc, "target_col": tc,
         "via": via, "type": "MANY_TO_ONE"},
        reason=f"探针 pass: {eA}.{sc} ↔ {eB}.{tc}",
        probe_result={"verdict": probe_result.verdict,
                      "checks": probe_result.checks,
                      "evidence": probe_result.evidence},
        proposed_by="probe", direction="add",
        tombstone_hit=tomb_hit, tombstone_note=tomb_note)
    out["proposals_created"] += 1
    return pid


def _tombstone_lookup(store, source: str, target: str,
                      via: str) -> tuple[bool, str]:
    """墓碑查询 (spec §5.7 R-11): 被拒提议 / 软删边 同三元组命中。

    ★ 无向匹配 (与 §4.2 D-2 探针"至少一端是 PK"的无向口径一致): 同一条边
    反向记录也算命中。真库里就有这样的实例: 一条 lib_book→lib_loan 的软删边,
    探针从 lib_loan→lib_book 重提同一条边 —— 有向匹配会漏掉, 人审看到
    "无历史处置", 正是本节要防的抖动。

    命中不静默跳过 —— 仍生成提议但 tombstone_hit=1 + 带处置摘要。
    """
    keys = {_norm_edge_key(source, target, via),
            _norm_edge_key(*_reverse_edge_key(source, target, via))}
    notes = []
    for p in store.list_proposals():
        if p["review_status"] != "rejected":
            continue
        payload = json.loads(p["payload_json"] or "{}")
        if _norm_edge_key(payload.get("source"), payload.get("target"),
                          payload.get("via")) in keys:
            when = p.get("reviewed_at") or p.get("proposed_at") or "?"
            note = p.get("review_note") or "无备注"
            notes.append(f"历史被拒提议 #{p['id']} ({when}): {note}")
    # 软删边: relation 表 status=0 同三元组 (直接 SQL, store 无专查方法)。
    # ★ 列名大小写归一在 SQL 侧用 COLLATE NOCASE —— 表名/实体名本就同形,
    # 整个 via 串比对等价于逐段比对, 不必在 Python 里拆串重拼。
    for s, t, v in sorted(keys):
        rows = store._conn.execute(
            "SELECT via FROM relation WHERE source=? AND target=? "
            "AND via=? COLLATE NOCASE AND status=0",
            (s, t, v)).fetchall()
        if rows:
            notes.append(f"软删边命中 ({s}→{t} via {rows[0]['via']})")
    return (bool(notes), "; ".join(notes))


def _norm_edge_key(source, target, via) -> tuple:
    """边 key 归一: 只拉平 **via 的大小写**, 端点名原样。

    ★ 2026-10-19 真库取证: `relation.via` 里软删边绝大多数存**小写**列名, 而
    `entity.attributes` 的列名是**大写** —— 探针 `_derive_edge_via` 从 attributes
    取列名拼 via, 墓碑用 `WHERE via=?` 精确匹配 (SQLite `'A'='a'` 为 0), 于是
    可比对的软删边几乎全部漏匹配。后果: 人审看到"无历史处置", 对一条自己判过死
    的边重新裸判, 正是 §5.7 要防的抖动。

    `probe.py:164 _norm_key_cols` 2026-09-09 已为同一个口径分裂修过上一层
    (information_schema 大写 vs 本体 via 小写), 墓碑这层当时没跟上。

    只归一大小写, **不碰别的** —— 列名真不同的边必须仍算不同条, 否则每条新提议
    都挂假墓碑, 人审看到的历史处置全是噪音 (给错信息比少给信息更糟)。
    """
    return (source, target, (via or "").lower())


def _reverse_edge_key(source: str, target: str, via: str) -> tuple:
    """把 (source, target, via) 翻成对端方向的等价 key。

    via 是 `{source}.{sc}={target}.{tc}` (store._derive_via 口径), 翻转即
    `{target}.{tc}={source}.{sc}`。via 不合口径时原样返回 (不猜)。
    """
    left, sep, right = (via or "").partition("=")
    if not sep:
        return (target, source, via)
    return (target, source, f"{right}={left}")
