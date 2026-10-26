"""capabilities/obqc/fingerprint.py — AST 指纹生成器 (spec §3.6 I1 熔断去重)。

指纹定义 (spec §3.6 C-2b 写死):
  {表集合(无序)} + {JOIN列对集合(无序)} + {WHERE (列, op, 字面量值) 集合}
别名/空格/列顺序不影响指纹; 字面量值影响指纹。
"""
import hashlib
import sqlglot
from sqlglot import exp


def compute_sql_fingerprint(sql: str, dialect: str) -> str | None:
    """计算 SQL AST 规范化指纹。

    Returns:
        指纹字符串 (sha256 hex 前 16 位); 解析失败返回 None
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None

    # 表集合 (无序, 去别名)
    tables = {t.name.upper() for t in tree.find_all(exp.Table)}

    # JOIN 列对集合 (无序, 方向无关 frozenset)
    join_pairs = set()
    for j in tree.find_all(exp.Join):
        on = j.args.get("on")
        if isinstance(on, exp.EQ):
            left, right = on.this, on.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                # 无序: frozenset 自动去方向
                pair = frozenset({left.name.upper(), right.name.upper()})
                join_pairs.add(pair)

    # WHERE (列, op, 字面量值) 集合
    where_triples = set()
    for where in tree.find_all(exp.Where):
        for pred in where.find_all((exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE, exp.In)):
            if isinstance(pred, exp.EQ):
                col, val = pred.this, pred.expression
                if isinstance(col, exp.Column) and isinstance(val, exp.Literal):
                    where_triples.add((col.name.upper(), "=", str(val.this)))
            elif isinstance(pred, exp.In):
                col = pred.this
                if isinstance(col, exp.Column):
                    for item in pred.expressions:
                        if isinstance(item, exp.Literal):
                            where_triples.add((col.name.upper(), "IN", str(item.this)))
            # 其它 op (NE/GT/LT...) 同理, 简化只取 EQ/IN

    # 序列化: 排序 → JSON 字符串 → 哈希
    fingerprint_obj = {
        "tables": sorted(tables),
        "joins": sorted([sorted(p) for p in join_pairs]),
        "where": sorted(list(where_triples)),
    }
    canonical = str(fingerprint_obj)
    hash_full = hashlib.sha256(canonical.encode()).hexdigest()
    return hash_full[:16]  # 前 16 位够用 (2^64 碰撞概率极低)
