"""测 rag/reorder.py 的纯函数 reorder_for_long_context."""

from capabilities.rag.reorder import reorder_for_long_context


def _mk_chunks(scores):
    """造测试用 chunks, score 由参数指定, 其它字段固定可识别。"""
    return [
        {
            "text": f"text-{i}",
            "source": f"src-{i}.md",
            "index": i,
            "score": s,
        }
        for i, s in enumerate(scores)
    ]


def test_长度小于_min_chunks_原样返回():
    """min_chunks=5, len=4 -> 原样返回 (顺序、内容都不变)。"""
    chunks = _mk_chunks([0.9, 0.8, 0.7, 0.6])
    out = reorder_for_long_context(chunks, min_chunks=5)
    assert out == chunks


def test_长度等于_5_奇数位高分前置():
    """len=5 -> [s0, s2, s4, s3, s1] (偶数下标顺序, 奇数下标倒序)。"""
    chunks = _mk_chunks([0.9, 0.8, 0.7, 0.6, 0.5])
    out = reorder_for_long_context(chunks, min_chunks=5)
    assert [c["score"] for c in out] == [0.9, 0.7, 0.5, 0.6, 0.8]


def test_长度等于_6_偶数下标顺序_奇数下标倒序():
    """len=6 -> [s0, s2, s4, s5, s3, s1]."""
    chunks = _mk_chunks([0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
    out = reorder_for_long_context(chunks, min_chunks=5)
    assert [c["score"] for c in out] == [0.9, 0.7, 0.5, 0.4, 0.6, 0.8]


def test_字段透传_只换顺序_不改值():
    """重排只调整 list 顺序, chunk 内 score/text/source/index 字段不动。"""
    chunks = _mk_chunks([0.9, 0.8, 0.7, 0.6, 0.5])
    out = reorder_for_long_context(chunks, min_chunks=5)
    # 每个原 chunk 必须能在 out 里找到完全相等的对象 (无字段被改)
    for c in chunks:
        assert c in out
    assert len(out) == len(chunks)


def test_min_chunks_参数生效_可调阈值():
    """min_chunks=3 时 len=3 触发重排; min_chunks=5 时同 len=3 不触发。"""
    chunks = _mk_chunks([0.9, 0.8, 0.7])
    # min_chunks=3 触发: [0.9, 0.7] 偶数下标, [0.8] 奇数下标倒序 -> [0.9, 0.7, 0.8]
    out_low = reorder_for_long_context(chunks, min_chunks=3)
    assert [c["score"] for c in out_low] == [0.9, 0.7, 0.8]
    # min_chunks=5 不触发, 原样返回
    out_high = reorder_for_long_context(chunks, min_chunks=5)
    assert out_high == chunks
