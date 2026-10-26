"""tools/rag_query.py 单测 — mock _retriever, 验过滤/重排/命中 print。

filter 断言从 test_rag_chat.py 迁移 (老 filter_by_score 纯函数逻辑
现内联进 rag_query, 测试针对工具行为重写)。
"""
import json

import pytest

import agent.tools.rag_query as rq


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """每个测试独立 JSONL 日志文件 (同 test_agent.py 风格)。"""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("OBS_LOG_DIR", str(log_dir))
    import core.observability
    monkeypatch.setattr("core.observability._LOG_FILE", None)
    return log_dir


def _read_jsonl(log_dir):
    """读最新 jsonl 文件返回 event 列表."""
    files = sorted(log_dir.glob("run-*.jsonl"))
    if not files:
        return []
    return [json.loads(line) for line in files[-1].read_text(encoding="utf-8").splitlines()]


class FakeRetriever:
    """search 返回预设 hits (含 score), 供过滤/重排断言。"""
    def __init__(self, hits):
        self._hits = hits
    def search(self, query, top_k):
        return self._hits[:top_k]


@pytest.fixture(autouse=True)
def _reset_retriever():
    """每个测试后复位模块级 _retriever, 防串扰。"""
    old = rq._retriever
    yield
    rq._retriever = old


def _hit(text, source, index, score):
    return {"text": text, "source": source, "index": index, "score": score}


def test_过滤_全部高于阈值_全留(monkeypatch):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.4)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([
        _hit("a", "x.md", 0, 0.8), _hit("b", "x.md", 1, 0.6),
    ]))
    out = rq.rag_query("q", top_k=5)
    assert out["n"] == 2

def test_过滤_全部低于阈值_全删(monkeypatch):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.4)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([
        _hit("a", "x.md", 0, 0.2), _hit("b", "x.md", 1, 0.3),
    ]))
    out = rq.rag_query("q", top_k=5)
    assert out["hits"] == []
    assert out["n"] == 0
    assert "note" in out   # 告诉 agent 是分太低不是没召回

def test_过滤_只留高于阈值(monkeypatch):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.4)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([
        _hit("a", "x.md", 0, 0.8), _hit("b", "x.md", 1, 0.3), _hit("c", "x.md", 2, 0.5),
    ]))
    out = rq.rag_query("q", top_k=5)
    assert [h["text"] for h in out["hits"]] == ["a", "c"]

def test_过滤_等于阈值算通过(monkeypatch):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.4)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([_hit("a", "x.md", 0, 0.4)]))
    out = rq.rag_query("q", top_k=5)
    assert out["n"] == 1

def test_检索空_返回空hits(monkeypatch):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.4)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([]))
    out = rq.rag_query("q", top_k=5)
    assert out["hits"] == []
    assert out["n"] == 0

def test_重排_开启时高分挪两端(monkeypatch):
    """reorder_for_long_context: >=min_chunks 时 [s0,s2,s4,...,s3,s1]。
    5 条降序 [0.9,0.8,0.7,0.6,0.5] → 重排后 [0.9,0.7,0.5,0.6,0.8]。"""
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.0)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", True)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_min_chunks", 5)
    hits = [_hit(f"c{i}", "x.md", i, s)
            for i, s in enumerate([0.9, 0.8, 0.7, 0.6, 0.5])]
    rq.set_retriever(FakeRetriever(hits))
    out = rq.rag_query("q", top_k=5)
    assert [h["score"] for h in out["hits"]] == [0.9, 0.7, 0.5, 0.6, 0.8]

def test_重排_关闭时保持降序(monkeypatch):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.0)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    hits = [_hit(f"c{i}", "x.md", i, s)
            for i, s in enumerate([0.9, 0.8, 0.7, 0.6, 0.5])]
    rq.set_retriever(FakeRetriever(hits))
    out = rq.rag_query("q", top_k=5)
    assert [h["score"] for h in out["hits"]] == [0.9, 0.8, 0.7, 0.6, 0.5]

def test_重排_关闭时落reorder_disabled埋点(monkeypatch, isolated_log):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.0)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    hits = [_hit(f"c{i}", "x.md", i, s)
            for i, s in enumerate([0.9, 0.8, 0.7, 0.6, 0.5])]
    rq.set_retriever(FakeRetriever(hits))
    rq.rag_query("q", top_k=5)
    events = _read_jsonl(isolated_log)
    disabled = [e for e in events if e["event"] == "reorder.disabled"]
    assert len(disabled) == 1
    assert disabled[0]["payload"] == {"reason": "disabled", "n_chunks": 5}

def test_命中print_显示来源和分数(monkeypatch, capsys):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.4)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([_hit("a", "/abs/HYBRID-GUIDE.md", 3, 0.812)]))
    rq.rag_query("q", top_k=5)
    captured = capsys.readouterr().out
    assert "命中 1 条资料" in captured
    assert "0.812" in captured
    assert "HYBRID-GUIDE.md" in captured  # basename, 不是全路径

def test_命中print_空时提示分太低(monkeypatch, capsys):
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.9)
    monkeypatch.setattr("agent.tools.rag_query.settings.rag_reorder_enabled", False)
    rq.set_retriever(FakeRetriever([_hit("a", "x.md", 0, 0.3)]))
    rq.rag_query("q", top_k=5)
    captured = capsys.readouterr().out
    assert "无资料过阈值" in captured
