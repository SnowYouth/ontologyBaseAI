import pytest
from capabilities.obqc.fingerprint import compute_sql_fingerprint

def test_fingerprint_相同SQL返回相同哈希():
    sql1 = "SELECT a FROM t WHERE x = 1"
    sql2 = "SELECT a FROM t WHERE x = 1"
    assert compute_sql_fingerprint(sql1, "mysql") == compute_sql_fingerprint(sql2, "mysql")

def test_fingerprint_别名不影响指纹():
    """别名改变不影响指纹 (spec §3.6: 避免 LLM 换别名绕过熔断)"""
    sql1 = "SELECT t1.a FROM my_table t1 WHERE t1.x = 1"
    sql2 = "SELECT t2.a FROM my_table t2 WHERE t2.x = 1"
    assert compute_sql_fingerprint(sql1, "mysql") == compute_sql_fingerprint(sql2, "mysql")

def test_fingerprint_字面量值影响指纹():
    """字面量值改变影响指纹 (spec §3.6: WHERE col=1 vs col=2 是不同查询)"""
    sql1 = "SELECT a FROM t WHERE x = 1"
    sql2 = "SELECT a FROM t WHERE x = 2"
    assert compute_sql_fingerprint(sql1, "mysql") != compute_sql_fingerprint(sql2, "mysql")

def test_fingerprint_空格不影响():
    sql1 = "SELECT a FROM t WHERE x=1"
    sql2 = "SELECT  a  FROM  t  WHERE  x = 1"
    assert compute_sql_fingerprint(sql1, "mysql") == compute_sql_fingerprint(sql2, "mysql")

def test_fingerprint_列顺序不影响():
    """SELECT 列顺序改变不影响 (spec §3.6: 无序集合)"""
    sql1 = "SELECT a, b FROM t"
    sql2 = "SELECT b, a FROM t"
    assert compute_sql_fingerprint(sql1, "mysql") == compute_sql_fingerprint(sql2, "mysql")

def test_fingerprint_JOIN顺序不影响():
    """JOIN 表顺序改变不影响 (spec §3.6: 无序列对集合)"""
    sql1 = "SELECT * FROM t1 JOIN t2 ON t1.x=t2.y"
    sql2 = "SELECT * FROM t2 JOIN t1 ON t2.y=t1.x"
    assert compute_sql_fingerprint(sql1, "mysql") == compute_sql_fingerprint(sql2, "mysql")

def test_fingerprint_解析失败返回None():
    """SQL 解析失败 → 返回 None (调用方跳过熔断)"""
    assert compute_sql_fingerprint("SELECT FROM )))", "mysql") is None
