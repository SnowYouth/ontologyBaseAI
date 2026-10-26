"""
ontologyBaseAI 阶段 1.3: CLI 对话客户端 + RAG + 持久化

阶段 1.3 新增 (相对 1.2):
- 向量库换成 ChromaDB, 启动自动从磁盘加载, 不用每次 /load
- /load 加 mtime 检测: 新文件入库 / 改过的文件删旧+加新 / 没动过的跳过
- 启动打印 "📚 知识库已就绪 N 块 (M 个文件)"

老命令保持:
- /load <文件夹>: 索引文件夹下 .md/.txt (按 mtime 增量)
- /reset:        清空知识库 (跟 /clear 对应)
- /ask <问题>:   走 RAG 检索 -> 拼 prompt -> LLM 流式回答
- 普通输入仍走原对话链路

用法: uv run python main.py
"""

import glob
import os
import sys

# Windows 默认 GBK 编码搞不定 emoji + 非 GBK 输入 (如 UTF-8 管道喂中文).
# stdout/stdin/stderr 三个都要切到 UTF-8:
# - stdout/stdin: 否则非交互测试 (printf "/ask 中文" | python main.py) 时中文会被按
#   GBK 解码成 lone surrogate (\udcXX), 下游 HF tokenizer 会拒收报
#   "TextEncodeInput must be Union[...]". 阶段 1 债务清算第 1 轮 smoke 暴露.
# - stderr: 漏了它, bootstrap 的中文降级警告在控制台是乱码 —— 正是没内网库的人
#   唯一该看懂的那几行 (2026-09-24 冷启动真跑 main.py 暴露).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from core.runtime.agent import run_agent
from core.runtime.bootstrap import init_agent_runtime
from core import model_config
from core.config import is_placeholder_api_key, settings
from core.conversation import Conversation
from capabilities.rag.chunker import chunk_file
from capabilities.rag.retriever import HybridRetriever

# 真相源是 model_config (config/model.json), .env 的 PRIMARY_* 只是它缺失时的种子 ——
# 用户可能只在 Web 设置面板配过模型, 从没碰 .env, 那时 .env 的占位值不该拦住 CLI。
_active_model = model_config.active()

if is_placeholder_api_key(_active_model.get("api_key", "")):
    print("❌ 还没配 API key (当前是空值或 .env.example 的占位值)")
    print("   两种配法任选: ① cp .env.example .env 后填 PRIMARY_API_KEY / PRIMARY_BASE_URL")
    print("                ② 起 Web 前端, 在 ⚙ 设置 → 🧠 模型 里加一条并激活")
    sys.exit(1)


# ============ 命令处理 ============


def _do_load(folder: str, store: HybridRetriever) -> None:
    """
    把文件夹下的 .md / .txt 索引到 store。
    按文件 mtime 决定三种处理:
      1) 不在库: 切块入库 (新增)
      2) 在库但 mtime 比记录的新: 删旧块 + 入新块 (重建)
      3) 在库且 mtime 没变: 跳过 (打印 X 个已是最新)

    注: store 类型是 HybridRetriever, 接口跟 VectorStore 对齐
    (add / delete_by_source / get_source_mtime / __len__), 这里改动最小.
    """
    if not folder:
        print("❓ 用法: /load <文件夹路径>  例: /load corpus/\n")
        return
    if not os.path.isdir(folder):
        print(f"❌ 找不到文件夹: {folder}\n")
        return

    files = sorted(
        glob.glob(os.path.join(folder, "*.md"))
        + glob.glob(os.path.join(folder, "*.txt"))
    )
    if not files:
        print(f"❌ {folder} 里没有 .md/.txt 文件\n")
        return

    print(f"📄 找到 {len(files)} 个文件, 检测 mtime...")
    added = 0   # 新文件
    rebuilt = 0 # 改过的文件
    skipped = 0 # 没动的
    before = len(store)

    for f in files:
        # 用绝对路径当 source, 避免相对路径在不同 cwd 下歧义
        source = os.path.abspath(f)
        cur_mtime = os.path.getmtime(f)
        old_mtime = store.get_source_mtime(source)

        basename = os.path.basename(f)

        if old_mtime is None:
            # 没见过的文件 → 切块入库
            chunks = chunk_file(
                f, chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap
            )
            for c in chunks:
                c["source"] = source   # 统一用绝对路径
                c["mtime"] = cur_mtime
            store.add(chunks)
            print(f"   + {basename} -> {len(chunks)} 块 (新增)")
            added += 1
        elif cur_mtime > old_mtime:
            # 改过的文件 → 删旧块, 切新块, 入库
            deleted = store.delete_by_source(source)
            chunks = chunk_file(
                f, chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap
            )
            for c in chunks:
                c["source"] = source
                c["mtime"] = cur_mtime
            store.add(chunks)
            print(f"   ~ {basename} -> 删 {deleted} 块, 入 {len(chunks)} 块 (重建)")
            rebuilt += 1
        else:
            skipped += 1

    print(
        f"✅ 完成: 新增 {added} 文件, 重建 {rebuilt} 文件, 跳过 {skipped} 文件 (已是最新)"
    )
    print(f"   知识库现共 {len(store)} 块 (本轮净变化 {len(store) - before:+d})\n")


def handle_command(cmd: str, conversation: Conversation, store: HybridRetriever) -> bool:
    """
    处理 / 开头的命令。
    返回 True = 已处理 (主循环 continue), False = 让主循环按普通对话处理。
    /exit 不在这里, 留给主循环 break。
    """
    parts = cmd.split(maxsplit=1)
    name = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name == "/clear":
        conversation.clear()
        print("🧹 对话历史已清空 (知识库未动, 想清知识库用 /reset)\n")
        return True

    if name == "/reset":
        # 按 source 逐个删 —— Chroma 没有"清空 collection"的直接 API,
        # 用 sources() + delete_by_source 一遍过去
        for src in store.sources():
            store.delete_by_source(src)
        print("🧹 知识库已清空\n")
        return True

    if name == "/load":
        _do_load(arg, store)
        return True

    if name == "/history":
        snap = conversation.snapshot()
        print(f"\n📚 当前对话 (共 {len(snap)} 条):")
        for i, m in enumerate(snap):
            content = m["content"]
            if isinstance(content, str) and len(content) > 200:
                content = content[:200] + f"...(共 {len(m['content'])} 字)"
            print(f"  [{i}] {m['role']}: {content}")
        print()
        return True

    if name == "/kb":
        # 看看知识库状态
        srcs = store.sources()
        print(f"\n📚 知识库当前 {len(store)} 块, {len(srcs)} 个文件")
        if srcs:
            shown = sorted({os.path.basename(s) for s in srcs})
            print(f"   来自: {', '.join(shown)}")
        print()
        return True

    if name.startswith("/"):
        print(
            f"❓ 未知命令: {name}\n"
            f"   可用: /clear /reset /history /load /kb /exit\n"
        )
        return True

    return False


def _print_startup_banner(store: HybridRetriever) -> None:
    """
    打印启动 banner: 知识库统计 + 检索模式 + 未校准阈值警告 + Round 2 三模式状态.

    - 知识库空 → 只打提示 + 三模式行
    - reranker is None → 纯 Hybrid 模式 banner
    - reranker 在 → Hybrid+Rerank banner; 阈值 < 0 加哨兵警告
    - 末尾追加 Round 2 三模式开关 (reorder / history truncate)
    """
    n_chunks = len(store)
    n_files = len(store.sources())
    if n_chunks == 0:
        print(f"📚 知识库为空 (持久化目录: {settings.chroma_persist_dir}/)")
    else:
        print(f"📚 知识库已就绪 {n_chunks} 块 ({n_files} 个文件, 来自 {settings.chroma_persist_dir}/)")
        if store._reranker is not None:
            print(
                f"🔀 检索模式: Hybrid + Rerank "
                f"(粗排 top-{settings.rag_rerank_candidates} → 精排 top-{settings.rag_top_k})"
            )
            if settings.rag_rerank_threshold < 0:
                print(
                    f"⚠️  RAG_RERANK_THRESHOLD={settings.rag_rerank_threshold} 是未校准哨兵值,\n"
                    f"   阈值过滤已禁用 (所有 sigmoid [0,1] 分都通过). 请跑 Task 5 实测校准."
                )
        else:
            print(f"🔀 检索模式: Hybrid (Vector + BM25 + RRF k={60})")

    # Round 2 三模式状态 (debt cleanup round 2): 不论知识库是否为空都打, 用户能一眼看见开关
    print(
        f"🔄 重排: {'on' if settings.rag_reorder_enabled else 'off'} "
        f"(min_chunks={settings.rag_reorder_min_chunks}) "
        f"｜ History 截断: {'on' if settings.history_truncate_enabled else 'off'} "
        f"(rounds={settings.history_max_rounds}, chars={settings.history_max_chars})"
    )


def main():
    print(f"💬 中转: {_active_model.get('base_url') or '(官方端点)'}")
    print(f"🧠 模型: {_active_model['model']}  (配置: {_active_model.get('name', '?')})")
    print(f"📊 RAG 阈值 {settings.rag_score_threshold} / top-k {settings.rag_top_k}")

    conversation = Conversation()
    # 抽取到 core/bootstrap.py: retriever + ontology + 只读库连接 + system prompt 注入,
    # CLI 和 Web 后端 (Task 2) 共用同一套初始化 (DRY 单一事实源).
    store = init_agent_runtime()

    _print_startup_banner(store)

    print(
        "📝 命令: /load <目录>  /kb  /reset  /clear  /history  /exit\n"
        "   直接打字即与 agent 对话 (是否检索由 agent 自主决定)\n"
    )

    while True:
        try:
            user_input = input("🧑 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见")
            break

        if not user_input:
            continue
        if user_input == "/exit":
            print("👋 再见")
            break
        if handle_command(user_input, conversation, store):
            continue

        try:
            run_agent(user_input, conversation=conversation)
            print()  # 答案已在 _llm_step 流式打出, 仅补一个空行分隔
        except Exception as e:
            print(f"\n❌ 出错了: {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    main()
