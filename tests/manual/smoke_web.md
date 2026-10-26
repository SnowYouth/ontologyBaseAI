# 前端 MVP 端到端 smoke

## 起服务
- 后端: `uv run uvicorn web.app:app --port 8000`
- 前端: `cd frontend && pnpm dev` (→ http://localhost:5173)

> 注: 装依赖默认走官方源 pypi.org; 需要镜像可自行用 `UV_DEFAULT_INDEX` 选源 (2026-09-24 起仓库不再写死镜像, uv.lock 已对齐 pypi.org)。
> 后端启动约 15s (一次性扫 Chroma + 加载 reranker + 连库), 等 `Application startup complete` 再发请求。

## 验收场景
1. 闲聊: 输入「你好」→ 应得 agent 自然回复
2. RAG: 输入「这个项目的 RAG 怎么做的」→ agent 应触发 rag_query, 回复含项目知识
3. OBDA: 输入「号码 13800138000 的状态」→ agent 应查库返回状态
4. 多轮记忆: 先问「号码 13800138000 的状态」, 再问「它的书目包呢」→ 第二问应记得上一个号码
5. 错误显示: 停掉后端再发送 → 前端应显示红字网络错误气泡, 不白屏

## 通过标准
- 5 场景均正常; console 0 报错; 后端不崩

## 已知取舍 (MVP)
- 全局单会话: 多标签页共享同一 `_conversation`, 会串。这是 MVP 定义 (单会话), 非 bug。
- 非流式: 一次性返回整段答案, 无中间步骤呈现。
- 刷新即清空: 纯内存, 无持久化。
- 前端 BASE 写死 `localhost:8000`, 只本地跑。
- Windows GBK: `web/app.py` 已在进程入口切 `sys.stdout/stderr` 为 UTF-8, 否则含 emoji 的回复会把 `/agent` 打成 500。
