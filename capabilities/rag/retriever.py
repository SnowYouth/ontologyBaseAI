"""
rag/retriever: Hybrid 检索 (Vector + BM25 + RRF)。

阶段 1.4 第 4 步:
- 把 VectorStore 包一层, 对外接口 search/add/delete_by_source/sources/__len__/get_source_mtime
  完全兼容 store, ask_with_rag 和 main.py 改动最小
- 内部多维护一个 BM25 内存索引, search 时双路召回 + RRF 融合

为啥这么设计 (理由 vs 替代):
1. **BM25 纯内存, 启动从 store 拉全量重建** — 学习项目规模小, 重建 < 1s 能扛。
   生产是 Elasticsearch / OpenSearch 持久化 + 增量, 我们差几个数量级.
   待补差距清单详见 [[production-vs-learning-discipline]] 和 stage 1.4 GUIDE.
2. **HybridRetriever 包 store** — 上层调用方 (ask_with_rag) 看到的 search 签名跟
   store.search 完全一致, 只是换了实现. 减少改动面.
3. **RRF (Reciprocal Rank Fusion)** — 不看分数尺度只看排名, 鲁棒性比 CombSUM/CombMNZ 好.
   k=60 是 Elasticsearch / Vespa 默认值, 沿用.
4. **jieba 分词** — 中文社区入门标杆, 准确率 ~92%, 纯 Python 慢. 生产严肃场景
   应该上 HanLP / LAC. 学习项目够用.

距离 / 分数说明 (重要):
- store.search 的 score = 1 - cosine_distance ∈ [0, 1] (归一化向量)
- BM25 的 score 是 0+ 的绝对值, 跟 cosine 完全不同尺度
- RRF 不用任何一种 score, 只看排名 → 融合后 score = Σ 1/(k+rank), 跟原 score 没关系

输出 results 里 score 字段的设计取舍:
- **排序按 RRF 融合分降序** (这是 Hybrid 的核心价值)
- **score 字段填 cosine 分** (BM25-only 召回的填 0.0) — 让老调用方的
  filter_by_score(threshold=0.4) 能继续工作, 不用全链改阈值.
- 这种"排序和 score 字段解耦"的设计跟生产 Elasticsearch 行为不一样
  (ES 是 score 字段直接给融合分). 我们这么做是因为下一步 1.5 rerank 会把
  cross-encoder 分写进 score, 那时阈值过滤会变成 rerank 分阈值, 统一再调.
"""

from __future__ import annotations

import re
import time
from typing import Iterable

import jieba
from rank_bm25 import BM25Okapi

from core.observability import log_event

from .store import VectorStore


# RRF 默认 k. Elasticsearch / Vespa 都是 60.
DEFAULT_RRF_K = 60

# 每路召回的候选数. 取 top_k * 多倍数, 给融合层留排序空间.
# 4 是经验值 — 太少融合无效, 太多 BM25 / 向量库都慢.
DEFAULT_CANDIDATE_MULTIPLIER = 4


# ============ 分词 ============


# 过滤纯标点 / 空白. 中文标点 + 英文标点 + 空白都打掉.
_NON_WORD = re.compile(r"^[\s　\W_]+$")


def tokenize_zh(text: str) -> list[str]:
    """
    jieba 切词 + 过滤标点空白.
    返回小写 token 列表 (英文统一小写, 中文不变).

    BM25 索引和查询都走这里, 必须对称.
    """
    if not text or not text.strip():
        return []
    raw = jieba.lcut(text)
    out: list[str] = []
    for t in raw:
        s = t.strip()
        if not s:
            continue
        if _NON_WORD.match(s):
            continue
        out.append(s.lower())
    return out


# ============ RRF 融合 ============


def rrf_fuse(
    ranked_lists: list[list[str]],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float]]:
    """
    Reciprocal Rank Fusion.

    输入: 多路召回结果, 每路是按相关性降序的 id 列表
    输出: [(id, fused_score), ...] 按 fused_score 降序

    公式: score(id) = Σ_lists  1 / (k + rank_in_list)
    rank 从 1 起算, 未出现在某路则不贡献分数.

    k 控制"低排名块的影响力", k 越大低排名贡献越接近高排名.
    Elasticsearch / Vespa 默认 60.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    # 按融合分降序
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ============ HybridRetriever ============


class HybridRetriever:
    """
    包 VectorStore + 内存 BM25 索引, 对外接口跟 store 对齐.

    搜索流程 (search):
      1. 向量召回 top_k * 4 候选 id
      2. BM25 召回 top_k * 4 候选 id
      3. RRF 融合排名
      4. 取融合后 top_k, 回填 metadata, 返回跟 store.search 同结构的 dict 列表
    """

    def __init__(
        self,
        store: VectorStore,
        rrf_k: int = DEFAULT_RRF_K,
        candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER,
        reranker=None,                  # ← 新增
        rerank_candidates: int = 30,    # ← 新增 (粗排候选池, 仅在 reranker 非 None 时生效)
    ) -> None:
        self._store = store
        self._rrf_k = rrf_k
        self._cand_mult = candidate_multiplier
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates

        # BM25 内存索引: 平行三个列表, 用 id 串起来.
        # id 跟 store 内部一致, "source::index"
        self._ids: list[str] = []
        self._tokens: list[list[str]] = []
        # text 缓存, search 返回时直接给, 省一次 store 二次查询.
        # 同时也是后期 reranker 的输入.
        self._texts: dict[str, str] = {}
        self._bm25: BM25Okapi | None = None

        # 启动时从 store 拉全量重建 BM25
        self._rebuild_from_store()

    # ---------- 内部: BM25 索引 ----------

    def _rebuild_from_store(self) -> None:
        """从 store 拉全量, 重建 BM25 索引."""
        if len(self._store) == 0:
            self._ids = []
            self._tokens = []
            self._texts = {}
            self._bm25 = None
            return

        # 直接读 Chroma collection. _col 是 store 内部, 这里跨边界拿一次,
        # 是个有意识的取舍 — 给 store 加一个 get_all 方法更干净但 YAGNI.
        all_data = self._store._col.get()
        self._ids = list(all_data["ids"])
        documents = list(all_data["documents"])
        self._tokens = [tokenize_zh(doc) for doc in documents]
        self._texts = dict(zip(self._ids, documents))
        # 空 token 列表会让 BM25Okapi 崩, 兜个底
        safe_tokens = [t if t else [""] for t in self._tokens]
        self._bm25 = BM25Okapi(safe_tokens)

    def _add_to_bm25(self, items: list[dict]) -> None:
        """增量加块到 BM25 索引. rank-bm25 不支持增量, 直接全量重建."""
        for it in items:
            item_id = f"{it['source']}::{it['index']}"
            self._ids.append(item_id)
            tokens = tokenize_zh(it["text"])
            self._tokens.append(tokens)
            self._texts[item_id] = it["text"]
        safe_tokens = [t if t else [""] for t in self._tokens]
        self._bm25 = BM25Okapi(safe_tokens) if safe_tokens else None

    def _remove_from_bm25(self, source: str) -> None:
        """删某 source 在 BM25 索引里的所有块, 然后整体重建."""
        keep_ids: list[str] = []
        keep_tokens: list[list[str]] = []
        for item_id, tokens in zip(self._ids, self._tokens):
            # id 形式 "source::index", 前缀匹配
            if item_id.startswith(f"{source}::"):
                self._texts.pop(item_id, None)
                continue
            keep_ids.append(item_id)
            keep_tokens.append(tokens)
        self._ids = keep_ids
        self._tokens = keep_tokens
        safe_tokens = [t if t else [""] for t in self._tokens]
        self._bm25 = BM25Okapi(safe_tokens) if safe_tokens else None

    # ---------- 公开 API (跟 store 对齐) ----------

    def add(self, items: list[dict]) -> None:
        """加块: 先写 store (产生 embedding 和持久化), 再加到 BM25 索引."""
        if not items:
            return
        self._store.add(items)
        self._add_to_bm25(items)

    def delete_by_source(self, source: str) -> int:
        """删: store + BM25 同步."""
        n = self._store.delete_by_source(source)
        if n > 0:
            self._remove_from_bm25(source)
        return n

    def get_source_mtime(self, source: str) -> float | None:
        return self._store.get_source_mtime(source)

    def sources(self) -> list[str]:
        return self._store.sources()

    def __len__(self) -> int:
        return len(self._store)

    def _coarse_n(self, top_k: int) -> int:
        """粗排候选池大小。启用 reranker 用 rerank_candidates, 否则 top_k * 倍数。"""
        if self._reranker is not None:
            return max(self._rerank_candidates, top_k)
        return max(top_k * self._cand_mult, top_k)

    def _bm25_topn(self, query: str, n: int) -> list[str]:
        """BM25 召回 top-n id. 索引空 / 查询无 token / 0 分一律返空段。"""
        if self._bm25 is None:
            return []
        q_tokens = tokenize_zh(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [self._ids[idx] for idx, sc in ranked[:n] if sc > 0]

    def _build_candidates(
        self,
        fused_for_rerank: list[tuple[str, float]],
        meta_by_id: dict[str, dict],
    ) -> list[dict]:
        """
        把 RRF 融合结果装成 candidate dict 列表.

        - vector 路命中的 metadata 直接来自 meta_by_id
        - BM25-only 命中的走 store.get_by_ids 公开方法回填 (审视发现 8: 不戳 store._col)
        - 每个 candidate 加 _vector_score / _rrf_score 下划线前缀字段
          (审视发现 2: rerank 覆盖 score 前保留两个调试字段)
        """
        missing_ids = [
            item_id for item_id, _ in fused_for_rerank if item_id not in meta_by_id
        ]
        if missing_ids:
            for extra in self._store.get_by_ids(missing_ids):
                extra_id = f"{extra['source']}::{extra['index']}"
                meta_by_id[extra_id] = {
                    "text": extra["text"],
                    "source": extra["source"],
                    "index": extra["index"],
                    "score": 0.0,  # BM25-only 命中没有 cosine, 填 0 让老阈值毙它
                    "heading_path": extra["heading_path"],
                    "level": extra["level"],
                    "content_hash": extra["content_hash"],
                }

        rrf_by_id = dict(fused_for_rerank)
        candidates = []
        for item_id, _ in fused_for_rerank:
            c = dict(meta_by_id[item_id])
            c["_vector_score"] = c["score"]
            c["_rrf_score"] = round(rrf_by_id[item_id], 6)
            candidates.append(c)
        return candidates

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """
        Hybrid 检索: 向量 + BM25 + RRF 融合. 可选 cross-encoder 精排.

        返回字段跟 store.search 一致 (text/source/index/score + heading_path/level/content_hash).
        没 reranker 时 score = cosine 分 (BM25-only 命中填 0).
        有 reranker 时 score = sigmoid(cross-encoder logit) ∈ [0, 1].

        1.5: 候选 dict 多两个下划线前缀的内部字段 _vector_score / _rrf_score, 调试 / rerank 前后对比用.
        """
        if len(self) == 0:
            log_event("retriever.fuse", {
                "query": query,
                "top_k": top_k,
                "bm25_topk": [],
                "vector_topk": [],
                "rrf_merged": [],
                "elapsed_ms": 0,
            })
            return []

        t0 = time.perf_counter()
        coarse_n = self._coarse_n(top_k)

        # 1. 向量召回. 复用 store.search, 不用重新写 embedding 调用.
        vector_results = self._store.search(query, top_k=coarse_n)
        vector_ids = [f"{r['source']}::{r['index']}" for r in vector_results]
        meta_by_id: dict[str, dict] = {
            f"{r['source']}::{r['index']}": r for r in vector_results
        }

        # 2. BM25 召回
        bm25_ids = self._bm25_topn(query, coarse_n)

        # 3. RRF 融合
        fused = rrf_fuse([vector_ids, bm25_ids], k=self._rrf_k)
        # 有 reranker 把 RRF 前 N 条交精排; 没就直接取 top_k
        fused_for_rerank = (
            fused[: self._rerank_candidates]
            if self._reranker is not None
            else fused[:top_k]
        )

        # 4-5. 回填 metadata + 组装 candidates (审视发现 2 / 8)
        candidates = self._build_candidates(fused_for_rerank, meta_by_id)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_event("retriever.fuse", {
            "query": query,
            "top_k": top_k,
            "bm25_topk": bm25_ids[:top_k],
            "vector_topk": vector_ids[:top_k],
            "rrf_merged": [
                {"id": item_id, "score": round(s, 6)}
                for item_id, s in fused_for_rerank[:top_k]
            ],
            "elapsed_ms": elapsed_ms,
        })

        # 6. 有 reranker 过精排, 没就直接返回 top_k
        if self._reranker is not None:
            return self._reranker.rerank(query, candidates, top_k=top_k)
        return candidates[:top_k]
