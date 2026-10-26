"""
rag/reranker: Cross-encoder 精排 (bge-reranker-v2-m3)。

阶段 1.5: 给 Hybrid 粗排结果叠一层"真·相关性判定"。

为啥要 rerank (而 Hybrid 已经够了不是吗):
- Hybrid (vector + BM25 + RRF) 是"粗排" — 双路独立打分再融合排名, 没有
  真正"看着 query 和 passage 一起判断相关性"。
- Cross-encoder 是把 (query, passage) 拼成一个序列丢进 BERT-like 模型,
  attention 全开, 输出一个相关性 logit. 这是"精排".
- 准确性远超双塔 / 双路融合, 代价是推理慢 (每条都得过一遍模型),
  所以工程上"粗排 N 条 → 精排 top-k". 用户原话: 30 → 5.

为啥用 bge-reranker-v2-m3:
- 中文 cross-encoder 标杆 (智源, BAAI), 基于 bge-m3
- 多语言 + 长上下文 (最长 8192 token)
- 体积可接受 (~568MB), CPU 也能跑 (单条 ~20-50ms)
- 比 v1 时代的 bge-reranker-base 强一档

模型来源 (跟 embedder 一致):
- 国内 HF 拉不通, 走 ModelScope 拉到 models/bge-reranker-v2-m3
- CrossEncoder 从本地路径加载, 不连网
- 首次使用前:
    uv run modelscope download --model BAAI/bge-reranker-v2-m3 \\
        --local_dir models/bge-reranker-v2-m3

score 字段说明 (重要):
- cross-encoder 原始输出是 logit (任意实数尺度)
- 我们 sigmoid 化到 [0, 1], 写回 candidate 的 score 字段
- 这覆盖了 HybridRetriever 之前的 cosine 分 — filter_by_score 还是同一条规则,
  但阈值数值要重调 (rerank 分跟 cosine 完全不同尺度).
- 实测后新阈值写在 .env.example 和 1.5 GUIDE.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time

from sentence_transformers import CrossEncoder

from core.observability import log_event

# 库内日志走 logging (默认 stderr), 不 print 到 stdout — 见债20:
# 加载进度属开发者诊断信息, stdout 留给用户输出/协议数据.
logger = logging.getLogger(__name__)

# 跟 embedder 一致: 项目根/models/<name>
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "bge-reranker-v2-m3")
MODEL_NAME = "BAAI/bge-reranker-v2-m3"  # 只用于打印


def _assert_model_exists() -> None:
    """模型不在本地直接报错, 不许偷偷连 HF。
    校验粒度: 目录 + config.json 都必须存在 (防止 ModelScope 下载中断的半成品)。

    抛 FileNotFoundError (Exception 子类) 让调用方决定是否兜住 — main.py
    `_load_reranker_if_enabled` 的宽 catch 会落 reranker.load_failed 埋点并降级.
    """
    missing_msg = (
        f"❌ reranker 模型不可用: {MODEL_PATH}\n"
        f"   请先在 PowerShell 跑:\n"
        f"   uv run modelscope download --model {MODEL_NAME} "
        f"--local_dir models/bge-reranker-v2-m3"
    )
    if not os.path.isdir(MODEL_PATH):
        print(missing_msg, file=sys.stderr)
        raise FileNotFoundError(missing_msg)
    # config.json 在则基本是完整下载; 不存在通常是断点
    if not os.path.isfile(os.path.join(MODEL_PATH, "config.json")):
        partial_msg = (
            f"❌ {MODEL_PATH}/config.json 缺失, 疑似下载未完成.\n"
            f"   删除目录后重新下载:\n"
            f"   Remove-Item -Recurse -Force {MODEL_PATH}\n"
            f"   uv run modelscope download --model {MODEL_NAME} "
            f"--local_dir models/bge-reranker-v2-m3"
        )
        print(partial_msg, file=sys.stderr)
        raise FileNotFoundError(partial_msg)


def _sigmoid(x: float) -> float:
    """logit → 概率, 单独抽出来好测、好埋点。
    NaN 输入返回 NaN (调用方负责排序兜底)。
    """
    if math.isnan(x):
        return float("nan")
    # 数值稳定写法, 避免 exp 溢出
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _sort_key_nan_last(score: float) -> tuple[int, float]:
    """排序 key: NaN 一律排末尾 (第一项 1 = NaN bucket, 0 = 正常 bucket)。
    主排序键还是分降序, 所以正常项 (bucket=0) 按 -score 升序 (= 分降序), NaN 排最后。
    """
    if math.isnan(score):
        return (1, 0.0)
    return (0, -score)


class Reranker:
    """
    Cross-encoder 精排. 单例懒加载模型, 多次 rerank 共用一个 CrossEncoder.

    用法:
        rr = Reranker()
        top5 = rr.rerank(query, candidates_top30, top_k=5)
    """

    def __init__(self, max_length: int = 512, batch_size: int = 16) -> None:
        _assert_model_exists()
        logger.info("加载本地 reranker 模型: %s", MODEL_PATH)
        t0 = time.perf_counter()
        # CrossEncoder 接 HF Transformers 路径, 给本地目录也认.
        self._model = CrossEncoder(MODEL_PATH, max_length=max_length)
        load_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("reranker 已加载 (耗时 %d ms)", load_ms)
        self._batch_size = batch_size
        log_event("reranker.loaded", {
            "model_path": MODEL_PATH,
            "load_ms": load_ms,
        })

    def rerank(
        self,
        query: str,
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]:
        """
        给一组候选, 用 cross-encoder 重排, 取 top_k.

        - 输入 candidates 不修改 (返回新 dict 列表)
        - 返回 dict 字段是输入字段超集 (score 被覆盖, 其他字段保留)
        - 候选为空 → 返回 []
        - query 为空 → 返回 [] (语义"没问题就没结果", 跟空候选对称)
        - cross-encoder 偶发 NaN logit (常见于 FP16): NaN 候选排到末尾, score 字段填 0.0
        """
        t0 = time.perf_counter()

        # 摘要: 用于 before/after 埋点 (放在最前面, 空候选也能发事件)
        before_summary = [
            {
                "source": c.get("source", ""),
                "index": c.get("index", 0),
                "score": round(float(c.get("score", 0.0)), 4),
            }
            for c in candidates
        ]

        # 空候选 / 空 query 都短路, 但都要发埋点 (审视发现 4)
        if not candidates or not query:
            log_event("reranker.rerank", {
                "query": query,
                "top_k": top_k,
                "before": before_summary,
                "after": [],
                "elapsed_ms": 0,
            })
            return []

        pairs = [(query, c["text"]) for c in candidates]
        logits = self._model.predict(
            pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )
        # logits 可能是 numpy.ndarray, 显式转 float 列表; sigmoid 内部已处理 NaN
        scored = [
            (i, _sigmoid(float(s)))
            for i, s in enumerate(logits)
        ]
        # NaN 一律排末尾 (审视发现 4)
        scored.sort(key=lambda x: _sort_key_nan_last(x[1]))
        top = scored[:top_k]

        results = []
        for idx, sc in top:
            r = dict(candidates[idx])
            # NaN 落到 candidate 的 score 字段, 给下游过滤一个明确语义: 直接当 0.0
            r["score"] = 0.0 if math.isnan(sc) else sc
            results.append(r)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_event("reranker.rerank", {
            "query": query,
            "top_k": top_k,
            "before": before_summary,
            "after": [
                {
                    "source": r["source"],
                    "index": r["index"],
                    "score": round(r["score"], 4),
                }
                for r in results
            ],
            "elapsed_ms": elapsed_ms,
        })
        return results
