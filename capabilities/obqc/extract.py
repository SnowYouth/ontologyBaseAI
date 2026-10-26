"""capabilities/obqc/extract.py — sqlglot 抽取层 (spec §3.3)。

只抽"我确定的": tables + 显式等值 JOIN 的连接列对。任何拿不准 (解析失败/带库前缀/
非等值 JOIN) 一律降级放行, 绝不向硬拦升级 (spec §3.3 铁律: 宁可少报绝不错报)。
用 sqlglot AST, dialect 从调用方传 (方言无关是选它的理由)。
"""
import sqlglot
from sqlglot import exp


def _literal_value(node: exp.Literal):
    """取 sqlglot Literal 的真值。数字字面量 (is_string=False) 转 int/float,
    字符串字面量原样返回 str (sqlglot 的 Literal.this 永远是 str, 数字需自行转型)。"""
    if node.is_string:
        return node.this
    try:
        return int(node.this)
    except ValueError:
        return float(node.this)


def extract_sql(sql: str, dialect: str) -> dict:
    """抽取 tables/joins; 返回 {tables, joins, skipped, parse_ok}。"""
    skipped: list = []
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return {"tables": [], "joins": [], "where_literals": [],
                "select_columns": [],
                "skipped": ["parse_failed"], "parse_ok": False}

    # 表: 带库名限定符 (table.catalog/db) → 标记并整个 tables 清空 (spec §3.2 护栏:
    # SQL 带库名与"库归属靠参数"分层冲突, OBQC 无法可信归 source, 强制放弃)
    tables = []
    qualified = False
    # 注: find_all 会连子查询/派生表里的表名一并列出 (如 FROM (SELECT..FROM X) → X 也进 tables)。
    # walking skeleton 容忍此过度列举 (JOIN 对不会引用子查询别名作真表, Task 7 无害); 计划二再收窄。
    for t in tree.find_all(exp.Table):
        if t.args.get("catalog") or t.args.get("db"):
            qualified = True
        tables.append({"name": t.name.upper(),
                       "alias": t.alias_or_name if t.alias else None})
    if qualified:
        skipped.append("qualified_table_name")
        tables = []

    # JOIN: 只认显式等值单列对 a.x = b.y (两侧都是 Column)
    joins = []
    for j in tree.find_all(exp.Join):
        on = j.args.get("on")
        if not isinstance(on, exp.EQ):
            continue  # 非等值 / USING / 复合 → 本计划不抽 (计划二再扩)
        left, right = on.this, on.expression
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        joins.append({
            "left": (left.table or None, left.name.upper()),
            "right": (right.table or None, right.name.upper()),
        })

    # WHERE 字面量 (spec §3.4 ③: 只取 =/IN 的字面量值, 其它 op 跳过)
    where_literals = []
    for where in tree.find_all(exp.Where):
        for pred in where.find_all((exp.EQ, exp.In)):
            if isinstance(pred, exp.EQ):
                col_node, val_node = pred.this, pred.expression
                if isinstance(col_node, exp.Column) and isinstance(val_node, exp.Literal):
                    where_literals.append({
                        "column": col_node.name.upper(),
                        "op": "=",
                        "value": _literal_value(val_node),  # 数字转型, 字符串原样
                    })
            elif isinstance(pred, exp.In):
                col_node = pred.this
                if not isinstance(col_node, exp.Column):
                    continue
                # IN 的右侧是 Tuple, 展开每个字面量
                for item in pred.expressions:
                    if isinstance(item, exp.Literal):
                        where_literals.append({
                            "column": col_node.name.upper(),
                            "op": "IN",
                            "value": _literal_value(item),
                        })

    # SELECT 列 (spec §3.4.1 ②: 用于列归属校验)
    select_columns = []
    select_node = tree.find(exp.Select)
    if select_node:
        # 检测 * (含 t.*: sqlglot 把 t.* 解析成 this=Star() 的 Column)
        has_star = any(
            isinstance(e, exp.Star)
            or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
            for e in select_node.expressions
        )
        if not has_star:
            for e in select_node.expressions:
                if isinstance(e, exp.Column):
                    table_or_alias = (e.table.upper() if e.table else None)
                    # 裸列单表 → 归给唯一表 (spec §3.3.1: 归属唯一确定)
                    if table_or_alias is None and len(tables) == 1:
                        table_or_alias = tables[0]["name"]
                    select_columns.append({
                        "table_or_alias": table_or_alias,
                        "column": e.name.upper(),
                    })

    return {"tables": tables, "joins": joins, "where_literals": where_literals,
            "select_columns": select_columns, "skipped": skipped, "parse_ok": True}
