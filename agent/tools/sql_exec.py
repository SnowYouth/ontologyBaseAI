"""agent/tools/sql_exec.py — 把只读 SQL 执行包成 agent 工具 (阶段 6 OBDA).

设计:
1. 模块级 _conn 变量, main.py 启动期 set_db_conn(conn) 注入 (同 rag_query._retriever 模式)
2. 只读护栏 _is_select: 判断 SQL 语句"首关键字"是不是 SELECT, 不做整句关键字
   包含检查 —— 防止 `SELECT UPDATE_DATE FROM x` 这类字段名撞词被误杀
3. 敏感字段值: 查询结果 rows 原样返回 (号码等本就是查询目的), 只在
   log_event 落盘前对 SQL 文本里的敏感列字面量打码 (Task 6 补全 _mask)
4. 连接失效 (OperationalError) 自动 reconnect 一次再重跑
5. rows 里 datetime/date 值转字符串 (下游 agent 层 json.dumps 不认 datetime 对象)
"""
import base64
import datetime
import decimal
import re

import pymysql

from core.observability import log_event
from agent.tools.base import register_tool
from agent.tools import db_adapters

_conn = None  # 向后兼容保留, 实际从 _connections["lib"] 取
_connections = {}  # 多源连接池: {"lib": conn1, "lib2": conn2, ...}
_source_dialects = {}  # 多源方言映射: {"lib": "mysql", "lib2": "mysql", ...}
_source_configs = {}  # 源名 → DbSourceConfig (oracle 重连重建用, bootstrap 注入)


def set_source_dialects(dialects: dict) -> None:
    """main.py/bootstrap 启动期调一次, 注入每源方言 {"lib": "mysql", ...}."""
    global _source_dialects
    _source_dialects = dialects


def get_source_dialect(source: str) -> str:
    """取某源方言; 未注册 → 回退 mysql (fail-safe, 老配置无 DB_TYPE 也当 mysql)."""
    return _source_dialects.get(source, "mysql")

_WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
    "TRUNCATE", "CREATE", "REPLACE", "GRANT", "MERGE",
}


def set_db_conn(conn) -> None:
    """main.py 启动期调一次, 把 pymysql connection 实例注入 (向后兼容)."""
    global _conn
    _conn = conn


def set_connections(connections: dict) -> None:
    """main.py 启动期调一次, 注入多源连接池字典 {"lib": conn1, "lib2": conn2}."""
    global _connections
    _connections = connections


def set_source_configs(cfgs: dict) -> None:
    """bootstrap 启动期调一次, 注入源配置 {源名: DbSourceConfig}。

    oracle 重连要拿凭据重建连接 (oracle 没有 ping(reconnect=True)),
    只 set_connections 不带凭据, 死了救不活。
    """
    global _source_configs
    _source_configs = cfgs


def _is_select(sql: str) -> bool:
    """判断 sql 是否为单条只读 SELECT 语句.

    只看语句最开头的第一个 token, 不管句子中间出现什么字段名/别名
    (UPDATE_DATE / DELETE_FLAG 这类字段名完全不受影响)。
    拒绝分号拼接多语句 (防 `SELECT 1; DROP TABLE x` 夹带)。
    """
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return False
    m = re.match(r"^\s*(\w+)", stripped)
    if not m:
        return False
    return m.group(1).upper() == "SELECT"


# 敏感列名集合: 启动期由 main.py 从本体扫描 sensitive:true 字段名注入 (Task 8 接线)
_sensitive_cols: set = set()

# I1 熔断: session 级状态 (回路层, 不进 obqc_check 纯函数判定核心, spec §2.7/§3.6)
_circuit_breaker_hits: dict = {}  # {fingerprint: hit_count}

# 自演化燃料去重: 本进程已采集过 failure_signal 的指纹 (2026-09-17 决策 A)
# no_edge 首次硬拦即采集, 靠这个集合防同一指纹重复攒信号。
_fuel_collected: set = set()


def _reset_circuit_breaker():
    """测试用: 清空熔断状态 (含燃料采集去重集)。"""
    global _circuit_breaker_hits, _fuel_collected
    _circuit_breaker_hits = {}
    _fuel_collected = set()


def set_circuit_breaker_state(state: dict) -> None:
    """注入熔断状态（启动期调用）。"""
    global _circuit_breaker_hits
    _circuit_breaker_hits = state


def _collect_fuel(fingerprint: str, verdict: dict, source: str) -> None:
    """自演化燃料采集 (热路径, 异常绝不外抛)。

    no_edge 首次即采; 其余违规类型仍等熔断阈值 2。理由见调用点注释。
    """
    try:
        from capabilities.ontology.selfevolve.signals import (
            log_failure_signal, extract_no_edge_signals)
        no_edge_signals = extract_no_edge_signals(verdict)
        has_no_edge = any(v.get("type") == "no_edge"
                          for v in verdict.get("violations") or [])
        hit_count = _circuit_breaker_hits.get(fingerprint, 1)
        if not has_no_edge and hit_count < 2:
            return
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": fingerprint,
            "violations": verdict["violations"],
            "hit_count": hit_count,
            # 冷路径燃料 (spec §3.5): entity 对 + AI 首选列对
            "no_edge_signals": no_edge_signals,
        }, source=source)
        _fuel_collected.add(fingerprint)
    except Exception:
        pass  # 热路径: 埋点失败绝不影响查询


def set_sensitive_cols(cols: set) -> None:
    """main.py 启动期调一次, 把本体里 sensitive:true 的列名集合注入 (打码日志用)."""
    global _sensitive_cols
    _sensitive_cols = cols


def _mask(sql: str) -> str:
    """把 sql 文本中敏感列名后面的字符串字面量替换成 ***.

    覆盖 `COL = 'value'` / `COL LIKE '%value%'` / `COL IN ('v1', 'v2')` 三种
    常见写法 (LIKE/IN 之前只打了等号这一种, 客户姓名走模糊查询会明文漏进日志)。
    结构(表名/列名/操作符本身)保留方便排查, 只吃掉引号内的值。
    """
    masked = sql
    for col in _sensitive_cols:
        masked = re.sub(
            rf"({col}\s*(?:=|LIKE|IN)\s*)((?:['\"][^'\"]*['\"]\s*,?\s*)|(?:\([^)]*\)))",
            lambda m: m.group(1) + re.sub(r"['\"][^'\"]*['\"]", "'***'", m.group(2)),
            masked,
            flags=re.IGNORECASE,
        )
    return masked


@register_tool(category="serving")
def execute_sql(sql: str, entity: str = None, source: str = None) -> dict:
    """对指定数据源执行 SELECT 查询, 返回结果行。

    仅允许 SELECT; 非查询语句会被拒绝。执行失败会返回具体报错信息,
    可根据报错修正 SQL 后重试 (如列名/表名拼错)。

    **自动路由（推荐）**:
    - 传 `entity` 参数, 工具自动从本体查该实体的 data_source 并路由
    - 示例: `execute_sql("SELECT * FROM lib_reader WHERE ...", entity="lib_reader")`
      → 工具自动查到 lib_reader.data_source=lib2, 路由到 lib2 库
    - **agent 无需记忆表在哪个库**, 避免试错浪费迭代

    **手动指定（调试/特殊场景）**:
    - 传 `source` 参数手动指定数据源 (优先级高于 entity)
    - 示例: `execute_sql("SELECT * FROM lib_reader", source="lib2")`

    **跨库两段式查询示例** (lib_reader[lib2] → lib_loan[lib]):
    1. 查源库: execute_sql("SELECT READER_ID FROM lib_reader WHERE READER_NAME='R0042'", entity="lib_reader")
       → 工具自动路由到 lib2, 返回 {"rows": [{"READER_ID": "12345"}], "n": 1}
    2. 用键值查目标库: execute_sql("SELECT CARD_NO FROM lib_loan WHERE READER_ID='12345'", entity="lib_loan")
       → 工具自动路由到 lib, 返回 {"rows": [{"CARD_NO": "13800001234"}], "n": 1}

    Args:
        sql: 一条 SELECT SQL 语句 (不带库名前缀)
        entity: 实体名 (如 "lib_reader"), 工具自动查本体路由到对应数据库
        source: 手动指定数据源 (如 "lib2"), 优先级高于 entity
    """
    # 1. 自动推断 source (如果未手动指定)
    if not source and entity:
        from core.ontology_view import get_ontology
        onto = get_ontology()
        if onto and entity in onto.entities:
            source = onto.entities[entity].get('data_source')
            if not source:
                return {"error": f"entity '{entity}' has no data_source field"}
        else:
            return {"error": f"entity '{entity}' not found in ontology"}

    # 2. 必须有明确的 source (不再默认回退)
    if not source:
        return {"error": "Missing required parameter: must provide either 'entity' or 'source'"}

    # 3. 多源模式: 优先从 _connections 池取; 单源兼容模式: 回退到全局 _conn
    conn = _connections.get(source) if _connections else _conn

    if conn is None:
        if _connections and source not in _connections:
            return {"error": f"data source '{source}' not found"}
        return {"error": "db not initialized"}

    if not _is_select(sql):
        return {"error": "只读模式，仅允许 SELECT 语句"}

    # OBQC 守门 (spec §3.1): 抽取 + ①JOIN 硬校验。hard_block 不碰库, 回违规供重写。
    from capabilities.obqc import obqc_check
    from core.ontology_view import get_ontology
    _onto = get_ontology()
    # I2: 暂存 soft_warn/pass 的 warnings (spec §2.4/§3.5: 强制上浮, 不靠 LLM 自觉)。
    # 在此初始化, 覆盖 _onto is None 路径也始终有定义。
    obqc_warnings = []
    if _onto is not None:
        verdict = obqc_check(sql, source, _onto)
        if verdict["verdict"] == "hard_block":
            # I1 熔断: 检查 AST 指纹是否第 2 次硬拦 (spec §3.6)
            from capabilities.obqc.fingerprint import compute_sql_fingerprint
            fingerprint = compute_sql_fingerprint(sql, get_source_dialect(source))
            if fingerprint:
                _circuit_breaker_hits[fingerprint] = _circuit_breaker_hits.get(fingerprint, 0) + 1
                if _circuit_breaker_hits[fingerprint] >= 2:
                    # 第 2+ 次硬拦 → 加强 hint (仍 hard_block, 不降级)
                    verdict["hints"].append({
                        "kind": "circuit_breaker",
                        "message": (
                            "你已第二次提交结构等价的被证伪 SQL (同一条边/列仍不合法)。"
                            "请换建模思路或换表，不要再提交等价查询。"
                        ),
                    })

            log_event("obqc.hard_block", {
                "sql": _mask(sql), "source": source,
                "violations": verdict["violations"], "skipped": verdict["skipped"],
                "circuit_hit": _circuit_breaker_hits.get(fingerprint, 1) if fingerprint else 1,
            })
            # ★ 自演化埋点 (2026-09-17 决策 A: no_edge 燃料脱钩熔断阈值)
            #
            # 采集条件二选一:
            #   a) 本次 hard_block 含 no_edge violation —— 首次即采。
            #      真实日志取证: no_edge 硬拦每次 circuit_hit 都是 1,
            #      因为 hints 会教育 agent 换路 (护栏按设计工作), 第 2 次永不到来。
            #      等熔断阈值 = 用防重复的机制喂只吃重复的闸门 = 燃料恒为零。
            #   b) 非 no_edge 违规撞满熔断阈值 2 —— 老规矩不变, 挡噪音。
            # 同一指纹只采一次 (_fuel_collected), 冷路径另有探针/去重/墓碑三道闸。
            if fingerprint and fingerprint not in _fuel_collected:
                _collect_fuel(fingerprint, verdict, source)
            return {"blocked": True, "violations": verdict["violations"],
                    "hints": verdict["hints"], "skipped": verdict["skipped"]}
        if verdict["skipped"]:
            log_event("obqc.skipped", {"source": source, "skipped": verdict["skipped"]})
        # I2: 暂存 warnings (soft_warn 或 pass 都可能有; hard_block 已在上面 return)
        obqc_warnings = verdict.get("warnings", [])

    dialect = get_source_dialect(source)
    masked_sql = _mask(sql)
    try:
        rows = _run_query(sql, conn, dialect)
    except db_adapters.reconnectable_errors(dialect):
        # 连接失效要复活再重跑: mysql 靠 ping(reconnect=True) 原地复活;
        # oracle 没有 ping, 用启动期注入的源配置重建连接并换掉池里的死 conn
        # (死连接留池 → 后续每次查询都重复报同一个错, 真库 smoke 复现过死循环)。
        try:
            conn = db_adapters.revive_connection(
                source, conn, _source_configs.get(source),
                on_rebound=lambda s, c: _connections.__setitem__(s, c))
            rows = _run_query(sql, conn, dialect)
        except Exception as e2:
            log_event("sql.error", {"sql": masked_sql, "error": str(e2)[:200], "source": source})
            return {"error": f"{type(e2).__name__}: {e2}"}
    except db_adapters.fatal_errors(dialect) as e:
        log_event("sql.error", {"sql": masked_sql, "error": str(e)[:200], "source": source})
        return {"error": f"{type(e).__name__}: {e}"}

    log_event("sql.query", {"sql": masked_sql, "n_rows": len(rows), "source": source})
    result = {"rows": rows, "n": len(rows)}
    # I2 强制上浮 warnings (spec §2.4: 代码插桩, 不靠 LLM 自觉带出)。
    # pass 且无 warnings → 不加字段。
    if obqc_warnings:
        result["warnings"] = obqc_warnings
    # ★ 自演化: used_edges 透传 (spec §3.2, cq_runner 消费)
    if _onto is not None and verdict.get("used_edges"):
        result["_used_edges"] = verdict["used_edges"]
        _bump_use_count(verdict["used_edges"])
    return result


def _get_health_store():
    """取 relation_health 写入口 (单测 monkeypatch 此函数注入假 store)。

    复用 selfevolve 的默认 DB 路径, 不自带配置。未配置 → None (静默跳过)。
    """
    from capabilities.ontology.selfevolve import signals
    path = getattr(signals, "_default_db_path", None)
    if not path:
        return None
    from capabilities.ontology.ontology_store import OntologyStore
    return OntologyStore(path)


def _bump_use_count(used_edges: list) -> None:
    """热路径: 命中的每条边 use_count += 1 (spec §3 架构图 / §5.4 分母)。

    这是 suspect_rate = suspect_count / use_count 的分母。缺了它,
    §5.4 点名的"高频主干边不被冤枉"保护失效 (裸 suspect>=N 会先淘汰
    lib_loan 那些最常用的边)。

    热路径纪律同 log_failure_signal: 异常静默, 埋点绝不影响查询。
    via 为空的边跳过 —— relation_health 主键是 (source,target,via) 三元组。
    """
    try:
        store = _get_health_store()
        if store is None:
            return
        for e in used_edges or []:
            if not e.get("via"):
                continue
            store.upsert_relation_health(
                e["source"], e["target"], e["via"], use_count_delta=1)
    except Exception:
        # 热路径: 埋点失败绝不影响查询 (同 failure_signal 的 except pass)
        log_event("relation_health.bump_failed", {"n_edges": len(used_edges or [])})


def _run_query(sql: str, conn, dialect: str) -> list[dict]:
    """内部 helper: 开 cursor 执行并取结果, 拆出来给 reconnect 分支复用.

    dialect 交给 db_adapters.prepare_cursor: oracle 在执行后设 rowfactory
    把默认 tuple 行转字典 (mysql 走 DictCursor 本来就是字典, no-op)。
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        db_adapters.prepare_cursor(cur, dialect)
        rows = cur.fetchall()
    return [_json_safe_row(row) for row in rows]


def list_table_columns(table: str, source: str) -> set:
    """取某物理表的列名集合 (校验器 table_cols 注入点)。

    走多源池 _connections[source], 方言分派见 db_adapters.columns_sql:
    mysql 走 information_schema.COLUMNS (分库中间件拒 SELECT *, 只此路
    能通, 表名参数化防注入); oracle 走 ALL_TAB_COLUMNS (OWNER=登录用户大写,
    表名大写 — Oracle 元数据区分大小写)。表不存在 → 空集合 (查无行, 调用方兜);
    但 oracle 未注入源配置 → ValueError 响亮报错, 不与"表不存在"混淆
    (计划评审发现 11)。
    """
    conn = _connections.get(source)
    if conn is None:
        raise ValueError(f"data source '{source}' not found")
    dialect = get_source_dialect(source)
    sql, bind_style = db_adapters.columns_sql(dialect)
    with conn.cursor() as cur:
        if bind_style == "named":
            cfg = _source_configs.get(source)
            if cfg is None:
                raise ValueError(
                    f"source '{source}' 未注入源配置 (set_source_configs), "
                    "oracle 取列需要登录用户做 ALL_TAB_COLUMNS.OWNER")
            cur.execute(sql, {"owner": cfg.user.upper(),
                              "tname": table.upper()})
        else:
            cur.execute(sql, (table,))
        db_adapters.prepare_cursor(cur, dialect)
        rows = cur.fetchall()
    return {row["COLUMN_NAME"] for row in rows}


def _json_safe_row(row: dict) -> dict:
    """把一行结果里的 datetime/date/Decimal/bytes 值转成可 json 序列化的类型。

    pymysql DictCursor 对 DATETIME/DATE 列返回 datetime 对象; oracledb 对
    NUMBER 列返回 decimal.Decimal、对 RAW/BLOB 列返回 bytes — json.dumps
    对这些类型都直接 TypeError。bytes 转 base64 (无损、可序列化)。
    """
    return {
        k: v.isoformat(sep=" ") if isinstance(v, datetime.datetime)
        else v.isoformat() if isinstance(v, datetime.date)
        else str(v) if isinstance(v, decimal.Decimal)
        else base64.b64encode(v).decode("ascii") if isinstance(v, bytes)
        else v
        for k, v in row.items()
    }
