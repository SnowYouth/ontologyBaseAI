import pytest
from core.ontology_view import build_indexes
from capabilities.obqc.validators import validate_column_attribution, validate_enum_values, validate_filter_rules

@pytest.fixture
def onto():
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "LIB_LOAN_ID"}, {"name": "READER_ID"}]},
        ],
        "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
        "relationships": [],
    }
    return build_indexes(raw)

def test_列在attributes且在物理表_pass(onto, monkeypatch):
    """列既在本体 attributes 也在物理表 → pass (spec §3.4.1 三态表首行)"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID", "READER_ID", "EXTRA_COL"}

    cols = [{"table_or_alias": "LIB_LOAN", "column": "LIB_LOAN_ID"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols)
    assert r["violations"] == []
    assert r["warnings"] == []

def test_列不在attributes也不在物理表_hard_block(onto, monkeypatch):
    """列既不在本体也不在物理表 → hard_block: 确定幻觉列 (§3.4.1 三态表第2行)"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID", "READER_ID"}

    cols = [{"table_or_alias": "LIB_LOAN", "column": "PHANTOM_COL"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols)
    assert len(r["violations"]) == 1
    assert r["violations"][0]["type"] == "column_not_found"

def test_列在物理表但不在attributes_soft_warn(onto, monkeypatch):
    """列在物理表但本体漏登 → soft_warn + attr_missing_in_ontology (§3.4.1 三态表第3行)"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID", "READER_ID", "UNREGISTERED_COL"}

    cols = [{"table_or_alias": "LIB_LOAN", "column": "UNREGISTERED_COL"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols)
    assert r["violations"] == []
    assert len(r["warnings"]) == 1
    assert "attr_missing_in_ontology" in r["warnings"][0]

def test_列在attributes但不在物理表_soft_warn(onto, monkeypatch):
    """本体登了但物理表没有 → soft_warn: 可能 schema 变更 (§3.4.1 三态表第4行)"""
    def mock_get_cols(source, table):
        return set()  # 物理表为空 (模拟删列)

    cols = [{"table_or_alias": "LIB_LOAN", "column": "LIB_LOAN_ID"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols)
    assert r["violations"] == []
    assert len(r["warnings"]) == 1

def test_对账失败_跳过不拦(onto, monkeypatch):
    """对账函数抛异常 (连库失败) → 不硬拦, 跳过 (§3.4.1 三态表最后一行)"""
    def mock_get_cols_fail(source, table):
        raise ValueError("connection timeout")

    cols = [{"table_or_alias": "LIB_LOAN", "column": "ANY_COL"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols_fail)
    assert r["violations"] == []
    assert "reconcile_error" in r["skipped"]

def test_裸列多表_table_or_alias为None_跳过(onto, monkeypatch):
    """裸列多表 JOIN 归属不明 (table_or_alias=None) → 跳过 (spec §3.3.1)"""
    def mock_get_cols(source, table):
        return {"COL1"}

    cols = [{"table_or_alias": None, "column": "COL1"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols)
    assert r["violations"] == []
    assert "skip_column" in r["skipped"]


# ============ ② 别名盲区 (alias_to_table 注入) ============

def test_别名列能解析到实体_幻觉列照样硬拦(onto):
    """SQL 写 `FROM LIB_LOAN pi` 时列带别名 PI, 必须经 alias_to_table 还原成真表名,
    否则整条列归属校验静默 skip_table (2026-09-19 实测真实 SQL 里相当一部分中招)。"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID", "READER_ID"}

    cols = [{"table_or_alias": "PI", "column": "PHANTOM_COL"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols,
                                    alias_to_table={"PI": "LIB_LOAN"})
    assert "skip_table" not in r["skipped"]
    assert len(r["violations"]) == 1
    assert r["violations"][0]["type"] == "column_not_found"


def test_别名列物理对账用真表名查(onto):
    """物理列回调必须收到真表名 —— 拿别名去 information_schema 查必然空集,
    会退化成 reconcile_error 静默放行 (换个名字的同一个盲区)。"""
    seen = []

    def mock_get_cols(source, table):
        seen.append(table)
        return {"LIB_LOAN_ID"}

    cols = [{"table_or_alias": "PI", "column": "LIB_LOAN_ID"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols,
                                    alias_to_table={"PI": "LIB_LOAN"})
    assert seen == ["LIB_LOAN"]
    assert r["violations"] == []
    assert r["warnings"] == []


def test_别名小写也能解析(onto):
    """sqlglot 抽取层大小写不保证一致, 映射查找要大小写无关。"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID"}

    cols = [{"table_or_alias": "pi", "column": "LIB_LOAN_ID"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols,
                                    alias_to_table={"PI": "LIB_LOAN"})
    assert r["violations"] == []
    assert "skip_table" not in r["skipped"]


def test_别名映射缺失_仍旧跳过不硬拦(onto):
    """映射里没有的别名 (跨库/子查询派生表) → 维持 skip_table 降级,
    绝不向硬拦升级 (spec §2.5 本体无知一律放行)。"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID"}

    cols = [{"table_or_alias": "XX", "column": "WHATEVER"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols,
                                    alias_to_table={"PI": "LIB_LOAN"})
    assert r["violations"] == []
    assert "skip_table" in r["skipped"]


def test_不传alias映射时裸表名照常工作(onto):
    """老调用方 (不传 alias_to_table) 行为不变 —— 裸表名直接命中。"""
    def mock_get_cols(source, table):
        return {"LIB_LOAN_ID"}

    cols = [{"table_or_alias": "LIB_LOAN", "column": "LIB_LOAN_ID"}]
    r = validate_column_attribution(cols, "lib", onto, mock_get_cols)
    assert r["violations"] == []
    assert "skip_table" not in r["skipped"]


# ============ ③ 取值枚举软校验 (validate_enum_values) ============

@pytest.fixture
def onto_with_enum():
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "STATUS", "hasEnum": True}]},
        ],
        "valueSets": {
            ("lib_loan", "STATUS"): {
                "name": "产品状态", "spec_ref": "lib_loan.STATUS",
                "codes": [
                    {"code": "ACTIVE", "name": "在用"},
                    {"code": "INACTIVE", "name": "停用"},
                ]
            }
        },
        "domains": [], "filterRules": [], "concepts": [], "relationships": [],
    }
    return build_indexes(raw)

def test_取值在枚举内_pass(onto_with_enum):
    """WHERE col = 'ACTIVE', 值在 codes → pass (spec §3.4 ③)"""
    lits = [{"column": "STATUS", "op": "=", "value": "ACTIVE"}]
    r = validate_enum_values(lits, "lib", onto_with_enum, {"lib_loan"})
    assert r["violations"] == []
    assert r["warnings"] == []

def test_取值不在枚举_soft_warn(onto_with_enum):
    """WHERE col = 'UNKNOWN_STATUS' → soft_warn (§3.4 ③: 软提醒, 不硬拦)"""
    lits = [{"column": "STATUS", "op": "=", "value": "UNKNOWN_STATUS"}]
    r = validate_enum_values(lits, "lib", onto_with_enum, {"lib_loan"})
    assert r["violations"] == []
    assert len(r["warnings"]) == 1
    assert "enum" in r["warnings"][0].lower()

def test_列无枚举_跳过(onto_with_enum):
    """WHERE 用了非枚举列 → 跳过 (spec: 仅对已登记枚举的列判)"""
    lits = [{"column": "NON_ENUM_COL", "op": "=", "value": "anything"}]
    r = validate_enum_values(lits, "lib", onto_with_enum, {"lib_loan"})
    assert r["warnings"] == []

def test_IN操作符展开多值(onto_with_enum):
    """WHERE col IN ('ACTIVE', 'WRONG') → 两值独立判, WRONG 触发 soft_warn"""
    lits = [
        {"column": "STATUS", "op": "IN", "value": "ACTIVE"},
        {"column": "STATUS", "op": "IN", "value": "WRONG"},
    ]
    r = validate_enum_values(lits, "lib", onto_with_enum, {"lib_loan"})
    assert len(r["warnings"]) == 1
    assert "WRONG" in r["warnings"][0]


@pytest.fixture
def onto_two_entities_enum():
    """两 entity 同 source, 同名列 STATUS 但枚举不同 → 测 ③ 只查 referenced 的那个。"""
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "STATUS", "hasEnum": True}]},
            {"id": "title", "tables": ["TITLE"], "data_source": "lib",
             "attributes": [{"name": "STATUS", "hasEnum": True}]},
        ],
        "valueSets": {
            ("lib_loan", "STATUS"): {
                "name": "产品状态", "spec_ref": "lib_loan.STATUS",
                "codes": [{"code": "ACTIVE", "name": "在用"}],
            },
            ("title", "STATUS"): {
                "name": "书目包状态", "spec_ref": "title.STATUS",
                "codes": [{"code": "ON_SHELF", "name": "在售"}],
            },
        },
        "domains": [], "filterRules": [], "concepts": [], "relationships": [],
    }
    return build_indexes(raw)

def test_枚举只查referenced_entity_不串到别的entity(onto_two_entities_enum):
    """③ 只对 SQL 引用到的 entity 判枚举 (bug 修复回归):

    SQL 只引用 lib_loan (STATUS 合法值 ACTIVE), 取值 'ON_SHELF' 虽是 title 的合法值
    但不属 lib_loan → 应报 soft_warn; 不应因遍历到 title 而误判为合法放行。
    """
    lits = [{"column": "STATUS", "op": "=", "value": "ON_SHELF"}]
    # 只引用 lib_loan
    r = validate_enum_values(lits, "lib", onto_two_entities_enum, {"lib_loan"})
    assert r["violations"] == []
    assert len(r["warnings"]) == 1
    assert "ON_SHELF" in r["warnings"][0]
    # 反向: 若只引用 title, ON_SHELF 是合法值 → 无 warning
    r2 = validate_enum_values(lits, "lib", onto_two_entities_enum, {"title"})
    assert r2["warnings"] == []


# ============ ④ filter_rule 软校验 (validate_filter_rules) ============

@pytest.fixture
def onto_with_filter_rule():
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "STATE"}]},
        ],
        "filterRules": [
            {"entity": "lib_loan", "col": "STATE", "op": "=", "value": "1", "mode": "soft"},
        ],
        "valueSets": {}, "domains": [], "concepts": [], "relationships": [],
    }
    return build_indexes(raw)

def test_WHERE带了filter_rule列_pass(onto_with_filter_rule):
    """WHERE 包含 STATE 列 → pass (spec §3.4 ④: 没带才 soft_warn)"""
    lits = [{"column": "STATE", "op": "=", "value": "1"}]
    r = validate_filter_rules(lits, "lib", onto_with_filter_rule, {"lib_loan"})
    assert r["warnings"] == []

def test_WHERE未带filter_rule列_soft_warn(onto_with_filter_rule):
    """WHERE 不含 STATE 列 → soft_warn 提示默认规则 (spec §3.4 ④)"""
    lits = []  # WHERE 为空或不含 STATE
    r = validate_filter_rules(lits, "lib", onto_with_filter_rule, {"lib_loan"})
    assert len(r["warnings"]) == 1
    assert "STATE" in r["warnings"][0]

def test_无filter_rule_不提示(onto_with_filter_rule):
    """entity 无 filter_rule → 不提示 (spec: filter_rule 全是软, 没规则没提示)"""
    # 造一个空 filter_rules 的本体
    raw = {
        "entities": [{"id": "e1", "tables": ["T1"], "data_source": "lib", "attributes": []}],
        "filterRules": [],
        "valueSets": {}, "domains": [], "concepts": [], "relationships": [],
    }
    onto_empty = build_indexes(raw)
    r = validate_filter_rules([], "lib", onto_empty, {"e1"})
    assert r["warnings"] == []


@pytest.fixture
def onto_two_entities_filter_rule():
    """两 entity 同 source, 各有 filter_rule → 测 ④ 只对 SQL 引用到的 entity 提示。

    真库 bug: lib_loan 查询把无关 entity (title/label/...) 的 filter_rule 也
    全喷成 soft_warn。修复后只判 referenced_entities 里的 entity。
    """
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "STATE"}]},
            {"id": "title", "tables": ["TITLE"], "data_source": "lib",
             "attributes": [{"name": "TITLE_STATE"}]},
        ],
        "filterRules": [
            {"entity": "lib_loan", "col": "STATE", "op": "=", "value": "1", "mode": "soft"},
            {"entity": "title", "col": "TITLE_STATE", "op": "=", "value": "2", "mode": "soft"},
        ],
        "valueSets": {}, "domains": [], "concepts": [], "relationships": [],
    }
    return build_indexes(raw)

def test_filter_rule只提示referenced_entity_不喷无关entity(onto_two_entities_filter_rule):
    """④ 只对 SQL 引用到的 entity 提示 filter_rule (bug 修复回归):

    SQL 只查 lib_loan (WHERE 空), 应只提示 lib_loan.STATE;
    绝不能因遍历整个 source 而把 title.TITLE_STATE 也喷成 warning。
    """
    lits = []  # WHERE 空: lib_loan.STATE 未包含 → 该提示
    r = validate_filter_rules(lits, "lib", onto_two_entities_filter_rule, {"lib_loan"})
    assert len(r["warnings"]) == 1
    assert "STATE" in r["warnings"][0]
    assert "lib_loan" in r["warnings"][0]
    # title 未被引用 → 其规则绝不能出现
    assert all("title" not in w and "TITLE_STATE" not in w for w in r["warnings"])
