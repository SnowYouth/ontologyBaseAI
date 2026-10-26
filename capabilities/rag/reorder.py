"""
rag/reorder: 长上下文 chunks 重排 (lost-in-the-middle 防御).

LLM 在长上下文里有"中间塌陷"效应 (Liu et al. 2024 "Lost in the Middle"),
中间位置的资料 LLM 召回率明显低于头尾. 我们当前 build_rag_prompt 按 score
降序拼: [资料 1] 最相关, [资料 N] 最差 — 这把"中间"位置留给了中等分块,
导致中等相关性的信息被 LLM 忽略.

策略: 长度 ≥ min_chunks 时, 把"偶数下标顺序 + 奇数下标倒序"重新排列,
让高分块占头尾、低分块挪到中间, 利用 LLM 头尾召回更准的特性.

仅在 chunks 数 ≥ min_chunks 时触发 (短列表本来就没"中间塌陷"问题, 不要画蛇添足).
"""

from core.observability import log_event


def reorder_for_long_context(
    chunks: list[dict], min_chunks: int = 5
) -> list[dict]:
    """
    chunks 已按 score 降序排列 (调用方保证). 长度 < min_chunks 时原样返回.
    否则按 odd-front 模式重排: [s0, s2, s4, ..., s_last, ..., s5, s3, s1]

    score / text / source / index 等字段透传, 只换 list 顺序.
    """
    if len(chunks) < min_chunks:
        return chunks

    evens = chunks[0::2]            # [s0, s2, s4, ...]
    odds_reversed = chunks[1::2][::-1]  # [..., s5, s3, s1]
    reordered = evens + odds_reversed

    log_event("reorder.applied", {
        "n_chunks": len(chunks),
        "min_chunks": min_chunks,
        "order_before": [c["score"] for c in chunks],
        "order_after": [c["score"] for c in reordered],
    })

    return reordered
