"""端到端集成测试: 计划二六特性联动验证 (②③④ + I1/I2 + 缓存)。

驱动的是真实链路: execute_sql → obqc_check(四类校验) → I1 熔断 → I2 上浮 →
物理列缓存。只 mock 两个真库 I/O 边界 (list_table_columns 物理列 / _run_query
执行), 其余全走线上代码。

★ 测试隔离 (Task 7 review 铁律):
- 物理列缓存 (obqc.cache._physical_col_cache) 是模块级单例;
- 熔断状态 (tools.sql_exec._circuit_breaker_hits) 是模块级 dict;
- 本体 (core.ontology_view._ontology) 是模块级单例。
setup_cache fixture 在 SETUP 与 TEARDOWN 两端都装新缓存 + 清熔断, 保证本文件内
测试之间零残留串味 (例如熔断测试之后的缓存命中计数测试不受上一条影响)。
"""
import pytest

from core import ontology_view as ontology
from core.ontology_view import build_indexes
from agent.tools import sql_exec
from agent.tools.sql_exec import execute_sql, _reset_circuit_breaker
from capabilities.obqc.cache import PhysicalColumnCache, set_physical_col_cache


class _FakeCursor:
    """mock cursor: 记录 executed_sql, 返回预设 rows (物理列查询用不到, 走 mock)。"""
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed_sql = sql

    def fetchall(self):
        return self._rows


class _FakeConn:
    """mock connection: 只为过 execute_sql 的 'db not initialized' 前置守门。"""
    def __init__(self, cursor=None):
        self._cursor = cursor or _FakeCursor()

    def cursor(self):
        return self._cursor

    def ping(self, reconnect=True):
        pass


@pytest.fixture
def full_onto(monkeypatch):
    """完整本体: 含 entity/relation/enum/filter_rule, 并注入 core.ontology_view 单例。

    execute_sql 内部走 get_ontology() 读模块单例才会进 OBQC 分支, 光 build_indexes
    返回值不注入是够不到线上校验的 —— 故这里 monkeypatch _ontology。
    同时注入 source 方言 (指纹/取列 SQL 分派需要)。
    """
    raw = {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [
                 {"name": "LIB_LOAN_ID"},
                 {"name": "STATUS", "hasEnum": True},
                 {"name": "STATE"},
             ]},
            {"id": "lib_reader", "tables": ["LIB_READER"], "data_source": "lib",
             "attributes": [{"name": "MEMBER_ID"}, {"name": "MEMBER_NAME"}]},
        ],
        "relationships": [
            {"source": "lib_loan", "target": "lib_reader",
             "source_col": "READER_ID", "target_col": "MEMBER_ID", "type": "MANY_TO_ONE"},
        ],
        "valueSets": {
            ("lib_loan", "STATUS"): {
                "name": "产品状态",
                "codes": [{"code": "ACTIVE"}, {"code": "INACTIVE"}],
            }
        },
        "filterRules": [
            {"entity": "lib_loan", "col": "STATE", "op": "=", "value": "1", "mode": "soft"},
        ],
        "domains": [], "concepts": [],
    }
    onto = build_indexes(raw)
    monkeypatch.setattr(ontology, "_ontology", onto)
    sql_exec.set_source_dialects({"lib": "mysql"})
    return onto


@pytest.fixture
def setup_cache(monkeypatch):
    """注入物理列缓存 + mock list_table_columns + 注入 lib 连接。

    ★ 隔离: SETUP 端先清熔断 + 装新缓存 (不只 teardown), 保证本文件内测试互不
    串味 (模块级单例 / 熔断 dict 是跨测试残留源)。TEARDOWN 端再清一遍并复位连接池。
    """
    # SETUP 端复位 (防上一条测试的残留)
    _reset_circuit_breaker()
    cache = PhysicalColumnCache()
    set_physical_col_cache(cache)

    # 注入 lib 连接 (execute_sql 过 'db not initialized' 守门 + 到达成功返回路径)
    sql_exec.set_connections({"lib": _FakeConn()})

    def mock_list_cols(table, source):
        cols_map = {
            "LIB_LOAN": {"LIB_LOAN_ID", "STATUS", "STATE", "READER_ID"},
            "LIB_READER": {"MEMBER_ID", "MEMBER_NAME"},
        }
        return cols_map.get(table.upper(), set())

    monkeypatch.setattr("agent.tools.sql_exec.list_table_columns", mock_list_cols)
    yield
    # TEARDOWN 端复位
    cache.clear()
    _reset_circuit_breaker()
    sql_exec.set_connections({})


def test_E2E_全绿场景_pass(full_onto, setup_cache, monkeypatch):
    """所有校验通过: JOIN 合法 + 列存在 + 枚举正确 + 带 filter_rule 列"""
    def mock_run(sql, conn, dialect=None):
        return [{"LIB_LOAN_ID": "123"}]
    monkeypatch.setattr("agent.tools.sql_exec._run_query", mock_run)

    sql = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
           "JOIN LIB_READER c ON p.READER_ID = c.MEMBER_ID "
           "WHERE p.STATUS = 'ACTIVE' AND p.STATE = '1'")

    r = execute_sql(sql, entity="lib_loan")
    assert r["rows"] == [{"LIB_LOAN_ID": "123"}]
    assert "warnings" not in r  # 全绿无 warnings


def test_E2E_多类违规_soft_warn聚合(full_onto, setup_cache, monkeypatch):
    """多类 soft_warn: 枚举不匹配 + 缺 filter_rule 列 → 聚合上浮"""
    def mock_run(sql, conn, dialect=None):
        return [{"LIB_LOAN_ID": "123"}]
    monkeypatch.setattr("agent.tools.sql_exec._run_query", mock_run)

    sql = ("SELECT LIB_LOAN_ID FROM LIB_LOAN "
           "WHERE STATUS = 'WRONG_STATUS'")  # 枚举错 + 缺 STATE 列

    r = execute_sql(sql, entity="lib_loan")
    assert r["rows"] == [{"LIB_LOAN_ID": "123"}]  # soft_warn 不拦
    assert "warnings" in r
    assert len(r["warnings"]) >= 2  # 枚举 + filter_rule 两条


def test_E2E_幻觉列_hard_block(full_onto, setup_cache):
    """② 列归属硬拦: 列既不在本体也不在物理表"""
    sql = "SELECT PHANTOM_COL FROM LIB_LOAN"
    r = execute_sql(sql, entity="lib_loan")
    assert r["blocked"] is True
    assert any(v["type"] == "column_not_found" for v in r["violations"])


def test_E2E_无边JOIN_hard_block_熔断(full_onto, setup_cache):
    """① JOIN 硬拦 + I1 熔断: 第 2 次加强 hint

    lib_loan ↔ lib_reader 有边 (READER_ID = MEMBER_ID), 这里故意把连接列写错
    (LIB_LOAN_ID = MEMBER_NAME) → 触发 join_col_mismatch hard_block。同一 SQL
    第 2 次提交 → 指纹命中, 熔断加强 hint。
    """
    sql1 = "SELECT * FROM LIB_LOAN p JOIN LIB_READER c ON p.LIB_LOAN_ID = c.MEMBER_NAME"

    r1 = execute_sql(sql1, entity="lib_loan")
    assert r1["blocked"] is True

    # 第 2 次同一 SQL
    r2 = execute_sql(sql1, entity="lib_loan")
    assert r2["blocked"] is True
    # 检查熔断加强 hint
    assert any("第二次" in str(h) or "等价" in str(h) for h in r2["hints"])


def test_E2E_物理列缓存生效(full_onto, setup_cache, monkeypatch):
    """§3.7 缓存: 物理列第 2 次查不打库 (缓存命中)"""
    call_count = [0]

    def mock_list_cols(table, source):
        call_count[0] += 1
        return {"LIB_LOAN_ID", "STATUS"}

    monkeypatch.setattr("agent.tools.sql_exec.list_table_columns", mock_list_cols)

    def mock_run(sql, conn, dialect=None):
        return []
    monkeypatch.setattr("agent.tools.sql_exec._run_query", mock_run)

    sql = "SELECT LIB_LOAN_ID FROM LIB_LOAN"

    execute_sql(sql, entity="lib_loan")  # 第 1 次: 打库 + 缓存
    execute_sql(sql, entity="lib_loan")  # 第 2 次: 缓存命中

    assert call_count[0] == 1  # 只打库 1 次 (第 2 次走缓存)
