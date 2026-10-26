"""capabilities/ontology/selfevolve/probe.py — 真库探针 (准入闸门, spec §4).

probe_relation: 三层判据纯函数, 真库 I/O 全注入 (run_sql / get_index_info),
无副作用可单测。判据只信物理真值, 不信本体声明 (本体缺边正是要发现的东西)。

find_orphan_cols: 孤儿列集合差 (spec §4.6) — 实体里未被任何边使用的列,
一次内存集合差, 零查库零 LLM。只当失败信号的增强, 不当独立生成源。

阈值常量: 2026-09-05 真库校准定稿 (校准脚本为本地过程记录, 不随仓分发;
SEED=20260905, 正样本为真库 active 边 + 合成负样本)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal

# 标识符白名单: 与 web.app._safe_table_ident 同口径 ([A-Za-z_][A-Za-z0-9_]*)。
# 探针的四个标识符 (两表名 + 两列名) 全部要过这一关, 理由见 _invalid_idents。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# --- 阈值常量 (2026-09-05 真库校准定稿, 校准脚本为本地过程记录, 不随仓分发) ---
# 六档 F1: 0.5→0.912 / 0.6→0.876 / 0.7→0.867 / 0.75→0.828 / 0.8→0.828 / 0.9→0.736
# 0.5-0.8 全档 FP 都是 4 条, 但 R 从 0.891 掉到 0.75 → 取 0.5 纯赚 (+7 召回, FP 不变)
INCLUSION_THRESHOLD = 0.5
# 正样本 sim<0.2 占 3/64 (跨域语义边 loan_channel/created_by/target_spec),
# 负样本占 326/400 → 护栏拦掉 4 条 FP 里的 3 条, 代价 3 条真边降 unknown (可接受)
NAME_SIM_THRESHOLD = 0.20
# 负样本 21/400 撞上, 正样本 0/64 → 纯赚 (target 极小 = 枚举表巧合)
CARDINALITY_MIN_RATIO = 0.001
# spec §4.1 ⑤: 大量 NULL 说明此列不是 FK 锚点 (校准期正样本仅 1 条 null>0.5)
NULL_RATE_MAX = 0.5


@dataclass
class ProbeResult:
    verdict: Literal["pass", "fail", "unknown"]
    checks: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def probe_relation(
    source_entity: str,
    target_entity: str,
    source_col: str,
    target_col: str,
    run_sql: Callable[[str, str], list[dict]],
    get_index_info: Callable[[str, str], dict],
) -> ProbeResult:
    """三层探针 (spec §4.1), 纯函数, 真库 I/O 全注入。

    第一层 (硬门, 不碰数据): 至少一端 PK/UNIQUE (无向 §4.2), 空表 → unknown。
    第二层 (量化, 采样真库): 包含率 ≥ 阈值, NULL 比例 ≤ 0.5。
    第三层 (兜底护栏): 列名 token Jaccard ≥ 0.2, 基数比 ≥ 0.001。

    verdict: 任意 unknown → "unknown"; 硬门/包含率不过 → "fail"; 全过 → "pass"。
    """
    # --- 第零层: 标识符白名单 (2026-10-16 审计) ---
    # 四个标识符都要拼进下面的 f-string SQL, 占位符绑不了标识符 → 拼之前把关。
    bad = _invalid_idents(source_entity, target_entity, source_col, target_col)
    if bad:
        return ProbeResult(
            "unknown",
            [{"name": "ident_whitelist", "ok": False,
              "detail": f"非法标识符 (只允许字母/数字/下划线): {bad}"}],
            {"invalid_idents": bad},
        )

    # get_index_info(table, source): source 参数是数据源路由 (如 "lib"),
    # 探针不感知库归属 → 透传调用方注入的 source 值, 测试按表名查 dict。
    src_info = get_index_info(source_entity, "source")
    tgt_info = get_index_info(target_entity, "source")
    checks: list[dict] = []
    evidence: dict = {}
    # ★ 2026-09-09 修: run_sql 的第二参是数据源名 (生产按它查连接池 _connections),
    # 原来误传 source_entity (表名) → 池里查不到 → 抛异常 → 被 _single_count 吞成 0
    # → inclusion_rate 恒 0.0 → 一切边恒判 fail (提议永远生不出来)。
    # get_index_info 已负责 table→data_source 解析, 复用其结果; 缺省兜底 DB_SOURCES 首源。
    from core.config import default_source  # 延迟导入, 避免本模块对 core.config 产生顶层耦合

    data_source = (src_info or {}).get("data_source") or default_source()

    # --- 第一层: PK/UNIQUE 硬门 (无向, §4.2) + 空表 (§4.1 ②) ---
    gate = _layer1_gate(source_col, target_col, src_info, tgt_info)
    checks.extend(gate["checks"])
    if gate["verdict"]:
        return ProbeResult(gate["verdict"], checks, evidence)
    src_rows = gate["src_rows"]
    tgt_rows = gate["tgt_rows"]

    # --- 第二层: 包含率 (fail 语义) + NULL 比例 (unknown 语义) ---
    inclusion_rate = _count_inclusion(
        run_sql, source_entity, target_entity, source_col, target_col,
        data_source)
    evidence["inclusion_rate"] = inclusion_rate
    if inclusion_rate is None:
        # 查询失败 = 证据缺失 → unknown (不是 fail, 见 _single_count)
        checks.append({"name": "inclusion", "ok": False,
                       "detail": "探针 SQL 执行失败, 包含率不可知"})
        return ProbeResult("unknown", checks, evidence)
    checks.append({
        "name": "inclusion", "ok": inclusion_rate >= INCLUSION_THRESHOLD,
        "detail": f"inclusion_rate={inclusion_rate:.4f} >= {INCLUSION_THRESHOLD}",
    })
    if inclusion_rate < INCLUSION_THRESHOLD:
        return ProbeResult("fail", checks, evidence)

    unknown = False
    null_rate = _count_null_rate(run_sql, source_entity, source_col, src_rows,
                                 data_source)
    evidence["null_rate"] = null_rate
    if null_rate is None:
        checks.append({"name": "null_rate", "ok": False,
                       "detail": "探针 SQL 执行失败, NULL 比例不可知"})
        return ProbeResult("unknown", checks, evidence)
    null_ok = null_rate <= NULL_RATE_MAX
    checks.append({
        "name": "null_rate", "ok": null_ok,
        "detail": f"null_rate={null_rate:.4f} <= {NULL_RATE_MAX}",
    })
    if not null_ok:
        unknown = True

    # --- 第三层: 命名 Jaccard + 基数比 (兜底护栏, unknown 语义) ---
    unknown = _layer3_guards(
        source_col, target_col, src_rows, tgt_rows, checks, evidence) or unknown

    if unknown:
        return ProbeResult("unknown", checks, evidence)
    return ProbeResult("pass", checks, evidence)


def find_orphan_cols(
    entity_id: str,
    entities: dict[str, dict],
    relations: list[dict],
) -> set[str]:
    """孤儿列集合差 (spec §4.6): 一个实体里未被任何边使用的列。

    纯函数, 零查库零 LLM。已知退化: 基于本体 attributes 声明,
    物理表漏登记的列扫不出来 (spec §4.6 退化点, 调用方须知)。
    """
    entity = entities.get(entity_id)
    if not entity:
        return set()
    all_cols = {a["name"] for a in entity.get("attributes") or [] if a.get("name")}
    used = _cols_used_by_edges(entity_id, relations)
    return {c for c in all_cols if c.upper() not in used}


def _cols_used_by_edges(entity_id: str, relations: list[dict]) -> set[str]:
    """该实体作为边一端 (方向无关) 时被用到的列集合 (大写)。"""
    used = set()
    for rel in relations:
        if rel.get("source") == entity_id:
            used.add((rel.get("source_col") or "").upper())
        if rel.get("target") == entity_id:
            used.add((rel.get("target_col") or "").upper())
    return used


# ---------- 内部 helper (拆分保圈复杂度 ≤ 11) ----------

def _norm_key_cols(info: dict) -> set:
    """PK ∪ UNIQUE 列名集合, 大小写归一。

    information_schema 返回大写列名, 而本体 via 存在小写形态
    (lib_loan.lib_loan_id=...) → 敏感比对会把这些真边全挡在硬门外 (2026-09-09 修)。
    """
    return {c.upper() for c in
            (info.get("pk_cols") or []) + (info.get("unique_cols") or [])}


def _layer1_gate(source_col: str, target_col: str,
                 src_info: dict, tgt_info: dict) -> dict:
    """第一层硬门: PK/UNIQUE (无向 §4.2) + 空表 (§4.1 ②)。

    返回 {"verdict": None|"fail"|"unknown", "checks": [...],
          "src_rows": int, "tgt_rows": int}。
    verdict 非 None 时上层直接短路返回。
    """
    checks: list[dict] = []
    pk_ok = ((source_col or "").upper() in _norm_key_cols(src_info)
             or (target_col or "").upper() in _norm_key_cols(tgt_info))
    checks.append({
        "name": "pk_or_unique", "ok": pk_ok,
        "detail": f"src(pk={src_info.get('pk_cols')},uniq={src_info.get('unique_cols')}) "
                  f"tgt(pk={tgt_info.get('pk_cols')},uniq={tgt_info.get('unique_cols')})",
    })
    if not pk_ok:
        return {"verdict": "fail", "checks": checks, "src_rows": 0, "tgt_rows": 0}

    src_rows = int(src_info.get("row_count") or 0)
    tgt_rows = int(tgt_info.get("row_count") or 0)
    empty_ok = src_rows > 0 and tgt_rows > 0
    checks.append({
        "name": "non_empty", "ok": empty_ok,
        "detail": f"src_rows={src_rows}, tgt_rows={tgt_rows}",
    })
    verdict = None if empty_ok else "unknown"
    return {"verdict": verdict, "checks": checks,
            "src_rows": src_rows, "tgt_rows": tgt_rows}


def _layer3_guards(source_col: str, target_col: str,
                   src_rows: int, tgt_rows: int,
                   checks: list[dict], evidence: dict) -> bool:
    """第三层兜底护栏 (spec §4.1 ⑥⑦): 命名 Jaccard + 基数比, 不过 → unknown。

    就地追加两条 check 并写 evidence, 返回是否触发 unknown。
    """
    col_similarity = _token_jaccard(source_col, target_col)
    evidence["col_similarity"] = col_similarity
    sim_ok = col_similarity >= NAME_SIM_THRESHOLD
    checks.append({
        "name": "col_name_similarity", "ok": sim_ok,
        "detail": f"jaccard={col_similarity:.4f} >= {NAME_SIM_THRESHOLD}",
    })

    cardinality_ratio = (tgt_rows / src_rows) if src_rows > 0 else 0.0
    evidence["cardinality_ratio"] = cardinality_ratio
    card_ok = cardinality_ratio >= CARDINALITY_MIN_RATIO
    checks.append({
        "name": "cardinality_ratio", "ok": card_ok,
        "detail": f"ratio={cardinality_ratio:.6f} >= {CARDINALITY_MIN_RATIO}",
    })
    return not sim_ok or not card_ok


def _invalid_idents(*idents: str) -> list[str]:
    """返回不满足白名单的标识符 (保序)。全合法则空列表。

    ★ 为什么探针必须自己把关 (2026-10-16 端到端取证):
    列名是**外部输入**, 一路无校验流到下面的 f-string:
      agent 写的 SQL → extract.py 保留反引号内原文 (含空格/SQL 片段)
      → check.py 的 no_edge hint 原样带上 lcol/rcol → failure_signal 落库
      → _candidate_pairs 的 hint 项 (hint 按设计**不预筛**, 它是行为证据)
      → 这里 f-string 拼 SQL
    实测 `b.\\`PK_ID FROM evil WHERE 1=1 OR 1=1 -- \\`` 能让探针发出子查询被
    改写的 SQL。表名那侧早有 web.app._safe_table_ident 把关, 列名这侧没有 ——
    "上游可信"这个假设不该只存在于一条拼接语句的隐含前提里 (同 _safe_table_ident)。

    ★ 为什么返回 unknown 而不是 fail / 抛异常:
    unknown = "证据不可知", 与 _single_count 查询失败同口径 (查不了 ≠ 查了发现
    不成立)。判 fail 会让这个列对进墓碑、永久挡住同名真列对; 抛异常则会炸穿
    _process_one_no_edge 的整条信号处理。两者都比"这对探不了"的事实更重。
    """
    return [i for i in idents if not _IDENT_RE.fullmatch(i or "")]


def _count_inclusion(run_sql, source_entity, target_entity,
                     source_col, target_col, data_source: str = "") -> float | None:
    """第二层 ④: 包含率 = source.col 值落在 target.col 里的比例。

    data_source 是连接池路由键 ("lib"/"lib2"), 不是表名 — 生产 run_sql
    按它取连接 (2026-09-09 前误传表名, 导致包含率恒 0.0)。

    两条探针 SQL (各一次往返):
      命中: SELECT COUNT(DISTINCT s.col) AS n FROM src s
            WHERE s.col IS NOT NULL AND s.col IN
              (SELECT DISTINCT t.col FROM tgt t WHERE t.col IS NOT NULL)
      总数: SELECT COUNT(DISTINCT s.col) AS n FROM src s WHERE s.col IS NOT NULL
    """
    hit_sql = (
        f"SELECT COUNT(DISTINCT s.{source_col}) AS n FROM {source_entity} s "
        f"WHERE s.{source_col} IS NOT NULL AND s.{source_col} IN "
        f"(SELECT DISTINCT t.{target_col} FROM {target_entity} t "
        f"WHERE t.{target_col} IS NOT NULL)"
    )
    total_sql = (
        f"SELECT COUNT(DISTINCT s.{source_col}) AS n FROM {source_entity} s "
        f"WHERE s.{source_col} IS NOT NULL"
    )
    hits = _single_count(run_sql, hit_sql, data_source)
    total = _single_count(run_sql, total_sql, data_source)
    if hits is None or total is None:
        return None  # 查询失败 → 证据缺失, 上层判 unknown (不是 fail)
    if total == 0:
        return 0.0
    return hits / total


def _count_null_rate(run_sql, source_entity, source_col, src_rows: int,
                     data_source: str = "") -> float | None:
    """第二层 ⑤: source_col 的 NULL 比例 (分母用 get_index_info 的行数)。

    查询失败返回 None (证据缺失), 与包含率同口径。
    """
    null_sql = (f"SELECT COUNT(*) AS n FROM {source_entity} s "
                f"WHERE s.{source_col} IS NULL")
    nulls = _single_count(run_sql, null_sql, data_source)
    if nulls is None:
        return None
    if src_rows == 0:
        return 0.0
    return nulls / src_rows


def _single_count(run_sql, sql: str, source: str) -> int | None:
    """跑一条 COUNT 探针, 返回 n; 查询失败返回 None (证据缺失 → 上层判 unknown)。

    ★ 2026-09-09 修: 原来 `except: return 0` 把"查不了"和"查了是 0"混为一谈,
    既会误拒真外键 (列名/权限问题 → fail), 又静默吞掉了数据源传参 bug
    (探针恒 fail 数日无人发现)。查不了 ≠ 查了发现不成立。
    """
    try:
        rows = run_sql(sql, source)
    except Exception:
        return None
    if not rows:
        return 0
    return int(rows[0].get("n") or 0)


def _token_jaccard(a: str, b: str) -> float:
    """第三层 ⑥: 按下划线拆 token 取 Jaccard (spec §4.3, 不做缩写映射)。

    READER_ID vs LIB_READER_ID → {reader,id} vs {lib,reader,id} → 0.5。
    SEQ_NO vs MEMBER_ID → 交集空 → 0.0。
    """
    ta = {t for t in (a or "").upper().split("_") if t}
    tb = {t for t in (b or "").upper().split("_") if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
