"""test_bootstrap.py — init_agent_runtime 接线测试.

副作用重函数不测真副作用 (真连库/建向量库), 只 mock 掉重依赖,
验证「该注入的都注入了、该拼的 prompt 拼了」——即接线没漏。
多源配置化 (2026-09-24): 连接构建走 db_adapters, 源清单来自 settings.db_sources,
mock 目标从 pymysql 换成 db_adapters.connect_source。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _cfg(host="10.0.0.1", dialect="mysql"):
    return SimpleNamespace(host=host, port=3306, user="u", password="p",
                           dbname="d", dialect=dialect)


def _run(monkeypatch, db_sources, connect_behaviors=None, capsys=None):
    """跑 init_agent_runtime, 返回记录到的注入产物。

    connect_behaviors: {host: 返回值或 Exception}, 决定 connect_source 对各源的行为;
    缺省全部返回 MagicMock。同时记录每次 connect 的 host 进 connect_hosts。
    """
    from core.config import settings
    monkeypatch.setattr(settings, "db_sources", db_sources)
    # connect_hosts 预置空 list: "压根没连" 的用例要断言它 == [], 不能靠
    # fake_connect 里的 setdefault 建键 (没连过就根本没这个键 → KeyError 而非断言失败)
    calls: dict = {"connect_hosts": []}
    fake_store = MagicMock(name="HybridRetriever")
    fake_onto = MagicMock(name="ontology")
    fake_onto.entities = {}
    behaviors = connect_behaviors or {}

    def fake_connect(cfg):
        calls.setdefault("connect_hosts", []).append(cfg.host)
        r = behaviors.get(cfg.host, MagicMock(name=f"conn-{cfg.host}"))
        if isinstance(r, Exception):
            raise r
        return r

    with patch("core.runtime.bootstrap.VectorStore"), \
         patch("core.runtime.bootstrap.HybridRetriever", return_value=fake_store), \
         patch("core.runtime.bootstrap._load_reranker_if_enabled", return_value=None), \
         patch("core.runtime.bootstrap.db_adapters") as fake_adapters, \
         patch("core.runtime.bootstrap.get_ontology", return_value=fake_onto), \
         patch("core.runtime.bootstrap.build_catalog_text", return_value="CATALOG"), \
         patch("core.runtime.bootstrap.set_retriever",
               side_effect=lambda s: calls.__setitem__("retriever", s)), \
         patch("core.runtime.bootstrap.set_ontology",
               side_effect=lambda p: calls.__setitem__("ontology", p)), \
         patch("core.runtime.bootstrap.set_sensitive_cols",
               side_effect=lambda c: calls.__setitem__("sensitive", c)), \
         patch("core.runtime.bootstrap.set_connections",
               side_effect=lambda c: calls.__setitem__("connections", c)), \
         patch("core.runtime.bootstrap.set_source_configs",
               side_effect=lambda c: calls.__setitem__("source_configs", c)), \
         patch("core.runtime.bootstrap.set_source_dialects",
               side_effect=lambda d: calls.__setitem__("dialects", d)):
        fake_adapters.connect_source.side_effect = fake_connect
        from core.runtime.bootstrap import init_agent_runtime
        calls["result"] = init_agent_runtime()

    return calls


def test_接线完整_retriever与本体与脱敏列都注入(monkeypatch):
    """init_agent_runtime 的非数据源三处注入也要守住: retriever / 本体 / 脱敏列。

    这三处是 CLI (main.py) 与 Web (web/app.py) 共用的启动期接线, 漏任一处
    都不会有测试报警 — 多源配置化改写本文件时就漏掉过 (2026-10-13 补回)。
    """
    calls = _run(monkeypatch, {"lib": _cfg(host="h-lib")})
    # 返回值就是注入给 agent 的那个 retriever (同一个对象, 不是另建的)
    assert calls["retriever"] is calls["result"]
    from core.config import settings
    assert calls["ontology"] == settings.obda_ontology_path
    assert calls["sensitive"] == set()  # fake 本体 entities 为空 → 空 sensitive 集


def test_多源全配_逐源建连登记(monkeypatch):
    calls = _run(monkeypatch, {
        "lib": _cfg(host="h-lib"),
        "crm": _cfg(host="h-crm", dialect="oracle"),
    })
    assert calls["connect_hosts"] == ["h-lib", "h-crm"]
    assert set(calls["connections"]) == {"lib", "crm"}
    # dialects 只含成功建连的源, 值来自 config
    assert calls["dialects"] == {"lib": "mysql", "crm": "oracle"}
    # 源配置也要注入 (oracle 重连重建用)
    assert calls["source_configs"]["crm"].dialect == "oracle"


def test_源host为空_压根不连库(monkeypatch, capsys):
    """host 空 → 不调 connect_source, 打"未配置"而非"连接失败"。

    pymysql 对空 host/user 会回落 localhost + 当前 OS 用户名连本机,
    警告变 "Access denied for user '<个人用户名>'@'localhost'" —
    既误导排障又泄漏本机用户信息 (2026-09-24 冷启动取证)。
    """
    calls = _run(monkeypatch, {"lib": _cfg(host="")}, capsys=capsys)
    assert calls["connect_hosts"] == [], "host 为空还去连库"
    assert "lib" not in calls["connections"]
    assert "lib" not in calls["dialects"]
    err = capsys.readouterr().err
    assert "未配置" in err
    assert "localhost" not in err and "Access denied" not in err


def test_单源连接失败_降级不崩(monkeypatch):
    """连不上要降级, 不能让 CLI 整个起不来 (冷启动第一分钟就放弃)。"""
    import pymysql as real_pymysql
    behaviors = {"h-lib": real_pymysql.err.OperationalError(2003, "Can't connect")}
    calls = _run(monkeypatch, {"lib": _cfg(host="h-lib"),
                               "crm": _cfg(host="h-crm", dialect="oracle")},
                 connect_behaviors=behaviors)
    assert calls["result"] is not None, "单源失败就崩 → CLI 起不来"
    assert set(calls["connections"]) == {"crm"}, "失败源不能留池"
    assert set(calls["dialects"]) == {"crm"}, "connections 与 dialects 口径要一致"


def test_连接失败是中文占位UnicodeEncodeError_也降级(monkeypatch):
    """照抄 .env.example 不改 → 中文占位密码让驱动在编码阶段炸 UnicodeEncodeError,
    不是任何 DB 异常 — 这是最常见的冷启动路径, 宽 catch 必须兜住。"""
    calls = _run(monkeypatch, {"lib": _cfg(host="h-lib")},
                 connect_behaviors={"h-lib": UnicodeEncodeError(
                     "latin-1", "填密码", 0, 3, "ordinal not in range(256)")})
    assert calls["result"] is not None
    assert "lib" not in calls["connections"]


def test_零源_打总警告不连库(monkeypatch, capsys):
    calls = _run(monkeypatch, {}, capsys=capsys)
    assert calls["connect_hosts"] == []
    assert calls["connections"] == {}
    err = capsys.readouterr().err
    assert "DB_SOURCES" in err, "零源警告要告诉用户去哪配"


# ============ 自演化飞轮燃料入口接线 (spec §3 热路径) ============
# 背景: 2026-09-17 端到端取证挖出三处"定义写好、调用点漏了":
#   1. set_default_db_path 零调用者 → _default_db_path 恒 None
#      → 三处热路径埋点 (sql_exec 熔断 / agent max_iters / cq_runner cq_fail)
#        全部静默丢弃, 飞轮燃料入口整条没接线
#   2. relation_health.use_count 无人累加 (见 test_tools_sql_exec use_count 组)
#   3. rebuild_circuit_breaker_state 零调用者 → 熔断状态不跨重启
# spec §3 架构图画了"热路径 ↓ INSERT failure_signal", 图画了线没接。


def _bootstrap_with_patches(monkeypatch, extra_calls: dict):
    """跑一次 init_agent_runtime, 把自演化两处接线的入参记进 extra_calls。"""
    from core.config import settings
    monkeypatch.setattr(settings, "db_sources", {})
    fake_store = MagicMock(name="HybridRetriever")
    fake_onto = MagicMock(name="ontology")
    fake_onto.entities = {}

    with patch("core.runtime.bootstrap.VectorStore"), \
         patch("core.runtime.bootstrap.HybridRetriever", return_value=fake_store), \
         patch("core.runtime.bootstrap._load_reranker_if_enabled", return_value=None), \
         patch("core.runtime.bootstrap.db_adapters"), \
         patch("core.runtime.bootstrap.get_ontology", return_value=fake_onto), \
         patch("core.runtime.bootstrap.build_catalog_text", return_value="CATALOG"), \
         patch("core.runtime.bootstrap.set_retriever"), \
         patch("core.runtime.bootstrap.set_ontology"), \
         patch("core.runtime.bootstrap.set_sensitive_cols"), \
         patch("core.runtime.bootstrap.set_connections"), \
         patch("core.runtime.bootstrap.set_source_configs"), \
         patch("capabilities.ontology.selfevolve.signals.set_default_db_path",
               side_effect=lambda p: extra_calls.__setitem__("db_path", p)), \
         patch("capabilities.ontology.selfevolve.signals.rebuild_circuit_breaker_state",
               side_effect=lambda p: extra_calls.__setitem__("rebuild", p) or {"fp1": 2}):
        from core.runtime.bootstrap import init_agent_runtime
        init_agent_runtime()


def test_启动期接线_failure_signal默认库路径(monkeypatch):
    """set_default_db_path 必须在启动期被调, 否则三处热路径埋点全静默丢弃。"""
    calls: dict = {}
    _bootstrap_with_patches(monkeypatch, calls)

    from core.config import settings
    assert calls.get("db_path") == settings.obda_ontology_path, (
        "set_default_db_path 未接线 → _default_db_path 恒 None → "
        "failure_signal 采集静默丢弃, 飞轮没燃料")


def test_启动期接线_熔断状态跨重启重建(monkeypatch):
    """rebuild_circuit_breaker_state 必须在启动期被调并把结果灌进 sql_exec。"""
    calls: dict = {}
    _bootstrap_with_patches(monkeypatch, calls)

    from core.config import settings
    assert calls.get("rebuild") == settings.obda_ontology_path, (
        "rebuild_circuit_breaker_state 未接线 → 重启后同一条被证伪 SQL "
        "可以再撞满两次才熔断")


def test_启动期接线_熔断状态真灌进sql_exec(monkeypatch):
    """重建出的 {fingerprint: hits} 要真进 sql_exec 的内存态, 不能只算不用。"""
    from agent.tools.sql_exec import _reset_circuit_breaker, _circuit_breaker_hits
    _reset_circuit_breaker()

    calls: dict = {}
    _bootstrap_with_patches(monkeypatch, calls)

    from agent.tools.sql_exec import _circuit_breaker_hits as hits_after
    assert hits_after.get("fp1") == 2, (
        "重建的熔断状态没灌进 _circuit_breaker_hits")
    _reset_circuit_breaker()


def test_启动期接线_熔断状态是替换而非合并(monkeypatch):
    """"从 failure_signal 重建状态"语义是替换, 不是往残留状态上合并。

    2026-09-18: bootstrap 原先绕过 set_circuit_breaker_state, 直接
    `from tools.sql_exec import _circuit_breaker_hits` 再 `.update()` ——
    合并语义。生产只在启动期跑一次 (dict 恒空) 所以现象没暴露, 但:
    ① 语义错 —— 重建就该是"以库为准", 陈旧指纹必须消失, 否则内存态成了
       "库里的 ∪ 上次残留的", 熔断阈值判定吃到不存在于任何 failure_signal
       的计数;
    ② 耦合私有模块变量, 绕过了为此写好却零调用者的公开注入口。
    """
    from agent.tools.sql_exec import _reset_circuit_breaker, set_circuit_breaker_state
    _reset_circuit_breaker()
    # 预置一个"上次残留"的指纹 (库里重建不出它)
    set_circuit_breaker_state({"stale_fp": 9})

    calls: dict = {}
    _bootstrap_with_patches(monkeypatch, calls)  # rebuild 桩固定返回 {"fp1": 2}

    from agent.tools.sql_exec import _circuit_breaker_hits as hits_after
    assert hits_after.get("fp1") == 2, "重建结果没进内存态"
    assert "stale_fp" not in hits_after, (
        "熔断状态重建是合并而非替换 → 陈旧指纹残留在内存态, "
        "熔断判定会吃到 failure_signal 里并不存在的计数")
    _reset_circuit_breaker()
