"""
core/observability.py — 极小 JSONL 事件日志。

为啥要这玩意儿:
    RAG 链路 (chunker / embedder / store / retriever / LLM) 全是黑盒,
    出问题不知道是哪段烂。在关键节点埋 log_event, 一行一个 event 写到
    logs/run-YYYYMMDD-HHMMSS.jsonl, 后期任何回归/调参都有可观察依据。
    详见 memory/observability-discipline.md。

只暴露一个函数:
    log_event(event: str, payload: dict | None = None) -> None

约定:
- 一个 Python 进程一个日志文件 (第一次调用时确定文件名, 之后追加)
- 文件名 run-YYYYMMDD-HHMMSS.jsonl (本地时间, 跟人对得上)
- 每行: {"ts": "...ISO 本地时间...", "event": "...", "payload": {...}}
- ensure_ascii=False, 中文原文落盘
- 默认 logs/ 在项目根, 可通过环境变量 OBS_LOG_DIR 覆盖 (测试隔离用)
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar, Token
from datetime import datetime
from pathlib import Path
from typing import Any

# 进程级单例: 第一次 log_event 时确定, 之后所有调用追加同一个文件
_LOG_FILE: Path | None = None

# request_id 请求级上下文 (阶段 3 Task 1):
# 中间件收到请求时 bind_request_id(rid), 请求结束 unbind。
# log_event 落盘前 _inject_request_id 把它塞进 payload, 这样每条事件
# 都能跟同一个 HTTP 请求对上, 后台排查时按 request_id 聚合。
# 没绑过 (CLI / 老调用方) 时 ContextVar 返回 None, 不影响老行为。
_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


def bind_request_id(request_id: str) -> Token:
    """绑定当前请求的 request_id, 返回 token 供 unbind 用。

    在 RequestIDMiddleware 入口调用, finally 里 unbind_request_id(token) 复位。
    """
    return _REQUEST_ID.set(request_id)


def unbind_request_id(token: Token) -> None:
    """复位 request_id 上下文到 bind 之前的状态。

    B2 兜底 (2026-06-25): bind 在协程 A 但 unbind 在协程 B 时, ContextVar.reset
    会抛 ValueError("Token was created in a different Context"). 这种情况发生在
    SSE event_gen 内 — starlette TaskGroup 把 generator 派到子协程跑, finally
    清理时已切到不同 context. 如果让 ValueError 逃出, 整个 generator 清理路径
    会被炸掉, 进而触发 route 的非 APIError 兜底缺口 (B1), 让前端看到"网络中断"。
    所以这里吞掉 ValueError, 其他异常仍抛 (失败显式)。
    """
    try:
        _REQUEST_ID.reset(token)
    except ValueError:
        # 跨 context: token 在另一个 context 里创建, reset 无效. 不影响后续:
        # 这个协程退出时 contextvar 自然回收, 老 context 也走自己的生命周期。
        pass


def get_request_id() -> str | None:
    """取当前请求绑定的 request_id (没绑过返 None)。

    SSE error 路径 (event_gen 是新协程, 但 contextvar 在 starlette TaskGroup 下
    会继承到子任务) 用这个 helper 取 request_id 塞进 ErrorEvent, 比直接摸
    _REQUEST_ID.get() 干净。
    """
    return _REQUEST_ID.get()


def _inject_request_id(payload: dict[str, Any]) -> dict[str, Any]:
    """把当前 request_id 注入 payload (没绑过 / payload 已自带时原样返回)。

    已自带时不覆盖, 让调用方显式给的值优先 (比如日志想记一个跟请求头不同的 id)。
    """
    request_id = _REQUEST_ID.get()
    if not request_id or "request_id" in payload:
        return payload
    merged = dict(payload)
    merged["request_id"] = request_id
    return merged


def _default_log_dir() -> Path:
    """默认日志目录 = 项目根的 logs/。OBS_LOG_DIR 环境变量可覆盖。"""
    env = os.environ.get("OBS_LOG_DIR")
    if env:
        return Path(env)
    # core/observability.py → 项目根 = 父目录的父目录
    return Path(__file__).resolve().parent.parent / "logs"


def _ensure_log_file() -> Path:
    """首次调用时确定文件名并建目录, 之后直接返回缓存的路径。"""
    global _LOG_FILE
    if _LOG_FILE is not None:
        return _LOG_FILE

    log_dir = _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # 本地时间, 跟人对得上, 不用 UTC
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _LOG_FILE = log_dir / f"run-{stamp}.jsonl"
    return _LOG_FILE


def log_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """
    追加一行事件到当前进程的日志文件。

    参数:
        event: 事件名, 形如 "chunker.done" / "llm.call"
        payload: 任意可 JSON 序列化的 dict, None 等同 {}

    落盘前会 _inject_request_id: 若当前上下文绑定了 request_id (HTTP 请求期),
    自动塞进 payload.request_id; 调用方已在 payload 里给了 request_id 则不覆盖。
    CLI / 老调用方没绑上下文时, 行为零变化。
    """
    payload_dict = payload if payload is not None else {}
    record = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": event,
        "payload": _inject_request_id(payload_dict),
    }
    line = json.dumps(record, ensure_ascii=False)
    with open(_ensure_log_file(), "a", encoding="utf-8") as f:
        f.write(line + "\n")
