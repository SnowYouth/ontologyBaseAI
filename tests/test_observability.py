"""
测 core/observability.py 的极小接口:

    from core.observability import log_event
    log_event("chunker.done", {"file": "x.md", "n_chunks": 12})

约定 (来自 stage-1-4-plan / observability-discipline):
- 落盘到 `logs/run-YYYYMMDD-HHMMSS.jsonl`, 一个进程一个文件
- 每行一个 event: {"ts": "...ISO...", "event": "...", "payload": {...}}
- ensure_ascii=False, 中文要原文出现
- 目录不存在自动建

测试隔离: 每个 case 用 tmp_path 当 logs 目录, 并把模块里的"已选定文件路径"单例
重置为 None, 避免上一个 case 的文件被复用。

跑法: uv run pytest tests/test_observability.py -v
"""

import json
import os
import re

import pytest


@pytest.fixture
def fresh_obs(tmp_path, monkeypatch):
    """
    每个测试隔离一个干净的 logs 目录, 并重置模块内的文件单例。

    返回 (obs_module, logs_dir) 二元组。
    - obs_module: 已经 import 好的 core.observability
    - logs_dir: 这个测试的 logs 目录 (tmp_path 下的子目录, 故意还没建, 验证模块会自己建)
    """
    logs_dir = tmp_path / "logs"
    # 通过环境变量告诉模块用这个目录
    monkeypatch.setenv("OBS_LOG_DIR", str(logs_dir))

    from core import observability

    # 重置模块级单例 (避免不同测试串号)
    monkeypatch.setattr(observability, "_LOG_FILE", None, raising=False)
    return observability, logs_dir


def _read_lines(path):
    """读 jsonl 文件, 每行 parse 成 dict, 返回 list。"""
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ============ 1. 最基础: 写出去能再读回来 ============


def test_log_event_writes_and_reads_back(fresh_obs):
    obs, logs_dir = fresh_obs

    obs.log_event("test.basic", {"k": 1, "v": "hello"})

    files = list(logs_dir.glob("*.jsonl"))
    assert len(files) == 1, "应该只产生 1 个日志文件"

    rows = _read_lines(files[0])
    assert len(rows) == 1
    assert rows[0]["event"] == "test.basic"
    assert rows[0]["payload"] == {"k": 1, "v": "hello"}


# ============ 2. ts 字段是 ISO 格式 ============


def test_ts_is_iso_format(fresh_obs):
    obs, logs_dir = fresh_obs

    obs.log_event("test.ts", {})

    row = _read_lines(next(logs_dir.glob("*.jsonl")))[0]
    ts = row["ts"]
    # ISO 8601 形如 2026-06-17T18:05:23.142  或  2026-06-17T18:05:23.142+08:00
    # 至少前缀 YYYY-MM-DDTHH:MM:SS 必须有
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts), f"ts 不是 ISO: {ts}"


# ============ 3. ensure_ascii=False, 中文不转义 ============


def test_chinese_not_escaped(fresh_obs):
    obs, logs_dir = fresh_obs

    obs.log_event("test.zh", {"msg": "你好,世界"})

    raw = next(logs_dir.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "你好,世界" in raw, "中文应该原文落盘, 不要被转成 \\uXXXX"


# ============ 4. 一个进程一个文件: 多次 log_event 复用同一文件 ============


def test_same_file_across_multiple_calls(fresh_obs):
    obs, logs_dir = fresh_obs

    obs.log_event("e1", {"i": 1})
    obs.log_event("e2", {"i": 2})
    obs.log_event("e3", {"i": 3})

    files = list(logs_dir.glob("*.jsonl"))
    assert len(files) == 1, "三次调用应该只产生一个文件"

    rows = _read_lines(files[0])
    assert [r["event"] for r in rows] == ["e1", "e2", "e3"], "顺序按调用顺序追加"


# ============ 5. 文件名格式 run-YYYYMMDD-HHMMSS.jsonl ============


def test_filename_format(fresh_obs):
    obs, logs_dir = fresh_obs

    obs.log_event("test.fname", {})

    name = next(logs_dir.glob("*.jsonl")).name
    assert re.match(r"^run-\d{8}-\d{6}\.jsonl$", name), f"文件名不符规范: {name}"


# ============ 6. logs/ 目录不存在时自动创建 ============


def test_creates_dir_if_missing(tmp_path, monkeypatch):
    deep = tmp_path / "a" / "b" / "logs"
    assert not deep.exists()
    monkeypatch.setenv("OBS_LOG_DIR", str(deep))

    from core import observability

    monkeypatch.setattr(observability, "_LOG_FILE", None, raising=False)

    observability.log_event("test.mkdir", {})

    assert deep.exists() and deep.is_dir()
    assert len(list(deep.glob("*.jsonl"))) == 1


# ============ 7. payload 支持嵌套 dict 和 list ============


def test_payload_nested(fresh_obs):
    obs, logs_dir = fresh_obs

    payload = {
        "outer": {"inner": [1, 2, {"deep": "v"}]},
        "list": ["a", "b"],
        "num": 3.14,
        "bool": True,
        "null": None,
    }
    obs.log_event("test.nested", payload)

    row = _read_lines(next(logs_dir.glob("*.jsonl")))[0]
    assert row["payload"] == payload


# ============ 8. payload 缺省允许 {} 或不传 ============


def test_payload_optional(fresh_obs):
    obs, logs_dir = fresh_obs

    obs.log_event("test.no_payload")  # 不传 payload

    row = _read_lines(next(logs_dir.glob("*.jsonl")))[0]
    assert row["event"] == "test.no_payload"
    assert row["payload"] == {}


# ============ 9. 每行必须独立 JSON, 不能多行/逗号分隔 ============


def test_strict_jsonl_format(fresh_obs):
    """JSONL 协议: 一行一个完整 JSON 对象, 行间不能有逗号/数组括号。"""
    obs, logs_dir = fresh_obs

    obs.log_event("a", {"i": 1})
    obs.log_event("b", {"i": 2})

    raw = next(logs_dir.glob("*.jsonl")).read_text(encoding="utf-8")
    # 不应该有数组括号或行间逗号
    assert not raw.lstrip().startswith("["), "不应该是 JSON 数组"
    # 每一行单独 parse 都得成功
    for line in raw.splitlines():
        if line.strip():
            json.loads(line)  # 任何一行 parse 失败这里就抛


# ============ 10. request_id 上下文注入 payload (阶段 3 Task 1) ============


def test_request_id_is_injected_into_payload(fresh_obs):
    """bind_request_id 后, log_event 的 payload 应自动带上 request_id 字段。"""
    obs, logs_dir = fresh_obs
    token = obs.bind_request_id("rid-123")
    try:
        obs.log_event("http.request", {"path": "/api/chat"})
    finally:
        obs.unbind_request_id(token)

    row = _read_lines(next(logs_dir.glob("*.jsonl")))[0]
    assert row["payload"]["request_id"] == "rid-123"


def test_request_id_not_injected_when_unbound(fresh_obs):
    """没 bind 时, payload 不应出现 request_id 字段 (老行为零回归)。"""
    obs, logs_dir = fresh_obs

    obs.log_event("plain.event", {"k": "v"})

    row = _read_lines(next(logs_dir.glob("*.jsonl")))[0]
    assert "request_id" not in row["payload"]


def test_request_id_not_overwriting_existing(fresh_obs):
    """payload 已带 request_id 时, 注入器不应覆盖调用方给的值。"""
    obs, logs_dir = fresh_obs
    token = obs.bind_request_id("rid-bound")
    try:
        obs.log_event("keep.event", {"request_id": "rid-explicit"})
    finally:
        obs.unbind_request_id(token)

    row = _read_lines(next(logs_dir.glob("*.jsonl")))[0]
    assert row["payload"]["request_id"] == "rid-explicit"


# ============ B2: unbind 跨 context 不应抛 ValueError (2026-06-25) ============


def test_unbind_request_id_swallows_cross_context_error(fresh_obs):
    """B2: bind 在 context A, unbind 在 context B → ContextVar.reset 会抛
    ValueError("Token was created in a different Context"). 这种情况发生在
    SSE event_gen 里 (starlette TaskGroup 跨协程切 context), 不应该让 finally
    里的清理把整个流的清理路径炸掉。

    修法: unbind_request_id 必须吞掉 ValueError (其他异常仍抛), 否则
    清理失败会让 generator 异常逃出, 触发 B1 误关流。

    回归: 14:47 era stderr 多次:
        ValueError: <Token ...> was created in a different Context
        File "core/observability.py:51", in unbind_request_id
            _REQUEST_ID.reset(token)
    """
    obs, _ = fresh_obs
    import asyncio

    async def main():
        # 在 A 协程 bind
        token = obs.bind_request_id("rid-cross")
        # 切到 B 协程 unbind (asyncio.create_task 会复制 context, reset 跨 context)
        ev = asyncio.Event()
        result = {}

        async def b():
            try:
                obs.unbind_request_id(token)
                result["ok"] = True
            except Exception as e:  # noqa: BLE001
                result["err"] = e
            finally:
                ev.set()

        asyncio.create_task(b())
        await ev.wait()
        return result

    result = asyncio.run(main())
    # 修法生效后: 不抛, 也无错; 修法没上时: result["err"] 会是 ValueError
    assert result.get("ok") is True, f"unbind 跨 context 仍抛: {result}"
