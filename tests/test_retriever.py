"""
测 rag/retriever.py — Hybrid 检索 (Vector + BM25 + RRF)。

分两层测:
1. 纯函数: tokenize_zh / rrf_fuse  — 不依赖 store, 不依赖 jieba 真实词典
2. HybridRetriever: 包了 VectorStore, 用假 embedder (跟 test_store 同款) 避免加载 bge

测试合约:
- HybridRetriever.search 返回字段是 store.search 字段的超集
  (老字段 text/source/index/score + 新字段 heading_path/level/content_hash)
- BM25-only 命中 (字面强, 语义弱) 和 vector-only 命中 (语义强, 字面弱) 都能召回
- 双边命中走 RRF 排前
- add / delete_by_source 后 BM25 索引立刻同步

跑法: uv run pytest tests/test_retriever.py -v
"""

import pytest


# ============ 公共 fixture: 假 embedder (跟 test_store 一致) ============


@pytest.fixture
def fake_embedder(monkeypatch):
    """把 store 里的 embed_text(s) 换成基于字符串哈希的确定性假向量。"""
    def _hash_vec(text: str) -> list[float]:
        h = abs(hash(text))
        v = [((h >> (i * 8)) & 0xFF) / 255.0 for i in range(4)]
        norm = sum(x * x for x in v) ** 0.5
        return [x / norm for x in v] if norm else [1.0, 0.0, 0.0, 0.0]

    import capabilities.rag.store as store_mod
    monkeypatch.setattr(store_mod, "embed_texts", lambda ts: [_hash_vec(t) for t in ts])
    monkeypatch.setattr(store_mod, "embed_text", lambda t: _hash_vec(t))


# ============ 1. tokenize_zh: jieba 分词包装 ============


def test_tokenize_zh_中文短句切出多个token():
    from capabilities.rag.retriever import tokenize_zh
    tokens = tokenize_zh("Python 是动态语言")
    # 至少切出 "Python" / "动态" / "语言" 这种成分
    assert len(tokens) >= 2
    assert all(isinstance(t, str) for t in tokens)


def test_tokenize_zh_过滤纯标点和空白():
    from capabilities.rag.retriever import tokenize_zh
    tokens = tokenize_zh("你好,世界! ")
    # 标点和空白不该独立成 token
    assert "," not in tokens
    assert " " not in tokens
    assert "" not in tokens


def test_tokenize_zh_空串返回空列表():
    from capabilities.rag.retriever import tokenize_zh
    assert tokenize_zh("") == []
    assert tokenize_zh("   ") == []


def test_tokenize_zh_英文也能切():
    """中文场景里夹英文 (Python / uv) 很常见, 别把英文吞了。"""
    from capabilities.rag.retriever import tokenize_zh
    tokens = tokenize_zh("Python 和 uv")
    joined = " ".join(tokens)
    assert "Python" in joined or "python" in joined
    assert "uv" in joined


# ============ 2. rrf_fuse: 纯函数, 倒数排名融合 ============


def test_rrf_fuse_两个相同列表_顺序保持():
    """两个排名完全一致的输入, 融合后顺序还是一致。"""
    from capabilities.rag.retriever import rrf_fuse
    list_a = ["x", "y", "z"]
    list_b = ["x", "y", "z"]
    fused = rrf_fuse([list_a, list_b], k=60)
    # 返回 [(id, score), ...] 按 score 降序
    assert [item_id for item_id, _ in fused] == ["x", "y", "z"]


def test_rrf_fuse_双边命中比单边命中分高():
    """两个列表都出现的 id 分数应该高于只在一个列表出现的。"""
    from capabilities.rag.retriever import rrf_fuse
    list_a = ["both", "only_a", "tail"]
    list_b = ["both", "only_b", "tail2"]
    fused = rrf_fuse([list_a, list_b], k=60)
    fused_ids = [item_id for item_id, _ in fused]
    # "both" 双边命中, 必须排第一
    assert fused_ids[0] == "both"


def test_rrf_fuse_不重合id都进结果():
    from capabilities.rag.retriever import rrf_fuse
    fused = rrf_fuse([["a"], ["b"]], k=60)
    fused_ids = {item_id for item_id, _ in fused}
    assert fused_ids == {"a", "b"}


def test_rrf_fuse_空输入不崩():
    from capabilities.rag.retriever import rrf_fuse
    assert rrf_fuse([], k=60) == []
    assert rrf_fuse([[]], k=60) == []
    assert rrf_fuse([[], []], k=60) == []


def test_rrf_fuse_单列表退化为该列表本身():
    """只给一个列表, 排名跟输入一致 (score 按 1/(k+rank) 递减)。"""
    from capabilities.rag.retriever import rrf_fuse
    fused = rrf_fuse([["a", "b", "c"]], k=60)
    assert [item_id for item_id, _ in fused] == ["a", "b", "c"]
    # 分数严格递减
    scores = [s for _, s in fused]
    assert scores[0] > scores[1] > scores[2]


def test_rrf_fuse_前位比后位分高():
    """RRF 的核心: rank 越小 (越靠前), 1/(k+rank) 越大。"""
    from capabilities.rag.retriever import rrf_fuse
    list_a = ["a", "b"]
    list_b = ["b", "a"]
    # a 排名 1+2=3, b 排名 2+1=3, RRF 分相同 → 顺序任意可接受
    # 这里用单边测前后位
    fused = rrf_fuse([["x", "y"]], k=60)
    score_x = next(s for i, s in fused if i == "x")
    score_y = next(s for i, s in fused if i == "y")
    assert score_x > score_y


# ============ 3. HybridRetriever: 接口与 store 对齐 ============


def test_retriever_score字段保留cosine_向量命中block分高于0(fake_embedder, tmp_path):
    """
    设计取舍: 排序按 RRF 融合, 但 score 字段填 cosine 分让老阈值过滤
    (filter_by_score(threshold=0.4)) 继续工作. 命中自己的块 cosine 应 > 0.9.
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "命中自己分应该很高", "source": "a.md", "index": 0, "mtime": 1.0},
    ])
    retriever = HybridRetriever(store)
    results = retriever.search("命中自己分应该很高", top_k=1)
    assert len(results) == 1
    # cosine 命中自己应该接近 1, 至少 > 0.9
    assert results[0]["score"] > 0.9, (
        f"score 应是 cosine 分, 命中自己接近 1; 实际 {results[0]['score']}"
    )


def test_retriever_bm25_only命中的块score是0(fake_embedder, tmp_path):
    """
    BM25-only (向量召回 top_k * 4 都没进, 只有 BM25 进) 的块,
    score 字段填 0.0 — 不用真 cosine 去重算, 让老阈值过滤把它毙掉.
    这是设计选择, 不是 bug: 1.5 rerank 会重新写 score.
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    # 灌一堆假向量上接近的噪音 (假向量是 hash, 随机) + 1 块带独特字面关键词
    store.add([
        {"text": f"完全无关 {i}", "source": "noise.md", "index": i, "mtime": 1.0}
        for i in range(20)
    ] + [
        {"text": "独特字面关键词 zebracorn", "source": "target.md", "index": 0, "mtime": 1.0},
    ])
    retriever = HybridRetriever(store)
    # 查 zebracorn — 向量很可能漏召 (top_k * 4 = 12 条, 21 条里随机排), BM25 必中
    results = retriever.search("zebracorn", top_k=3)
    # 找到 target.md 那条
    target = next((r for r in results if "zebracorn" in r["text"]), None)
    assert target is not None
    # 如果向量没召回它, BM25-only 时 score 应该是 0.0
    # 不强测一定 == 0 (有时假向量随机命中也可能 > 0), 但要测"score 字段不是 RRF 分"
    # RRF 分极小 (< 0.04), cosine 分接近 1; 取 cosine 命中或 0, 都不会落 (0.04, 0.9) 区间
    assert results[0]["score"] > 0.5 or results[0]["score"] == 0.0, \
        f"score 应该是 cosine 或 0, 不能是 RRF 中间值; 实际 {results[0]['score']}"


def test_retriever_search返回字段是store_search的超集(fake_embedder, tmp_path):
    """
    硬合同: ask_with_rag 等老调用方只看 text/source/index/score, 必须保留;
    新字段 heading_path/level/content_hash 也要带 (chunker D2 透传链路)。
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {
            "text": "Python 是动态语言",
            "source": "a.md",
            "index": 0,
            "mtime": 1.0,
            "heading_path": "标题1",
            "level": 1,
            "content_hash": "abc123",
        },
    ])

    retriever = HybridRetriever(store)
    results = retriever.search("Python", top_k=3)

    assert len(results) >= 1
    r = results[0]
    # 老字段
    assert {"text", "source", "index", "score"} <= set(r.keys())
    # 新字段
    assert {"heading_path", "level", "content_hash"} <= set(r.keys())


def test_retriever_启动时从store全量重建bm25(fake_embedder, tmp_path):
    """
    现实场景: 进程启动时 store 已经有持久化数据,
    HybridRetriever 必须在 __init__ 里把它们全部喂给 BM25, 否则 BM25 永远查不到老数据。
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    # 第 1 个进程: 灌数据
    store1 = VectorStore(persist_dir=str(tmp_path))
    store1.add([
        {"text": "uv 管理 Python 依赖", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "Chroma 持久化向量", "source": "b.md", "index": 0, "mtime": 1.0},
    ])
    del store1

    # 第 2 个进程: 只起 retriever, 不再 add
    store2 = VectorStore(persist_dir=str(tmp_path))
    retriever = HybridRetriever(store2)

    # 字面命中 (BM25 拿手): "uv" 这个英文 token 在 store1 里
    results = retriever.search("uv", top_k=3)
    assert len(results) >= 1
    assert any("uv" in r["text"] for r in results)


def test_retriever_BM25字面命中_即使语义假向量不准(fake_embedder, tmp_path):
    """
    Hybrid 的核心价值: 即使语义检索 (这里是 hash 假向量, 完全随机) 不给力,
    BM25 仍能靠字面匹配把含 query 关键字的块召回。
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    # 灌一堆完全不相关的块 + 1 块含 "Elasticsearch" 关键词
    store.add([
        {"text": f"无关内容 {i}", "source": "noise.md", "index": i, "mtime": 1.0}
        for i in range(10)
    ] + [
        {"text": "Elasticsearch 是搜索引擎", "source": "target.md", "index": 0, "mtime": 1.0},
    ])

    retriever = HybridRetriever(store)
    results = retriever.search("Elasticsearch", top_k=5)

    # 含 "Elasticsearch" 的目标块必须出现在结果里
    assert any("Elasticsearch" in r["text"] for r in results), \
        f"BM25 应能字面命中, 实际: {[r['text'][:30] for r in results]}"


def test_retriever_add后bm25立刻同步(fake_embedder, tmp_path):
    """add 新块后, BM25 必须能立刻搜到新块 (不能等下次重启)。"""
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    retriever = HybridRetriever(store)

    retriever.add([
        {"text": "新增的块包含 jieba 分词", "source": "new.md", "index": 0, "mtime": 1.0},
    ])

    results = retriever.search("jieba", top_k=3)
    assert len(results) >= 1
    assert any("jieba" in r["text"] for r in results)


def test_retriever_delete_by_source后bm25不再召回(fake_embedder, tmp_path):
    """删 source 后, 该 source 的块不能再被 BM25 召回。"""
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    retriever = HybridRetriever(store)

    retriever.add([
        {"text": "独特关键词 zebracorn", "source": "a.md", "index": 0, "mtime": 1.0},
    ])

    # 删之前能搜到
    assert any("zebracorn" in r["text"] for r in retriever.search("zebracorn", top_k=3))

    retriever.delete_by_source("a.md")

    # 删之后搜不到
    results = retriever.search("zebracorn", top_k=3)
    assert not any("zebracorn" in r["text"] for r in results)


def test_retriever_空库search返回空_不崩(fake_embedder, tmp_path):
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    retriever = HybridRetriever(store)
    assert retriever.search("任何", top_k=3) == []


def test_retriever_透传store的辅助方法(fake_embedder, tmp_path):
    """sources / __len__ / get_source_mtime 直接透传 store, 给 main.py /kb 用。"""
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    retriever = HybridRetriever(store)
    retriever.add([
        {"text": "A", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "B", "source": "b.md", "index": 0, "mtime": 2.0},
    ])
    assert len(retriever) == 2
    assert sorted(retriever.sources()) == ["a.md", "b.md"]
    assert retriever.get_source_mtime("a.md") == 1.0


def test_retriever_双边都命中比单边更靠前(fake_embedder, tmp_path):
    """
    RRF 的端到端验证: 一块"既字面匹配又是查询自身"应该排在
    "只字面匹配" 或 "只语义匹配" 的块前面。

    假向量 = hash, 所以 "查询自身" 命中向量第一名;
    BM25 命中靠字面共现.
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "查询字符串 Elasticsearch", "source": "both.md", "index": 0, "mtime": 1.0},
        {"text": "查询字符串", "source": "vec_only.md", "index": 0, "mtime": 1.0},
        {"text": "完全无关内容 Elasticsearch", "source": "bm_only.md", "index": 0, "mtime": 1.0},
    ])

    retriever = HybridRetriever(store)
    results = retriever.search("查询字符串 Elasticsearch", top_k=3)

    # 至少三个都召回了
    sources_in_result = [r["source"] for r in results]
    assert "both.md" in sources_in_result
    # 双边命中的 both.md 排名应该不晚于任一单边命中
    rank_both = sources_in_result.index("both.md")
    if "vec_only.md" in sources_in_result:
        assert rank_both <= sources_in_result.index("vec_only.md")
    if "bm_only.md" in sources_in_result:
        assert rank_both <= sources_in_result.index("bm_only.md")


# ============ 4. 埋点: retriever.fuse 事件 ============


@pytest.fixture
def spy_log_event(monkeypatch):
    """跟 test_observability_hooks 同款 spy。"""
    events: list[tuple[str, dict]] = []

    def fake_log_event(event: str, payload: dict | None = None) -> None:
        events.append((event, payload or {}))

    def install(module_path: str) -> None:
        monkeypatch.setattr(f"{module_path}.log_event", fake_log_event)

    return events, install


def test_retriever_search发fuse事件_带关键字段(spy_log_event, fake_embedder, tmp_path):
    events, install = spy_log_event
    install("capabilities.rag.retriever")

    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "Python", "source": "a.md", "index": 0, "mtime": 1.0},
    ])

    retriever = HybridRetriever(store)
    retriever.search("Python", top_k=3)

    fuse_events = [e for e in events if e[0] == "retriever.fuse"]
    assert len(fuse_events) == 1
    payload = fuse_events[0][1]
    assert payload["query"] == "Python"
    # 关键字段: 两路各自的 topk + 融合后结果 + 耗时
    assert "bm25_topk" in payload
    assert "vector_topk" in payload
    assert "rrf_merged" in payload
    assert "elapsed_ms" in payload
    assert payload["elapsed_ms"] >= 0


# ============ 5. Reranker 集成 (1.5) ============


class _FakeReranker:
    """
    跟 test_reranker.py 的 fake_cross_encoder 同思路: 按 query 在 text
    出现次数倒序, score 改成 0.9 / 0.5 / 0.1 三档模拟 sigmoid 化结果。
    """

    def rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        scored = [
            (c, c["text"].count(query) if query else 0) for c in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        out = []
        for i, (c, n) in enumerate(scored[:top_k]):
            r = dict(c)
            # 假装的 sigmoid 化分, 三档
            r["score"] = 0.9 if n >= 2 else (0.5 if n == 1 else 0.1)
            out.append(r)
        return out


def test_retriever_注入reranker后_score被rerank覆盖(fake_embedder, tmp_path):
    """
    给 HybridRetriever 注入 reranker, 返回 result 的 score 字段
    应该是 fake reranker 的 0.9/0.5/0.1 之一, 不再是 cosine.
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "Python Python 出现两次", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "Python 出现一次", "source": "b.md", "index": 0, "mtime": 1.0},
        {"text": "完全无关", "source": "c.md", "index": 0, "mtime": 1.0},
    ])
    retriever = HybridRetriever(store, reranker=_FakeReranker())
    results = retriever.search("Python", top_k=3)

    assert len(results) == 3
    # 第一个 source 应该是 a.md (出现 2 次)
    assert results[0]["source"] == "a.md"
    # score 应该来自 reranker (0.1 / 0.5 / 0.9), 不可能是 cosine
    assert results[0]["score"] in (0.1, 0.5, 0.9)


def test_retriever_无reranker时行为不变(fake_embedder, tmp_path):
    """
    没传 reranker 参数时, search 必须跟老 Hybrid 完全一致 (cosine score / RRF 排序).
    这是兼容性合同, 保护已有 80 测试.
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "命中自己分高", "source": "a.md", "index": 0, "mtime": 1.0},
    ])
    retriever = HybridRetriever(store)  # 不传 reranker
    results = retriever.search("命中自己分高", top_k=1)
    # 老行为: cosine 命中自己 > 0.9
    assert results[0]["score"] > 0.9


def test_retriever_粗排候选数受rerank_candidates控制(fake_embedder, tmp_path):
    """
    打开 reranker 后, 候选池 = rerank_candidates (默认 30, 不是 top_k * 4).
    用 spy 验证 reranker 收到的候选数 = min(rerank_candidates, 库大小).
    """
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore

    seen_candidate_counts: list[int] = []

    class _CountingReranker:
        def rerank(self, query, candidates, top_k):
            seen_candidate_counts.append(len(candidates))
            return candidates[:top_k]

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": f"块 {i}", "source": f"f{i}.md", "index": 0, "mtime": 1.0}
        for i in range(50)
    ])
    retriever = HybridRetriever(
        store,
        reranker=_CountingReranker(),
        rerank_candidates=30,
    )
    retriever.search("块", top_k=5)
    # 库里 50 条, 粗排候选应该是 30 (vector 30 + BM25 30 RRF 融合后去重, 可能略少)
    assert seen_candidate_counts[0] <= 30
    # 但至少要有一些, 别是 0
    assert seen_candidate_counts[0] > 0
