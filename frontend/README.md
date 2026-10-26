# frontend — ontologyBaseAI Web 前端

React + TypeScript + Vite。两件事: 把 agent 的工具调用过程流式呈现 (SSE),
以及给本体一套可视化编辑 + 验证界面。

## 跑起来

后端得先起 (在仓库根): `uv run uvicorn web.app:app --port 8000`

```bash
pnpm install     # 仓库只锁 pnpm-lock.yaml, 用 npm/yarn 会绕过锁定版本
pnpm dev         # 落在 5173
```

Node 要 **20.19+ / 22.12+** (vite 8 的 engines 要求; 20.0–20.18 装完跑不起来)。
开发用的是 Node 24 + pnpm 11。

```bash
pnpm test        # vitest (当前 182 绿)
pnpm lint        # oxlint
pnpm build       # 跑 tsc -b, 顺带挡类型错误
```

## 两个视图

**对话** — 多会话侧边栏 (列出/切换/新建/删除, 标题由 LLM 概括), agent 每步工具
调用实时显示成时间线, 切回旧会话工具过程完整重现。⚙ 设置面板可视化启停本地
工具、增删 MCP server、管理 skill, 配置驱动热部署。

**本体** — 项目主体那一半。9 个 Tab:

| Tab | 干什么 |
|---|---|
| 域 / 实体 / 关系 / 枚举 / 过滤规则 / 概念 | 本体七层的行级增删改, 写回 SQLite 热重载 |
| 验证 | CQ 面板: 重跑竞争性问题 → 人工三档打分 → 与上轮对比高亮 |
| 提议 | 自演化飞轮产出的补边/淘汰提议, 人审通过才落地 |
| 网络图 | 力导向图看全局结构, 按域着色 |

关系 Tab 里裸边 (没 label) 会高亮 —— 它会让 agent 选边时失去区分度; 方向错了
可以直接 flip。实体 Tab 能跳到按该实体预筛的关系列表。

> ⚠️ 设置面板的 MCP server 表单等于**本机任意命令执行**, 后端无鉴权不校验命令。
> 只在"监听 localhost + 单用户"下才安全, 详见仓库根 `SECURITY.md`。
