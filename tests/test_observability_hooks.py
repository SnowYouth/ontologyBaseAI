"""
测 RAG 链路 4 个关键节点的 log_event 埋点 (阶段 1.4.3):

- chunker.chunk_file → "chunker.done"
- embedder.get_model (首次) → "embedder.loaded"
- embedder.embed_texts → "embedder.encode"
- store.add → "store.add"
- store.search → "store.search"

为啥不真写文件: 用 monkeypatch 把每个模块里 import 进来的 log_event
替换成一个 spy 列表, 测试断言"调用了几次、payload 关键字段是啥"。
比读 jsonl 文件解析快很多, 也跟 observability 本体的测试解耦。

跑法: uv run pytest tests/test_observability_hooks.py -v
"""

import os
import pytest


# ============ 公用 fixture: 给一个模块装 spy log_event ============


@pytest.fixture
def spy_log_event(monkeypatch):
    """
    返回 (events, install) 二元组:
    - events: 累积所有 log_event 调用, 元素 (event_name, payload)
    - install(module_path): 把指定模块里的 log_event 替换成 spy
    """
    events: list[tuple[str, dict]] = []

    def fake_log_event(event: str, payload: dict | None = None) -> None:
        events.append((event, payload or {}))

    def install(module_path: str) -> None:
        monkeypatch.setattr(f"{module_path}.log_event", fake_log_event)

    return events, install


# ============ chunker.done ============


def test_chunk_file_发chunker_done事件_payload带n_chunks和sample(spy_log_event, tmp_path):
    events, install = spy_log_event
    install("capabilities.rag.chunker")

    md = tmp_path / "x.md"
    md.write_text("# 标题1\n\n这是一段相对短的正文,够分一两块。\n\n## 标题1.1\n\n更多内容。", encoding="utf-8")

    from capabilities.rag.chunker import chunk_file
    chunks = chunk_file(str(md))

    assert len(chunks) > 0
    # 应该有且仅有一次 chunker.done
    done = [e for e in events if e[0] == "chunker.done"]
    assert len(done) == 1, f"chunker.done 应该恰好 1 次, 实际: {[e[0] for e in events]}"

    payload = done[0][1]
    assert payload["file"] == str(md)
    assert payload["n_chunks"] == len(chunks)
    assert "len_min" in payload and "len_max" in payload and "len_avg" in payload
    # sample_first 是首块文本前 200 字符
    assert "sample_first" in payload
    assert len(payload["sample_first"]) <= 200


def test_chunk_file_空文件不发事件或n_chunks为0(spy_log_event, tmp_path):
    """空文件就算发也只能发 0, 不能崩 (除零保护)。"""
    events, install = spy_log_event
    install("capabilities.rag.chunker")

    md = tmp_path / "empty.md"
    md.write_text("", encoding="utf-8")

    from capabilities.rag.chunker import chunk_file
    chunks = chunk_file(str(md))

    assert chunks == []
    # 空文件可以选择不发, 或发 n_chunks=0; 两种都接受
    done = [e for e in events if e[0] == "chunker.done"]
    if done:
        assert done[0][1]["n_chunks"] == 0


# ============ embedder.encode + embedder.loaded ============


def test_embed_texts_发encode事件_带n_texts和elapsed_ms(spy_log_event, monkeypatch):
    events, install = spy_log_event
    install("capabilities.rag.embedder")

    # mock get_model 返回一个假 model, 避免真加载 bge
    class FakeModel:
        def encode(self, texts, normalize_embeddings, show_progress_bar):
            import numpy as np
            return np.zeros((len(texts), 4))

    monkeypatch.setattr("capabilities.rag.embedder.get_model", lambda: FakeModel())

    from capabilities.rag.embedder import embed_texts
    vecs = embed_texts(["a", "b", "c"])

    assert len(vecs) == 3
    encode_events = [e for e in events if e[0] == "embedder.encode"]
    assert len(encode_events) == 1
    payload = encode_events[0][1]
    assert payload["n_texts"] == 3
    assert payload["dim"] == 4
    assert "elapsed_ms" in payload
    assert payload["elapsed_ms"] >= 0


def test_get_model_首次加载发loaded事件_第二次不发(spy_log_event, monkeypatch):
    """模型懒加载, 首次发 embedder.loaded, 第二次直接复用不发。"""
    events, install = spy_log_event
    install("capabilities.rag.embedder")

    # 重置模块级 _model 单例, 强制走"首次加载"分支
    import capabilities.rag.embedder as emb_mod
    monkeypatch.setattr(emb_mod, "_model", None)

    # 假 SentenceTransformer, 避免真去文件系统找 models/
    class FakeSentenceTransformer:
        def __init__(self, path):
            self.path = path
        def get_sentence_embedding_dimension(self):
            return 512

    monkeypatch.setattr(emb_mod, "SentenceTransformer", FakeSentenceTransformer)
    # 假 isdir, 避免 sys.exit
    monkeypatch.setattr(os.path, "isdir", lambda p: True)

    m1 = emb_mod.get_model()
    m2 = emb_mod.get_model()
    assert m1 is m2  # 同一个实例

    loaded = [e for e in events if e[0] == "embedder.loaded"]
    assert len(loaded) == 1, f"首次加载应发 1 次 loaded, 实际 {len(loaded)}"
    payload = loaded[0][1]
    assert "model_path" in payload
    assert payload["dim"] == 512
    assert "load_ms" in payload


# ============ store.add + store.search ============


@pytest.fixture
def fake_embedder_for_store(monkeypatch):
    """store 测试通用 fixture: 把 embed_text(s) 换成假向量, 不依赖 bge。"""
    def _hash_vec(text: str) -> list[float]:
        h = abs(hash(text))
        v = [((h >> (i * 8)) & 0xFF) / 255.0 for i in range(4)]
        norm = sum(x * x for x in v) ** 0.5
        return [x / norm for x in v] if norm else [1.0, 0.0, 0.0, 0.0]

    import capabilities.rag.store as store_mod
    monkeypatch.setattr(store_mod, "embed_texts", lambda ts: [_hash_vec(t) for t in ts])
    monkeypatch.setattr(store_mod, "embed_text", lambda t: _hash_vec(t))


def test_store_add_发store_add事件_带n_items(spy_log_event, fake_embedder_for_store, tmp_path):
    events, install = spy_log_event
    install("capabilities.rag.store")

    from capabilities.rag.store import VectorStore
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "A", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "B", "source": "a.md", "index": 1, "mtime": 1.0},
    ])

    add_events = [e for e in events if e[0] == "store.add"]
    assert len(add_events) == 1
    assert add_events[0][1]["n_items"] == 2


def test_store_search_发store_search事件_带query_top_k_results摘要(
    spy_log_event, fake_embedder_for_store, tmp_path
):
    events, install = spy_log_event
    install("capabilities.rag.store")

    from capabilities.rag.store import VectorStore
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "Python 动态语言", "source": "a.md", "index": 0, "mtime": 1.0},
    ])

    # 清掉 add 阶段的日志, 只看 search
    events.clear()

    store.search("Python", top_k=3)

    search_events = [e for e in events if e[0] == "store.search"]
    assert len(search_events) == 1
    payload = search_events[0][1]
    assert payload["query"] == "Python"
    assert payload["top_k"] == 3
    # results 是摘要列表, 每个 {text_preview, score, source}
    assert "results" in payload
    assert isinstance(payload["results"], list)
    if payload["results"]:
        r0 = payload["results"][0]
        assert "text_preview" in r0
        assert "score" in r0
        assert "source" in r0


def test_store_search_空库不报错_results为空(
    spy_log_event, fake_embedder_for_store, tmp_path
):
    events, install = spy_log_event
    install("capabilities.rag.store")

    from capabilities.rag.store import VectorStore
    store = VectorStore(persist_dir=str(tmp_path))
    store.search("任何", top_k=3)

    search_events = [e for e in events if e[0] == "store.search"]
    assert len(search_events) == 1
    assert search_events[0][1]["results"] == []
