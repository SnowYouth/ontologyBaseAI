"""tools/sql_exec.py 单元测试 — mock pymysql connection, 不连真库."""
import datetime

import pytest


class TestIsSelect:
    def test_纯select_通过(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("SELECT * FROM lib_loan") is True

    def test_字段名撞词不误杀(self):
        """核心坑: 字段名 UPDATE_DATE 不该被整句关键字包含检查误杀."""
        from agent.tools.sql_exec import _is_select
        assert _is_select("SELECT UPDATE_DATE FROM lib_loan WHERE CARD_NO = '138'") is True
        assert _is_select("SELECT DELETE_FLAG, CREATE_DATE FROM lib_copy") is True

    def test_insert_拒(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("INSERT INTO lib_loan VALUES (1)") is False

    def test_update_拒(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("UPDATE lib_loan SET REC_STATUS='1'") is False

    def test_delete_拒(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("DELETE FROM lib_loan WHERE ID=1") is False

    def test_drop_拒(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("DROP TABLE lib_loan") is False

    def test_分号多语句拒(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("SELECT 1; DROP TABLE lib_loan") is False

    def test_大小写混合空白_通过(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("  select  *  from lib_loan  ") is True

    def test_单条语句末尾分号_通过(self):
        from agent.tools.sql_exec import _is_select
        assert _is_select("SELECT * FROM lib_loan;") is True


class FakeCursor:
    """mock pymysql cursor: 记录被执行的 sql, 返回预设 rows 或抛预设异常."""
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise_exc = raise_exc
        self.executed_sql = None
        self.executed_params = None
        self.all_sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed_sql = sql
        self.executed_params = params
        self.all_sql.append(sql)
        if self._raise_exc:
            raise self._raise_exc

    def fetchall(self):
        return self._rows


class FakeConn:
    """mock pymysql connection: cursor() 返回 FakeCursor; reconnect_calls 记重连次数."""
    def __init__(self, cursor):
        self._cursor = cursor
        self.reconnect_calls = 0

    def cursor(self):
        return self._cursor

    def ping(self, reconnect=True):
        self.reconnect_calls += 1


@pytest.fixture(autouse=True)
def _reset_conn():
    import agent.tools.sql_exec as mod
    mod._conn = None
    mod._connections = {}
    mod._source_dialects = {}
    mod._source_configs = {}
    yield
    mod._conn = None
    mod._connections = {}
    mod._source_dialects = {}
    mod._source_configs = {}


class TestExecuteSql:
    def test_缺省参数_返回error(self):
        """既不传 entity 也不传 source 时, 返回参数缺失错误."""
        from agent.tools.sql_exec import execute_sql
        result = execute_sql(sql="SELECT 1")
        assert "error" in result
        assert "entity" in result["error"].lower() or "source" in result["error"].lower()

    def test_未初始化_返回error(self):
        from agent.tools.sql_exec import execute_sql
        result = execute_sql(sql="SELECT 1", source="lib")
        assert "error" in result
        assert "db" in result["error"].lower() or "initialized" in result["error"].lower()

    def test_非select_直接拒绝不调用cursor(self):
        from agent.tools.sql_exec import execute_sql, set_db_conn
        cursor = FakeCursor()
        set_db_conn(FakeConn(cursor))
        result = execute_sql(sql="DROP TABLE lib_loan", source="lib")
        assert "error" in result
        assert cursor.executed_sql is None  # 没打库

    def test_正常查询_返回rows(self):
        from agent.tools.sql_exec import execute_sql, set_db_conn
        rows = [{"REC_STATUS": "300001"}]
        cursor = FakeCursor(rows=rows)
        set_db_conn(FakeConn(cursor))
        result = execute_sql(sql="SELECT REC_STATUS FROM lib_loan WHERE CARD_NO='138'", source="lib")
        assert result["rows"] == rows
        assert result["n"] == 1

    def test_pymysql报错_转error_dict(self):
        from agent.tools.sql_exec import execute_sql, set_db_conn
        import pymysql
        cursor = FakeCursor(raise_exc=pymysql.err.ProgrammingError("列名不存在"))
        set_db_conn(FakeConn(cursor))
        result = execute_sql(sql="SELECT NOT_EXIST_COL FROM lib_loan", source="lib")
        assert "error" in result
        assert "ProgrammingError" in result["error"]

    def test_连接失效_触发一次reconnect(self):
        from agent.tools.sql_exec import execute_sql, set_db_conn
        import pymysql
        cursor = FakeCursor(raise_exc=pymysql.err.OperationalError(2006, "连接已断开"))
        conn = FakeConn(cursor)
        set_db_conn(conn)
        result = execute_sql(sql="SELECT 1", source="lib")
        assert conn.reconnect_calls == 1
        assert "error" in result  # cursor 每次都抛同样异常, reconnect 后仍失败也如实报错

    def test_InterfaceError也触发reconnect(self):
        """核心坑 (真库 smoke 复现): pymysql 对"连接已断开后再用"抛的是
        InterfaceError, 不是 OperationalError 的子类。老代码只 except
        OperationalError, InterfaceError 落进最后通用分支不重连、不 ping,
        死连接状态永久保留 —— 后续每次查询都立刻炸相同错误,
        agent loop 只能不断重试直到 max_iters 触顶。
        InterfaceError 也必须走 reconnect 分支。
        """
        from agent.tools.sql_exec import execute_sql, set_db_conn
        import pymysql
        cursor = FakeCursor(raise_exc=pymysql.err.InterfaceError(0, ""))
        conn = FakeConn(cursor)
        set_db_conn(conn)
        result = execute_sql(sql="SELECT 1", source="lib")
        assert conn.reconnect_calls == 1
        assert "error" in result

    def test_reconnect后恢复正常查询(self):
        """连接失效重连一次后, 若底层连接已恢复(下一次 execute 不再抛),
        应正常返回结果, 而不是永远返回 error。"""
        from agent.tools.sql_exec import execute_sql, set_db_conn
        import pymysql

        class _FlakyCursor(FakeCursor):
            """第一次调用抛 InterfaceError, 之后正常返回 rows (模拟 reconnect 后恢复)."""
            def __init__(self):
                super().__init__(rows=[{"REC_STATUS": "300001"}])
                self._raised_once = False

            def execute(self, sql):
                self.executed_sql = sql
                if not self._raised_once:
                    self._raised_once = True
                    raise pymysql.err.InterfaceError(0, "")

        cursor = _FlakyCursor()
        conn = FakeConn(cursor)
        set_db_conn(conn)
        result = execute_sql(sql="SELECT REC_STATUS FROM lib_loan", source="lib")
        assert conn.reconnect_calls == 1
        assert "error" not in result
        assert result["rows"] == [{"REC_STATUS": "300001"}]


class TestMask:
    def test_等号打码(self):
        from agent.tools.sql_exec import _mask, set_sensitive_cols
        set_sensitive_cols({"MEMBER_NAME"})
        masked = _mask("SELECT * FROM t WHERE MEMBER_NAME = '张三'")
        assert "张三" not in masked
        assert "***" in masked
        set_sensitive_cols(set())

    def test_LIKE语句打码(self):
        """核心坑: LIKE '%张三%' 这类模糊匹配之前漏打码, 客户姓名明文进日志。"""
        from agent.tools.sql_exec import _mask, set_sensitive_cols
        set_sensitive_cols({"MEMBER_NAME"})
        masked = _mask("SELECT * FROM t WHERE MEMBER_NAME LIKE '%张三%'")
        assert "张三" not in masked
        assert "***" in masked
        set_sensitive_cols(set())

    def test_IN语句打码(self):
        from agent.tools.sql_exec import _mask, set_sensitive_cols
        set_sensitive_cols({"CARD_NO"})
        masked = _mask("SELECT * FROM t WHERE CARD_NO IN ('138', '139')")
        assert "138" not in masked
        assert "139" not in masked
        set_sensitive_cols(set())

    def test_非敏感列不受影响(self):
        from agent.tools.sql_exec import _mask, set_sensitive_cols
        set_sensitive_cols({"MEMBER_NAME"})
        masked = _mask("SELECT * FROM t WHERE REC_STATUS = '300001'")
        assert masked == "SELECT * FROM t WHERE REC_STATUS = '300001'"
        set_sensitive_cols(set())


class TestExecuteSqlDatetime:
    def test_结果含datetime字段_不炸_转字符串(self):
        """核心坑: pymysql 返回 rows 里的 datetime/date 值, 下游 json.dumps 会抛
        TypeError, execute_sql 必须在这里就转成 JSON 兼容类型, 不能让报错漏到 agent 层。"""
        from agent.tools.sql_exec import execute_sql, set_db_conn
        import json
        rows = [{"CARD_NO": "138", "CREATE_DATE": datetime.datetime(2024, 1, 1, 12, 30, 0)}]
        cursor = FakeCursor(rows=rows)
        set_db_conn(FakeConn(cursor))
        result = execute_sql(sql="SELECT CARD_NO, CREATE_DATE FROM lib_loan", source="lib")
        assert "error" not in result
        json.dumps(result, ensure_ascii=False)  # 不该抛
        assert result["rows"][0]["CREATE_DATE"] == "2024-01-01 12:30:00"

    def test_结果含date字段_转字符串(self):
        from agent.tools.sql_exec import execute_sql, set_db_conn
        import json
        rows = [{"VALID_FROM": datetime.date(2024, 1, 1)}]
        cursor = FakeCursor(rows=rows)
        set_db_conn(FakeConn(cursor))
        result = execute_sql(sql="SELECT VALID_FROM FROM lib_loan", source="lib")
        json.dumps(result, ensure_ascii=False)  # 不该抛


class TestMultiSource:
    """多数据源连接池测试 (阶段: 多库支持)."""

    def test_execute_sql_source参数选择不同连接(self):
        """execute_sql 按 source 参数从连接池选对应连接."""
        from agent.tools.sql_exec import execute_sql, set_connections

        lib_rows = [{"MEMBER_ID": "C001", "CARD_NO": "138"}]
        lib2_rows = [{"memberId": "C001", "memberName": "张三"}]
        lib_cursor = FakeCursor(rows=lib_rows)
        lib2_cursor = FakeCursor(rows=lib2_rows)

        set_connections({
            "lib": FakeConn(lib_cursor),
            "lib2": FakeConn(lib2_cursor),
        })

        # 查 lib
        result_lib = execute_sql(sql="SELECT * FROM cust", source="lib")
        assert result_lib["rows"] == lib_rows
        assert lib_cursor.executed_sql == "SELECT * FROM cust"

        # 查 lib2
        result_lib2 = execute_sql(sql="SELECT * FROM lib_reader", source="lib2")
        assert result_lib2["rows"] == lib2_rows
        assert lib2_cursor.executed_sql == "SELECT * FROM lib_reader"

    def test_execute_sql_缺省参数_返回error(self):
        """source 和 entity 都不传时应返回错误 (新规则: 取消默认值)."""
        from agent.tools.sql_exec import execute_sql, set_connections

        lib_rows = [{"REC_STATUS": "300001"}]
        lib_cursor = FakeCursor(rows=lib_rows)
        lib2_cursor = FakeCursor(rows=[])

        set_connections({
            "lib": FakeConn(lib_cursor),
            "lib2": FakeConn(lib2_cursor),
        })

        result = execute_sql(sql="SELECT REC_STATUS FROM lib_loan")  # 不传 source 或 entity
        assert "error" in result
        assert "entity" in result["error"] or "source" in result["error"]

    def test_execute_sql_source不存在_返回error(self):
        """source 不在连接池里时返回 error."""
        from agent.tools.sql_exec import execute_sql, set_connections

        set_connections({"lib": FakeConn(FakeCursor())})

        result = execute_sql(sql="SELECT 1", source="billing")
        assert "error" in result
        assert "billing" in result["error"].lower()

    def test_混合数据源批量查询(self):
        """模拟场景: agent 根据 entity.data_source 字段对不同实体选择对应连接池."""
        from agent.tools.sql_exec import execute_sql, set_connections

        # 准备两个源的 mock 数据
        lib_cursor = FakeCursor(rows=[{"CARD_NO": "138", "REC_STATUS": "300001"}])
        lib2_cursor = FakeCursor(rows=[{"memberId": "C001", "memberName": "张三"}])

        set_connections({
            "lib": FakeConn(lib_cursor),
            "lib2": FakeConn(lib2_cursor),
        })

        # 场景: agent 查询两个实体, 根据 data_source 路由到不同连接
        # 实体1 (AccNum) 在 lib 库
        result1 = execute_sql(
            sql="SELECT CARD_NO, REC_STATUS FROM lib_loan WHERE CARD_NO='138'",
            source="lib"
        )
        assert result1["rows"] == [{"CARD_NO": "138", "REC_STATUS": "300001"}]

        # 实体2 (Lib_Reader) 在 lib2 库
        result2 = execute_sql(
            sql="SELECT memberId, memberName FROM lib_reader WHERE memberId='C001'",
            source="lib2"
        )
        assert result2["rows"] == [{"memberId": "C001", "memberName": "张三"}]

        # 验证两个连接各自被正确调用
        assert lib_cursor.executed_sql.startswith("SELECT CARD_NO")
        assert lib2_cursor.executed_sql.startswith("SELECT memberId")


def test_list_table_columns_走information_schema取列名(monkeypatch):
    """核心 (P0 债务): 分库中间件拦 SELECT *, 改走 information_schema.COLUMNS。
    从查询 rows 里取 COLUMN_NAME (DictCursor 形态), 不再读 cur.description。"""
    import agent.tools.sql_exec as se

    rows = [{"COLUMN_NAME": "MEMBER_ID"}, {"COLUMN_NAME": "REC_STATUS"},
            {"COLUMN_NAME": "CREATE_DATE"}]
    cursor = FakeCursor(rows=rows)
    monkeypatch.setattr(se, "_connections", {"lib": FakeConn(cursor)})

    cols = se.list_table_columns("cust_info", "lib")

    assert cols == {"MEMBER_ID", "REC_STATUS", "CREATE_DATE"}


def test_list_table_columns_不含SELECT星_避开分库中间件(monkeypatch):
    """分库中间件拒 SELECT * (OperationalError 3013), 新实现的 SQL 绝不能含 *。"""
    import agent.tools.sql_exec as se

    cursor = FakeCursor(rows=[{"COLUMN_NAME": "A"}])
    monkeypatch.setattr(se, "_connections", {"lib": FakeConn(cursor)})

    se.list_table_columns("lib_loan", "lib")

    assert "SELECT *" not in cursor.executed_sql.upper()
    assert "INFORMATION_SCHEMA" in cursor.executed_sql.upper()
    # 表名参数化传入 (防注入), 不拼进 SQL 文本
    assert "lib_loan" in (cursor.executed_params or ())


def test_list_table_columns_源未注册抛异常(monkeypatch):
    import agent.tools.sql_exec as se
    monkeypatch.setattr(se, "_connections", {"lib": object()})
    import pytest
    with pytest.raises(ValueError, match="lib2"):
        se.list_table_columns("t", "lib2")


def test_source_dialect_注入与读取():
    from agent.tools import sql_exec
    sql_exec.set_source_dialects({"lib": "mysql", "lib2": "mysql"})
    assert sql_exec.get_source_dialect("lib") == "mysql"
    assert sql_exec.get_source_dialect("lib2") == "mysql"


def test_source_dialect_未知源回退mysql():
    from agent.tools import sql_exec
    sql_exec.set_source_dialects({"lib": "mysql"})
    # 未注册的 source → 回退 mysql (fail-safe, 不炸)
    assert sql_exec.get_source_dialect("unknown") == "mysql"


def test_列清单sql_按方言分派_mysql():
    """mysql 分派出 information_schema.COLUMNS 语句 (现状行为不变)。

    取列 SQL 的方言知识已搬到 db_adapters.columns_sql (多源配置化 Task 2)。
    """
    from agent.tools.db_adapters import columns_sql
    sql, bind_style = columns_sql("mysql")
    assert "information_schema.COLUMNS" in sql
    assert "TABLE_NAME = %s" in sql
    assert bind_style == "seq"


class TestOBQCIntegration:
    """OBQC 挂载到 execute_sql 的集成测试 (Task8)."""

    def test_execute_sql_非法join被拦不执行(self, monkeypatch):
        """OBQC hard_block → execute_sql 不碰库, 返回 blocked。"""
        from agent.tools import sql_exec
        from core import ontology_view as ontology
        from core.ontology_view import build_indexes

        raw = {
            "entities": [
                {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
                 "attributes": [{"name": "READER_ID"}]},
                {"id": "acct", "tables": ["ACCOUNT"], "data_source": "lib",
                 "attributes": [{"name": "MEMBER_ID"}]},
            ],
            "relationships": [],  # 无边 → 任何 JOIN 都 hard_block
            "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
        }
        monkeypatch.setattr(ontology, "_ontology", build_indexes(raw))
        sql_exec.set_source_dialects({"lib": "mysql"})

        # 注入 fake conn (虽然 hard_block 不应碰库，但要让代码走到 OBQC 检查点)
        cursor = FakeCursor(rows=[])
        sql_exec.set_connections({"lib": FakeConn(cursor)})

        sql = ("SELECT p.READER_ID FROM LIB_LOAN p "
               "JOIN ACCOUNT a ON p.READER_ID = a.MEMBER_ID")
        res = sql_exec.execute_sql(sql, source="lib")
        assert res.get("blocked") is True
        assert any(v["type"] == "no_edge" for v in res["violations"])
        # 确认用户 SQL 没被执行 (obqc 列校验自己那次 information_schema 元数据
        # 查询是正常动作, 不算"碰库"; 别名盲区修复前该 SQL 走 skip_table 从不触发它)
        assert all("information_schema" in s for s in cursor.all_sql)

    def test_execute_sql_合法join放行执行(self, monkeypatch):
        """OBQC pass → 照常执行 (走既有 fake conn 返回 rows)。"""
        from agent.tools import sql_exec
        from core import ontology_view as ontology
        from core.ontology_view import build_indexes

        raw = {
            "entities": [
                {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
                 "attributes": [{"name": "READER_ID"}]},
                {"id": "cust", "tables": ["LIB_READER"], "data_source": "lib",
                 "attributes": [{"name": "MEMBER_ID"}]},
            ],
            "relationships": [
                {"source": "lib_loan", "target": "cust",
                 "source_col": "READER_ID", "target_col": "MEMBER_ID", "type": "MANY_TO_ONE"},
            ],
            "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
        }
        monkeypatch.setattr(ontology, "_ontology", build_indexes(raw))
        sql_exec.set_source_dialects({"lib": "mysql"})

        # 注入 fake conn (复用既有 FakeCursor/FakeConn)
        cursor = FakeCursor(rows=[{"READER_ID": "1"}])
        sql_exec.set_connections({"lib": FakeConn(cursor)})

        sql = ("SELECT p.READER_ID FROM LIB_LOAN p "
               "JOIN LIB_READER c ON p.READER_ID = c.MEMBER_ID")
        res = sql_exec.execute_sql(sql, source="lib")
        assert "blocked" not in res
        assert res["n"] == 1


def test_熔断_同一SQL第二次硬拦加强hint(monkeypatch):
    """I1 熔断: 同一 SQL AST 指纹第 2 次被硬拦 → 仍 hard_block 但 hint 更强 (spec §3.6)"""
    from agent.tools.sql_exec import execute_sql, _reset_circuit_breaker, set_connections
    from core import ontology_view as ontology

    _reset_circuit_breaker()
    # OBQC 分支只在 get_ontology() 非 None 时进入; obqc_check 已被 mock, 内容不重要
    monkeypatch.setattr(ontology, "_ontology", object())
    # hard_block 在碰库前返回, conn 只为过 "db not initialized" 前置守门
    set_connections({"lib": FakeConn(FakeCursor())})

    # Mock obqc_check 让它硬拦
    def mock_obqc_hard_block(sql, source, onto):
        return {
            "verdict": "hard_block",
            "violations": [{"type": "no_edge"}],
            "hints": [{"kind": "no_edge"}],
            "warnings": [],
            "skipped": [],
        }
    monkeypatch.setattr("capabilities.obqc.obqc_check", mock_obqc_hard_block)

    sql = "SELECT * FROM t1 JOIN t2 ON t1.x=t2.y"

    # 第 1 次硬拦
    r1 = execute_sql(sql, source="lib")
    assert r1["blocked"] is True
    assert not any("第二次" in str(h) for h in r1.get("hints", []))

    # 第 2 次硬拦 (同一 SQL) → 加强 hint
    r2 = execute_sql(sql, source="lib")
    assert r2["blocked"] is True
    # 检查加强 hint (包含"第二次"或"等价"关键词)
    assert "重复" in str(r2) or "等价" in str(r2) or "第二次" in str(r2)

    # 清理 session 状态
    _reset_circuit_breaker()


def test_I2_soft_warn强制上浮到返回值(monkeypatch):
    """I2: soft_warn 的 warnings 强制注入返回值, 不靠 LLM 自觉 (spec §2.4/§3.5)"""
    from agent.tools.sql_exec import execute_sql, set_connections
    from core import ontology_view as ontology

    # OBQC 分支只在 get_ontology() 非 None 时进入
    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})

    def mock_obqc_soft_warn(sql, source, onto):
        return {
            "verdict": "soft_warn",
            "violations": [],
            "hints": [],
            "warnings": ["列 STATUS 取值 'WRONG' 不在枚举内"],
            "skipped": [],
        }
    monkeypatch.setattr("capabilities.obqc.obqc_check", mock_obqc_soft_warn)

    # Mock _run_query 返回正常结果 (让代码走到成功返回路径)
    def mock_run(sql, conn, dialect=None):
        return [{"col": "val"}]
    monkeypatch.setattr("agent.tools.sql_exec._run_query", mock_run)

    sql = "SELECT * FROM t WHERE STATUS='WRONG'"
    r = execute_sql(sql, source="lib")

    # 照常执行 (soft_warn 不拦)
    assert r["rows"] == [{"col": "val"}]
    # warnings 强制上浮
    assert "warnings" in r
    assert any("枚举" in w or "WRONG" in w for w in r["warnings"])


def test_I2_pass无warnings不加字段(monkeypatch):
    """pass 或 warnings 为空 → 返回值不加 warnings 字段"""
    from agent.tools.sql_exec import execute_sql, set_connections
    from core import ontology_view as ontology

    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})

    def mock_obqc_pass(sql, source, onto):
        return {"verdict": "pass", "violations": [], "hints": [], "warnings": [], "skipped": []}
    monkeypatch.setattr("capabilities.obqc.obqc_check", mock_obqc_pass)

    def mock_run(sql, conn, dialect=None):
        return [{"col": "val"}]
    monkeypatch.setattr("agent.tools.sql_exec._run_query", mock_run)

    r = execute_sql("SELECT * FROM t", source="lib")
    assert "warnings" not in r  # pass 且无 warnings → 不加字段


def test_熔断_不同SQL不触发(monkeypatch):
    """不同 SQL (不同 AST 指纹) → 不触发熔断"""
    from agent.tools.sql_exec import execute_sql, _reset_circuit_breaker, set_connections
    from core import ontology_view as ontology

    _reset_circuit_breaker()
    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})

    def mock_obqc_hard_block(sql, source, onto):
        return {"verdict": "hard_block", "violations": [{"type": "no_edge"}],
                "hints": [], "warnings": [], "skipped": []}
    monkeypatch.setattr("capabilities.obqc.obqc_check", mock_obqc_hard_block)

    sql1 = "SELECT * FROM t1 WHERE x=1"
    sql2 = "SELECT * FROM t1 WHERE x=2"  # 字面量不同 → 不同指纹

    r1 = execute_sql(sql1, source="lib")
    r2 = execute_sql(sql2, source="lib")

    assert r1["blocked"] is True
    assert r2["blocked"] is True
    # r2 不包含"第二次"提示 (不同指纹)
    assert not ("重复" in str(r2) or "第二次" in str(r2))

    _reset_circuit_breaker()


# ============ use_count 热路径写库 (spec §3 架构图 / §5.4 分母) ============
# 背景: spec 明写 "obqc_check 返回值带 used_edges → sql_exec 写 relation_health",
# 但透传实现了、写库那半句从没落地 → relation_health.use_count 恒 0。
# 后果: §5.4 点名的保护机制 (suspect_rate = suspect/use 而非裸 suspect>=N)
# 因分母恒 0 而形同不存在, 首批自动淘汰目标会是 lib_loan 那些高频主干边。


class _FakeHealthStore:
    """记录 upsert_relation_health 调用的假 store (只测热路径调没调、参数对不对)。"""
    def __init__(self, raise_exc=None):
        self.calls = []
        self._raise_exc = raise_exc

    def upsert_relation_health(self, source, target, via, **kw):
        if self._raise_exc:
            raise self._raise_exc
        self.calls.append({"source": source, "target": target, "via": via, **kw})


def _mock_obqc_with_edges(edges):
    def _m(sql, source, onto):
        return {"verdict": "pass", "violations": [], "hints": [],
                "warnings": [], "skipped": [], "used_edges": edges}
    return _m


def test_use_count_命中边各累加一次(monkeypatch):
    """used_edges 里每条边 → upsert_relation_health(use_count_delta=1)。"""
    from agent.tools.sql_exec import execute_sql, set_connections
    from core import ontology_view as ontology

    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})
    monkeypatch.setattr("capabilities.obqc.obqc_check", _mock_obqc_with_edges([
        {"source": "lib_reader_order", "target": "lib_copy",
         "via": "LIB_ORDER_ID", "source_col": "LIB_ORDER_ID",
         "target_col": "LIB_ORDER_ID"},
        {"source": "lib_copy", "target": "lib_service",
         "via": "TITLE_ID", "source_col": "TITLE_ID", "target_col": "TITLE_ID"},
    ]))
    monkeypatch.setattr("agent.tools.sql_exec._run_query",
                        lambda sql, conn, dialect=None: [{"c": 1}])

    store = _FakeHealthStore()
    monkeypatch.setattr("agent.tools.sql_exec._get_health_store", lambda: store)

    r = execute_sql("SELECT * FROM lib_reader_order", source="lib")

    assert r["n"] == 1                      # 查询本身照常返回
    assert len(store.calls) == 2            # 两条边各记一次
    assert all(c["use_count_delta"] == 1 for c in store.calls)
    triples = {(c["source"], c["target"], c["via"]) for c in store.calls}
    assert ("lib_reader_order", "lib_copy", "LIB_ORDER_ID") in triples
    assert ("lib_copy", "lib_service", "TITLE_ID") in triples


def test_use_count_无命中边不写库(monkeypatch):
    """单表查询 (used_edges 空) → 不碰 relation_health。"""
    from agent.tools.sql_exec import execute_sql, set_connections
    from core import ontology_view as ontology

    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})
    monkeypatch.setattr("capabilities.obqc.obqc_check", _mock_obqc_with_edges([]))
    monkeypatch.setattr("agent.tools.sql_exec._run_query",
                        lambda sql, conn, dialect=None: [{"c": 1}])

    store = _FakeHealthStore()
    monkeypatch.setattr("agent.tools.sql_exec._get_health_store", lambda: store)

    execute_sql("SELECT * FROM lib_loan", source="lib")
    assert store.calls == []


def test_use_count_写库失败不影响查询(monkeypatch):
    """热路径纪律: 埋点炸了查询仍须正常返回 (同 failure_signal 的 except pass)。"""
    from agent.tools.sql_exec import execute_sql, set_connections
    from core import ontology_view as ontology

    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})
    monkeypatch.setattr("capabilities.obqc.obqc_check", _mock_obqc_with_edges([
        {"source": "a", "target": "b", "via": "X",
         "source_col": "X", "target_col": "X"},
    ]))
    monkeypatch.setattr("agent.tools.sql_exec._run_query",
                        lambda sql, conn, dialect=None: [{"c": 1}])
    monkeypatch.setattr("agent.tools.sql_exec._get_health_store",
                        lambda: _FakeHealthStore(raise_exc=RuntimeError("库锁了")))

    r = execute_sql("SELECT * FROM a JOIN b ON a.X=b.X", source="lib")
    assert r["n"] == 1                       # 查询不受影响
    assert "error" not in r
    assert r["_used_edges"]                  # 透传仍在


def test_use_count_无via边跳过(monkeypatch):
    """via 为空的边不能进 relation_health (主键三元组不完整)。"""
    from agent.tools.sql_exec import execute_sql, set_connections
    from core import ontology_view as ontology

    monkeypatch.setattr(ontology, "_ontology", object())
    set_connections({"lib": FakeConn(FakeCursor())})
    monkeypatch.setattr("capabilities.obqc.obqc_check", _mock_obqc_with_edges([
        {"source": "a", "target": "b", "via": None,
         "source_col": "X", "target_col": "Y"},
        {"source": "c", "target": "d", "via": "Z",
         "source_col": "Z", "target_col": "Z"},
    ]))
    monkeypatch.setattr("agent.tools.sql_exec._run_query",
                        lambda sql, conn, dialect=None: [{"c": 1}])

    store = _FakeHealthStore()
    monkeypatch.setattr("agent.tools.sql_exec._get_health_store", lambda: store)

    execute_sql("SELECT 1 FROM a", source="lib")
    assert len(store.calls) == 1
    assert store.calls[0]["via"] == "Z"


# ============ oracle 分支 (多源配置化, 2026-09-24 spec) ============

import base64
import decimal
from types import SimpleNamespace

import oracledb  # 只用异常类, 不连库

from agent.tools import db_adapters
from agent.tools import sql_exec


class FakeOracleCursor:
    """模拟 oracledb cursor: 默认 tuple 行 + description, 支持 rowfactory。"""

    def __init__(self, rows, cols):
        self._rows = rows
        self.description = [(c, None, None, None, None, None, None) for c in cols]
        self.rowfactory = None
        self.executed = None

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchall(self):
        if self.rowfactory is not None:
            return [self.rowfactory(*r) for r in self._rows]
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOracleConn:
    """cursor() 恒返同一实例 — 测试要断言 executed, 每次新实例会拿不到。"""

    def __init__(self, rows, cols):
        self.cursor_obj = FakeOracleCursor(rows, cols)

    def cursor(self):
        return self.cursor_obj


def _oracle_cfg():
    return SimpleNamespace(host="h", port=1521, user="crm", password="p",
                           dbname="svc", dialect="oracle")


def test_oracle_行转字典_Decimal和bytes转字符串():
    """oracledb 默认 tuple 行 + NUMBER 出 Decimal + RAW/BLOB 出 bytes —
    三者都会让下游 json.dumps 炸。bytes 转 base64 (无损可序列化)。"""
    conn = FakeOracleConn(
        [("v1", decimal.Decimal("1.5"), b"\x01")], ["COL_A", "COL_B", "COL_C"])
    sql_exec.set_connections({"o": conn})
    sql_exec.set_source_dialects({"o": "oracle"})
    sql_exec.set_source_configs({"o": _oracle_cfg()})
    res = sql_exec.execute_sql("SELECT COL_A, COL_B, COL_C FROM T", source="o")
    assert res.get("rows") == [
        {"COL_A": "v1", "COL_B": "1.5", "COL_C": base64.b64encode(b"\x01").decode()}
    ], f"tuple 行没转字典或 Decimal/bytes 没转: {res}"


def test_oracle_取列走ALL_TAB_COLUMNS_命名绑定():
    conn = FakeOracleConn([("COL_A",), ("COL_B",)], ["COLUMN_NAME"])
    sql_exec.set_connections({"o": conn})
    sql_exec.set_source_dialects({"o": "oracle"})
    sql_exec.set_source_configs({"o": _oracle_cfg()})
    cols = sql_exec.list_table_columns("t_order", "o")
    assert cols == {"COL_A", "COL_B"}
    sql, params = conn.cursor().executed
    assert "ALL_TAB_COLUMNS" in sql
    # OWNER=登录用户大写 (Oracle 元数据区分大小写), 表名大写
    assert params == {"owner": "CRM", "tname": "T_ORDER"}


def test_oracle_取列无源配置_响亮报错():
    """cfg 未注入 (旧式手动 set_connections 不走 bootstrap) → ValueError,
    不许静默 OWNER='' 查无行返回空集合 — 那会把"没配置"伪装成"表不存在",
    列校验系统性偏空还无日志 (计划评审发现 11, 项目头号失败模式 silent
    plausible error)。"""
    import pytest
    conn = FakeOracleConn([], [])
    sql_exec.set_connections({"o": conn})
    sql_exec.set_source_dialects({"o": "oracle"})
    sql_exec.set_source_configs({})  # 故意不注入 o 的 cfg
    with pytest.raises(ValueError, match="set_source_configs"):
        sql_exec.list_table_columns("T", "o")


def test_oracle_连接失效用配置重建并换池(monkeypatch):
    class DeadCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            raise oracledb.OperationalError("ORA-03113: connection lost")

    class DeadConn:
        def cursor(self):
            return DeadCursor()

    good = FakeOracleConn([("v",)], ["A"])
    cfg = _oracle_cfg()
    sql_exec.set_connections({"o": DeadConn()})
    sql_exec.set_source_dialects({"o": "oracle"})
    sql_exec.set_source_configs({"o": cfg})

    rebuilt = []
    monkeypatch.setattr(db_adapters, "connect_source",
                        lambda c: rebuilt.append(c) or good)

    res = sql_exec.execute_sql("SELECT A FROM T", source="o")
    assert res.get("rows") == [{"A": "v"}], f"oracle 重连失败: {res}"
    assert rebuilt == [cfg], "没用启动期注入的源配置重建连接"
    assert sql_exec._connections["o"] is good, "重建的新连接没换进池, 死连接会留池"


def test_oracle_SQL错误走fatal分支回错误不重连():
    class SqlErrCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            raise oracledb.ProgrammingError("ORA-00904: invalid identifier")

    class Conn:
        def cursor(self):
            return SqlErrCursor()

    sql_exec.set_connections({"o": Conn()})
    sql_exec.set_source_dialects({"o": "oracle"})
    sql_exec.set_source_configs({"o": _oracle_cfg()})
    res = sql_exec.execute_sql("SELECT NOPE FROM T", source="o")
    assert "error" in res and "ProgrammingError" in res["error"]
