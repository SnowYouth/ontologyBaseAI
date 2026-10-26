import pytest
from core.ontology_view import build_indexes
from capabilities.obqc.check import obqc_check


@pytest.fixture
def onto():
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "LIB_LOAN_ID"}, {"name": "READER_ID"}]},
            {"id": "cust", "tables": ["LIB_READER"], "data_source": "lib",
             "attributes": [{"name": "MEMBER_ID"}, {"name": "MEMBER_NAME"}]},
            {"id": "acct", "tables": ["ACCOUNT"], "data_source": "lib",
             "attributes": [{"name": "LIBCARD_ID"}, {"name": "MEMBER_ID"}]},
        ],
        "relationships": [
            {"source": "lib_loan", "target": "cust",
             "source_col": "READER_ID", "target_col": "MEMBER_ID", "type": "MANY_TO_ONE"},
        ],
        "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
    }
    return build_indexes(raw)


def test_合法join_pass(onto):
    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN LIB_READER c ON p.READER_ID = c.MEMBER_ID")
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "pass"


def test_连接列写反_方向无关仍pass(onto):
    """b.y = a.x 写反, 无序集合比较兜底 (spec §3.4.2 方向无关)。"""
    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN LIB_READER c ON c.MEMBER_ID = p.READER_ID")
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "pass"


def test_无边join_hard_block(onto):
    """lib_loan ↔ acct 之间无 relation 边 → 硬拦。"""
    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN ACCOUNT a ON p.READER_ID = a.MEMBER_ID")
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "hard_block"
    assert any(v["type"] == "no_edge" for v in r["violations"])


def test_边存在但连接列对不上_hard_block(onto):
    """lib_loan↔cust 有边, 但用了错误的连接列 → 硬拦。"""
    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN LIB_READER c ON p.LIB_LOAN_ID = c.MEMBER_NAME")
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "hard_block"
    assert any(v["type"] == "join_col_mismatch" for v in r["violations"])


def test_同名连接列不误配_先定位边再比列(onto):
    """acct 有 MEMBER_ID 列, 但 lib_loan↔acct 无边。即便列名巧合,
    先定位边 (无边) 就该 hard_block, 不能拿列集合全库搜到 lib_loan↔cust 的边误判 pass。"""
    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN ACCOUNT a ON p.READER_ID = a.MEMBER_ID")
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "hard_block"


def test_解析失败_放行不拦(onto):
    r = obqc_check("SELECT FROM )))", "lib", onto)
    assert r["verdict"] == "pass"
    assert "parse_failed" in r["skipped"]


def test_表resolve不到_跳过不拦(onto):
    """SQL 用了本体没登记的表 → 该 JOIN 跳过, 不硬拦 (spec: 本体无知≠证伪)。"""
    sql = ("SELECT x.A FROM UNKNOWN_TABLE x "
           "JOIN LIB_READER c ON x.MEMBER_ID = c.MEMBER_ID")
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "pass"
    assert any("skip" in s for s in r["skipped"])


def test_列归属硬拦_集成(onto, monkeypatch):
    """端到端: SQL 用了幻觉列 → obqc_check hard_block"""
    # Mock list_table_columns 返回真实列
    def mock_list_cols(table, source):
        if table.upper() == "LIB_LOAN":
            return {"LIB_LOAN_ID", "READER_ID"}
        return set()
    monkeypatch.setattr("agent.tools.sql_exec.list_table_columns", mock_list_cols)

    sql = "SELECT PHANTOM_COL FROM LIB_LOAN"
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "hard_block"
    assert any(v["type"] == "column_not_found" for v in r["violations"])


def test_列归属硬拦_带别名_集成(onto, monkeypatch):
    """★ 别名盲区回归: `FROM LIB_LOAN p` 时列写 p.X, 列归属必须照常判。
    修复前 table_or_alias='P' 查 table_to_entity(键是真表名) 落空 → skip_table
    静默放行, 真实 SQL 里有相当比例中招 (本地取证脚本验过, 不随仓分发)。"""
    seen = []

    def mock_list_cols(table, source):
        seen.append(table)
        if table.upper() == "LIB_LOAN":
            return {"LIB_LOAN_ID", "READER_ID"}
        return set()
    monkeypatch.setattr("agent.tools.sql_exec.list_table_columns", mock_list_cols)

    sql = "SELECT p.PHANTOM_COL FROM LIB_LOAN p"
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "hard_block"
    assert any(v["type"] == "column_not_found" for v in r["violations"])
    assert "skip_table" not in r["skipped"]
    # 物理对账必须拿真表名去查, 拿别名查 information_schema 必然空集
    assert seen and all(t.upper() == "LIB_LOAN" for t in seen)


def test_枚举值不匹配_soft_warn_集成(monkeypatch):
    """端到端: WHERE 用了不合法枚举值 → soft_warn"""
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "STATUS", "hasEnum": True}]},
        ],
        "valueSets": {("lib_loan", "STATUS"): {"name": "状态", "codes": [{"code": "ACTIVE"}]}},
        "relationships": [], "domains": [], "filterRules": [], "concepts": [],
    }
    onto = build_indexes(raw)

    sql = "SELECT * FROM LIB_LOAN WHERE STATUS = 'WRONG_STATUS'"
    r = obqc_check(sql, "lib", onto)
    assert r["verdict"] == "soft_warn"
    assert len(r["warnings"]) > 0
