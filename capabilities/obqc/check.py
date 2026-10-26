"""capabilities/obqc/check.py — OBQC 纯函数裁判 (spec §3.4)。

obqc_check(sql, source, onto) → {verdict, violations, hints, warnings, skipped}。
接四类校验: ①JOIN 路径硬校验 (两步定位: 别名→表→per-source entity→定位边→
无序列对比较) + ②列归属硬校验 + ③枚举软校验 + ④filter_rule 软校验。
warnings 去重限流 (spec §3.5 I-4)。verdict 优先级: 有 violations→hard_block,
否则有 warnings→soft_warn, 否则 pass。
本体无知 (表/边/列查不到) 一律降级放行, 绝不硬拦 (spec §2.5 统一铁律)。
"""
from capabilities.obqc.index import build_obqc_index
from capabilities.obqc.extract import extract_sql
from agent.tools.sql_exec import get_source_dialect


def obqc_check(sql: str, source: str, onto) -> dict:
    """审一条 SQL 的 ①JOIN + ②列归属 + ③枚举 + ④filter_rule。

    纯函数: 同一 (sql, source, onto 快照) → 同一 verdict。
    """
    from capabilities.obqc.validators import validate_column_attribution, validate_enum_values, validate_filter_rules
    from capabilities.obqc.cache import get_physical_col_cache
    from agent.tools.sql_exec import list_table_columns

    dialect = get_source_dialect(source)
    ext = extract_sql(sql, dialect)
    skipped = list(ext["skipped"])
    violations: list = []
    warnings: list = []
    hints: list = []

    if not ext["parse_ok"]:
        return {"verdict": "pass", "violations": [], "hints": [], "warnings": [], "skipped": skipped}

    idx = build_obqc_index(onto)
    src_tables = idx.table_to_entity.get(source, {})

    # SQL FROM/JOIN 实际引用的 entity 集合 (spec §3.4 ③/④: 只判引用到的 entity,
    # 不遍历整个 source, 否则真库上会把无关 entity 的枚举/filter_rule 全喷成 warning)
    referenced_entities = set()
    for t in ext["tables"]:
        eid = src_tables.get(t["name"].upper())
        if eid:
            referenced_entities.add(eid)

    # ①JOIN 路径硬校验 (委托 helper, 与 ②③④ 一致的三返回值累加)
    join_violations, join_hints, join_skipped, used_edges = _check_joins(ext, idx, src_tables)
    violations.extend(join_violations)
    hints.extend(join_hints)
    skipped.extend(join_skipped)

    # ② 列归属硬校验 (注入物理列查询回调: cache + fallback)
    def get_physical_cols_cached(src, tbl):
        cache = get_physical_col_cache()
        if cache:
            cached = cache.get(src, tbl)
            if cached is not None:
                return cached
        # cache miss → 打库 + 回填
        try:
            cols = list_table_columns(tbl, src)
            # 空结果 = 对账失败哨兵 (下面转 None), 故意不入缓存: 缓存 None 会污染
            # 三态判定 (无法区分"没查过"与"查了对不上"), 只回填非空列集。
            if cache and cols:
                cache.set(src, tbl, cols)
            return cols if cols else None  # 空集合 → None (三态: 对账失败)
        except Exception:
            return None

    col_result = validate_column_attribution(ext["select_columns"], source, onto,
                                             get_physical_cols_cached,
                                             alias_to_table=_build_alias_map(ext))
    violations.extend(col_result["violations"])
    warnings.extend(col_result["warnings"])
    skipped.extend(col_result["skipped"])

    # ③ 枚举值软校验
    enum_result = validate_enum_values(ext["where_literals"], source, onto, referenced_entities)
    warnings.extend(enum_result["warnings"])
    skipped.extend(enum_result["skipped"])

    # ④ filter_rule 软校验
    fr_result = validate_filter_rules(ext["where_literals"], source, onto, referenced_entities)
    warnings.extend(fr_result["warnings"])

    # 去重/聚合/限流 warnings (spec §3.5 I-4)
    warnings = _dedup_and_limit_warnings(warnings)

    verdict = "hard_block" if violations else ("soft_warn" if warnings else "pass")
    result = {"verdict": verdict, "violations": violations, "hints": hints,
              "warnings": warnings, "skipped": skipped}
    # ★ 自演化: used_edges 透传 (spec §3.2, 纯函数只在返回值加字段)
    if used_edges:
        result["used_edges"] = used_edges
    return result


def _build_alias_map(ext) -> dict:
    """{别名或裸表名(大写): 真表名(大写)}。①JOIN 与 ②列归属共用同一份映射。"""
    return {(t["alias"] or t["name"]).upper(): t["name"].upper() for t in ext["tables"]}


def _check_joins(ext, idx, src_tables) -> tuple[list, list, list, list]:
    """①JOIN 路径硬校验 (两步定位: 先按 entity 对定位边, 再无序列对集合比较)。

    返回 (violations, hints, skipped, used_edges)。
    used_edges: 命中的边列表 [{source, target, via, source_col, target_col}, ...]。
    """
    violations: list = []
    hints: list = []
    skipped: list = []
    used_edges: list = []

    # 别名(或裸表名) → 表名 → entity。SQL 里 JOIN 用别名引列, 先建别名→表映射。
    alias_to_table = _build_alias_map(ext)

    def resolve(alias):
        """别名 → entity_id; 解析不到返回 None (UNKNOWN, 降级不拦)。"""
        table = alias_to_table.get((alias or "").upper())
        if table is None:
            return None
        return src_tables.get(table)

    for j in ext["joins"]:
        (la, lcol), (ra, rcol) = j["left"], j["right"]
        eA, eB = resolve(la), resolve(ra)
        if eA is None or eB is None:
            # 表在当前 source resolve 不到 → 跳过 (UNKNOWN / 跨库, spec §3.2)
            skipped.append("skip_table")
            continue
        v, h, edges = _check_one_join(eA, eB, la, lcol, ra, rcol, idx)
        violations.extend(v)
        hints.extend(h)
        used_edges.extend(edges)

    return violations, hints, skipped, used_edges


def _check_one_join(eA, eB, la, lcol, ra, rcol, idx) -> tuple[list, list, list]:
    """单条 JOIN 的两步定位 (先按 entity 对定位边, 再无序列对集合比较)。

    返回 (violations, hints, used_edges)。
    """
    violations: list = []
    hints: list = []
    used_edges: list = []
    # 第 1 步: 定位边 (先按 entity 对, 不拿列集合全库搜, spec §3.4.2 堵同名列误配)
    key = frozenset({eA, eB})
    cand = idx.edges.get(key)
    if not cand:
        violations.append({
            "type": "no_edge", "hit": f"{eA} JOIN {eB}",
            "reason": f"本体 relation 里 {eA} 与 {eB} 之间无边",
            "evidence": "relation adjacency (per-source index)",
        })
        # ★ no_edge hint 补列对 (spec §3.5 R-7): lcol/rcol 已在参数列表
        hints.append({"kind": "no_edge", "entities": [eA, eB],
                      "cols": [lcol, rcol]})
        return violations, hints, used_edges
    # 第 2 步: 该边上无序列对集合比较 (方向无关, spec §3.4.2)
    sql_pair = frozenset({(lcol or "").upper(), (rcol or "").upper()})
    matched_edge = None
    for e in cand:
        if frozenset({(e["source_col"] or "").upper(), (e["target_col"] or "").upper()}) == sql_pair:
            matched_edge = e
            break
    if matched_edge is None:
        legal = [{"source_col": e["source_col"], "target_col": e["target_col"]} for e in cand]
        violations.append({
            "type": "join_col_mismatch", "hit": f"{la}.{lcol} = {ra}.{rcol}",
            "reason": f"{eA}↔{eB} 有边但连接列对不上",
            "evidence": f"legal cols: {legal}",
            # ★ 自演化 (spec §5.3): mismatch 累计需要 entity 对;
            # 只加字段, 不碰判定逻辑 (violation 判定 = 该 dict 存在与否, 与字段无关)
            "entities": [eA, eB],
            # ★ 自演化 (2026-09-18): 淘汰归因要指认"现有的那条真边"。
            # SQL 写的列对按 mismatch 定义必然不等于本体里任何一条边的列对 →
            # 由它派生的 via 在 relation 表查无此行 (会淘汰一条虚构的边)。
            # 真正的嫌疑人是这些候选边 (把 LLM 带偏的那条), 结构化带出,
            # 免得下游 parse evidence 字符串。
            "legal_edges": [{"source": e.get("source"), "target": e.get("target"),
                             "via": e.get("via"),
                             "source_col": e.get("source_col"),
                             "target_col": e.get("target_col")} for e in cand],
        })
        hints.append({"kind": "join_col", "entities": [eA, eB], "legal": legal})
    else:
        # ★ 自演化: 收集命中的边 (spec §3.2)
        used_edges.append({
            "source": matched_edge.get("source"),
            "target": matched_edge.get("target"),
            "via": matched_edge.get("via"),
            "source_col": matched_edge.get("source_col"),
            "target_col": matched_edge.get("target_col"),
        })
    return violations, hints, used_edges


def _dedup_and_limit_warnings(warnings: list, limit: int = 5) -> list:
    """去重 + 限流 warnings (spec §3.5 I-4): 按内容去重, 超 limit 折叠。"""
    unique = list(dict.fromkeys(warnings))  # 保序去重
    if len(unique) <= limit:
        return unique
    return unique[:limit] + [f"(另有 {len(unique) - limit} 条提示被折叠)"]
