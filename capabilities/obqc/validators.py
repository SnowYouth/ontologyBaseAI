"""capabilities/obqc/validators.py — ②③④ 三类校验器 (spec §3.4)。

纯函数, 不直接操作缓存/DB, 由 obqc/check 传 get_physical_cols 回调注入依赖。
"""

def validate_column_attribution(select_columns: list, source: str, onto, get_physical_cols,
                                alias_to_table: dict | None = None) -> dict:
    """② 列归属硬校验 (spec §3.4.1): SELECT 列必须在 attributes + 物理表真有。

    Args:
        select_columns: extract_sql 返回的 [{table_or_alias, column}]
        source: 数据源 (用于 resolve entity + 取物理列)
        onto: Ontology 查询态 (build_indexes 结果)
        get_physical_cols: (source, table) -> set[str] 回调, 由 check 注入 (缓存+fallback)
        alias_to_table: {别名大写: 真表名大写}; 不传则只认裸表名

    Returns:
        {violations: [...], warnings: [...], skipped: [...]}
    """
    violations, warnings, skipped = [], [], []
    alias_map = alias_to_table or {}

    # 建 table_name -> entity 映射 (per-source, 复用 index 逻辑)
    from capabilities.obqc.index import build_obqc_index
    idx = build_obqc_index(onto)
    table_to_entity = idx.table_to_entity.get(source, {})

    for col_info in select_columns:
        table_or_alias = col_info["table_or_alias"]
        column = col_info["column"]

        # 裸列多表 (归属不明) → 跳过 (spec §3.3.1)
        if table_or_alias is None:
            skipped.append("skip_column")
            continue

        # 别名还原成真表名: table_to_entity 与物理列查询的键都是真表名,
        # 拿别名查两边都查不到 → 整条校验静默失效 (2026-09-19 修)
        table = alias_map.get(table_or_alias.upper(), table_or_alias.upper())

        # resolve table -> entity
        eid = table_to_entity.get(table)
        if eid is None:
            skipped.append("skip_table")
            continue

        entity = onto.entities.get(eid)
        if not entity:
            skipped.append("skip_table")
            continue

        # 本体 attributes 是否有此列
        attr_names = {a["name"].upper() for a in entity.get("attributes", [])}
        in_ontology = column in attr_names

        # 物理对账: 取表列清单
        try:
            physical_cols = get_physical_cols(source, table)
            if physical_cols is None:
                # 对账取不到 (空/表不存在) → 不硬拦 (spec §3.4.1 I-5)
                skipped.append("reconcile_error")
                continue
            in_physical = column in physical_cols
        except Exception:
            # 对账失败 (连库异常) → 不硬拦 (spec §3.4.1 最后一行)
            skipped.append("reconcile_error")
            continue

        # 三态判定 (spec §3.4.1 表格)
        if in_physical and in_ontology:
            # pass: 列既在物理也在本体
            pass
        elif not in_physical and not in_ontology:
            # hard_block: 列既不在物理也不在本体 = 确定幻觉列/用错表
            violations.append({
                "type": "column_not_found",
                "hit": f"{table_or_alias}.{column}",
                "reason": f"列 {column} 既不在本体 {eid}.attributes 也不在物理表 {table}",
                "evidence": f"physical_cols={sorted(physical_cols)[:5]}..., ontology_attrs={sorted(attr_names)[:5]}...",
            })
        elif in_physical and not in_ontology:
            # soft_warn: 列在物理但本体漏登 → 反哺自演化矿脉
            warnings.append(f"列 {table}.{column} 在物理表但本体未登记 (attr_missing_in_ontology)")
        else:  # in_ontology and not in_physical
            # soft_warn: 本体登了物理没有 (可能 schema 变更或对账口径问题)
            warnings.append(f"列 {table}.{column} 在本体但物理表不存在 (可能 schema 变更)")

    return {"violations": violations, "warnings": warnings, "skipped": skipped}


def validate_enum_values(where_literals: list, source: str, onto, referenced_entities) -> dict:
    """③ 取值枚举软校验 (spec §3.4 ③): WHERE 字面量值 vs enum_value 取值域。

    Args:
        where_literals: extract_sql 返回的 [{column, op, value}] (已过滤 =/IN)
        source: 数据源 (用于 resolve entity)
        onto: Ontology 查询态
        referenced_entities: SQL FROM/JOIN 实际引用的 entity_id 集合 (spec §3.4 ③:
            仅判 SQL 引用到的 entity, 不遍历整个 source 以免喷无关 entity 的枚举)

    Returns:
        {violations: [], warnings: [...], skipped: [...]}
        violations 始终空 (③ 全是软校验, spec §3.4 表格)
    """
    warnings, skipped = [], []

    # 先把 literals 按 column 分组
    col_to_values: dict = {}
    for lit in where_literals:
        col_to_values.setdefault(lit["column"], []).append(lit["value"])

    # 对每列查枚举
    for column, values in col_to_values.items():
        # 只遍历 SQL 引用到的 entity, 看哪个有此列且有枚举
        found_enum = False
        for eid in referenced_entities:
            entity = onto.entities.get(eid)
            if not entity:
                continue
            # 找 attributes 里是否有此列 + hasEnum=True
            for attr in entity.get("attributes", []):
                if attr["name"].upper() == column and attr.get("hasEnum"):
                    found_enum = True
                    # 取 value_sets
                    vs = onto.value_sets.get((eid, attr["name"]))
                    if not vs:
                        # 本体标 hasEnum 但无 valueSets → 跳过 (本体不完整)
                        skipped.append("enum_missing")
                        break
                    legal_codes = {c["code"].upper() for c in vs.get("codes", [])}
                    # 判每个值
                    for val in values:
                        if str(val).upper() not in legal_codes:
                            warnings.append(
                                f"列 {column} 取值 '{val}' 不在枚举 {vs['name']} 合法值 {sorted(legal_codes)[:5]}... 内 (enum_value_out_of_range)"
                            )
                    break
            if found_enum:
                break
        # 列无枚举 → 跳过 (spec: 仅对已登记枚举的列判)

    return {"violations": [], "warnings": warnings, "skipped": skipped}


def validate_filter_rules(where_literals: list, source: str, onto, referenced_entities) -> dict:
    """④ filter_rule 软校验 (spec §3.4 ④): WHERE 有没有带该 entity 的 filter_rule 列。

    Args:
        where_literals: extract_sql 返回的 [{column, op, value}]
        source: 数据源
        onto: Ontology 查询态 (build_indexes 后 filter_rules 已按 entity 索引)
        referenced_entities: SQL FROM/JOIN 实际引用的 entity_id 集合 (spec §3.4 ④:
            仅判 SQL 引用到的 entity 的 filter_rule, 不遍历整个 source 以免喷无关 entity)

    Returns:
        {violations: [], warnings: [...], skipped: [...]}
        violations 始终空 (④ 全是软校验, spec §4 明确: filter_rule 不硬拦)
    """
    warnings = []

    # WHERE 用了哪些列 (去重, 大写归一)
    where_cols = {lit["column"].upper() for lit in where_literals}

    # 只遍历 SQL 引用到的 entity 的 filter_rules
    checked_rules = set()  # 去重: 同 entity 同 col 规则只提示一次
    for eid in referenced_entities:
        for rule in onto.filter_rules.get(eid, []):
            rule_col = rule["col"].upper()
            rule_key = (eid, rule_col)
            if rule_key in checked_rules:
                continue
            checked_rules.add(rule_key)

            # 该列不在 WHERE → soft_warn
            if rule_col not in where_cols:
                warnings.append(
                    f"entity {eid} 有默认过滤规则 {rule_col} {rule['op']} {rule.get('value', 'NULL')}, "
                    f"但 WHERE 未包含此列 (结果可能含不符预期的记录)"
                )

    return {"violations": [], "warnings": warnings, "skipped": []}
