"""阶段 1.5 阈值实测脚本 (RAG_RERANK_THRESHOLD 怎么定值).

跑法:
  1. 先把你自己的语料索引进 chroma_db/ (CLI 里 /load <你的目录>)
  2. ★ 按你的语料改下面的 RELEVANT / IRRELEVANT query 集 —— 下面那 10 条是
     当年标定 0.50 时用的中文技术文档语料, 跟你的知识库对不上就测不出东西
  3. uv run python tests/manual/smoke_reranker.py

干啥:
- 对 5 条相关 + 5 条不相关 query, 跑 Hybrid 粗排 30 → reranker 精排,
  打印每条 (rerank_score, source, text_preview)
- 输出建议阈值: min(relevant_top1) 与 max(irrelevant_top1) 的中点
- 倒挂时仍输出中点 + 警告
- stdout 按 UTF-8 输出, 避免 PowerShell 重定向 BOM 乱码.
"""
import io
import os
import sys
from datetime import datetime

# 让 tests/manual/ 能 import 项目模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Windows 控制台 emoji 兜底 (跟 main.py 一致思路, 但不破坏 stdout 类型)
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from capabilities.rag.reranker import Reranker  # noqa: E402
from capabilities.rag.retriever import HybridRetriever  # noqa: E402
from capabilities.rag.store import VectorStore  # noqa: E402
from core.config import settings  # noqa: E402


NOTES_PATH = os.path.join(
    PROJECT_ROOT, "journal", "stages", "stage-1-5-threshold-calibration.md"
)

# ---- 实测 query 集 (5 相关 + 5 不相关) ----
# ★ 这是示例, 不是固定值: 标定 0.50 时用的是一批 RAG/检索主题的中文技术文档。
#   换成你自己的语料就得换这两组 —— "相关"要能在你库里命中, "不相关"要确定命中不了。
RELEVANT = [
    "什么是 RAG",
    "RAG 检索增强生成怎么实现",
    "Hybrid 检索是怎么融合的",
    "BM25 跟向量检索有啥区别",
    "chunker 切块策略有几种",
]
IRRELEVANT = [
    "今晚吃什么",
    "比特币最近涨了吗",
    "怎么训狗",
    "明天天气如何",
    "推荐一首流行歌",
]


class _Tee(io.TextIOBase):
    """同步把写入分发给多个 stream (stdout + file)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
            st.flush()
        return len(s)

    def flush(self):
        for st in self._streams:
            st.flush()


def _emit_header(n_chunks: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("# 阶段 1.5 rerank 阈值校准 (原始数据)")
    print("")
    print(f"- 跑于: {now}")
    print(f"- docs 规模: {n_chunks} 块")
    print(f"- query 集: 5 相关 + 5 不相关 (脚本内 RELEVANT / IRRELEVANT)")
    print(f"- 模型: bge-reranker-v2-m3 (本地 models/, 实际 ~2.12GB)")
    print(f"- 检索: HybridRetriever(rerank_candidates=30) → top_k=5")
    print("")
    print("## 结论")
    print("")
    print("> 脚本算好的建议阈值在末尾\"阈值参考\"节, 人眼判定后向下取整 2 位小数填到 .env.example")
    print("")
    print("## 原始输出")
    print("")
    print("```text")


def _emit_footer(rel_scores, irr_scores):
    print("```")
    print("")
    print("## 选定阈值")
    print("")
    if not rel_scores or not irr_scores:
        print("- ⚠️ 缺数据: 相关或不相关 top1 分为空, 检查 RELEVANT/IRRELEVANT query 是否都至少命中 1 条")
        return
    rel_min = min(rel_scores)
    irr_max = max(irr_scores)
    mid = (rel_min + irr_max) / 2
    print(f"- min(relevant_top1) = {rel_min:.4f}")
    print(f"- max(irrelevant_top1) = {irr_max:.4f}")
    if rel_min > irr_max:
        print(f"- 分布健康 ✅, 建议阈值 (中点): {mid:.4f}")
        print(f"- 写到 .env: 向下取整 2 位小数, 留容错 (例如 {mid:.4f} → {((int(mid * 100)) / 100):.2f})")
    else:
        gap = irr_max - rel_min
        print(f"- 分布倒挂 ⚠️ (相关 min {rel_min:.4f} ≤ 不相关 max {irr_max:.4f}, 重叠 {gap:.4f})")
        print(f"- 仍用中点 {mid:.4f}, 但 reranker 在当前 11 块 docs 上分辨力不足")
        print(f"- 建议: 扩 docs / 换模型 / 用 ROC-Youden 等更细的算法")


def _run_query(retriever: HybridRetriever, query: str, tag: str) -> list[float]:
    print(f"\n=== [{tag}] {query} ===")
    results = retriever.search(query, top_k=5)
    scores: list[float] = []
    for i, r in enumerate(results, 1):
        preview = r["text"][:60].replace("\n", " ")
        src = os.path.basename(r.get("source", ""))
        print(f"  [{i}] score={r['score']:.4f} | {src} | {preview}")
        scores.append(float(r["score"]))
    return scores


def _emit_summary(rel_scores: list[float], irr_scores: list[float]):
    print("")
    print("========== 阈值参考 ==========")
    print(f"相关 query top1 分 (越高越好): {[round(s, 4) for s in rel_scores]}")
    print(f"不相关 query top1 分 (越低越好): {[round(s, 4) for s in irr_scores]}")
    if not rel_scores or not irr_scores:
        print("⚠️ 相关或不相关 top1 分为空, 无法算阈值")
        return
    rel_min = min(rel_scores)
    irr_max = max(irr_scores)
    mid = (rel_min + irr_max) / 2
    print(f"  min(relevant_top1) = {rel_min:.4f}")
    print(f"  max(irrelevant_top1) = {irr_max:.4f}")
    if rel_min > irr_max:
        print(f"✅ 分布健康, 建议阈值: {mid:.4f}")
    else:
        gap = irr_max - rel_min
        print(f"⚠️ 分布倒挂! 相关 min {rel_min:.4f} ≤ 不相关 max {irr_max:.4f}, 重叠 {gap:.4f}")
        print(f"   仍用中点 {mid:.4f}, 但 reranker 在当前 docs 上分辨力不足")
    print(f"建议阈值: {mid:.4f}")


def main():
    vs = VectorStore(persist_dir=settings.chroma_persist_dir)
    n_chunks = len(vs)
    if n_chunks == 0:
        print("❌ chroma_db 为空, 先跑 main.py /load corpus/", file=sys.stderr)
        sys.exit(1)

    # 同步把所有 stdout 双写到 journal 文件 (UTF-8, 绕开 PowerShell 重定向 BOM 坑)
    os.makedirs(os.path.dirname(NOTES_PATH), exist_ok=True)
    f = open(NOTES_PATH, "w", encoding="utf-8")
    original_stdout = sys.stdout
    sys.stdout = _Tee(original_stdout, f)

    try:
        _emit_header(n_chunks)
        print(f"📚 库里 {n_chunks} 块")
        rr = Reranker()
        retriever = HybridRetriever(vs, reranker=rr, rerank_candidates=30)

        rel_scores: list[float] = []
        for q in RELEVANT:
            s = _run_query(retriever, q, "相关")
            if s:
                rel_scores.append(s[0])

        irr_scores: list[float] = []
        for q in IRRELEVANT:
            s = _run_query(retriever, q, "不相关")
            if s:
                irr_scores.append(s[0])

        _emit_summary(rel_scores, irr_scores)
        _emit_footer(rel_scores, irr_scores)
    finally:
        sys.stdout = original_stdout
        f.close()

    print(f"\n📝 校准档案已写到: {NOTES_PATH}")


if __name__ == "__main__":
    main()
