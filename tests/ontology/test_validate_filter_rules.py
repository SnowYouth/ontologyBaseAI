"""filter_rule 校验函数单测 (TDD)。

校验器三类 (spec §4.3): 列存在性 / 值合法性(枚举内) / SQL 合法性(真库跑通)。
真库 I/O 由 table_cols 集合 + run_sql 回调注入, 单测全 mock 不连真库。
"""
from core.ontology_view import build_indexes
from capabilities.ontology.validate_filter_rules import validate_filter_rule


def _onto_with_enum():
    """entity_a 有 REC_STATUS 枚举 {000,001,002}。"""
    raw = {
        "entities": [{"id": "entity_a", "displayName": "A", "maps_to": "t",
                      "tables": ["t_a"], "attributes": [], "summary": ""}],
        "relationships": [],
        "valueSets": {("entity_a", "REC_STATUS"): {
            "name": "有效性", "spec_ref": None,
            "codes": [{"code": "000", "name": "在途"}, {"code": "001", "name": "生效"},
                      {"code": "002", "name": "注销"}]}},
        "domains": [], "filterRules": [],
    }
    return build_indexes(raw)


def _ok_run_sql(sql):
    return None   # 恒通过


def _boom_run_sql(sql):
    raise Exception("Unknown column 'NOPE' in 'where clause'")


def test_全通过_列存在_值在枚举_SQL跑通():
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"}
    res = validate_filter_rule(rule, onto, table_cols={"REC_STATUS", "OTHER"},
                               run_sql=_ok_run_sql)
    assert res["ok"] is True
    assert all(c["ok"] for c in res["checks"])


def test_列不存在_列存在性检查失败():
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "NOPE", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"}
    res = validate_filter_rule(rule, onto, table_cols={"REC_STATUS"},
                               run_sql=_ok_run_sql)
    assert res["ok"] is False
    col_check = next(c for c in res["checks"] if c["name"] == "column_exists")
    assert col_check["ok"] is False


def test_值不在枚举_值合法性检查失败():
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "REC_STATUS", "op": "=", "value": "999",
            "mode": "soft", "origin": "source_code"}   # 999 不在 {000,001,002}
    res = validate_filter_rule(rule, onto, table_cols={"REC_STATUS"},
                               run_sql=_ok_run_sql)
    assert res["ok"] is False
    val_check = next(c for c in res["checks"] if c["name"] == "value_in_enum")
    assert val_check["ok"] is False


def test_IN多值全在枚举通过():
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "REC_STATUS", "op": "IN", "value": "000,001",
            "mode": "soft", "origin": "source_code"}
    res = validate_filter_rule(rule, onto, table_cols={"REC_STATUS"},
                               run_sql=_ok_run_sql)
    val_check = next(c for c in res["checks"] if c["name"] == "value_in_enum")
    assert val_check["ok"] is True


def test_字段无枚举登记_值合法性跳过标skipped():
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "OTHER", "op": "=", "value": "x",
            "mode": "soft", "origin": "source_code"}   # OTHER 无枚举
    res = validate_filter_rule(rule, onto, table_cols={"OTHER"},
                               run_sql=_ok_run_sql)
    val_check = next(c for c in res["checks"] if c["name"] == "value_in_enum")
    assert val_check["ok"] is True          # 无枚举不算错
    assert val_check.get("skipped") is True


def test_SQL跑不通_SQL合法性检查失败():
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"}
    res = validate_filter_rule(rule, onto, table_cols={"REC_STATUS"},
                               run_sql=_boom_run_sql)
    assert res["ok"] is False
    sql_check = next(c for c in res["checks"] if c["name"] == "sql_runs")
    assert sql_check["ok"] is False
    assert "Unknown column" in sql_check["detail"]


def test_values_空串视为无值():
    from capabilities.ontology.validate_filter_rules import _values
    assert _values({"op": "IS NULL", "value": ""}) == []
    assert _values({"op": "=", "value": ""}) == []
    assert _values({"op": "=", "value": "001"}) == ["001"]
    assert _values({"op": "IN", "value": "000,001"}) == ["000", "001"]


# ============ 标识符/字面量闸 (2026-10-20, 与 probe.py 同构) ============
#
# _build_where_sql 把 col / op / value 三者直接拼进 f-string。三条真实写入路径:
#   POST /filter-rules (FilterRuleIn: entity/col/value 是裸 str, 只有 op 过
#   Literal 白名单) → upsert_filter_rule (只校验 mode/origin) → 落库
#   → POST /filter-rules/validate 把它拼进 SQL 发往真库。
# 所以 col 和 value 一路无校验。op 这层目前靠 pydantic Literal 挡着, 但校验器
# 是独立模块 (selfevolve / 脚本 / 测试都能直调), 不该依赖"调用方帮我挡住"。
#
# ★ 真库实测 (某 MySQL 源, 2026-10-20): value="x\\' OR 1=1 -- " 在**只翻倍引号**时
# 仍然逃逸 —— 该库 sql_mode 不含 NO_BACKSLASH_ESCAPES, 反斜杠吃掉我们补的那个
# 引号, 注入的 `--` 把 LIMIT 1 一起注释掉, 一条 `LIMIT 1` 的探测查询回了整张表
# 的全部行。所以**必须先转反斜杠再转引号**, 顺序反了等于没转。


def _sql_for(rule, table="t_a"):
    from capabilities.ontology.validate_filter_rules import _build_where_sql
    return _build_where_sql(rule, table)


def _strip_literals(sql: str) -> str:
    """按 MySQL 默认 sql_mode 的口径吃掉所有字符串字面量, 返回 SQL 骨架。

    ★ 判据为什么必须长这样: 朴素的"数引号/查子串"判不了注入 —— 转义正确时
    `'x\\'' OR 1=1 -- '` 整体是**一个**字面量 (内容 = x\\' OR 1=1 -- ),
    `OR` 和 `--` 都在字面量**里面**, 完全无害; 而转义错误时同样的字符会落在
    字面量**外面**成为 SQL 结构。两种情况字符构成几乎一样, 只有按解析器的
    口径走一遍才分得开。我第一版断言就是栽在这: 判据的抽象层级低于被判对象,
    于是把正确的实现判成了错的。

    规则 (与真库实测一致, sql_mode 不含 NO_BACKSLASH_ESCAPES):
    - 字面量内 `\\X` = 转义, 吃掉两个字符 (所以 `\\'` 不闭合字面量)
    - 字面量内 `''` = 一个引号字符, 不闭合
    - 其余 `'` 闭合
    返回值里字面量被换成 <S>, 便于断言骨架形状。
    字面量没闭合 → 返回 "<UNCLOSED>" (是一种判定结果, 不是 judge 自己坏了;
    零转义的旧实现正好落这一档)。
    """
    out, i, n, in_str = [], 0, len(sql), False
    while i < n:
        ch = sql[i]
        if not in_str:
            if ch == "'":
                in_str = True
                out.append("<S>")
            else:
                out.append(ch)
            i += 1
            continue
        # 字面量内部
        if ch == "\\" and i + 1 < n:
            i += 2                      # 转义序列, 整个吃掉
        elif ch == "'" and i + 1 < n and sql[i + 1] == "'":
            i += 2                      # '' = 一个引号字符
        elif ch == "'":
            in_str = False              # 真正闭合
            i += 1
        else:
            i += 1
    if in_str:
        return "<UNCLOSED>"
    return "".join(out)


_EXPECTED_SKELETON = "SELECT 1 FROM t_a WHERE REC_STATUS = <S> LIMIT 1"


def test_value_里的单引号被转义_骨架不变():
    """值里带 ' 时, SQL 骨架必须与普通值完全一致 (只是字面量内容不同)。"""
    sql = _sql_for({"col": "REC_STATUS", "op": "=", "value": "x' OR 1=1 -- "})
    assert _strip_literals(sql) == _EXPECTED_SKELETON, f"注入进了结构: {sql}"


def test_value_尾部反斜杠必须先转义_否则吃掉闭引号():
    """★ 真库逮住的那条 (只翻引号不转反斜杠 → 反斜杠吃掉闭引号 → 全表泄漏)。

    实测: 这个 payload 在旧实现下让一条 LIMIT 1 的探测查询回了整张表的全部行,
    因为注入的 `--` 把 LIMIT 一起注释掉了。
    """
    sql = _sql_for({"col": "REC_STATUS", "op": "=", "value": "x\\' OR 1=1 -- "})
    assert _strip_literals(sql) == _EXPECTED_SKELETON, f"注入进了结构: {sql}"


def test_value_纯尾部反斜杠不产生语法错():
    """值以单个反斜杠结尾 → 旧实现直接 1064 语法错 (真库实测)。"""
    sql = _sql_for({"col": "REC_STATUS", "op": "=", "value": "x\\"})
    assert _strip_literals(sql) == _EXPECTED_SKELETON, f"字面量坏了: {sql}"


def test_IN_多值逐个转义():
    sql = _sql_for({"col": "REC_STATUS", "op": "IN", "value": "a,b' OR 1=1 -- "})
    assert _strip_literals(sql) == "SELECT 1 FROM t_a WHERE REC_STATUS IN (<S>, <S>) LIMIT 1", \
        f"注入进了结构: {sql}"


def test_strip_literals_判据自身能逮住真注入():
    """★ 判据的反证: 拿**旧实现**的输出喂给它, 必须判红。

    否则这个 judge 可能是"恒绿"的, 那上面三条测试就都是装饰。
    旧实现 = 零转义直接拼 (git show 0201a1c 之前的 _build_where_sql)。
    """
    naive = "SELECT 1 FROM t_a WHERE REC_STATUS = 'x' OR 1=1 -- ' LIMIT 1"
    assert _strip_literals(naive) != _EXPECTED_SKELETON
    # 只翻倍引号、不转反斜杠的那版 (真库回了全表的那个形态)
    half = "SELECT 1 FROM t_a WHERE REC_STATUS = 'x\\'' OR 1=1 -- ' LIMIT 1"
    assert _strip_literals(half) != _EXPECTED_SKELETON


def test_非法列名直接抛_不拼进SQL():
    """col 不是合法标识符 → 拼之前就拒, 不等 SQL 引擎报错 (同 probe._invalid_idents)。"""
    import pytest
    with pytest.raises(ValueError, match="标识符"):
        _sql_for({"col": "1=1 OR REC_STATUS", "op": "=", "value": "001"})


def test_非法表名直接抛():
    import pytest
    with pytest.raises(ValueError, match="标识符"):
        _sql_for({"col": "REC_STATUS", "op": "=", "value": "001"}, table="t_a; DROP TABLE x")


def test_非法op直接抛_不信任调用方的白名单():
    """op 当前由 pydantic Literal 挡, 但校验器不该依赖调用方 (selfevolve/脚本可直调)。"""
    import pytest
    with pytest.raises(ValueError, match="op"):
        _sql_for({"col": "REC_STATUS", "op": "= '' OR 1=1 -- ", "value": "001"})


def test_合法规则的SQL形状不变_回归():
    """闸不许改变正常规则生成的 SQL (老行为是硬合同)。"""
    assert _sql_for({"col": "REC_STATUS", "op": "=", "value": "001"}) == \
        "SELECT 1 FROM t_a WHERE REC_STATUS = '001' LIMIT 1"
    assert _sql_for({"col": "REC_STATUS", "op": "IN", "value": "000,001"}) == \
        "SELECT 1 FROM t_a WHERE REC_STATUS IN ('000', '001') LIMIT 1"
    assert _sql_for({"col": "REC_STATUS", "op": "IS NULL", "value": ""}) == \
        "SELECT 1 FROM t_a WHERE REC_STATUS IS NULL LIMIT 1"


def test_校验器把非法标识符报成红灯_不炸穿调用方():
    """端到端: validate_filter_rule 遇非法 col → sql_runs 红灯, 不抛。

    理由同 probe: 一条坏规则不许拖垮整轮校验 (端点是"一键校验全部规则")。
    """
    onto = _onto_with_enum()
    rule = {"entity": "entity_a", "col": "1=1 OR X", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"}
    res = validate_filter_rule(rule, onto, table_cols={"REC_STATUS"}, run_sql=_ok_run_sql)
    assert res["ok"] is False
    sql_check = next(c for c in res["checks"] if c["name"] == "sql_runs")
    assert sql_check["ok"] is False
    assert "标识符" in sql_check["detail"]
