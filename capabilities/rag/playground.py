"""
playground：阶段 1.1 的实验台。

把 chunker → embedder → store 串起来跑一遍,让你看到每一步的中间产物:
1. 读 corpus/ 下所有文档
2. 切块,打印切了多少块、第一块长啥样
3. 向量化,打印第一个向量的前几维 (让你直观看到"向量"是啥)
4. 喂三个问题,看检索的 top-k 结果

用法: uv run python -m rag.playground
"""

import glob
import os
import sys

# Windows 默认 GBK 编码搞不定 emoji,强制 stdout 走 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from .chunker import chunk_file
from .store import VectorStore


def main():
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    corpus_dir = os.path.join(project_root, "corpus")

    # ---- 1. 找文档 ----
    files = sorted(
        glob.glob(os.path.join(corpus_dir, "*.md"))
        + glob.glob(os.path.join(corpus_dir, "*.txt"))
    )
    if not files:
        print(f"❌ corpus 目录 ({corpus_dir}) 里没找到 .md/.txt 文件,请先放几个进去。")
        return

    print(f"📄 找到 {len(files)} 个文档:")
    for f in files:
        print(f"   - {os.path.basename(f)}")
    print()

    # ---- 2. 切块 ----
    all_chunks = []
    for f in files:
        chunks = chunk_file(f, chunk_size=300, chunk_overlap=50)
        print(f"   {os.path.basename(f)} 切成 {len(chunks)} 块")
        all_chunks.extend(chunks)
    print(f"\n📦 总共 {len(all_chunks)} 块。第一块内容预览:")
    print(f"   {all_chunks[0]['text'][:120]}...\n")

    # ---- 3. 向量化 + 入库 ----
    store = VectorStore()
    store.add(all_chunks)

    # 偷看一眼第一个向量长啥样,只看前 5 维就够了 (整 512 维你也看不完)
    print(f"🔢 第一块对应的向量 (前 5 维): {store.vectors[0][:5]}")
    print(f"   完整向量维度: {store.vectors.shape}\n")  # (N, 512)

    # ---- 4. 检索几个问题,看 top-k 结果 ----
    queries = [
        "这个项目的目标是什么?",
        "Python 用的什么版本?",
        "今天晚饭吃什么?",  # 故意来个完全不相关的,看会检索出啥
    ]

    for q in queries:
        print("=" * 60)
        print(f"❓ 查询: {q}")
        results = store.search(q, top_k=3)
        for i, r in enumerate(results, 1):
            print(f"\n  [{i}] 相似度 {r['score']:.4f}  |  {os.path.basename(r['source'])}#chunk{r['index']}")
            print(f"      {r['text'][:150]}...")
        print()


if __name__ == "__main__":
    main()
