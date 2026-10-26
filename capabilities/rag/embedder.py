"""
embedder：把文本变成向量 (embedding)。

向量是啥? —— 一串浮点数 (这里是 512 维),代表这段话在"语义空间"里的位置。
两段意思相近的话,它们的向量在这个空间里距离也近。"语义检索"就靠这个性质。

我们用的模型 BAAI/bge-small-zh-v1.5:
- 中文社区标杆,智源研究院出
- 95MB,CPU 也能跑
- 输出 512 维向量

模型来源:
2026 年初 HuggingFace 转向 xet 后端,国内 hf-mirror 同步不了大文件,所以走 ModelScope
(阿里魔搭) 把模型预先下载到本地 models/ 目录,SentenceTransformer 从本地路径加载,
彻底绕开网络。
首次使用前需要在 PowerShell 跑一次:
    uv run modelscope download --model BAAI/bge-small-zh-v1.5 --local_dir models/bge-small-zh-v1.5
"""

import os
import sys
import time
import logging

from sentence_transformers import SentenceTransformer

from core.observability import log_event

# 库内日志走 logging (默认 stderr), 不 print 到 stdout — 见债20:
# 加载进度属开发者诊断信息, stdout 留给用户输出/协议数据.
logger = logging.getLogger(__name__)

# 模型在项目根目录的相对位置 (models/bge-small-zh-v1.5); 本文件在 capabilities/rag/ 下, 上溯三层
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "bge-small-zh-v1.5")
MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # 仅用于打印,不实际访问网络

# 全局只加载一次模型 (加载要几秒,反复加载是浪费)
_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """懒加载模型。第一次调用时从本地路径加载,之后直接返回缓存的实例。"""
    global _model
    if _model is None:
        if not os.path.isdir(MODEL_PATH):
            print(
                f"❌ 模型目录不存在: {MODEL_PATH}\n"
                f"   请先在 PowerShell 跑:\n"
                f"   uv run modelscope download --model {MODEL_NAME} "
                f"--local_dir models/bge-small-zh-v1.5",
                file=sys.stderr,
            )
            raise SystemExit(1)
        logger.info("加载本地 embedding 模型: %s", MODEL_PATH)
        t0 = time.perf_counter()
        _model = SentenceTransformer(MODEL_PATH)
        load_ms = int((time.perf_counter() - t0) * 1000)
        # 5.5+ 改名为 get_embedding_dimension(); 用兼容写法避免旧版本报错
        dim_fn = getattr(_model, "get_embedding_dimension", None) or _model.get_sentence_embedding_dimension
        dim = dim_fn()
        logger.info("embedding 模型已加载, 向量维度: %d", dim)
        # 可观测性: 首次加载非常重 (动辄数秒), 必埋
        log_event("embedder.loaded", {
            "model_path": MODEL_PATH,
            "dim": dim,
            "load_ms": load_ms,
        })
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    把一堆文本批量转成向量。批量比逐个快得多 —— GPU/CPU 都喜欢 batch。

    返回 list[list[float]],每个内层 list 是一个 512 维向量。
    """
    model = get_model()
    # normalize_embeddings=True: 把向量长度归一化到 1,这样后面算"余弦相似度"
    # 就退化成简单的点积,快且数值更稳定。bge 官方推荐这么用。
    t0 = time.perf_counter()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    out = vectors.tolist()
    log_event("embedder.encode", {
        "n_texts": len(texts),
        "dim": len(out[0]) if out else 0,
        "elapsed_ms": elapsed_ms,
    })
    return out


def embed_text(text: str) -> list[float]:
    """单条文本转向量。"""
    return embed_texts([text])[0]
