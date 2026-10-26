"""agent/tools/db_adapters.py — 数据源驱动适配层 (多源配置化, 2026-09-24 spec)。

方言知识集中于此: 连接构建 / 异常分类 / 重连 / 取列 SQL / 行形态。
sql_exec 与 bootstrap 只调这里的分派函数, 不再各自 if dialect。
以后加第三方言 (如 postgres) 只改本模块。

cfg 是 duck-typing 对象 (core.config.DbSourceConfig 或测试的 SimpleNamespace),
字段: host / port / user / password / dbname / dialect。
oracle 驱动一律惰性 import: mysql-only 的启动/测试路径零开销。
"""
import pymysql

MYSQL = "mysql"
ORACLE = "oracle"


def connect_source(cfg):
    """按 cfg.dialect 建一个新连接。

    两个分支都带连接超时 (计划评审发现 8): 防火墙 DROP 型假源会阻塞到
    OS 默认 TCP 超时 (Windows ~21s+), 源一多启动像挂死, 与"连不上要降级"
    的设计意图相悖。
    """
    if cfg.dialect == MYSQL:
        return pymysql.connect(
            host=cfg.host, port=cfg.port, user=cfg.user,
            password=cfg.password, database=cfg.dbname,
            cursorclass=pymysql.cursors.DictCursor, connect_timeout=5)
    if cfg.dialect == ORACLE:
        import oracledb
        # fetch_lobs=False (进程级, 幂等): thin 默认 CLOB 返回 LOB 定位对象,
        # json.dumps 不认; 关掉后 CLOB→str (计划评审发现 7)
        oracledb.defaults.fetch_lobs = False
        return oracledb.connect(
            user=cfg.user, password=cfg.password,
            host=cfg.host, port=cfg.port, service_name=cfg.dbname,
            tcp_connect_timeout=5)
    raise ValueError(f"不支持的方言: {cfg.dialect}")


def reconnectable_errors(dialect: str) -> tuple:
    """连接失效 (可重连候选) 的异常类。

    mysql 两种形态: OperationalError(2006/2013 断线) 是"执行时发现断了",
    InterfaceError(0, '') 是"连接对象已关闭后再用" — 只抓前者会让后者落进
    fatal 分支不重连, 死连接永久留池 (真库 smoke 复现过 agent 死循环)。
    """
    if dialect == ORACLE:
        import oracledb
        return (oracledb.InterfaceError, oracledb.OperationalError)
    return (pymysql.err.OperationalError, pymysql.err.InterfaceError)


def fatal_errors(dialect: str) -> tuple:
    """执行期错误 (SQL 本身的问题, 重试无用, 直接回给 agent 修 SQL)。"""
    if dialect == ORACLE:
        import oracledb
        return (oracledb.Error,)
    return (pymysql.Error,)


def revive_connection(source: str, conn, cfg, on_rebound=None):
    """连接失效后的复活。

    mysql: ping(reconnect=True) 原地复活, 返回的还是原 conn。
    oracle: 没有 ping, 用 cfg 凭据重建; 新连接经 on_rebound(source, new_conn)
    回调给调用方换连接池 (死连接不留池)。
    oracle 且 cfg 缺失 (旧式手动注入没走 bootstrap) → 只能落回 ping,
    oracle conn 没有 ping 会 AttributeError, 由调用方的宽 catch 兜住报错。
    """
    if cfg is not None and cfg.dialect == ORACLE:
        new_conn = connect_source(cfg)
        if on_rebound:
            on_rebound(source, new_conn)
        return new_conn
    conn.ping(reconnect=True)
    return conn


def columns_sql(dialect: str) -> tuple[str, str]:
    """(取列 SQL, 绑定风格)。

    绑定风格: "seq" = 位置参数序列 (mysql 的 %s);
              "named" = 命名参数字典 (oracle 的 :owner/:tname)。
    mysql 走 information_schema.COLUMNS (分库中间件拒 SELECT *, 只此路能通);
    oracle 走 ALL_TAB_COLUMNS (OWNER 由调用方给登录用户大写)。
    """
    if dialect == MYSQL:
        return (
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            "seq",
        )
    if dialect == ORACLE:
        return (
            "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = :owner AND TABLE_NAME = :tname",
            "named",
        )
    raise NotImplementedError(f"list_table_columns 暂不支持方言: {dialect}")


def prepare_cursor(cur, dialect: str) -> None:
    """cur.execute 之后、fetchall 之前调: oracle 把默认 tuple 行转字典。

    oracle 的 rowfactory 需要 description (执行后才有), 所以分两步:
    execute → prepare_cursor → fetchall。mysql 走 DictCursor 本来就是字典, no-op。
    """
    if dialect != ORACLE:
        return
    cols = [d[0] for d in cur.description]
    cur.rowfactory = lambda *args: dict(zip(cols, args))
