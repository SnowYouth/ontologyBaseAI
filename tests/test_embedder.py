"""rag/embedder 测试.

债20 (主线债务清算): 验证库内"加载模型"状态日志走 logging (默认 stderr),
不 print 到 stdout — 跟 reranker 同一纪律。

mock 策略: 把 SentenceTransformer 换成假类, 避免加载 95MB 真模型。
"""
import logging

import pytest


@pytest.fixture
def fake_sentence_transformer(monkeypatch):
    """把 SentenceTransformer 换成确定性假类, 不碰真模型/网络。"""
    class _FakeST:
        def __init__(self, *a, **kw):
            pass

        def get_embedding_dimension(self):
            return 512

        def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
            # 返回确定性零向量, 维度 512 (numpy 数组, 真实代码会 .tolist())
            import numpy as np
            return np.zeros((len(texts), 512), dtype=float)

    import capabilities.rag.embedder as e_mod
    monkeypatch.setattr(e_mod, "SentenceTransformer", _FakeST)
    # 路径检查跳过 (CI 无真模型目录)
    monkeypatch.setattr(e_mod.os.path, "isdir", lambda p: True)
    # 重置模块级单例, 防别的测试已加载过缓存
    monkeypatch.setattr(e_mod, "_model", None)


def test_embedder_加载日志走logging不进stdout(fake_sentence_transformer, caplog, capsys):
    """库内加载日志应走 logging (caplog 抓得到), 不进 stdout (capsys 抓不到)。"""
    import capabilities.rag.embedder as e_mod

    with caplog.at_level(logging.INFO):
        e_mod.get_model()

    logged = caplog.text
    out = capsys.readouterr().out
    assert "加载" in logged, f"加载日志应走 logging, caplog 没抓到: {logged!r}"
    assert "加载本地 embedding" not in out, f"加载日志不该进 stdout, 但 stdout 有: {out!r}"


def test_embedder_embed_texts_返回正确维度(fake_sentence_transformer):
    """回归保险: 改 logging 后 embed_texts 行为不变。"""
    from capabilities.rag.embedder import embed_texts

    out = embed_texts(["你好", "世界"])
    assert len(out) == 2
    assert len(out[0]) == 512
