"""Stage 2 端到端 smoke. 跑两个真实问题 × 当前激活的模型.

需 `config/model.json` 里有一条可用模型 (前端"设置 → 模型"页配, 或照
`.env.example` 的 `PRIMARY_*` 冷启动种子)。跑法 (在项目根):
    uv run python tests/manual/smoke_agent.py

★ 2026-10-15 LLM 层重构后, 模型三要素的真相源是 `config/model.json`
  (`core/model_config.py` 读写), 不再是 env。本脚本只跑**当前激活**的那条 ——
  `core.llm.complete()` 每次调用现读 `model_config.active()`, 想换模型在前端
  切一下再重跑即可 (换候选项的多 provider 对比见 smoke_litellm_providers.py)。

判定 (Section 12.5 spec):
1. 2 次跑全 ✅
2. JSONL agent.tool_call ≥ 2 条 / 至少一次
3. JSONL 含 4 类常规 event (start/iter/tool_call/done)
"""
from __future__ import annotations

import os
import sys

# 项目根入 sys.path (smoke 在 tests/manual/ 内, 跑时 cwd 在项目根)
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

# stdout/stdin UTF-8 (Windows)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")


TASKS = [
    "阶段 1.4 是怎么做 hybrid 检索的, 用了哪些算法",   # 期望: 调 rag_query + read_file
    "看一下 core 目录有哪些 .py 文件",                  # 期望: 调 list_dir, 不需 rag
]

def _run_one(task: str) -> bool:
    from core.runtime.agent import run_agent
    from capabilities.rag.retriever import HybridRetriever
    from capabilities.rag.store import VectorStore
    from core import model_config
    from core.config import settings
    import agent.tools as tool_pkg

    # 注入 retriever (smoke 走真实 chroma_db)
    vector_store = VectorStore(persist_dir=settings.chroma_persist_dir)
    store = HybridRetriever(vector_store)
    tool_pkg.set_retriever(store)

    cfg = model_config.active()
    print(f"\n{'='*60}\n[{cfg['name']}/{cfg['model']}] {task[:50]}\n{'='*60}")
    try:
        answer = run_agent(task)
        print(f"\n✅ 答案前 300 字:\n{answer[:300]}")
        return True
    except Exception as e:
        print(f"\n❌ FAIL: {type(e).__name__}: {e}")
        return False


def main():
    from core import model_config

    cfg = model_config.active()
    print(f"当前激活模型: {cfg['name']} → {cfg['model']}"
          f"{' (中转 ' + cfg['base_url'] + ')' if cfg.get('base_url') else ''}")

    results = []
    for task in TASKS:
        ok = _run_one(task)
        results.append((task[:30], ok))

    print(f"\n\n{'='*60}\nSmoke 结果总结\n{'='*60}")
    for task, ok in results:
        flag = "✅" if ok else "❌"
        print(f"  {flag} {task}...")

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\n通过 {n_ok}/{len(results)}")
    print("\nJSONL 在 logs/run-*.jsonl, 用 jq 查:")
    print("  cat logs/run-*.jsonl | jq -r 'select(.event==\"agent.tool_call\")'")
    print("\n错误路径验证 (单独跑):")
    print("  在前端模型页把 model 改成不存在的名字, 再跑一次, 期望:")
    print("  - 调用直接抛错 (单模型无 fallback, 2026-10-15 拍板: 挂就暴露)")
    print("  - 换回可用模型后下一次调用即恢复, 不需重启进程")


if __name__ == "__main__":
    main()
