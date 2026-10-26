"""
store: 用 ChromaDB 做持久化的向量库。

阶段 1.3 把内存版换掉, 接口尽量保持向后兼容:
  add / search / __len__ 三个老方法签名不变 (老测试照样能跑);
  新增 delete_by_source / get_source_mtime / sources 给"按文件 mtime 重 load"用。

为啥用 Chroma:
- 文件级持久化, 启动直接读盘, 不用每次重新 embed
- 自带元数据过滤, "按 source 删除某文件所有块"一行 API
- 接口稳, 后期换 Milvus / pgvector 时这层 store.py 还是单文件可换

为啥还是从外面传向量, 不用 Chroma 内置 embedding:
- 项目里 embedder.py 已经把本地 bge-small-zh 装好了, 复用它
- Chroma 默认的 EmbeddingFunction 会去 HuggingFace 拉模型, 走不通 (见 [[stage-1-1-done]])
- 让 embedder 留在外面, store 只负责存和检索, 职责更干净

距离 vs 分数:
- chromadb 默认空间是 L2; 我们指定 cosine, 距离 d ∈ [0, 2]
- score 取 1 - d, 数值范围跟阶段 1.1 的 numpy 实现完全对齐
  (归一化向量的余弦相似度 = 1 - 余弦距离 / 2 ... 但 chroma 的 cosine distance
   本身就定义为 1 - cos, 所以 score = 1 - d 正好等于 cos 相似度)
"""

import chromadb

from core.observability import log_event

from .embedder import embed_text, embed_texts


# Chroma collection 名要求 3-512 字符, 单一集合够用
_COLLECTION = "knowledge"


class VectorStore:
    """基于 ChromaDB 的向量库, 持久化到磁盘。"""

    def __init__(self, persist_dir: str) -> None:
        # PersistentClient 自动把数据写到 persist_dir, 重启进程后再开同一目录数据还在
        self._client = chromadb.PersistentClient(path=persist_dir)
        # 显式指定 cosine, 不然默认 L2, score 计算就要换公式
        self._col = self._client.get_or_create_collection(
            _COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    # ---------- 写 ----------

    def add(self, items: list[dict]) -> None:
        """
        添加文档块。items 元素形如:
          {"text": "...", "source": "...", "index": 0, "mtime": 1700000000.0,
           "heading_path": "标题1 > 标题1.2", "level": 2, "content_hash": "..."}
        - mtime 缺省存 0.0 (阶段 1.1/1.2 老调用方还没传, 兼容)
        - heading_path/level/content_hash 是阶段 1.4 chunker D2 新增,
          缺省存 "" / 0 / "" (老调用方也能跑)

        Chroma 必须传唯一 id, 我们用 "source::index" 拼。
        这意味着调用方对同一 (source, index) 不能重复 add —— 想重建就先 delete_by_source。
        """
        if not items:
            return

        ids = [f"{it['source']}::{it['index']}" for it in items]
        documents = [it["text"] for it in items]
        embeddings = embed_texts(documents)
        metadatas = [
            {
                "source": it["source"],
                "index": it["index"],
                "mtime": float(it.get("mtime", 0.0)),
                # 阶段 1.4: chunker 新字段透传, 老调用方不传时用默认值
                "heading_path": it.get("heading_path", ""),
                "level": int(it.get("level", 0)),
                "content_hash": it.get("content_hash", ""),
            }
            for it in items
        ]
        self._col.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        log_event("store.add", {"n_items": len(items)})

    def delete_by_source(self, source: str) -> int:
        """
        删掉某个 source 的所有块, 返回删除条数。
        典型场景: 文件 mtime 变了, 旧块要清掉重 add。
        文件不在库时返回 0, 不报错。
        """
        existing = self._col.get(where={"source": source})
        count = len(existing["ids"])
        if count == 0:
            return 0
        self._col.delete(where={"source": source})
        return count

    # ---------- 读 ----------

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """
        给一个自然语言查询, 返回最相关的 top_k 个块。
        每个结果保持跟阶段 1.1 一致的字段: text/source/index/score。
        """
        if self._col.count() == 0:
            log_event("store.search", {"query": query, "top_k": top_k, "results": []})
            return []

        q_vec = embed_text(query)
        # n_results 不能超过库里现有条数, 否则部分版本会报错
        n_results = min(top_k, self._col.count())
        raw = self._col.query(query_embeddings=[q_vec], n_results=n_results)

        # chroma 返回的是 list[list], 我们只查了 1 个 query, 所以全取 [0]
        documents = raw["documents"][0]
        metadatas = raw["metadatas"][0]
        distances = raw["distances"][0]

        results = []
        for doc, meta, dist in zip(documents, metadatas, distances):
            results.append({
                "text": doc,
                "source": meta["source"],
                "index": meta["index"],
                # cosine 距离 d ∈ [0, 2], score = 1 - d 跟阶段 1.1 的余弦相似度同口径
                "score": 1.0 - float(dist),
                # 阶段 1.4: 透传 chunker 新字段; 老数据 metadata 没有时给默认值
                "heading_path": meta.get("heading_path", ""),
                "level": int(meta.get("level", 0)),
                "content_hash": meta.get("content_hash", ""),
            })

        # 可观测性: 检索是 RAG 链路最关键一环, payload 摘要每条命中
        log_event("store.search", {
            "query": query,
            "top_k": top_k,
            "results": [
                {
                    "text_preview": r["text"][:80],
                    "score": round(r["score"], 4),
                    "source": r["source"],
                }
                for r in results
            ],
        })
        return results

    def get_by_ids(self, ids: list[str]) -> list[dict]:
        """
        按 id 批量取块. 返回字段跟 search 输出对齐 (除 score, 因为没 query 无法算分).
        - 空列表 → []
        - id 部分不存在 → 只返回命中的, 不报错
        - 用途: HybridRetriever 融合排名后, 回填那些"只在 BM25 路命中, vector 路没召回"
                的块的 metadata.
        """
        if not ids:
            return []
        raw = self._col.get(ids=ids)
        out = []
        for i, item_id in enumerate(raw["ids"]):
            m = raw["metadatas"][i]
            out.append({
                "text": raw["documents"][i],
                "source": m["source"],
                "index": m["index"],
                "heading_path": m.get("heading_path", ""),
                "level": int(m.get("level", 0)),
                "content_hash": m.get("content_hash", ""),
            })
        return out

    def get_source_mtime(self, source: str) -> float | None:
        """
        返回某 source 在库里记录的 mtime; 不在库返回 None。
        给上层做"文件改没改"判断, 改了才走"删 + 重 add"。
        """
        got = self._col.get(where={"source": source}, limit=1)
        if not got["ids"]:
            return None
        return float(got["metadatas"][0]["mtime"])

    def sources(self) -> list[str]:
        """列出库里所有 source (去重), 给 /kb 命令展示用。"""
        if self._col.count() == 0:
            return []
        # 拉全表的 metadata; 知识库规模小 (学习项目), 不需要分页
        all_meta = self._col.get()["metadatas"]
        return list({m["source"] for m in all_meta})

    def __len__(self) -> int:
        return self._col.count()
