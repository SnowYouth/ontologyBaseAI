"""test_db_adapters.py — 方言分派纯函数测试 (不连真库)。

cfg 用 SimpleNamespace duck-typing (db_adapters 不 import DbSourceConfig,
避免 core.config 的 prompt 加载链)。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pymysql

from agent.tools import db_adapters


def _cfg(dialect="mysql", **kw):
    # 默认值放 dict 里让 kw 覆盖: 直接当关键字参数写会和 kw 里的同名键
    # 撞 "got multiple values for keyword argument"
    fields = {"host": "h", "port": 3306, "user": "u", "password": "p",
              "dbname": "d", "dialect": dialect}
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_connect_mysql_全参数():
    with patch("pymysql.connect") as fake:
        db_adapters.connect_source(_cfg())
    fake.assert_called_once_with(
        host="h", port=3306, user="u", password="p", database="d",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5)


def test_connect_oracle_service_name():
    import oracledb
    with patch("oracledb.connect") as fake:
        db_adapters.connect_source(_cfg(dialect="oracle", port=1521, dbname="ORCLPDB1"))
    fake.assert_called_once_with(user="u", password="p", host="h", port=1521,
                                 service_name="ORCLPDB1", tcp_connect_timeout=5)
    # fetch_lobs=False: CLOB→str, LOB 定位对象 json 不认 (计划评审发现 7)
    assert oracledb.defaults.fetch_lobs is False


def test_connect_未知方言炸():
    import pytest
    with pytest.raises(ValueError, match="方言"):
        db_adapters.connect_source(_cfg(dialect="postgres"))


def test_columns_sql_mysql():
    sql, style = db_adapters.columns_sql("mysql")
    assert "information_schema.COLUMNS" in sql and style == "seq"


def test_columns_sql_oracle_命名绑定():
    sql, style = db_adapters.columns_sql("oracle")
    assert "ALL_TAB_COLUMNS" in sql and ":owner" in sql and ":tname" in sql
    assert style == "named"


def test_columns_sql_未知方言炸():
    import pytest
    with pytest.raises(NotImplementedError):
        db_adapters.columns_sql("postgres")


def test_错误类_mysql():
    assert db_adapters.reconnectable_errors("mysql") == (
        pymysql.err.OperationalError, pymysql.err.InterfaceError)
    assert db_adapters.fatal_errors("mysql") == (pymysql.Error,)


def test_错误类_oracle():
    import oracledb
    assert db_adapters.reconnectable_errors("oracle") == (
        oracledb.InterfaceError, oracledb.OperationalError)
    assert db_adapters.fatal_errors("oracle") == (oracledb.Error,)


def test_revive_mysql_ping原地复活():
    conn = MagicMock()
    out = db_adapters.revive_connection("lib", conn, _cfg())
    assert out is conn
    conn.ping.assert_called_once_with(reconnect=True)


def test_revive_oracle_重建并回调换池():
    cfg = _cfg(dialect="oracle")
    rebound = []
    with patch("agent.tools.db_adapters.connect_source") as fake:
        fake.return_value = "NEW_CONN"
        out = db_adapters.revive_connection("o", "DEAD", cfg,
                                            on_rebound=lambda s, c: rebound.append((s, c)))
    assert out == "NEW_CONN"
    assert rebound == [("o", "NEW_CONN")]


def test_prepare_cursor_oracle设rowfactory():
    cur = MagicMock()
    cur.description = [("COL_A",), ("COL_B",)]
    db_adapters.prepare_cursor(cur, "oracle")
    row = cur.rowfactory("v1", "v2")
    assert row == {"COL_A": "v1", "COL_B": "v2"}


def test_prepare_cursor_mysql_noop():
    class C:
        pass

    cur = C()
    db_adapters.prepare_cursor(cur, "mysql")
    assert not hasattr(cur, "rowfactory"), "mysql 分支不该设置 rowfactory"
