"""tools/rag_query.py 单元测试 — FakeRetriever mock 掉真实 HybridRetriever."""
import pytest


class FakeRetriever:
    """最小 Mock: 只暴露 search 方法, 返回固定 hits."""
    def __init__(self, hits=None):
        self._hits = hits or []

    def search(self, query, top_k=5):
        return self._hits[:top_k]


@pytest.fixture(autouse=True)
def _reset_retriever():
    """每个测试前后清掉模块级 _retriever, 避免污染."""
    import agent.tools.rag_query as mod
    mod._retriever = None
    yield
    mod._retriever = None


class TestRagQuery:
    def test_正常返回_hits(self):
        from agent.tools.rag_query import rag_query, set_retriever
        fake = FakeRetriever(hits=[
            {"source": "/a.md", "score": 0.81, "text": "片段 A"},
            {"source": "/b.md", "score": 0.72, "text": "片段 B"},
        ])
        set_retriever(fake)
        result = rag_query(query="测试", top_k=2)
        assert result["n"] == 2
        assert result["hits"][0]["source"] == "/a.md"
        assert result["hits"][0]["score"] == 0.81
        assert result["hits"][0]["text"] == "片段 A"

    def test_retriever_未注入_返回_error(self):
        from agent.tools.rag_query import rag_query
        result = rag_query(query="x")
        assert "error" in result
        assert "retriever" in result["error"].lower()

    def test_top_k_上限裁剪到_20(self):
        from agent.tools.rag_query import rag_query, set_retriever
        # FakeRetriever 给 25 个 hits, 但 top_k 应被裁到 20
        hits = [{"source": f"/{i}.md", "score": 0.5, "text": f"t{i}"} for i in range(25)]
        fake = FakeRetriever(hits=hits)
        set_retriever(fake)
        result = rag_query(query="x", top_k=999)
        assert result["n"] == 20

    def test_top_k_下限裁剪到_1(self):
        from agent.tools.rag_query import rag_query, set_retriever
        fake = FakeRetriever(hits=[{"source": "/a.md", "score": 0.5, "text": "t"}])
        set_retriever(fake)
        result = rag_query(query="x", top_k=0)
        assert result["n"] == 1

    def test_hit_缺_score_默认_0(self, monkeypatch):
        from agent.tools.rag_query import rag_query, set_retriever
        # 模拟 BM25-only 召回 (无 score 字段); 阈值过滤是本任务新加行为,
        # 这里把阈值降到 0 单独验证"缺 score 默认 0"这一件事, 避免被过滤掉.
        monkeypatch.setattr("agent.tools.rag_query.settings.rag_score_threshold", 0.0)
        fake = FakeRetriever(hits=[{"source": "/a.md", "text": "t"}])
        set_retriever(fake)
        result = rag_query(query="x")
        assert result["hits"][0]["score"] == 0
