from capabilities.obqc.extract import extract_sql


def test_单表select():
    r = extract_sql("SELECT LIB_LOAN_ID FROM LIB_LOAN", "mysql")
    assert r["parse_ok"] is True
    assert r["tables"] == [{"name": "LIB_LOAN", "alias": None}]
    assert r["joins"] == []


def test_两表等值join_抽出连接列对():
    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN LIB_READER c ON p.READER_ID = c.MEMBER_ID")
    r = extract_sql(sql, "mysql")
    assert r["parse_ok"] is True
    names = {t["name"] for t in r["tables"]}
    assert names == {"LIB_LOAN", "LIB_READER"}
    assert len(r["joins"]) == 1
    j = r["joins"][0]
    assert {j["left"], j["right"]} == {("p", "READER_ID"), ("c", "MEMBER_ID")}


def test_解析失败_整段放行标记():
    r = extract_sql("SELECT FROM WHERE )))", "mysql")
    assert r["parse_ok"] is False
    assert "parse_failed" in r["skipped"]


def test_带库名前缀_标记qualified_不进tables():
    sql = "SELECT * FROM lib2.LIB_READER c JOIN lib.LIB_LOAN p ON c.MEMBER_ID = p.READER_ID"
    r = extract_sql(sql, "mysql")
    assert "qualified_table_name" in r["skipped"]
    # 带库前缀的表不进 tables (无法可信归 source)
    assert r["tables"] == []


def test_表名统一大写():
    r = extract_sql("SELECT * FROM lib_loan", "mysql")
    assert r["tables"][0]["name"] == "LIB_LOAN"


def test_非等值join不抽():
    """a.x > b.y 非等值 → 不抽 join (①JOIN 只认等值单列对)。"""
    r = extract_sql("SELECT * FROM a JOIN b ON a.x > b.y", "mysql")
    assert r["parse_ok"] is True
    assert r["joins"] == []


def test_using_join不抽():
    r = extract_sql("SELECT * FROM a JOIN b USING(x)", "mysql")
    assert r["parse_ok"] is True
    assert r["joins"] == []


def test_隐式逗号join不抽():
    r = extract_sql("SELECT * FROM a, b WHERE a.x = b.y", "mysql")
    assert r["parse_ok"] is True
    assert r["joins"] == []


def test_复合and条件join不抽():
    """ON a.x=b.y AND a.z=b.w 复合 → 整个 on 是 exp.And 非 exp.EQ → 不抽。"""
    r = extract_sql("SELECT * FROM a JOIN b ON a.x = b.y AND a.z = b.w", "mysql")
    assert r["parse_ok"] is True
    assert r["joins"] == []


def test_自关联join_表重名_join元组带别名():
    """self-join: tables 两行同 name, joins 元组 [0] 是别名不是表名 (Task 7 契约)。"""
    sql = ("SELECT * FROM LIB_LOAN a JOIN LIB_LOAN b "
           "ON a.PARENT_ID = b.LIB_LOAN_ID")
    r = extract_sql(sql, "mysql")
    names = [t["name"] for t in r["tables"]]
    assert names == ["LIB_LOAN", "LIB_LOAN"]  # 两行同名
    assert len(r["joins"]) == 1
    j = r["joins"][0]
    # 元组 [0] 是别名 a/b, 不是表名 LIB_LOAN
    assert {j["left"][0], j["right"][0]} == {"a", "b"}


def test_extract_where_literals_等值():
    sql = "SELECT * FROM t WHERE col1 = 'value1' AND col2 = 123"
    r = extract_sql(sql, "mysql")
    assert r["where_literals"] == [
        {"column": "COL1", "op": "=", "value": "value1"},
        {"column": "COL2", "op": "=", "value": 123},
    ]


def test_extract_where_literals_IN():
    sql = "SELECT * FROM t WHERE status IN ('ACTIVE', 'PENDING')"
    r = extract_sql(sql, "mysql")
    lits = r["where_literals"]
    assert len(lits) == 2
    assert {"column": "STATUS", "op": "IN", "value": "ACTIVE"} in lits
    assert {"column": "STATUS", "op": "IN", "value": "PENDING"} in lits


def test_extract_where_literals_跳过非等值():
    """>, <, LIKE, !=, BETWEEN → 空 (spec §3.4 ③ M1 仅对 =/IN 判)"""
    sql = "SELECT * FROM t WHERE col > 10 AND name LIKE '%foo%'"
    r = extract_sql(sql, "mysql")
    assert r["where_literals"] == []


def test_extract_select_columns_带表前缀():
    sql = "SELECT t.col1, t.col2 FROM my_table t"
    r = extract_sql(sql, "mysql")
    assert r["select_columns"] == [
        {"table_or_alias": "T", "column": "COL1"},
        {"table_or_alias": "T", "column": "COL2"},
    ]


def test_extract_select_columns_裸列单表():
    sql = "SELECT col1, col2 FROM my_table"
    r = extract_sql(sql, "mysql")
    # 裸列单表 → table_or_alias 填唯一表名 (归属唯一确定, spec §3.3.1)
    assert r["select_columns"] == [
        {"table_or_alias": "MY_TABLE", "column": "COL1"},
        {"table_or_alias": "MY_TABLE", "column": "COL2"},
    ]


def test_extract_select_columns_裸列多表JOIN_标UNKNOWN():
    """裸列多表 JOIN → 归属不明, 标 UNKNOWN 不校验 (spec §3.3.1 陷阱清单)"""
    sql = "SELECT col1 FROM t1 JOIN t2 ON t1.x=t2.y"
    r = extract_sql(sql, "mysql")
    assert r["select_columns"] == [{"table_or_alias": None, "column": "COL1"}]


def test_extract_select_columns_星号跳过():
    """SELECT * 或 t.* → 不抽 (spec: 展开列集合本体无法还原)"""
    sql = "SELECT * FROM t"
    r = extract_sql(sql, "mysql")
    assert r["select_columns"] == []


def test_extract_select_columns_表限定星号跳过():
    """t.* 被 sqlglot 解析成 this=Star() 的 Column, 同样不抽 (spec: 含 t.*)"""
    sql = "SELECT t.* FROM my_table t"
    r = extract_sql(sql, "mysql")
    assert r["select_columns"] == []
