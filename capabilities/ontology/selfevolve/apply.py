"""capabilities/ontology/selfevolve/apply.py — 提议落地 + CQ 回归门 (spec §5.6/§6.3).

apply_proposal: 审批守卫 (approved + patch_status=pending 才执行) + 二元组分发。
run_regression: 独立 CQ 回归门 — sha256 钉死考题集, 重跑 CQ (不写 current/previous),
对比 baseline, 新增 hard_block → 拒绝落地。

★ kind='relation_col' (mismatch 累计定罪) 走 retire 语义: 软删 payload.via 指认的
那条**本体现有的边**。★ via 必须在 relation 表查得到 —— 查无此行要响亮失败,
不许静默匹配 0 行 (2026-09-18: 老的 mismatch 提议指向的是错写列对派生的虚构边)。
★ baseline 机制: apply 开始时拷贝 cq_result_current.json →
kg_private/cq_baselines/{run_id}.json, 回归门读它对比。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable

_KG = Path(__file__).resolve().parent.parent.parent.parent / "kg_private"
_BASELINES_DIR = _KG / "cq_baselines"

# 分发字典 (扩展加一行, 不改函数体)
_APPLY_DISPATCH: dict[tuple[str, str], Callable] = {
    ("relation", "add"): "_apply_relation_proposal",
    ("relation", "retire"): "_apply_retire_proposal",
    ("relation_col", "retire"): "_apply_relation_col_proposal",
}


def apply_proposal(proposal: dict, store) -> dict:
    """审批守卫 + 分发执行。返回 {"ok": bool, "detail": str}。

    流程 (plan Task 6 baseline 机制):
    1. 守卫: review_status='approved' AND patch_status='pending'
    2. dispatch 落库 (upsert / soft_delete)
    3. run_regression 对比 baseline
    4. 按 regression_ok 更新 patch_status (调用方做, 或此处做)
    """
    if proposal.get("review_status") != "approved":
        return {"ok": False, "detail": "提议未经人审批准 (review_status != approved)"}
    if proposal.get("patch_status") != "pending":
        return {"ok": False,
                "detail": f"提议已处理过 (patch_status={proposal.get('patch_status')})"}

    handler_name = _APPLY_DISPATCH.get(
        (proposal.get("kind"), proposal.get("direction")))
    if not handler_name:
        return {"ok": False,
                "detail": f"无 apply 分支: kind={proposal.get('kind')} "
                          f"direction={proposal.get('direction')}"}
    handler = globals()[handler_name]

    payload = json.loads(proposal.get("payload_json") or "{}")
    # ★ 落库前拍快照: 回归门 (闸2) 在本函数之后跑, 拒绝时靠它精确还原 (见 rollback_apply)
    snapshot = _edge_snapshot(store, payload, proposal.get("direction"))
    out = handler(payload, store)
    if not out.get("ok"):
        return out
    store.patch_proposal_patch_status(proposal["id"], "applied")
    out["rollback_snapshot"] = snapshot
    return out


def _apply_relation_proposal(payload: dict, store) -> dict:
    """relation add: label 兜底闸 → 解包 → upsert_relation → 本体热重载。

    ★ label 非空是硬要求 (2026-09-19): probe 产的 payload 不带 label, 首道闸
    在人审端点 (review 必填), 这里兜底挡住绕过前端直接 PATCH+apply 的路径。
    落库的裸边会让概念层/agent 靠 get_join_path 选边时没区分度
    (见 check_relation_label_distinct), 与 R10 那轮"裸边 93→0"的成果对冲。
    """
    if not (payload.get("label") or "").strip():
        return {"ok": False,
                "detail": "提议缺 label, 拒绝落库 —— 新增边必须带能说清业务语义的 "
                          "label (概念层/agent 靠它选边)。请在人审时填写后重试。"}
    store.upsert_relation(payload)
    _reload_ontology()
    return {"ok": True,
            "detail": f"已落库: {payload.get('source')}→{payload.get('target')} "
                      f"via {payload.get('via')}"}


def _apply_retire_proposal(payload: dict, store) -> dict:
    """relation retire: 闸1 依赖检查 → 无依赖才 soft_delete。"""
    deps = check_relation_dependencies(payload, store)
    if deps:
        return {"ok": False, "detail": f"依赖未解除: {deps}"}
    store.soft_delete_relation(payload["source"], payload["target"],
                               payload["via"])
    _reload_ontology()
    return {"ok": True,
            "detail": f"已软删: {payload.get('source')}→{payload.get('target')} "
                      f"via {payload.get('via')}"}


def _apply_relation_col_proposal(payload: dict, store) -> dict:
    """relation_col retire (mismatch 累计定罪): 软删 via 指认的那条现有边。

    与 _apply_retire_proposal 的差别只在**入口校验**: relation_col 的 payload
    来自 mismatch 归因, via 是从 violation.legal_edges 抄来的; 必须先确认它在
    relation 表里真的存在, 否则 soft_delete 匹配 0 行还返回成功 —— 安静的无害失败。
    """
    via = payload.get("via")
    if not via:
        return {"ok": False,
                "detail": "payload 缺 via, 无法定位要淘汰的边 (拒绝静默 no-op)"}
    # ★ 守卫必须按**三键**查, 与 soft_delete_relation 同口径 (2026-10-19):
    # via 非唯一 —— 真库实测 3 组各 2 行, 每组是同一条边的正反两向
    # (`lib_fee_rule→title` 与 `title→lib_fee_rule` 共用一个 via),
    # UNIQUE 建在三元组上不是 via 上。单键 fetchone() 会读到另一行的 status →
    # 守卫放行 → 软删匹配 0 行 → 返回 ok=True → 回归门白跑一次 LLM CQ (几十秒)
    # → 边其实没动。正是本函数 docstring 自称要防的"安静的无害失败"。
    source, target = payload.get("source"), payload.get("target")
    row = store._conn.execute(
        "SELECT status FROM relation WHERE source=? AND target=? AND via=?",
        (source, target, via)).fetchone()
    if row is None:
        return {"ok": False,
                "detail": f"边不存在于 relation 表 (查无此行): "
                          f"{source}→{target} via {via} —— "
                          f"拒绝落地, 避免软删匹配 0 行却报成功"}
    if row["status"] == 0:
        return {"ok": False, "detail": f"边已是软删状态, 无需重复淘汰: {via}"}
    return _apply_retire_proposal(payload, store)


def _edge_snapshot(store, payload: dict, direction: str | None) -> dict:
    """落库前记下这条边的原状, 供回归门拒绝时还原 (spec §5.6 闸2)。

    只关心三元组定位到的那一行: 存在与否 + status。不存在则 existed=False,
    还原时硬删 —— 避免留下 status=0 的幽灵行污染墓碑查询。
    """
    source, target = payload.get("source"), payload.get("target")
    source_col, target_col = payload.get("source_col"), payload.get("target_col")
    if not source or not target:
        return {}
    # via 优先从 col 派生 (与 upsert_relation 同口径 —— 它忽略传入 via, 只认 col;
    # add 路径必须走这条, 否则快照可能指向另一行)。
    # col 缺失时兜底用 payload.via: relation_col 提议是从 legal_edges 抄的真边 via,
    # 不保证带 col。★ 不兜底 = 空快照 = 回归门拒绝时 rollback 静默失效, 边留在软删态。
    if source_col and target_col:
        via = f"{source}.{source_col}={target}.{target_col}"
    elif payload.get("via"):
        via = payload["via"]
    else:
        return {}
    row = store._conn.execute(
        "SELECT * FROM relation WHERE source=? AND target=? AND via=?",
        (source, target, via)).fetchone()
    return {"source": source, "target": target, "via": via,
            "direction": direction,
            "existed": row is not None,
            "status": row["status"] if row is not None else None,
            # ★ 整行留档, 不只 status (2026-10-19): upsert 的 ON CONFLICT DO UPDATE
            # 把 _ROLLBACK_COLS 这几列**全列覆盖**, 只写回 status 等于让人工精校的
            # label / extra_json.origin 永久消失, 而界面报的是"改动已回滚"。
            # ontology.db 不进 git → 丢了无从恢复 (R10 那轮 label 是人一条条填的)。
            "row": {k: row[k] for k in _ROLLBACK_COLS} if row is not None else None}


# 回滚要还原的列 = upsert 的 ON CONFLICT DO UPDATE 会覆盖的列 + status。
# ★ 两处必须同步: 这里少一列, 那一列就在"改动已回滚"的字面下悄悄留下新值。
_ROLLBACK_COLS = ("type", "label", "source_col", "target_col",
                  "bridge_count", "extra_json", "status")


def rollback_apply(store, snapshot: dict) -> None:
    """回归门拒绝后还原落库前状态 (spec §5.6: 闸拒绝 = 改动不得生效)。

    - 原先不存在的边 (add 新增) → 硬删该行, 不留 status=0 幽灵
    - 原先存在 → status 写回原值 (retire 软删的边恢复 active)
    还原后热重载本体, 否则内存里仍是坏边。
    """
    if not snapshot or not snapshot.get("via"):
        return
    key = (snapshot["source"], snapshot["target"], snapshot["via"])
    if snapshot.get("existed"):
        # 整行还原 (_ROLLBACK_COLS)。老快照只有 status 时退化成只写 status ——
        # 回归门拒绝那一刻的快照可能是本次修复前拍的, 不能因此抛。
        row = snapshot.get("row")
        if row:
            # 列名取自模块常量 _ROLLBACK_COLS (非外部输入) 才敢进 f-string;
            # 值全部走占位符。快照是本进程 _edge_snapshot 产的, 不过网。
            cols = [c for c in _ROLLBACK_COLS if c in row]
            store._conn.execute(
                f"UPDATE relation SET {', '.join(f'{c}=?' for c in cols)} "
                "WHERE source=? AND target=? AND via=?",
                (*(row[c] for c in cols), *key))
        else:
            store._conn.execute(
                "UPDATE relation SET status=? WHERE source=? AND target=? AND via=?",
                (snapshot["status"], *key))
    else:
        store._conn.execute(
            "DELETE FROM relation WHERE source=? AND target=? AND via=?", key)
    store._conn.commit()
    _reload_ontology()


def _reload_ontology() -> None:
    """本体热重载 (同 web 层 _reload_ontology 模式)。单测里静默 (无 settings 依赖)。"""
    try:
        from core.config import settings
        from core.ontology_view import set_ontology
        set_ontology(settings.obda_ontology_path)
    except Exception:
        # 单测环境 (tmp_path 库, 不是权威库) 重载无意义且可能失败 → 静默
        pass


def check_relation_dependencies(relation: dict, store) -> list[str]:
    """retire 闸1: 返回依赖列表 (空 = 可淘汰)。

    ★ 真实 concept_condition / filter_rule 是结构化列, 不是 JSON join_path
    (plan v3 已知坑, 按结构化列判):
    - concept_condition 按 concept_id 分组, 组内 distinct entity 集合同时
      包含边的 source 和 target → 该概念跨表, 依赖这条边
    - filter_rule.entity 命中边任一端点 → 依赖 (保守: 闸1 宁严勿松)
    """
    deps: list[str] = []
    src, tgt = relation.get("source"), relation.get("target")
    if not src or not tgt:
        return deps

    rows = store._conn.execute(
        "SELECT concept_id, entity FROM concept_condition WHERE status=1"
    ).fetchall()
    concept_ents: dict[str, set] = {}
    for r in rows:
        concept_ents.setdefault(r["concept_id"], set()).add(r["entity"])
    for cid, ents in concept_ents.items():
        if src in ents and tgt in ents:
            deps.append(f"concept '{cid}' 条件跨 {src}↔{tgt} (跨表 JOIN 走此边)")

    fr_rows = store._conn.execute(
        "SELECT entity, col FROM filter_rule WHERE status=1").fetchall()
    for r in fr_rows:
        if r["entity"] in (src, tgt):
            deps.append(f"filter_rule {r['entity']}.{r['col']} 挂在边端点")
    return deps


def run_regression(store, proposal: dict,
                   cq_set_path: Path | None = None,
                   baselines_dir: Path | None = None) -> dict:
    """独立 CQ 回归门 (spec §5.6 闸2)。

    1. sha256 钉死 cq_set (运行前后比对, 防偷换)
    2. 跑全套 CQ (复用 run_one_cq, 结果不写 current/previous)
    3. 对比 baseline (baselines_dir/{regression_run_id}.json, 默认 kg_private)
    4. 新增 hard_block → regression_ok=0

    返回 {"regression_ok": bool, "run_id": str, "new_hard_blocks": [...],
          "uncovered_cqs": [...], "unrelated_blocks": [...],
          "truncated_cqs": [...], "no_db_evidence_cqs": [...]}
    - new_hard_blocks: 被判定为本次改动引入的 (只认 ①JOIN 类违规)
    - uncovered_cqs: 本轮跑了但 baseline 没跑过 → 这轮闸**判不了**
    - unrelated_blocks: 硬拦了但违规类型与边无因果 (LLM 噪音) → 不定罪
    - truncated_cqs: 撞 max_tokens 没跑完 → 它的"零违规"不可信, 判不了
    - no_db_evidence_cqs: 没有一次 execute_sql 成功 → 零违规只因没查库, 判不了
    后四者非空时调用方要响亮说出来 (判据全部见 _diff_new_hard_blocks)。
    """
    from core.runtime import cq_runner

    path = cq_set_path or cq_runner.CQ_SET_PATH
    # ★ 必须在 sha_before 之前: _cq_set_sha256 直接 read_bytes(), 比下面的
    # load_cq_set (自己也会 ensure) 更早读盘。只挂 load_cq_set 时, clone 后
    # 第一次 approve→apply 会炸在这一行 → 穿成 500 → 边已落库但回归门没跑、
    # 改动没回滚。两个挂点都要, 缺一个这条路径等于没闸。
    cq_runner.ensure_cq_set(path)
    sha_before = _cq_set_sha256(path)
    cases = cq_runner.load_cq_set(path)
    results = [cq_runner.run_one_cq(case) for case in cases]
    sha_after = _cq_set_sha256(path)
    if sha_before != sha_after:
        raise RuntimeError(
            f"cq_set 在回归运行中被修改: {sha_before[:12]} -> {sha_after[:12]}")

    run_id = proposal.get("regression_run_id") or ""
    baseline = _read_baseline(run_id, baselines_dir)
    new_blocks, uncovered, unrelated = _diff_new_hard_blocks(results, baseline)
    truncated = sorted(r.get("id") for r in results or [] if r.get("truncated"))
    no_db = sorted(r.get("id") for r in results or [] if r.get("no_db_evidence"))
    return {"regression_ok": not new_blocks, "run_id": run_id,
            "new_hard_blocks": new_blocks, "uncovered_cqs": uncovered,
            "unrelated_blocks": unrelated, "truncated_cqs": truncated,
            "no_db_evidence_cqs": no_db}


def snapshot_baseline(run_id: str | None = None) -> str:
    """apply 开始时拷贝 current → baselines/{run_id}.json, 返回 run_id。"""
    from core.runtime.cq_runner import CUR_PATH, read_results

    run_id = run_id or f"{int(time.time() * 1000)}"
    cur = read_results(CUR_PATH)
    _BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    target = _BASELINES_DIR / f"{run_id}.json"
    if cur is not None:
        target.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    else:
        target.write_text("{}", encoding="utf-8")
    return run_id


def _read_baseline(run_id: str, baselines_dir: Path | None = None) -> dict:
    base = baselines_dir or _BASELINES_DIR
    p = base / f"{run_id}.json"
    if not p.exists():
        return {"results": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _cq_set_sha256(path: Path) -> str:
    """sha256 钉死 (spec §10): 只在内存比对, 不存储。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ①JOIN 路径类违规 (obqc/check.py _check_one_join 产出的两种)。
# 只有这两类与 relation 的增删有因果关系 —— 回归门的定罪面限于此, 理由见
# _diff_new_hard_blocks 的 "为什么只拿 JOIN 类定罪"。
_JOIN_VIOLATION_TYPES = frozenset({"no_edge", "join_col_mismatch"})


def _hard_block_cq_ids(results: list, join_only: bool = False) -> set:
    """有 hard_block (execute_sql 被 obqc 拦) 的 CQ id 集合。

    join_only=True 时只认 ①JOIN 类违规 (_JOIN_VIOLATION_TYPES)。
    """
    out = set()
    for r in results or []:
        for te in r.get("tool_events") or []:
            vs = te.get("violations") or []
            if join_only:
                vs = [v for v in vs if v.get("type") in _JOIN_VIOLATION_TYPES]
            if vs:
                out.add(r.get("id"))
                break
    return out


def _diff_new_hard_blocks(results: list, baseline: dict) -> tuple[list, list, list]:
    """新增 hard_block: **只在 baseline 跑过的 CQ 上算**, 且**只认 ①JOIN 类违规**。

    返回 (new_hard_blocks, uncovered_cqs, unrelated_blocks)。

    ★ 为什么要按 baseline 覆盖面收窄 (2026-09-19 真库取证):
    cq_set 的题数多于 baseline 来源 cq_result_current.json 跑过的题数
    —— 后来新扩的那些题从没进过 baseline。按裸差集算, 它们硬拦
    一次就被记成"新增", 闸拒绝并回滚, **而这跟被审的提议毫无关系**。
    回归门问的是"这次改动有没有弄坏**原本好的**"; baseline 里没有的 CQ
    无从判断"原本"是好是坏, 判不了就不该定罪。

    ★ 为什么只拿 JOIN 类定罪 (同日 approve→apply 真数据首跑实测):
    闸拒了 proposal #3, 理由是 CQ-14 新增 hard_block。查日志那条违规是
    `column_not_found: LIB_LOAN_ACCT_REL.REL_ID` —— agent 自己猜了个物理表
    和本体里都不存在的列名。与被审的边无因果, 三点证据:
      1. 那是 obqc **②列归属**校验, 新边属 **①JOIN 路径**, 两回事
      2. 全程每一条 SQL **零次** JOIN 到新边 (agent 仍走 WHERE IN 字面量)
      3. 该列名错误在全部历史真实运行里只出现过这一次 = LLM 非确定性噪音
    CQ 重跑本身是非确定性过程。把"任何 violations"都算改动引入, 闸就会
    随机误拒任何提议 —— 而 relation 的增删只可能影响 ①JOIN 那条校验路径。

    ★ 三处收窄都不许静默: uncovered_cqs (baseline 没跑过, 判不了)、
    unrelated_blocks (硬拦了但类型与边无关), 以及 truncated_cqs (撞
    max_tokens 没跑完 —— 定罪依据是 tool_events 里的 violations, 截断的
    运行可能根本没走到那句会被拦的 execute_sql, 它的"零违规"不可信;
    由 run_regression 直接从 results 取, 不参与本函数的差集运算)。
    三者都要回传给调用方说出来。
    闸放行分"真没坏"和"根本没看"两种, 不可分辨就等于没有闸 —— 同本项目
    反复栽的"安静的无害失败"。
    """
    base_results = baseline.get("results") or []
    base_all_ids = {r.get("id") for r in base_results}
    cur_join = _hard_block_cq_ids(results, join_only=True)
    base_join = _hard_block_cq_ids(base_results, join_only=True)
    new_blocks = sorted((cur_join & base_all_ids) - base_join)
    uncovered = sorted(r.get("id") for r in results or []
                       if r.get("id") not in base_all_ids)
    # 硬拦了但没有一条 JOIN 类违规 → 与边无因果的噪音, 报出不定罪
    cur_any = _hard_block_cq_ids(results)
    base_any = _hard_block_cq_ids(base_results)
    unrelated = sorted(((cur_any - cur_join) & base_all_ids) - base_any)
    return new_blocks, uncovered, unrelated
