# Agent Loop 实战指南:从 chat 到 agent 中间到底发生了什么

> 一份给所有正在做 LLM 应用的朋友看的、共同进步的策略对比手册。
>
> 本文聚焦一件事:**把"模型生成文本"变成"模型决定调用工具",中间到底加了哪几层?**
> 适合已经做过 chat 或 RAG、想给 LLM 加上"主动获取信息和执行动作"能力的工程师。

---

## 目录

- [前言:agent 不是新框架,是一行 while 循环](#前言agent-不是新框架是一行-while-循环)
- [第一部分:agent vs chat vs RAG](#第一部分agent-vs-chat-vs-rag)
  - [1. 三种范式的差别](#1-三种范式的差别)
  - [2. 五种主流 agent 形态](#2-五种主流-agent-形态)
  - [3. 什么时候真的需要 agent](#3-什么时候真的需要-agent)
- [第二部分:主流框架横评](#第二部分主流框架横评)
  - [1. 裸 SDK 自写](#1-裸-sdk-自写)
  - [2. LangGraph](#2-langgraph)
  - [3. LangChain](#3-langchain)
  - [4. Pydantic AI](#4-pydantic-ai)
  - [5. OpenAI Agents SDK](#5-openai-agents-sdk)
  - [6. Anthropic Claude Agent SDK](#6-anthropic-claude-agent-sdk)
  - [7. 选型决策矩阵](#7-选型决策矩阵)
- [第三部分:agent loop 的核心范式](#第三部分agent-loop-的核心范式)
  - [1. ReAct 的本来面目](#1-react-的本来面目)
  - [2. 原生 tool_use 协议](#2-原生-tool_use-协议)
  - [3. think / act / observe 三循环](#3-think--act--observe-三循环)
  - [4. 退出条件与 max_iters](#4-退出条件与-max_iters)
- [第四部分:provider 抽象与 fallback](#第四部分provider-抽象与-fallback)
  - [1. 为什么要抽 provider 层](#1-为什么要抽-provider-层)
  - [2. SDK 级 fallback vs Router 级 fallback](#2-sdk-级-fallback-vs-router-级-fallback)
  - [3. fallback 不是免费午餐](#3-fallback-不是免费午餐)
- [第五部分:工具就是协议](#第五部分工具就是协议)
  - [1. LLM 看到的是 schema 不是函数](#1-llm-看到的是-schema-不是函数)
  - [2. 自动 schema 推导的陷阱](#2-自动-schema-推导的陷阱)
  - [3. safe_path 与工具沙盒](#3-safe_path-与工具沙盒)
  - [4. 串行 vs 并行 tool_use](#4-串行-vs-并行-tool_use)
- [第六部分:生产真实做法 (规模 / SLA / 成本 / 运维)](#第六部分生产真实做法-规模--sla--成本--运维)
  - [1. Anthropic 自家 agent 实践](#1-anthropic-自家-agent-实践)
  - [2. LangGraph Platform / Cloud](#2-langgraph-platform--cloud)
  - [3. OpenAI Assistants API](#3-openai-assistants-api)
  - [4. 自部署 + 服务网格 (Vespa-like 思路)](#4-自部署--服务网格-vespa-like-思路)
  - [5. Observability 标配:OpenTelemetry / LangSmith / Helicone](#5-observability-标配opentelemetry--langsmith--helicone)
- [第七部分:小型实现的常见坑 → 生产对策](#第七部分小型实现的常见坑--生产对策)
  - [7.1 常见坑](#71-常见坑)
  - [7.2 生产对策](#72-生产对策)
  - [7.3 综合对照表](#73-综合对照表)
- [第八部分:补齐路线 (按 ROI 排序)](#第八部分补齐路线-按-roi-排序)
- [附录 A:术语小词典](#附录-a术语小词典)
- [附录 B:进一步阅读](#附录-b进一步阅读)
- [写在最后](#写在最后)

---

## 前言:agent 不是新框架,是一行 while 循环

如果你看过几篇 "agent 入门" 的文章, 大概率会被 "ReAct / tool_use / multi-agent / planner-executor / state machine" 这几个词洗一遍, 心里默认 agent 就是一种 "比 chat 复杂一档" 的运行时, 至少要有一个调度器, 一张状态图, 一个 tool registry, 大概率还得 async + 队列。

**真写起来不是这样的**。

剥掉所有名词, agent loop 就是 30 行 Python:

```python
messages = [system_prompt, user_query]
for i in range(max_iters):
    resp = llm.complete(messages, tools=tool_schemas)   # think
    msg = resp.choices[0].message
    if not msg.tool_calls:                              # 没要调工具 → 退
        return msg.content
    messages.append({"role": "assistant", "tool_calls": msg.tool_calls, "content": msg.content})
    for tc in msg.tool_calls:                           # act
        result = tools[tc.name](**json.loads(tc.arguments))
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})  # observe
raise RuntimeError(f"超过 {max_iters} 轮还没收敛")
```

`think → act → observe` 三步, 套一个带上限的 while, 完事。所有 agent 框架做的事都是:

- 把这个 while 抽象成 state machine 节点 (LangGraph)
- 把 tool 注册 / schema 推导封装得更甜 (Pydantic AI)
- 把 multi-agent 协作 / 并行 tool / 长期记忆 加进来 (Claude Agent SDK / OpenAI Agents SDK)
- 把云上跑 / 调试 / trace / 部署一条龙 (LangGraph Platform / OpenAI Assistants)

不是 "你必须用框架", 是 "框架在你长大到某个 scale 之后能省事"。**这一篇所有的讨论都基于一个前提:你已经能 30 行写出 agent loop, 然后看哪些 scale 上去后开始疼**。

---

## 第一部分:agent vs chat vs RAG

### 1. 三种范式的差别

```
chat        : user → LLM → reply
RAG         : user → retrieve(query) → LLM(query + chunks) → reply
agent       : user → LLM(decide) ↔ tools (loop) → final reply
              ↑__________________|
              (think → act → observe, 多轮)
```

| 范式 | LLM 角色 | 数据流 | 多步? | 状态? |
|---|---|---|---|---|
| **chat** | 生成器 | 单向 | 单轮 | 无 (除了 history) |
| **RAG** | 生成器 | 单向 (检索前置) | 单轮 | 无 |
| **agent** | 决策器 + 生成器 | **闭环** | 多轮 | 有 (messages 累积) |

最大的差别是 LLM 角色:**chat / RAG 里 LLM 只负责生成, agent 里 LLM 多负责一件事:决定下一步做什么**。这个 "决定" 通过 `tool_calls` 字段表达 — LLM 不直接执行, 它告诉你 "请帮我调 foo(x=1, y=2), 然后把结果给我"。

RAG 跟 agent 经常被混在一起讲, 其实差别很清楚:
- **RAG** 是 "用户问问题 → 必然检索 → 必然生成", 检索是**前置硬步骤**
- **agent** 是 "用户给任务 → LLM 自己判断要不要查 / 查什么 / 查多少次", 检索是 **agent 决定的一个可选 tool**

换句话说:**RAG 是 agent 的退化情况**, 当 agent 只有一个工具 (`retrieve`) 且强制每次必调一次时, agent 就退化成 RAG。

### 2. 五种主流 agent 形态

Anthropic 在 *Building effective agents* (2024) 里把 agent 应用分了 5 种, 复杂度递增:

1. **Augmented LLM** — LLM 加 retrieval / memory / tools 但每次只调一次, 不循环。本质是 "更聪明的 chat"
2. **Tool use** — LLM 在一轮里返回 `tool_calls`, 业务执行后回填, **再让 LLM 决定下一步是回答还是继续调**。这是最基础的 agent loop
3. **Conversation state** — 在 (2) 之上累积 history, 让 agent 跨多 user turn 保持状态
4. **Multi-agent** — 多个 agent 协作 (planner / executor / critic / 客户代理), 互相通过消息或共享 state 通信
5. **Computer use** — agent 通过截屏 + 鼠标键盘操作真正的电脑, 这是最重的形态

绝大多数项目从 (2) 起步就够用了。(3) 是把 (2) 接上对话引擎, 把 messages 累积起来就行。(4) 和 (5) 是**完全不同的工程难度**, 别一上来就上多 agent — 大多数 "我要 multi-agent" 的需求, 单 agent 多工具就能解决。

### 3. 什么时候真的需要 agent

**不需要 agent**:
- 单次问答 / 单次摘要 / 单次翻译 → chat
- 知识库问答, 用户问什么就检索什么 → RAG
- 工作流写死 (先 A 再 B 再 C), 步骤跟 LLM 决策无关 → 直接 pipeline

**真正需要 agent**:
- 用户给的任务**不能预先拆步骤**, 必须看中间结果决定下一步 (例:"帮我看看这个项目里 X 模块是怎么实现的", agent 要先列目录探结构, 再决定读哪个文件)
- 工具调用次数 **取决于复杂度**, 简单 query 一次工具就够, 复杂 query 需要 3-5 次工具组合
- 需要**自我纠错** — 工具返回错误, LLM 看到后换个参数重试

简单判断准则:**如果你能写出"先做 A 再做 B"的流程图, 就不需要 agent; 如果你只能写"看情况", 才需要 agent**。

---

## 第二部分:主流框架横评

到 2026 年, agent 框架进入"百花齐放"阶段。下面这 6 个最值得了解。

### 1. 裸 SDK 自写

不引任何 agent 框架, 用 Anthropic SDK / OpenAI SDK / LiteLLM 直接拼一个 while 循环。

- **代码量**: 30-300 行 (随你多少 helper)
- **学习成本**: 零 (只要懂 LLM API)
- **优点**: 完全透明, debug 直接读代码, 没有"框架在背后干了什么"的疑问
- **缺点**: multi-agent / 并行 tool / 长期记忆 全得自己写; state 复杂后代码会膨胀

**适合**: 教学项目、小型生产 (单 agent 单工具集)、想吃透 agent 原理的人; 不适合多 agent 协作或要求开箱即用的运维栈。

### 2. LangGraph

LangChain 团队在 2024 年中推出的 graph-based agent 框架, 目前 (2026) 是新项目首选之一。

- **抽象**: 把 agent 描述成 nodes (步骤) + edges (跳转条件) 的 state machine
- **核心**: `StateGraph` + `add_node` + `add_edge` + `add_conditional_edges`
- **优点**: state 显式, multi-agent / human-in-the-loop / checkpoint / time travel 都有原生支持; LangSmith trace 集成好
- **缺点**: 学习曲线陡, 简单单 agent 用 LangGraph 是用大炮打蚊子; 调试时 graph 抽象会挡住一层

**适合**: multi-agent、需要 checkpoint / 回放、需要 human-in-the-loop 的中大型项目。

### 3. LangChain

老牌, agents 模块从 2023 年就有, 现在维护状态。

- **抽象**: `AgentExecutor` + `Tool` + Memory, 各种 chain 拼起来
- **优点**: 生态最大, 社区案例最多, 各种 vector store / LLM / loader 一键集成
- **缺点**: 抽象层多, "为什么这里要 wrap 一下" 的疑问多; debug 时 callback 漫天飞; 团队官方推荐新项目用 LangGraph 而不是 LangChain agents

**适合**: 已经在用 LangChain 生态的团队; 新项目优先看 LangGraph。

### 4. Pydantic AI

Pydantic 团队 2024 年底出的 agent 框架, 主打 type-safe。

- **抽象**: `Agent` + `RunContext` + Pydantic schema 当 tool input / output
- **优点**: 类型安全, IDE 补全顺畅, schema 自动验证; 极简, 不到 100 行就能上手
- **缺点**: 生态新, multi-agent 支持还在加; LangSmith 之类的 trace 集成靠社区
- **核心理念**: "agent 应该跟写 FastAPI 一样, 函数签名就是接口契约"

**适合**: 类型安全敏感的 Python 团队、中小项目、不想引重型框架但想要 schema 校验。

### 5. OpenAI Agents SDK

OpenAI 在 2025 年初推出的官方 agent SDK, 跟 Assistants API 配套。

- **抽象**: `Agent` + `Handoff` (agent 间转交) + `Guardrails` (输入/输出校验)
- **优点**: OpenAI 自家协议优化最深; multi-agent handoff 模型清晰; tracing 接 OpenAI dashboard
- **缺点**: 跟 OpenAI 生态强绑定, 切其他 provider 麻烦; multi-agent handoff 是特定模型 (不是任意图)

**适合**: 重度用 OpenAI 的团队、想要"官方背书 + 一站式" 的项目。

### 6. Anthropic Claude Agent SDK

Anthropic 2025 年推的官方 SDK, 跟 Claude Code 同源的 agent 基建。

- **抽象**: `Agent` + `Tool` + `Subagent` + `Memory` + `Skill`
- **优点**: Claude 协议优化最深, subagent / skill 系统是 Claude Code 验证过的; sandbox 默认有
- **缺点**: 跟 Claude 强绑定; 用非 Anthropic 模型时优势减弱
- **核心理念**: "agent = LLM + tools + 子任务委派", subagent 是一等公民

**适合**: 重度用 Claude 的团队、想要 Claude Code 同款 subagent 抽象的项目。

### 7. 选型决策矩阵

| 场景 | 推荐 | 理由 |
|---|---|---|
| 学习 agent 原理 | 裸 SDK | 30 行写完一遍, 比读 1000 页文档管用 |
| 小型生产 (单 agent 几个工具) | 裸 SDK + LiteLLM | 加 LiteLLM 抽 provider, 业务零改动切模型 |
| 类型安全敏感的中小项目 | Pydantic AI | schema 校验最干净 |
| 多 agent + state machine | LangGraph | 唯一为这场景设计的 |
| 重度 Anthropic 生态 | Claude Agent SDK | subagent / skill 复用 Claude Code 经验 |
| 重度 OpenAI 生态 | OpenAI Agents SDK | Handoff + tracing 顺 |
| 已经在 LangChain 生态 | 升级到 LangGraph | 平滑迁移路径 |

**一个反直觉的事实**: 框架不是 "scale 大了一定要用"。**Anthropic 自家的 Claude Code 本质就是裸 SDK + 自写 while 循环**, 不用 LangGraph。框架是 "团队人数多 + 多 agent + 复杂 state" 时才真省事; 单 agent 哪怕跑十亿 query, 裸 SDK 也撑得住。

---

## 第三部分:agent loop 的核心范式

### 1. ReAct 的本来面目

ReAct (*ReAct: Synergizing Reasoning and Acting in Language Models*, Yao et al., 2022) 是早期 agent 论文, 提出用 **"Thought / Action / Observation"** 三段式 prompt 让 LLM 显式输出推理 + 工具调用:

```
Question: What is the capital of the country where Tokyo is?
Thought: I need to find which country Tokyo is in.
Action: search[Tokyo country]
Observation: Tokyo is in Japan.
Thought: Now I need the capital of Japan.
Action: search[Japan capital]
Observation: Tokyo.
Thought: So the answer is Tokyo.
Answer: Tokyo
```

这套是**纯文本协议**, 业务代码要解析 LLM 输出的 `Action:` 行, 拆出函数名和参数, 跑完把结果作 `Observation:` 再塞回去。

**为什么 ReAct 现在很少用**:
- 解析容易出错 (LLM 偶尔写 `Action: search[...]` 偶尔写 `action: search ...` 偶尔不写)
- 主流模型已经原生支持 `tool_use` (Anthropic) / `tools` (OpenAI) / `functions` (Google), 协议层面就有结构化 schema
- 串行性差 (一次只能一个 action)

ReAct 现在主要的价值是**论文意义**: 它证明了"推理 + 行动交错" 的 agent 范式可行。生产 99% 用原生 tool_use, 不用文本协议。

### 2. 原生 tool_use 协议

OpenAI 在 2023 年中推出 `function calling`, 2024 年改名 `tool_use`。Anthropic / Google / 智谱 / 阿里 / 各家国内厂商陆续跟进, 现在已经是事实标准。

请求长这样 (OpenAI 风格, LiteLLM / Anthropic 都兼容):

```python
resp = client.chat.completions.create(
    model="...",
    messages=[
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
    ],
    tools=[
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "搜索知识库",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        }
    ]
)
```

响应回 `tool_calls` 列表:

```python
msg = resp.choices[0].message
# msg.content       = "我先查一下知识库..."  (可选, 可能为空)
# msg.tool_calls    = [{id, function: {name, arguments (JSON str)}}]
```

业务代码:
1. 把 `msg` 整体作 `role=assistant` 消息回填 (含 tool_calls)
2. 跑每个 tool_call, 结果做成 `role=tool, tool_call_id=tc.id, content=json_str`
3. 整个 messages 再喂 LLM, 拿下一轮

这套**比 ReAct 干净一大截**, 不用解析, 不用怕模型自由发挥乱了协议。

### 3. think / act / observe 三循环

不管 ReAct 还是 tool_use, 本质都是三循环:

```
[think]   LLM 看完当前 messages, 决定下一步
   ↓
[act]     业务执行 (tool 函数 / API / SQL / shell)
   ↓
[observe] 结果回填进 messages
   ↓
   └─→ 回到 [think]
```

退出有 3 种:
- LLM 不再返回 `tool_calls` (它觉得可以直接答了) — **正常退出**
- 达到 `max_iters` 上限 — **强制退出, 抛错**
- LLM 调用挂 / 工具异常 — **顶层 catch, 落 error 日志**

agent loop 全部复杂度都在这 3 种退出 + tool 错误处理上, 不在主流程。

### 4. 退出条件与 max_iters

**为什么必须有 max_iters**:

LLM 自由发挥时, 经常出现 "查了再查再查" 死循环。一种典型情况:
1. LLM 调 `search("X")` → 返回空
2. LLM 觉得查得不对, 调 `search("X 详细")` → 又空
3. LLM 调 `search("有关 X 的信息")` → 又空
4. ...

如果不设 max_iters, agent 可以转十几轮, 用户已经走了。`max_iters=10` 是社区经验值: 大多数任务 2-5 轮就收敛, 10 轮还没出来基本是任务不合适或工具不够。

**为什么不能光靠 max_iters**:

光设上限不够, 因为撞上 max_iters = 失败, 用户体验差。还要**在 system prompt 里软引导**:

```
何时停止:
信息充足时立刻给最终回答, 不要继续调工具。
一般 2-5 步内应该能给出答案。
如果超过 5 步还没收敛, 说明任务可能不适合 agent,
直接告诉用户"这个问题我答不出来"。
```

让 LLM 自己有 "知道何时该停" 的意识, 比靠硬上限兜底好得多。

---

## 第四部分:provider 抽象与 fallback

### 1. 为什么要抽 provider 层

最朴素的写法是直接调 Anthropic SDK 或 OpenAI SDK:

```python
from anthropic import Anthropic
client = Anthropic(api_key=...)
resp = client.messages.create(model="claude-3-5-sonnet-20241022", ...)
```

问题:
- 换 provider (Anthropic → OpenAI → 某国内 LLM 厂商) 整段重写
- 测试时切 mock 难
- 多 provider 灰度 / fallback 自己实现

**provider 抽象层** (LiteLLM / aisuite / Vercel AI SDK) 把所有 provider 统一成一个协议 (通常是 OpenAI 兼容):

```python
import litellm
resp = litellm.completion(
    model="anthropic/claude-3-5-sonnet-20241022",  # 改前缀切 provider
    messages=[...],
    fallbacks=["openai/gpt-4o", "vendor-b/model-x"],
)
```

业务代码看到的永远是 OpenAI 风格, 改 model id 就切了。

### 2. SDK 级 fallback vs Router 级 fallback

LiteLLM 提供两种 fallback:

**SDK 级**:`litellm.completion(model=..., fallbacks=[...])`
- 调用方法签名里加一个参数, 用法最简
- 主 model 异常时**调用栈内**切到下一个 fallback
- 缺点: 没有 cooldown, 主 model 持续挂的话每次都先撞一下主, 撞挂了再切, 浪费 1 次往返

**Router 级**:`litellm.Router(model_list=[...])`
- 独立的 Router 对象, 维护多个 model 的健康状态
- 支持 cooldown (主 model 挂了之后 N 分钟内不再尝试)、retry、load-balance、rate-limit
- 缺点: 配置复杂, 又是一层抽象; 有 known issue (#12624 / #17001 等关于 fallback 不按预期切的)

**经验**: 小项目用 SDK 级够; 多 model + 高并发再上 Router; Router 上线前必做 chaos test (主动让主 model 挂, 看 cooldown / retry 是否按预期).

### 3. fallback 不是免费午餐

容易忽视的事:**"fallback 触发了" 这件事必须能被监控到**, 否则 fallback 等于沉默降级 — 系统看似正常, 实际成本/质量已经在悄悄漂移。

更隐蔽的坑:**怎么判断 fallback 真的触发了?**

一个看起来天经地义的判断: "如果 `response.model` 跟 primary 不一样, 就是切了"。但这套**经常误判**, 因为:

- 有些 provider (比如各种 OpenAI 兼容中间层) 在 `response.model` 里返回**别名 / 带 tag 的 model id**, 跟你配的 primary 字符串不严格相等
- 子串包含也不行 (`"claude-3-5-sonnet" in "anthropic/claude-3-5-sonnet-20241022-pro"` 可能 True 可能 False 取决于 provider 怎么命名)
- 真正的 SDK 级 fallback 切换是 LiteLLM 内部的 try/except, 外部看不到 "我现在用 fallback 了" 的明确信号

**这是个语义偏差陷阱**:"response.model 跟 primary 不一样" 跟 "我切到了 fallback" 是两件事。前者是观察, 后者是事件。

**实战做法 (推荐, 二选一)**:
- (a) 在 provider 抽象层自己包一层 try/except, 主 model 真挂了再调备用 model, 这时落 `model_fallback` 事件 (语义精确, 但要放弃 LiteLLM SDK 级 fallback 的自动行为)
- (b) 接受 LiteLLM 内部 fallback 是 "黑盒", 不去外部模拟; 仅在主 model 调用挂掉走 except 的那条路上落 `model_call_failed` (能落的事件少了, 但不会假报)

千万别走第三条:"`response.model not in primary` 就报 fallback" — 这条路看起来对, 实际每次都误报, 把真正的 fallback 信号淹没。

---

## 第五部分:工具就是协议

### 1. LLM 看到的是 schema 不是函数

新人最常见的误解: "我注册的 Python 函数, LLM 调用时执行的是我这个函数"。

**LLM 永远不执行任何 Python 代码**。它看到的只是 JSON schema:

```json
{
  "type": "function",
  "function": {
    "name": "search",
    "description": "搜索知识库",
    "parameters": {"type": "object", "properties": {...}, "required": [...]}
  }
}
```

它能输出的也只是 schema 的 instance:

```json
{"name": "search", "arguments": "{\"query\": \"...\"}"}
```

**业务代码把这个 instance 翻译成 Python 函数调用, 跑出来再喂回 LLM**。

所以工具的两端是:
- **LLM 端**: schema (描述工具能干什么 + 参数怎么填)
- **业务端**: Python 函数 (实际执行)

中间靠装饰器 + 反射 (`inspect`) 自动从函数签名 + docstring 推 schema。这套设计让"加一个新工具"成本 = 写一个 Python 函数 + 装饰器一行。

### 2. 自动 schema 推导的陷阱

`inspect.signature(func).parameters` 拿到 `param.annotation`, 直接比对类型对象 (`str` / `int` / `list[str]`) 推 JSON schema:

```python
TYPE_MAP = {str: {"type": "string"}, int: {"type": "integer"}, ...}

def _type_to_json_schema(annotation):
    if annotation in TYPE_MAP:
        return TYPE_MAP[annotation]
    raise ValueError(f"不支持的 type hint: {annotation}")
```

**经典坑**:**`from __future__ import annotations`** (PEP 563) 会把模块里所有 type annotation 变字符串, `param.annotation` 拿到的是 `'str'` 字符串而不是 `str` 类型对象, `'str' in TYPE_MAP` 永远 False, 所有工具注册全挂。

更阴的是 Python 3.10+ 默认行为没变 (仍是运行时类型), 3.13+ 一度计划开 PEP 649 默认行为又变回类型, 这套 hack 容易跟 future import / Python 版本相互打架。

**两种修法**:
- (a) **禁用 future import** (装饰器项目最干净) — Python 3.12+ 原生支持 `list[dict]` / `dict | None` 运行时注解, future 不是必需的
- (b) 用 `get_type_hints(func)` 替代 `param.annotation`, 它会自动 resolve 字符串注解回类型对象

社区上, FastAPI / Pydantic AI 走 (b) (它们 wrap 重, get_type_hints 是常规操作); 教学项目 / 极简框架走 (a) (写一行硬注释 "工具文件禁用 future import" 比加 wrap 简单)。

### 3. safe_path 与工具沙盒

任何让 LLM 决定文件路径的工具都是**安全洞**。LLM 不被信任, 它可能被 prompt injection 操纵, 也可能就是自己 hallucinate 出 `path="../../etc/passwd"`。

**最基础的沙盒**:把所有路径限制在项目根目录内, 用 `os.path.realpath` (而不是 `abspath`) 解符号链接, 不在根内的拒绝:

```python
def safe_path(path: str) -> str:
    p = os.path.realpath(os.path.join(PROJECT_ROOT, path))
    if p != PROJECT_ROOT and not p.startswith(PROJECT_ROOT + os.sep):
        raise ValueError(f"path 越出项目根: {path}")
    return p
```

为什么 `realpath` 不用 `abspath`:
- `abspath` 只规范化 `..` 和分隔符, **不解符号链接**
- 如果项目根里有个符号链接指向 `/etc/passwd`, `abspath` 会觉得它在项目内, `realpath` 解开后发现真实路径在外 → 拒绝

**5 种边界 case 必须测**:
- 绝对路径 (`D:\foo`) → realpath 后 startswith fail
- 中段含 `..` (`a/../b`) → realpath 归一化掉
- 符号链接 → realpath 解开
- 大小写 (Windows) → realpath 返回真实大小写, 自动一致
- 跨盘符 (`E:\foo`) → realpath 后 startswith fail

工具沙盒做得不够严, agent 是个**proxy attack vector**:用户给一个看起来无害的 query, 注入 prompt 让 LLM 调一个文件读取工具去拿 `/etc/passwd`。

### 4. 串行 vs 并行 tool_use

LLM 一轮可以返回多个 `tool_calls` (一个 list)。业务端两种处理:
- **串行**: `for tc in msg.tool_calls: result = run(tc)`, 一个一个跑
- **并行**: `await asyncio.gather(*[run(tc) for tc in msg.tool_calls])`, 同时跑

**串行的优点**:
- 简单 (没有 async / 并发抽象)
- 顺序确定, debug 容易
- tool 之间有依赖时 (后一个用前一个结果) 也只能串行

**并行的优点**:
- 多 tool 独立调用时延迟降一档 (3 个 200ms 的 tool, 串行 600ms, 并行 200ms)
- LLM 实际经常一轮返回 3-5 个 tool_calls (它知道并行的优势, prompt 也鼓励)

**经验**: 小型 agent 串行起步 (单元测试简单, async 是负担); 流量上去 / 工具 latency 高 / 多工具独立时再切并行。LangGraph / OpenAI Agents SDK 默认并行, Pydantic AI 可配, 裸 SDK 看你想不想写 asyncio。

---

## 第六部分:生产真实做法 (规模 / SLA / 成本 / 运维)

这一节讲工业代表方案, 不绑定任何具体项目身份。看完后心里大致有数 "如果有一天要扛真流量, 应该长什么样"。

### 1. Anthropic 自家 agent 实践

Anthropic 内部 (Claude Code / Computer Use 系列) 的 agent 实现, 公开资料里能看到的几条经验:

- **不用 LangGraph 之类的图框架**, 大多数 agent 是裸 SDK + 自写 while 循环
- **subagent 是一等公民**:复杂任务 dispatch 给独立 subagent, 主 agent 只看 subagent 的总结结果 (节省主 context window)
- **skill 系统**:把 prompt 模板 + 工具 + 行为约束打包成 skill, agent 按需 invoke
- **memory 系统**:文件式持久, 而不是 vector embedding 检索 — 更结构化, 失败更易诊断
- **错误处理倾向"工具失败时把错误回填给 LLM 自己处理"**, 而不是业务端 retry, LLM 通常会换参数或换工具

参考: *Building effective agents* (Anthropic, 2024)、Claude Code 公开架构文档。

### 2. LangGraph Platform / Cloud

LangChain 团队在 2024 末推出的 LangGraph Cloud, 现在叫 LangGraph Platform。

- **核心**: 把 LangGraph 写的 agent 一键部署成 stateful server, 自动持久化 state / checkpoint
- **配套**: LangSmith trace、A/B 实验、human-in-the-loop UI
- **定价**: 按 agent-run 收费, 起步 free tier 够小项目跑
- **优点**: state 持久化、time travel debug、生产级监控 一站搞定; 中型团队最快上线路径
- **缺点**: 跟 LangGraph 绑定, 切其他框架要重写; 价格随流量涨得快

适合**已经选了 LangGraph、想直接吃官方运维栈**的团队。

### 3. OpenAI Assistants API

OpenAI 2023 年底推, 2025 年改 Agents API。

- **核心**: 把 thread (对话) + assistant (agent 配置) + run (一次执行) 抽象到 OpenAI 平台, 业务只调 API
- **托管特性**: file_search / code_interpreter / function calling 全有, 自动管理 messages
- **优点**: 不用自己写 agent loop, OpenAI 跑; tracing 接 dashboard
- **缺点**: 跟 OpenAI 绑死, 用其他模型不行; thread 模型对长对话开销大

适合**重度 OpenAI 用户、想要"零运维"agent**的小团队。

### 4. 自部署 + 服务网格 (Vespa-like 思路)

大流量、强隔离、私有化部署的方案:

- **agent runtime**: 自部署裸 SDK 或 LangGraph
- **provider 接入**: LiteLLM proxy 起一个独立服务, 业务通过统一 endpoint 调多 provider
- **tool 服务化**: 每个 tool 是一个 microservice, agent 通过 RPC 调
- **state**: Redis / Postgres 持久化 messages
- **rate limit**: API Gateway 层
- **observability**: OpenTelemetry → Grafana / Datadog

工作量重, 但**可控性最高**。大模型产品方 / 大企业内部平台基本走这路。

### 5. Observability 标配:OpenTelemetry / LangSmith / Helicone

agent 是 "多步黑盒", 没 trace 等于盲飞。三个生态:

- **OpenTelemetry**: 通用 trace 标准, 几乎所有 APM 都支持; LiteLLM / LangGraph 都有 OTel 集成。**生产首选**, 不锁厂商
- **LangSmith**: LangChain 团队的 trace + eval 平台, agent step 级可视化最好; 跟 LangChain / LangGraph 集成深
- **Helicone**: LLM 调用层 proxy, 自动 trace 每次 completion, 成本统计

**最小可行 observability**:
- 每次 LLM 调用落一条事件 (model, in_tokens, out_tokens, latency)
- 每次 tool_call 落一条 (tool_name, args_summary, success, latency)
- 顶层 error 必须落 (error_type, phase, iter_idx)
- agent 完成时落一条 done 事件 (n_iters, total_latency, total_tokens, exit_reason)

入门项目用 JSONL 文件即可, 量上去再接 OTel + Grafana。

---

## 第七部分:小型实现的常见坑 → 生产对策

> 这一节是全文最重的部分。主语统一用 "学习项目 / 入门做法 / 小型实现", 对面是 "生产 / 工业级 / 规模化部署"。所有坑都是 agent 社区里反复出现的, 不绑定任何特定项目。

### 7.1 常见坑

#### 坑 1: `model_fallback` 事件触发条件错位 (上面 §4.3 的延伸)

**学习项目常见做法**: 在 agent loop 里加一段判断 `if primary_model not in response.model: log("fallback")`, 想用来监控 "fallback 真的触发了"。

**症状**:
- 主 model 实际从未挂掉, 但 fallback 事件每个 iter 都触发
- 日志里几千条假 fallback, 真 fallback 被淹没
- 监控告警的 "fallback 率" 失去意义

**本质**: 把"observation"当"event"。`response.model` 是 provider 自报的别名 (可能带版本后缀 / 厂商前缀), 跟你配的 primary 字符串不一定严格相等, 不是 "fallback 是否触发" 的可靠信号。这是个**语义偏差** — 想监控的是"主 model 不可用导致切了备用", 但实际监控的是"返回的 model id 跟配的 primary 字面不等"。

**退化级**: ⭐ (1 颗星, 严重退化, 监控失效)

**生产做法 (二选一)**:
- (a) 自己在 provider 抽象层加 try/except, 主 model 真挂了再调备用, 这条路径上落 `model_fallback`
- (b) 接受 LiteLLM SDK 级 fallback 是黑盒, 只在主调用挂掉的 except 分支上落 `model_call_failed`, 不去模拟 "fallback 触发"

**生产参考**:
- LiteLLM Router 级有 `success_callback` / `failure_callback` hook, 能精确拿到 "本次 completion 走了哪个 model"
- LangSmith / Helicone 是 proxy 层, 直接看到调用请求与响应, 不依赖 model id 字符串比对

**退化级**: ⭐⭐⭐⭐ (4 颗星, 接近最佳实践)

---

#### 坑 2: provider 抽象层的 stderr 噪音

**学习项目常见做法**: 直接 `import litellm` 跑 agent, 控制台收到一大堆 `Task was destroyed but it is pending` / `coroutine was never awaited` 之类 asyncio 警告。

**症状**:
- 用户每次跑完 agent, 终端被刷屏, 真正错误埋在噪音里
- CI 日志暴增, 增加排查成本
- 用户 (人) 一开始以为 agent 真挂了

**本质**: 一些 provider 抽象层 (LiteLLM 在某些版本) 内部用 asyncio 做 logging worker, 进程退出时清理不干净, asyncio 抛 ResourceWarning 到 stderr。**上游 bug**, 不影响功能, 影响观感。

**退化级**: ⭐⭐ (2 颗星, 中等退化, 不致命但累)

**生产做法**:
- (a) 三件套抑制: `import litellm` 之前设 `LITELLM_LOCAL_MODEL_COST_MAP=True` env、设 `litellm.suppress_debug_info = True`、把 `logging.getLogger("LiteLLM").setLevel(logging.WARNING)` — 大部分情况能压下来
- (b) pin 一个干净版本: 关注上游 GitHub issue (类似 #12624 / #17001 这种已知 bug), 选噪音少的 minor version, lock 在 lockfile 里
- (c) 切到 proxy 模式: 用 LiteLLM proxy server 起独立进程, agent 业务通过 HTTP 调, 噪音留在 proxy 进程里

**生产参考**:
- 大部分团队走 (a) + (b) 组合: env 三件套 + pin 已知干净版本
- 大型团队走 (c): provider 抽象集中部署, 业务进程干净

**退化级**: ⭐⭐⭐⭐ (4 颗星, 该压的压, 该 pin 的 pin)

---

#### 坑 3: max_iters 触顶时 fallback 路径未测

**学习项目常见做法**: 配 `max_iters=10`, 主 model + 备用 model fallback 都配上, 然后**只测主 model 正常时的路径**, 没真验证过 "主 model 真挂了" 跟 "max_iters 真触顶" 这两条边界。

**症状**:
- 上生产后第一次主 model 挂, fallback 路径里某个细节没考虑到, agent 直接抛错
- max_iters 触顶时 LLM 还在调工具, 业务没记 "为什么触顶" 的诊断信息
- 用户拿到一堆 stack trace, 不知道为啥

**本质**: 边界路径成本高 (要烧 token / 临时挂主 model), 容易跳过; 但这条路径上线后一定会撞。

**退化级**: ⭐ (1 颗星, 严重退化, 是事故温床)

**生产做法**:
- (a) **专门的 chaos 测试**: 主动让主 model 挂 (改 API key / 改 base_url 到无效地址), 跑完整 agent 流程, 验证 fallback 真切到备用
- (b) **max_iters 触顶单元测试**: mock LLM 永远返回 tool_calls, 验证 agent 触顶后抛 RuntimeError, error 事件落了完整诊断字段
- (c) **smoke 必包含边界**:不光跑 "happy path", 必须有至少 1 个 task 故意让 agent 多走几轮看是否撞 max_iters, 验证退出信息友好

**生产参考**:
- Netflix Chaos Monkey 思路: 定期主动制造故障, 验证降级路径还活着
- LangSmith 的 eval 套件: 标注集里专门有 "应该触发 fallback / 应该触发 max_iters" 的 task

**退化级**: ⭐⭐⭐⭐⭐ (5 颗星, chaos test 是降级路径的真相)

---

#### 坑 4: tool schema 自动推导被 `from __future__ import annotations` 破坏

**学习项目常见做法**: tool 文件顶部按现代 Python 风格加 `from __future__ import annotations`, 装饰器自动 schema 推导原本好的, 突然全挂。

**症状**:
- 所有工具注册时报 "不支持的 type hint" 或 "annotation in TYPE_MAP returned False"
- 单元测试全红, 但目测代码没问题
- 找半小时根因, 才发现是 future import 的副作用

**本质**: PEP 563 让所有 annotation 变 lazy string, `param.annotation` 拿到的是 `'str'` 字符串而不是 `str` 类型对象, 直接 `in TYPE_MAP` 永远 False。**上面 §5.2 详讲**。

**退化级**: ⭐⭐ (2 颗星, 一次性踩坑, 但难定位)

**生产做法**:
- (a) tool 文件硬约定: **禁用 future import**, 文件顶部加注释说明
- (b) schema 推导改用 `get_type_hints(func)` 替代 `param.annotation`, 自动 resolve 字符串
- (c) 用现成框架 (Pydantic AI / OpenAI Agents SDK), schema 推导是它们的核心能力, 这种 corner case 早处理好了

**生产参考**:
- Pydantic AI 走 (b) + Pydantic schema 校验
- FastAPI 走 (b) + 一套 fastapi.params 抽象
- LangChain `@tool` 装饰器也走 (b)

**退化级**: ⭐⭐⭐⭐ (4 颗星, get_type_hints 是标准做法)

---

#### 坑 5: 工具沙盒只用 `abspath`, 符号链接穿越攻击

**学习项目常见做法**: tool 接受 `path` 参数, 用 `os.path.abspath(path)` 规范化, 检查 `startswith(PROJECT_ROOT)`, 觉得安全了。

**症状**:
- 攻击者 (或被 prompt injection 操纵的 LLM) 提交 `path="link_to_outside"`, 而项目里有个无害的符号链接指向 `/etc/passwd`
- `abspath` 不解符号链接, 觉得它在项目内, 读到了项目外的敏感文件

**本质**: `abspath` 只规范 `..` 和分隔符, 不 resolve 符号链接。

**退化级**: ⭐ (1 颗星, 安全漏洞)

**生产做法**:
- (a) 用 `os.path.realpath`, 解符号链接, 检查解开后的真实路径
- (b) 用 `pathlib.Path.resolve(strict=True)`, 同样解符号链接
- (c) 工业级: 容器化 + 只 mount 项目目录 (Docker / firejail / nsjail), 进程根本看不到外部文件

**生产参考**:
- Claude Code 用 mount 隔离 + safe_path 双层防御
- 在线 code execution 服务 (Code Interpreter / E2B) 都是容器隔离

**退化级**: ⭐⭐⭐⭐⭐ (5 颗星, 容器化是终极)

---

### 7.2 生产对策

把上面 5 个坑的对策汇总成 "从小到大" 的清单:

**Tier 0 — 任何 agent 项目最低标配**:
- tool 文件禁用 future import (或换 `get_type_hints`)
- safe_path 用 realpath 不用 abspath
- max_iters 配上 + 顶层 try/except 落 error 事件
- 工具执行用 try/except 包住, 失败回填 LLM 处理 (不抛)

**Tier 1 — 上小生产前**:
- provider 抽象层 stderr 噪音三件套压下来
- model_fallback 事件触发条件审计 (避免假报)
- chaos test:至少手动验证一次主 model 挂 / max_iters 触顶
- JSONL 落每次 LLM 调用 + 每次 tool 调用 + 顶层 error

**Tier 2 — 流量起来后**:
- OpenTelemetry 接 LiteLLM / agent runtime
- 评测集 (10-50 个 query, 答案标好), 每次模型升级 / 框架变更跑一遍
- multi-provider Router (而不是 SDK 级 fallback), 配 cooldown
- 异常情况 dashboard (fallback 率 / max_iters 触顶率 / 工具失败率)

**Tier 3 — 大流量 / 强合规**:
- 工具沙盒容器化 (Docker / firejail / nsjail), tool 进程隔离
- 多 region 部署 + DNS-level 灰度
- agent runtime 跟 provider 解耦 (proxy 服务化)
- prompt injection 防御 (input scanner / output guard)

### 7.3 综合对照表

| 退化点 | 学习项目常见做法 | 生产做法 | 退化级对照 |
|---|---|---|---|
| fallback 事件触发条件 | `response.model not in primary` 字符串比对 | provider 抽象层包 try/except, 真切了才落 | ⭐ vs ⭐⭐⭐⭐ |
| provider stderr 噪音 | 不管, 用户看一堆 warning | env 三件套 + pin 干净版本 + (大型) proxy 隔离 | ⭐⭐ vs ⭐⭐⭐⭐ |
| max_iters / fallback 边界路径 | 只测 happy path | chaos test + 单元测试 + smoke 覆盖边界 | ⭐ vs ⭐⭐⭐⭐⭐ |
| tool schema 推导 | 装饰器 + future import 撞挂 | 禁 future / 用 get_type_hints / 用现成框架 | ⭐⭐ vs ⭐⭐⭐⭐ |
| 工具路径沙盒 | abspath startswith 检查 | realpath + 容器隔离 | ⭐ vs ⭐⭐⭐⭐⭐ |
| 并行 tool_use | 串行执行 | asyncio.gather 并行 / 流式回填 | ⭐⭐ vs ⭐⭐⭐⭐ |
| messages 上下文管理 | 只监控不裁剪 | 摘要压缩 / 滑窗 / token 上限多策略 | ⭐⭐ vs ⭐⭐⭐⭐⭐ |
| observability | 单机 JSONL | OpenTelemetry + LangSmith / Helicone + Dashboard | ⭐ vs ⭐⭐⭐⭐⭐ |
| 错误处理 | 工具失败回填 LLM 自己处理 | 智能重试 + circuit breaker + 失败分类 | ⭐⭐ vs ⭐⭐⭐⭐ |
| multi-agent | 单 agent 单工具集 | subagent / handoff / planner-executor / 长期记忆 | ⭐⭐ vs ⭐⭐⭐⭐⭐ |

⭐ 数越多, 离生产最佳实践越近。退化级 ⭐ 跟 ⭐⭐⭐⭐⭐ 差三档以上的, 都是补齐路线里 ROI 最高的几条。

---

## 第八部分:补齐路线 (按 ROI 排序)

给"想从 demo 走到生产"的读者一份路线图。顺序不是技术先进度, 是 **"改最少代码 / 拿最大收益"** 的排序:

1. **加观测最小集**。每次 LLM 调用 / 每次 tool 调用 / 顶层 error 都落 JSONL, 字段至少 `event` / `model` / `latency_ms` / `in_tokens` / `out_tokens` / `success`。后续所有优化都靠数据说话, 没数据全是猜。

2. **修 fallback 事件触发条件** (坑 1)。从 "字符串比对" 改成 "真走 except 才落", 假报清零, 真 fallback 信号才有意义。

3. **压 provider stderr 噪音** (坑 2)。三件套压一下, 不光用户体验上去, CI 日志也干净。

4. **chaos test 跑一次** (坑 3)。主动让主 model 挂, 跑一次完整 agent, 验证 fallback 路径活着、max_iters 触顶有友好错误信息。10 分钟工时, 防一次真出事故。

5. **tool schema 推导规范化** (坑 4)。要么禁 future import 加硬注释, 要么换 `get_type_hints`, 选一个堵住。

6. **safe_path 用 realpath** (坑 5)。一行改动, 堵符号链接穿越。

7. **加最小评测集**。10-30 个 task (含 happy / fallback / max_iters / 工具失败几类), 每次改动跑一遍, 看 success rate / iter 分布有没有回归。

8. **若上量, 切 Router 级 fallback + cooldown**。SDK 级 fallback 适合冷启动, 流量大了 Router 的 cooldown / retry / load-balance 才扛得住。

9. **若要 multi-agent, 切 LangGraph / Claude Agent SDK**。单 agent 多工具撑不动 (planner-executor / human-in-the-loop / 复杂 state) 时再上图框架。

10. **若强合规, 工具沙盒容器化**。Docker / firejail / nsjail 把 tool 进程隔离, prompt injection 攻击面收敛。

前 6 条都是 "几小时到几天" 能做完的, 性价比最高。后 4 条是 "几周到几个月" 的工程, 量级到了再上。

---

## 附录 A:术语小词典

- **agent**: LLM + 工具 + 多轮决策循环, "LLM 不只是生成, 还决定下一步做什么"
- **agent loop**: think → act → observe 的 while 循环, agent 的核心结构
- **tool_use / function calling**: LLM 协议级的工具调用机制, OpenAI / Anthropic / Google / 国内主流厂商都支持
- **ReAct**: *Reasoning + Acting* 的早期 agent 范式, Thought/Action/Observation 三段式文本协议, 现在大多被 tool_use 取代
- **think / act / observe**: agent 三步循环, LLM 思考 / 业务执行 / 结果回填
- **max_iters**: agent 单次运行的最大循环次数, 防死循环兜底
- **system prompt**: agent 行为规约, 通常包含角色、工具使用约束、何时停止等
- **multi-agent**: 多个 agent 协作, 通过消息或共享 state 通信; planner-executor / critic / handoff 都是 multi-agent 子模式
- **subagent**: 主 agent dispatch 给独立 agent 处理子任务, 主 agent 只看 subagent 的总结, 节省主 context
- **handoff**: agent 之间的"转交"语义, 一个 agent 完成自己部分后把 control 转给另一个
- **state machine**: agent 用 nodes + edges 描述, LangGraph 核心抽象
- **checkpoint**: agent 中间状态持久化, 失败后可从断点恢复
- **time travel**: 把 agent 回退到某个历史状态重新运行, debug / A/B 用
- **human-in-the-loop**: agent 在某个节点暂停等人确认才继续
- **fallback**: 主 model 不可用时切到备用 model, SDK 级 / Router 级 / 自己写
- **cooldown**: Router fallback 时, 主 model 挂了之后 N 分钟内不再尝试, 避免每次都先撞主再切
- **circuit breaker**: 错误率高于阈值时直接拒绝调用, 让失败 fast-fail 不拖整体延迟
- **chaos test**: 主动制造故障验证降级路径, Netflix Chaos Monkey 思路
- **prompt injection**: 攻击者通过 user input 操纵 LLM 行为, agent 场景下可能让 LLM 调危险工具
- **safe_path / sandbox**: 工具路径安全机制, 限制 LLM 可访问的文件范围
- **streaming**: LLM 返回 token 流式输出而不是等全部生成完; agent + tool_use + streaming 三者组合协议复杂
- **token 上限 / context window**: 模型一次能看的最大 token 数, 超了就报错或截断
- **messages summarization**: 把历史 messages 压缩成摘要, 留出 context 给新内容
- **observability**: 可观测性, agent 多步黑盒必须有 trace 才能 debug
- **OpenTelemetry**: 通用 trace 标准, 厂商无关
- **LangSmith**: LangChain 团队的 trace + eval 平台, agent step 级可视化
- **Helicone**: LLM 调用 proxy 层, 自动 trace + 成本统计
- **provider abstraction**: provider 抽象层 (LiteLLM / aisuite / Vercel AI SDK), 统一多 provider 调用协议
- **SDK 级 fallback / Router 级 fallback**: 两种 fallback 形态, 前者调用栈内切, 后者独立健康状态管理
- **LiteLLM**: Python provider 抽象库, 100+ provider 统一为 OpenAI 协议
- **LangGraph**: LangChain 团队的 graph-based agent 框架, state machine 抽象
- **Pydantic AI**: 类型安全 agent 框架, schema 驱动
- **OpenAI Agents SDK / Anthropic Claude Agent SDK**: 两家厂商各自的官方 agent SDK
- **Computer Use**: agent 通过截屏 + 鼠标键盘操作真实电脑, Anthropic 2024 推出的形态

---

## 附录 B:进一步阅读

**论文 / 原始资料**:

- *ReAct: Synergizing Reasoning and Acting in Language Models* (Yao et al., 2022) — ReAct 原始论文, agent 范式奠基
- *Toolformer: Language Models Can Teach Themselves to Use Tools* (Schick et al., 2023) — tool_use 的训练范式
- *Reflexion: Language Agents with Verbal Reinforcement Learning* (Shinn et al., 2023) — agent 自我反思
- *Building effective agents* (Anthropic, 2024) — Anthropic 官方 agent 实践综述, 必读
- *Generative Agents: Interactive Simulacra of Human Behavior* (Park et al., 2023) — 多 agent 模拟人类社会的开创性工作
- *AutoGPT / BabyAGI* 早期项目源码 — 看一遍能理解 "为什么 agent 容易死循环"

**框架文档**:

- LiteLLM 官方文档 (`docs.litellm.ai`) — provider 列表 / fallback / Router / observability
- LangGraph 官方文档 (`langchain-ai.github.io/langgraph/`) — state machine / checkpoint / human-in-the-loop
- Pydantic AI 官方文档 (`ai.pydantic.dev`) — type-safe agent / 极简范例
- OpenAI Agents SDK (`platform.openai.com/docs/guides/agents`)
- Anthropic Claude Agent SDK (`docs.anthropic.com/agent-sdk`)
- LangChain `@tool` 装饰器源码 — 看一遍 schema 自动推导是怎么做的

**工程实践**:

- Claude Code 公开架构 — Anthropic 自家 agent 的 subagent / skill / memory 模型
- OpenAI Cookbook 的 agents 章节 — 多个端到端范例
- LangSmith 文档 — agent trace 实战
- Helicone 文档 — LLM proxy + 成本监控
- Anthropic Computer Use cookbook — agent 通过截屏 + 键鼠操作的范例

**评测 / 基准**:

- AgentBench — 多任务 agent 评测 (Web / Code / Game / Reasoning), 跨模型横评
- ToolBench — 工具调用准确率评测
- HumanEval / SWE-bench — agent 在 coding 任务上的标准评测
- BrowseComp — agent 在浏览器任务上的评测

---

## 写在最后

agent 不是银弹, 是一把 "把 LLM 从生成器变成决策器" 的杠杆。它的天花板由几件事共同决定:

1. **agent 是 while 循环不是新引擎** —— 框架是 scale 大了才省事, 30 行你也写得出来
2. **工具是协议不是函数** —— LLM 看的是 schema, 业务跑的是 Python, 中间靠装饰器 + 反射
3. **fallback 不能字符串比对** —— `response.model not in primary` 是观察不是事件, 监控全废
4. **stderr 噪音必须压** —— provider 抽象层往往不干净, 三件套压下来
5. **边界路径必须 chaos test** —— max_iters 触顶 / 主 model 挂掉的路径, 不主动测就是事故温床
6. **观测是后续所有改进的地基** —— 没 trace 等于盲飞, JSONL 起步, OpenTelemetry 上量
7. **multi-agent 不是默认选项** —— 单 agent 多工具能解决的 90% 别上 multi-agent

最后一句送给所有要做 agent 的朋友: **agent 给你的不是 "更聪明的 LLM", 是 "你终于知道 LLM 决策出问题时, 是 prompt 错 / tool schema 错 / 上下文丢了 / 还是 fallback 误触发"**。把工具协议、退出条件、降级路径、观测指标一件件搞清楚, 比换一个更强的模型管用得多。

希望这份指南对你有帮助。

— *本文档遵循 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 协议, 可自由引用、修改、再发布, 保留出处即可。欢迎根据你自己的实战经验提 issue 或 PR。*
