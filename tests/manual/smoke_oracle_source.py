"""smoke_oracle_source.py — Oracle 数据源端到端 smoke (真库, 手动跑)。

多源配置化 (2026-09-24) 的 Oracle 非真库不算数验收: 连接 → 取列
(ALL_TAB_COLUMNS) → OBQC oracle 方言解析 → 真 SELECT, 全走生产同一条代码路径。

用法:
  1. .env 里临时加一个 oracle 源, 如:
       DB_SOURCES=lib,lib2,oratest
       DB_ORATEST_HOST=<你的 Oracle 主机>
       DB_ORATEST_PORT=1521
       DB_ORATEST_USER=<只读账号>
       DB_ORATEST_PASSWORD=<密码>
       DB_ORATEST_NAME=<service_name>
       DB_ORATEST_TYPE=oracle
  2. uv run --frozen python tests/manual/smoke_oracle_source.py oratest
  3. 验完还原 .env (DB_SOURCES 去掉 oratest, 删 DB_ORATEST_*)

退出码: 0=全链路 OK / 1=某步失败 / 2=前置条件不满足 (源没配/账号下无表) /
3=数据路径通但 OBQC 这步没验到 (本体没挂上, 不算全链路 OK)。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# 直接 python 跑本脚本时只有 tests/manual/ 进 sys.path, 项目根不在 → import agent 炸。
# 与同目录其它 manual 脚本同一处置 (如 smoke_obda_cq.py:19)。
sys.path.insert(0, os.path.abspath(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from agent.tools import db_adapters  # noqa: E402
from agent.tools.sql_exec import (  # noqa: E402
    execute_sql, list_table_columns, set_connections, set_source_configs,
    set_source_dialects,
)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "oratest"
    from core.config import settings
    cfg = settings.db_sources.get(name)
    if cfg is None or cfg.dialect != "oracle":
        print(f"源 '{name}' 不存在或不是 oracle; 当前源: {list(settings.db_sources)}")
        return 2

    # [1/4] 连接 (走 db_adapters, 生产同路径)
    conn = db_adapters.connect_source(cfg)
    print(f"[1/4] 连接 OK: {cfg.user}@{cfg.host}:{cfg.port}/{cfg.dbname}")

    # 接线进 sql_exec 模块态 (与 bootstrap 等价的手动版)
    set_connections({name: conn})
    set_source_configs({name: cfg})
    set_source_dialects({name: cfg.dialect})

    # OBQC 要本体: 挂真本体 (表不在本体 → 校验降级放行, 但 oracle 方言解析
    # 链路真实走过)。挂不上 → ontology_ok=False, 末尾如实报 "OBQC 未验" 并
    # exit 3 — 不许静默当全链路通过 (计划评审发现 4: 原稿 import 错入口,
    # 异常被 except 吞掉后脚本照样打 "全链路 OK", 正是 "全绿但从没测它声称
    # 测的" 同类陷阱)。
    # 判据取 get_ontology() 的返回值而非 "set_ontology 没抛": execute_sql 内部
    # 就是靠 get_ontology() 取单例, 只有它非 None 才证明 OBQC 真有本体可用。
    ontology_ok = False
    try:
        from core.ontology_view import get_ontology, set_ontology
        set_ontology(settings.obda_ontology_path)
        ontology_ok = get_ontology() is not None
        if ontology_ok:
            print(f"      (本体已挂: {settings.obda_ontology_path})")
        else:
            print("      ✗ set_ontology 没抛但 get_ontology() 仍为 None — OBQC 这步没验到")
    except Exception as e:
        print(f"      ✗ 本体不可用 ({type(e).__name__}: {e}) — OBQC oracle 方言这步没验到")

    # [2/4] 该账号可见的表
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM USER_TABLES ORDER BY TABLE_NAME")
        db_adapters.prepare_cursor(cur, cfg.dialect)
        tables = [r["TABLE_NAME"] for r in cur.fetchall()]
    print(f"[2/4] USER_TABLES 共 {len(tables)} 张, 采样: {tables[:5]}")
    if not tables:
        print("      该账号下无表, 后续步骤无靶子; 换个账号或先建表")
        return 2

    # [3/4] 取列 (sql_exec 的方言分派, 生产同路径)
    table = tables[0]
    cols = list_table_columns(table, name)
    print(f"[3/4] list_table_columns({table}) -> {len(cols)} 列, 采样: {sorted(cols)[:5]}")
    if not cols:
        print("      取列空集合, ALL_TAB_COLUMNS 分支可疑")
        return 1

    # [4/4] execute_sql 全链路 (OBQC oracle 方言解析 + rowfactory + Decimal)
    # 选列避开 LOB/二进制 (CLOB 定位对象/RAW 会把验收引向 "驱动 bug" 的假
    # 失败 — 计划评审发现 7): 只挑 VARCHAR2/NUMBER/DATE 型列
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = :o AND TABLE_NAME = :t "
            "AND DATA_TYPE IN ('VARCHAR2','NVARCHAR2','NUMBER','DATE') "
            "AND ROWNUM <= 1",
            {"o": cfg.user.upper(), "t": table})
        db_adapters.prepare_cursor(cur, cfg.dialect)
        r = cur.fetchone()
    if r is None:
        print("[4/4] 该表没有 VARCHAR2/NUMBER/DATE 型列, 无安全靶子")
        return 1
    col = r["COLUMN_NAME"]
    sql = f"SELECT {col} FROM {table} WHERE ROWNUM <= 3"
    res = execute_sql(sql, source=name)
    if "error" in res or "blocked" in res:
        print(f"[4/4] ✗ {res}")
        return 1
    print(f"[4/4] execute_sql OK ({col}): {res['n']} 行, "
          f"首行: {res['rows'][0] if res['rows'] else '(空)'}")
    if not ontology_ok:
        print("⚠ OBQC oracle 方言一步未验 (本体没挂上) — 不算全链路 OK")
        return 3
    print("全链路 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
