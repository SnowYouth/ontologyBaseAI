"""core/runtime/bootstrap.py — agent 运行时初始化 (CLI 与 Web 共用的单一事实源).

从 main.py 抽出来: retriever + ontology + 只读库连接 + system prompt 注入。
CLI (main.py) 和 Web (web/app.py) 都调 init_agent_runtime(), 保证两条腿吃的是
同一套 agent 依赖, 改一处即两边生效 (DRY)。
"""
import sys

from core.config import settings
from core.observability import log_event
from core.ontology_view import build_catalog_text, get_ontology
from capabilities.rag.store import VectorStore
from capabilities.rag.retriever import HybridRetriever
from agent.tools import (
    db_adapters,
    set_connections,
    set_source_configs,
    set_source_dialects,
    set_ontology,
    set_retriever,
    set_sensitive_cols,
)
from capabilities.obqc.cache import PhysicalColumnCache, set_physical_col_cache


def _load_reranker_if_enabled():
    """按配置决定是否加载 reranker; 失败回退纯 Hybrid (启动期宽 catch, 不让崩)."""
    if not settings.rag_use_reranker:
        return None
    try:
        from capabilities.rag.reranker import Reranker
        return Reranker()
    except Exception as e:
        print(
            f"⚠️  Reranker 加载失败 ({type(e).__name__}): {e}\n"
            f"   回退到纯 Hybrid 检索, 不影响主流程",
            file=sys.stderr,
        )
        log_event("reranker.load_failed", {
            "error_type": type(e).__name__,
            "error_msg": str(e)[:200],
        })
        return None


def init_agent_runtime() -> HybridRetriever:
    """初始化 agent 运行时依赖, 返回 HybridRetriever (CLI 的 /load /kb 要用; Web 可忽略).

    副作用: 给 tools 注入 retriever / ontology / sensitive_cols / db 连接,
    并把 ontology 目录拼进 settings.system_prompt。
    """
    vector_store = VectorStore(persist_dir=settings.chroma_persist_dir)
    reranker = _load_reranker_if_enabled()
    store = HybridRetriever(
        vector_store,
        reranker=reranker,
        rerank_candidates=settings.rag_rerank_candidates,
    )
    set_retriever(store)

    set_ontology(settings.obda_ontology_path)
    onto = get_ontology()
    sensitive_cols = {
        attr["name"]
        for entity in onto.entities.values()
        for attr in entity["attributes"]
        if attr.get("sensitive")
    }
    set_sensitive_cols(sensitive_cols)

    # 初始化多数据源连接池 (源清单来自 DB_SOURCES: 名字/数量/方言全可配)
    connections = {}
    if not settings.db_sources:
        print(
            "⚠️  未配置任何数据源 (DB_SOURCES 为空), 跳过连接\n"
            "   RAG / 对话可正常用；查库类问题 (execute_sql) 会报错。\n"
            "   要接真库: 在 .env 里配 DB_SOURCES=源名 + DB_<源名>_HOST 等各项",
            file=sys.stderr,
        )
        log_event("db.sources_empty", {})
    for source, cfg in settings.db_sources.items():
        # 留空 host = 没库可连: 压根不去 connect。pymysql 对空 host/user 会回落
        # localhost + 当前 OS 用户名去连本机, 警告变成 "Access denied for user
        # '<个人用户名>'@'localhost'" —— 既误导排障方向又泄漏本机用户信息
        # (2026-09-24 冷启动取证)。
        if not cfg.host:
            print(
                f"⚠️  数据源 {source} 未配置 host (DB_{source.upper()}_HOST 为空), 跳过连接\n"
                f"   RAG / 对话可正常用；entity.data_source='{source}' 的实体查询会报错。",
                file=sys.stderr,
            )
            log_event("db.source_not_configured", {"source": source})
            continue
        # 连不上要降级, 不能让整个 CLI 起不来: clone 下来的人没有内网库, 裸调
        # connect 会直接崩在启动期 (2026-09-23 冷启动取证)。宽 catch 不只为
        # DB 驱动的异常 —— 照抄 .env.example 不改的人, 中文占位密码会让驱动
        # 在编码阶段抛 UnicodeEncodeError, 那是最常见的冷启动路径。
        try:
            connections[source] = db_adapters.connect_source(cfg)
            log_event("db.source_connected",
                      {"source": source, "host": cfg.host, "dialect": cfg.dialect})
        except Exception as e:
            log_event("db.source_connect_failed", {
                "source": source, "host": cfg.host,
                "error_type": type(e).__name__, "error": str(e)[:200],
            })
            print(
                f"⚠️  数据源 {source} 连接失败，跳过 ({type(e).__name__}): {e}\n"
                f"   RAG / 对话可正常用；entity.data_source='{source}' 的实体查询会报错。\n"
                f"   要接真库: 检查 .env 里 DB_{source.upper()}_* 各项",
                file=sys.stderr,
            )

    set_connections(connections)
    set_source_configs(settings.db_sources)

    # 每源方言映射 (供 OBQC sqlglot 解析 + 物理对账分派)
    # 只给成功建连的源登记 (连失败不在 connections 里也不进 dialect 映射, 口径一致)
    dialects = {s: cfg.dialect for s, cfg in settings.db_sources.items()
                if s in connections}
    set_source_dialects(dialects)

    # OBQC 物理列缓存 (spec §3.7: 进程级单例)
    set_physical_col_cache(PhysicalColumnCache())

    _init_selfevolve(settings.obda_ontology_path)

    settings.system_prompt = (
        settings.system_prompt
        + "\n\n" + settings.ontology_usage_prompt
        + "\n\n" + build_catalog_text(onto)
    )
    return store


def _init_selfevolve(db_path: str) -> None:
    """自演化飞轮的启动期接线 (spec §3 热路径)。

    两件事, 缺一条飞轮就没燃料:
    1. set_default_db_path —— 三处热路径埋点 (sql_exec 熔断 / agent max_iters /
       cq_runner cq_fail) 都零参数调 log_failure_signal, 不设默认路径则
       _default_db_path 恒 None, 信号静默丢弃。
    2. rebuild_circuit_breaker_state —— 熔断命中数是内存态, 不从
       failure_signal 重建则重启后同一条被证伪 SQL 可以再撞满两次。
       走 set_circuit_breaker_state 注入 (替换语义: 重建即以库为准),
       不要 import 私有 _circuit_breaker_hits 再 .update() —— 那是合并,
       会把上次残留的陈旧指纹留在内存态。

    启动期宽 catch: 自演化是旁路遥测, 挂了不该拦住 agent 起不来
    (同 reranker 加载失败的处置)。
    """
    try:
        from capabilities.ontology.selfevolve.signals import (
            rebuild_circuit_breaker_state, set_default_db_path)
        from agent.tools.sql_exec import set_circuit_breaker_state
        set_default_db_path(db_path)
        set_circuit_breaker_state(rebuild_circuit_breaker_state(db_path))
    except Exception as e:
        log_event("selfevolve.init_failed", {
            "error_type": type(e).__name__,
            "error_msg": str(e)[:200],
        })
