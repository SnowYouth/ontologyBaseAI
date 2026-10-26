"""capabilities/ontology/validate_filter_rules.py — filter_rule 三类校验 (spec §4.3)。

纯逻辑, 真库 I/O 全靠注入隔离 (单测不连真库):
- table_cols: 该实体物理表的列名集合 (调用方从真库 DESCRIBE 得到)
- run_sql:    跑一条 SQL 的回调, 正常返回 None, 跑不通抛异常

本轮只交付函数 + mock 单测; 接进验证回路 B 打分页由 Plan B 承接。
"""
from __future__ import annotations

import re

# 标识符白名单: 与 web.app._safe_table_ident / probe._IDENT_RE 同口径。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# op 白名单: 与 web.app.FilterRuleIn 的 Literal 逐字一致。
# 为什么校验器自己也要有一份: FilterRuleIn 只管 HTTP 入口, 而本模块是公开函数
# (selfevolve / 脚本 / 测试都能直调), "调用方会帮我挡住"不该是拼接语句的隐含前提。
_ALLOWED_OPS = frozenset({"=", "!=", ">", "<", ">=", "<=", "IN", "IS NULL", "IS NOT NULL"})

_VALUELESS_OPS = frozenset({"IS NULL", "IS NOT NULL"})


def _values(rule: dict) -> list[str]:
    """把 rule 的 value 拆成值列表: op='IN' 按逗号分隔, 否则单值。

    None 与空串都视为无值(对齐 concept 范本: value 焊 NOT NULL DEFAULT '' 后,
    无值 op 的 value 是 '' 而非 None)。
    """
    v = rule.get("value")
    if v is None or v == "":
        return []
    if rule.get("op", "").upper() == "IN":
        return [x.strip() for x in v.split(",") if x.strip()]
    return [v]


def _quote(value: str) -> str:
    """把一个值包成 SQL 字符串字面量, 转义反斜杠与单引号。

    ★ 两个替换都必须做, 少一个就有洞 (顺序无所谓 —— 两者作用在互不相交的字符
    上, `\\`→`\\\\` 不产引号, `'`→`''` 不产反斜杠, 可交换)。
    真库实测 (某 MySQL 源, 2026-10-20), **只翻引号不转反斜杠**仍然逃逸:
        value = "x\\' OR 1=1 -- "  → `TAG_CODE = 'x\\'' OR 1=1 -- '`
        → 反斜杠转义掉我们补的那个引号, 字面量提前闭合, `OR 1=1` 成了 SQL
          结构, 注入的 `--` 把尾部 LIMIT 1 一起注释掉
        → 一条 `LIMIT 1` 的探测查询回了整张表。
    另: 值以单个反斜杠结尾时 (`"x\\"`), 只翻引号会直接 1064 语法错。
    该库 sql_mode 不含 NO_BACKSLASH_ESCAPES (MySQL 默认), 所以反斜杠是转义符。

    占位符是更好的答案, 但这里绑不了 —— col 是标识符 (占位符只能绑值), 而
    run_sql 回调的签名只收一条完整 SQL 字符串。拼接是既有合同, 本函数只负责
    让拼出来的东西是安全的。
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _safe_ident(ident: str, kind: str) -> str:
    """标识符白名单校验, 非法抛 ValueError (拼之前把关, 不等 SQL 引擎报错)。"""
    if not _IDENT_RE.fullmatch(ident or ""):
        raise ValueError(f"非法{kind}标识符 (只允许字母/数字/下划线): {ident!r}")
    return ident


def _build_where_sql(rule: dict, table: str) -> str:
    """构造一条只探测语法/列的 SELECT (LIMIT 1, 不取数)。

    三个拼接位各有闸: table/col 过标识符白名单, op 过 _ALLOWED_OPS, value 过
    _quote 转义。非法标识符/op 抛 ValueError, 由 _check_sql_runs 收成红灯。
    """
    op = rule.get("op", "").upper()
    if op not in _ALLOWED_OPS:
        raise ValueError(f"非法 op (不在白名单 {sorted(_ALLOWED_OPS)} 内): {rule.get('op')!r}")
    col = _safe_ident(rule["col"], "列名")
    tbl = _safe_ident(table, "表名")
    vals = _values(rule)
    if op == "IN":
        cond = f"{col} IN ({', '.join(_quote(v) for v in vals)})"
    elif op in _VALUELESS_OPS:
        cond = f"{col} {op}"
    else:
        cond = f"{col} {op} {_quote(vals[0])}" if vals else f"{col} {op}"
    return f"SELECT 1 FROM {tbl} WHERE {cond} LIMIT 1"


def _check_column_exists(rule: dict, table_cols: set) -> dict:
    ok = rule["col"] in table_cols
    return {"name": "column_exists", "ok": ok,
            "detail": "" if ok else f"列 {rule['col']} 不在物理表列集合内"}


def _check_value_in_enum(rule: dict, onto) -> dict:
    ev = onto.value_sets.get((rule["entity"], rule["col"]))
    if ev is None:
        return {"name": "value_in_enum", "ok": True, "skipped": True,
                "detail": "该字段无枚举登记, 跳过值校验"}
    codes = {c["code"] for c in ev.get("codes", [])}
    bad = [v for v in _values(rule) if v not in codes]
    ok = not bad
    return {"name": "value_in_enum", "ok": ok,
            "detail": "" if ok else f"值 {bad} 不在枚举码值 {sorted(codes)} 内"}


def _check_sql_runs(rule: dict, table: str, run_sql) -> dict:
    """拼 SQL + 真库跑一次。拼不出来 (非法标识符/op) 也收成红灯, 不外抛。

    ★ 组装放进 try: 一条坏规则不许拖垮整轮校验 —— 端点是"一键校验全部
    filter_rule", 抛出去会让其余规则一条都验不了 (同 probe 的 unknown 取舍)。
    """
    try:
        sql = _build_where_sql(rule, table)
        run_sql(sql)
        return {"name": "sql_runs", "ok": True, "detail": ""}
    except Exception as e:
        return {"name": "sql_runs", "ok": False, "detail": f"{type(e).__name__}: {e}"}


def validate_filter_rule(rule: dict, onto, table_cols: set, run_sql) -> dict:
    """校验单条 filter_rule, 返回 {entity, col, checks:[...], ok}。

    三类检查 (spec §4.3): 列存在性 / 值在枚举内 / WHERE 真库跑通。
    ok = 三项全 ok (skipped 视为 ok)。校验失败不抛 —— 标记式 (对齐"响亮失败但不
    让一条坏规则拖垮整个本体加载")。
    """
    table = (onto.entities.get(rule["entity"], {}).get("tables") or [None])[0]
    checks = [
        _check_column_exists(rule, table_cols),
        _check_value_in_enum(rule, onto),
        _check_sql_runs(rule, table, run_sql),
    ]
    return {"entity": rule["entity"], "col": rule["col"],
            "checks": checks, "ok": all(c["ok"] for c in checks)}
