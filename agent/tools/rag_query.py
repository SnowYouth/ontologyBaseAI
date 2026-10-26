"""agent/tools/rag_query.py — 把项目 RAG 检索包成 agent 工具.

设计:
1. 模块级 _retriever 变量, main.py 启动期 set_retriever(store) 注入
   不在 import 时强耦合 rag.retriever, 单元测试容易 mock
2. top_k 上下限裁剪 (1 ≤ top_k ≤ 20) 防 LLM 给离谱值
3. retriever 未注入返回 error dict, 不抛 — 让 agent loop 看到 error 回填给 LLM 处理

Note: 不用 `from __future__ import annotations` — base._type_to_json_schema 用
`annotation in _TYPE_MAP` 直接比对类型对象, future import 会让注解变字符串匹配失败.
Python 3.12 原生支持 `Any | None` / `list[dict]` 运行时注解, 无需 future.
"""
import os
from typing import Any

from core.config import settings
from core.observability import log_event
from capabilities.rag.reorder import reorder_for_long_context
from agent.tools.base import register_tool

# 启动期由 main.py 注入. 类型注释成 Any 避免 import rag.retriever 形成循环.
_retriever: Any | None = None


def set_retriever(r: Any) -> None:
    """main.py 启动期调一次, 把 HybridRetriever 实例注入."""
    global _retriever
    _retriever = r


@register_tool(category="serving")
def rag_query(query: str, top_k: int = 5) -> dict:
    """在项目知识库 (corpus/) 中检索与 query 相关的文档片段.

    适用: 想了解 ontologyBaseAI 项目的设计 / 决策 / 阶段进度时.
    不适用: 一般编程问题、跟本项目无关的问题 (直接答即可, 不用调本工具).

    重要 (指代消解): 传入 query 前请结合对话历史把指代/省略补全成完整检索短语.
    例: 用户问"那它怎么用", 应传 query="HybridRetriever 怎么用" 而不是原话"那它怎么用"
    —— 向量检索看不到对话历史, 只吃这一个 query 字符串, 指代不消解检索必然打偏.

    Args:
        query: 检索短语, 中文, 已消解指代 (例: "hybrid 检索 阶段 1.4")
        top_k: 返回结果数, 默认 5, 想多查就调大 (上限 20)

    Returns:
        {"hits": [{"source": "...", "score": 0.81, "text": "..."}], "n": 3}
        低分块已按阈值过滤掉. hits 空 + 带 note 表示召回的分都太低 (不是没召回),
        agent 可据此换问法或直接作答.
    """
    if _retriever is None:
        return {"error": "retriever not initialized"}
    k = min(max(top_k, 1), 20)
    raw = _retriever.search(query, top_k=k)

    threshold = settings.rag_score_threshold
    hits = [h for h in raw if h.get("score", 0) >= threshold]

    if not hits:
        top = raw[0].get("score", 0) if raw else 0.0
        _print_hits([], len(raw), threshold, top)
        return {"hits": [], "n": 0,
                "note": f"无资料过阈值 (最高分 {top:.3f} < 阈值 {threshold})"}

    if settings.rag_reorder_enabled:
        hits = reorder_for_long_context(hits, min_chunks=settings.rag_reorder_min_chunks)
    else:
        log_event("reorder.disabled", {"reason": "disabled", "n_chunks": len(hits)})

    _print_hits(hits, len(raw), threshold, None)

    return {
        "hits": [{"source": h["source"], "score": h.get("score", 0), "text": h["text"]}
                 for h in hits],
        "n": len(hits),
    }


def _print_hits(hits: list, n_raw: int, threshold: float, top_score: float | None) -> None:
    """命中可视化: 让用户看到 agent 基于哪些 chunk 答 (工具内 print, 与 agent loop 的
    [工具] 调用... 摘要并列)。hits 空时打一行"无资料过阈值"。"""
    if not hits:
        print(f"\n🔎 无资料过阈值 (最高分 {top_score:.3f} < {threshold}, 共 {n_raw} 条候选)")
        return
    print(f"\n🔎 命中 {len(hits)} 条资料 (从 {n_raw} 条候选过滤):")
    for i, h in enumerate(hits, start=1):
        src = os.path.basename(h["source"])
        print(f"   [{i}] {h.get('score', 0):.3f} | {src}#chunk{h.get('index', '?')}")
