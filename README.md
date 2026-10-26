# ontologyBaseAI

**让 LLM 查业务数据库, 靠的不是更强的 Text-to-SQL, 而是一层本体。**

这个项目手写了一条完整的本体链路: 本体当语义层 → LLM 据此拼 SQL → 执行前用本体
校验纠错 → 从失败里反推本体缺什么 → 人审后补回本体。数据全程留在原库, 不搬进
三元组, 不做形式化推理 (OBDA 范式)。

> **为什么是本体**: 把几百张裸表的 schema 直接丢给 LLM, 它写出来的 SQL 语法没错、
> 业务全错 —— 表名列名不说人话, 它只能猜。学术界同口径实测: 裸表 **16%** →
> 映射成知识图谱 **54%** → 加本体校验回路 **72%**
> (Sequeda & Allemang, arXiv:2311.07509 / 2405.11706)。这三跳的机制在本项目里都落地了。
>
> 项目也是一条滚雪球学习路线的终点: CLI 对话 → RAG → Agent → Skill/MCP → 本体 KG,
> 每层都手写不套框架。前面几棒现在是本体链路的底座。

## 本体链路 (项目主体)

```
      ┌──────────────────────── 人审 ────────────────────────┐
      │                                                      ▼
  ① 本体 ──→ ② Agent 拼 SQL ──→ ③ OBQC 校验 ──→ 真库  ④ 自演化飞轮
  (语义层)      (卡片/JOIN 路径)    (执行前拦)              (失败→提议)
      ▲                                  │                   ▲
      └──────────── 补边/补属性 ─────────┴─── 失败信号 ───────┘
```

### ① 本体: 一张可编辑的语义层

本体存在 SQLite 里 (`capabilities/ontology/ontology.db`), **一行一个节点**, 改一个
实体就是 update 一行 —— 不是一个大 JSON 整文件覆盖。这个选择换来了前端行级 CRUD
和自演化的软删, 代价是放弃 RDF 生态的现成工具。

| 层 | 是什么 |
|---|---|
| **域** (domain) | 业务分区, 给图着色和做可视化聚类 |
| **实体** (entity) | 一个业务概念 ← 映射到一张或几张物理表 (`maps_to` / `data_source`) |
| **属性** (attributes) | 实体下的列 + 业务语义描述 (不是抄 `COLUMN_COMMENT`, 真库注释多半是列名复读) |
| **关系** (relation) | 实体间的边, 带**完整 JOIN 字段对** (`source_col` / `target_col`) + label + 基数 |
| **枚举** (enum_value) | 码值 → 人话 (`REC_STATUS='100'` → "生效中"), 答案回译用 |
| **过滤规则** (filter_rule) | "查这张表默认得带 `status=1`"这类业务缺省条件, 交给 agent 读, 不在代码里硬控 |
| **概念** (concept) | 声明式派生概念 (OBDA 的 TBox 层): 用条件 + 关系边组合, 零 SQL 模板 |

关系边是这层的重点。边不是"画"出来的, 是**扫出来 → 聚起来 → 喂语义 → 再狠狠删掉
一大半**磨出来的 —— 后半程的减法才是这个本体能用的原因: 一条该走的 JOIN 路和一条
物理上连得通但业务上不该走的路, 对 LLM 来说长得一模一样。

### ② Agent 怎么用本体拼 SQL

agent 不读 schema, 读本体。全程**渐进披露** —— 一次性灌全本体会挤爆上下文:

1. `get_entity_cards` — 命中实体, 拿**轻卡**(摘要 + 属性 + 有哪些邻居), 不含 JOIN 细节
2. `get_join_path` — 真要拼多表了才问 JOIN 字段对; 两表不直连会退一步给经中转实体的
   多跳路径 (跳数上限 2, 再远就响亮失败, 不许瞎猜)
3. `get_enum_values` — 遇到码值列再查值集
4. `execute_sql` — 只读执行, 出口挂着 OBQC

跨数据源 (如 MySQL + Oracle) 的查询由 `cross_source_planner` 切段接力, 不是真 JOIN。

### ③ OBQC: 执行前的本体校验

OBQC (Ontology-Based Query Check) 挂在 SQL 执行**出口**: LLM 写完、真正执行之前, 用
纯代码解析 SQL (sqlglot) 并对照本体校验。这是 54% → 72% 那一跳的全部来源。

| 校验 | 拦什么 |
|---|---|
| **JOIN 边校验** | 本体里两实体之间没有这条边 (`no_edge`), 或连接列对不上 (`join_col_mismatch`) |
| **列归属** | SELECT 的列不属于它所在的那张表 |
| **枚举值** | WHERE 里的码值字面量不在值集里 |
| **过滤规则** | 该带的业务缺省条件没带 |

有违规 → `hard_block`, **不碰库**, 把违规和合法选项一起回给 LLM 重写。同一个错写法
第二次撞上来会升级 hint (I1 熔断), 避免原地打转。

铁律一条: **硬拦的资格 = 对该实体有可对账的物理真值**。拿不到真值 (表 resolve 不到、
跨源那段) 就只能上浮成警告, 不许硬拦 —— 宁可漏拦, 不许拦错。

### ④ 自演化飞轮: 从失败里长本体

本体永远不完整。缺一条边, 表现是 agent 问了个本体答不出的问题。飞轮把这种失败变成
补本体的燃料:

1. **采集** (热路径常开) — OBQC 每次硬拦, 失败信号直接 INSERT 落库, 不靠手动触发
2. **候选构造** — 信号攒够了反推"可能缺哪条边", 外加孤儿列的集合差 (实体里没被任何边用到的列)
3. **真库探针** — 候选边拿去真库验: 三层判据只信物理真值 (包含率 / 基数 / 索引),
   不信本体声明 —— 本体缺的东西正是要发现的
4. **人审** — 探针过了也只是**提议**, 必须人点通过。LLM 是起草者, 不是作者
5. **CQ 回归门** — 落地前拿钉死的考题集 (sha256 锁定) 重跑, 新增硬拦就拒绝落地
6. **淘汰** — 反方向也走: 同一错误列对在 ≥3 个不同问题里出现 → 定罪那条边, 软删 + 立墓碑防复活

> 飞轮**机制是通的**, 但有个诚实的结论: 真正的瓶颈是**流量, 不是机制**。一个守规矩
> 的 agent 一个月只撞上几次 no_edge —— 燃料太少。这是取证得出的, 不是猜的。

### 验证回路: CQ

CQ (Competency Question) = 考本体建得好不好的问题集。改本体 → 重跑 CQ → 人工三档
打分 → 跟上轮比高亮, 形成"改了变好还是变坏"的闭环。前端有面板。

## 其它能力 (本体链路的底座)

滚雪球前几棒的产物, 都是手写不套框架。现在主要是给本体链路垫底:

- **RAG** — 结构感知切块 → 本地 bge embedding → Chroma → Hybrid 检索 (BM25 + 向量 + RRF) → bge-reranker 精排 → 分数过滤
- **Agent loop** — 裸 SDK 手写循环 + `@register_tool` 工具注册, 模型自主决定调哪个工具
- **Skill / MCP** — 能力包渐进披露 (L1 清单 → `load_skill` 拉正文); FastMCP server + 三层沙箱
- **多轮对话** — 流式输出 + token 累计 + 轮数/字符双级截断 + 查询改写
- **observability** — 链路任何"黑盒" I/O 必落一条 JSONL, 调参排查有依据
- **Web 前端** — FastAPI + React + SSE, 把工具调用过程流式呈现; 多会话磁盘持久化;
  本体编辑器 (七层行级 CRUD + 力导向图 + CQ 验证 + 提议审批) + 设置面板
  (详见 [`frontend/README.md`](./frontend/README.md))

## 跑起来

### 依赖

- **Python 3.12** (锁定; 不要 3.13/3.14, 兼容性踩过坑)
- **[uv](https://docs.astral.sh/uv/)** — 包管理, 命令一律 `uv run xxx`
- 一个 LLM API key (走 litellm, provider 在 `.env` 里配; 官方端点或自建代理都行)
- 只跑 CLI 的话以上就够。要跑 Web 前端再加 **Node 20.19+ / 22.12+**
  (vite 8 的 engines 要求) 和 **[pnpm](https://pnpm.io/)** (仓库只锁 `pnpm-lock.yaml`)

```bash
# 1. 装依赖 (uv 自动建 venv, 走官方源 pypi.org)
git clone <repo-url> ontologyBaseAI
cd ontologyBaseAI
uv sync
# 网络受限可自行选镜像源, 用 UV_DEFAULT_INDEX 环境变量覆盖

# 2. 配 LLM
cp .env.example .env
# 至少填 PRIMARY_API_KEY / PRIMARY_MODEL (+ 自建中转填 PRIMARY_BASE_URL)。
# 这三行是冷启动种子; 起了 Web 之后在 ⚙ 设置 → 🧠 模型 里加/切模型 (写
# config/model.json, 切完下一轮对话即生效, 不用重启), 那之后就不再读 .env。
# 数据源 (DB_SOURCES 及 DB_<源名>_*) 可以全留空 —— 连不上只打警告后降级。

# 3. 下 embedding 模型 (RAG 要用, 不下 RAG 起不来)
uv run modelscope download --model BAAI/bge-small-zh-v1.5 \
    --local_dir models/bge-small-zh-v1.5

# 4. 建本体库 (必须, 库不进 git)
uv run python scripts/init_ontology.py

# 5. 跑
uv run python main.py
```

**默认直接打字就是跟 agent 对话** —— 它自己决定要不要查知识库 (RAG) 或查数据库
(本体链路), 不用手动切模式。斜杠命令只管知识库/会话维护:

| 命令 | 作用 |
|---|---|
| `/load <目录>` | 索引目录下的 `.md` / `.txt` (按 mtime 增量) |
| `/kb` | 看知识库状态 (块数、文件名) |
| `/reset` / `/clear` | 清知识库 / 清对话历史 |
| `/history` / `/exit` | 看当前对话 / 退出 |

Web 前端:

```bash
uv run uvicorn web.app:app --port 8000          # 后端
cd frontend && pnpm install && pnpm dev         # 前端, 落在 5173
```

> ⚠️ **先读安全边界**: 设置面板的 MCP server 表单会把你填的 `command` 原样存下,
> 下一轮对话由 agent **在本机 spawn 子进程执行**, 后端**不校验命令、无鉴权**。
> 能访问设置面板 = 能在本机执行任意命令。**现状只在"监听 localhost + 单用户"下
> 才安全**, 这是部署假设不是代码保证。暴露到 localhost 之外 (`--host 0.0.0.0` /
> 反代 / 内网穿透 / 同网段设备访问) 之前**必须先加命令白名单或鉴权**, 否则等于
> 开放远程代码执行。完整信任链和防护方案见 [`SECURITY.md`](./SECURITY.md)。

### 几件容易踩的事

- **本体库不进 git** (含真实业务表名)。第 4 步用脱敏 seed 建一个**图书馆域**的示例
  本体 (3 域 / 6 实体 / 8 关系 / 4 枚举 / 2 规则 / 2 概念), 够你看清本体长什么样、
  本体类工具怎么用。接自己的真库是另一件事: 本体里 `entity.data_source` 的值必须
  与 `.env` 的 `DB_SOURCES` 源名逐字一致, 这个契约**没有启动期校验** ——
  `.env.example` 里有一条对账命令, 配完跑一下。
- **embedding 模型不会自动下载**, 得显式跑第 3 步。reranker (~2.12GB) 默认关闭。
- **`corpus/` 是空的, 知识库归你自己** —— 想试 RAG 就把 `.md` / `.txt` 放进
  `corpus/` (或任意目录), 在 CLI 里 `/load <目录>`。向量库 `chroma_db/` 不进 git,
  首次启动显示"知识库为空"是正常的。
- **首次启动可能卡几十秒**: litellm 去 GitHub 拉模型价格表, 拉不通会超时降级到内置
  副本 (实测约 30 秒自行恢复)。不是卡死。
- **`config/model.json` 不进 git** (含 api_key 明文), 跟 `agent/config/*.json`
  不同 —— 那几个只是能力开关故进 git。clone 下来照上面配 `.env` 就能跑, 不缺这个
  文件。前端 ⚙ 设置 → 🧠 模型 里的 👁 看明文只在 `DEPLOY_PROFILE=local` 下放行,
  因为 web 端没有认证、CORS 又放行 localhost 任意端口。
- 没配数据库时: RAG / 对话 / 本体浏览都正常, 只有 `execute_sql` 这类查库工具报错。

## 技术栈

| 层 | 选型 | 为啥 |
|---|---|---|
| **本体存储** | SQLite (行级可编辑) | 改一节点 = update 一行, 前端 CRUD 天然映射; 放弃 RDF 生态是自觉取舍 |
| **SQL 解析** | sqlglot | OBQC 校验要 AST, 不是正则 |
| **业务库** | MySQL + Oracle (pymysql / oracledb thin, 只读) | 源清单/数量/方言全 `.env` 可配 |
| LLM | litellm | 一层壳接各家 provider, 换模型改一行配置 |
| Embedding | 本地 bge-small-zh | 中文友好, 离线, 不付费 |
| Rerank | 本地 bge-reranker-v2-m3 | Hybrid 召回后精排, 治"分数倒挂" |
| 向量库 | Chroma PersistentClient | 单机够用, 自带持久化 |
| Agent | 手写 loop | 学透工具调用, 不藏在框架里 |
| MCP | FastMCP | 标准协议, 工具沙箱化 |
| Web | FastAPI + React + Vite (SSE) | 把 agent 工具过程流式呈现 |
| 包管理 / 测试 | uv (Python 3.12) / pytest | 快; TDD 走完整循环 |

## 项目结构

```
.
├── main.py              # CLI 入口 (默认打字即 agent 对话)
├── capabilities/
│   ├── ontology/        # ★ 本体权威源 ontology.db (SQLite) + selfevolve/ 自演化飞轮
│   ├── obqc/            # ★ 本体查询校验 (解析 / 抽取 / 四类校验 / 物理列缓存)
│   └── rag/             # chunker / embedder / store(Chroma) / retriever / reranker
├── core/                # 底座: 不依赖上层
│   ├── ontology_view.py # ★ 本体只读视图: 索引 + 卡片拼装 + 语义邻居 / JOIN 路径
│   ├── config.py        # .env 加载 + 配置校验
│   ├── conversation.py  # 对话历史 (+ 序列化)
│   ├── llm.py           # LLM 调用 (litellm 薄壳, 默认流式)
│   ├── session_store.py # 多会话磁盘 CRUD (一会话一 JSON)
│   ├── observability.py # JSONL 事件日志
│   └── runtime/         # 运行时装配: agent loop / bootstrap / cq_runner
├── agent/               # agent 装配层
│   ├── tools/           # ★ ontology_cards / ontology_join / sql_exec / rag_query ...
│   ├── prompts/         # system.md / ontology_usage.md 等模板
│   ├── skills/          # skill 定义
│   ├── mcp_server/      # FastMCP server (写文件工具 + 沙箱)
│   └── toolconfig/      # 按 DEPLOY_PROFILE 决定加载哪些工具
├── scripts/             # init_ontology.py = 冷启动从脱敏 seed 建库
├── web/app.py           # Web 后端 (SSE + 本体/CQ 接口)
├── frontend/            # React + Vite (对话 + 本体编辑器 + 力导向图 + CQ 面板)
├── tests/               # pytest (tests/manual/ 是手动 smoke, 不进自动化)
├── corpus/              # RAG 输入目录, 空的 —— 放你自己的知识库
├── guides/              # 通用技术科普 (CC BY 4.0)
└── .env.example
```

## 开发

```bash
uv run pytest -q                      # 后端全量
uv run pytest tests/test_obqc.py -v   # 单文件
cd frontend && pnpm test              # 前端
```

两边全量测试 + 冷启动由 [CI](./.github/workflows/ci.yml) 在每次 push / PR 上跑。

约定:
- **TDD 是硬规则** — 新功能 / bug fix / 重构, 先写测试看见红, 再写实现看绿
- **接口稳定性** — `capabilities/rag/store.py` 的 `add / search / __len__` 签名是硬合同, 只能加新方法
- **observability** — 链路任何"黑盒" I/O 必须 `log_event` 落一条 JSONL
- 详见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)

提 PR 看 [`CONTRIBUTING.md`](./CONTRIBUTING.md); 安全边界看 [`SECURITY.md`](./SECURITY.md)。

## 文档

### 本体相关 (主线)

| 文档 | 主题 |
|---|---|
| [`OBDA-GUIDE.md`](./guides/OBDA-GUIDE.md) | 本体驱动数据访问: 本体当语义层接 Text-to-SQL |
| [`ONTOLOGY-AND-KNOWLEDGE-GRAPH-GUIDE.md`](./guides/ONTOLOGY-AND-KNOWLEDGE-GRAPH-GUIDE.md) | 本体 / 知识图谱基础 |
| [`SELF-EVOLVING-KNOWLEDGE-GUIDE.md`](./guides/SELF-EVOLVING-KNOWLEDGE-GUIDE.md) | 自进化与知识飞轮: 业界三条路线 + 失败驱动的知识更新 |
| [`FORCE-DIRECTED-GRAPH-GUIDE.md`](./guides/FORCE-DIRECTED-GRAPH-GUIDE.md) | 力导向图: 渲染器选型 + 规模瓶颈 + 图交互怎么测 |

### 底座能力

| 文档 | 主题 |
|---|---|
| [`RAG-CHUNKING-AND-RETRIEVAL-GUIDE.md`](./guides/RAG-CHUNKING-AND-RETRIEVAL-GUIDE.md) | 11 种切块策略 + 12 种检索策略 |
| [`HYBRID-SEARCH-GUIDE.md`](./guides/HYBRID-SEARCH-GUIDE.md) | BM25 + 向量混合检索 + 中文分词 + RRF 融合 |
| [`RAG-RERANK-GUIDE.md`](./guides/RAG-RERANK-GUIDE.md) | 重排模型: 为什么召回后要精排, cross-encoder 原理 |
| [`AGENT-LOOP-GUIDE.md`](./guides/AGENT-LOOP-GUIDE.md) | Agent 循环 / tool use / ReAct 的本质 |
| [`CONVERSATION-MEMORY-AND-QUERY-REWRITE-GUIDE.md`](./guides/CONVERSATION-MEMORY-AND-QUERY-REWRITE-GUIDE.md) | 多轮记忆 + 查询改写 |
| [`MCP-GUIDE.md`](./guides/MCP-GUIDE.md) | Model Context Protocol + 工具沙箱 |
| [`FASTAPI-REACT-FULLSTACK-GUIDE.md`](./guides/FASTAPI-REACT-FULLSTACK-GUIDE.md) | 前后端流式 (SSE) 全栈 |
| [`LLM-工具调用与Agent协议-认知指南.md`](./guides/LLM-工具调用与Agent协议-认知指南.md) | LLM 工具调用与 Agent 协议 |
| [`Agent沙箱与Prompt注入-安全认知指南.md`](./guides/Agent沙箱与Prompt注入-安全认知指南.md) | Agent 沙箱 + Prompt 注入的安全边界 |

`guides/` 下的文档不含项目业务细节, 能独立传播 (CC BY 4.0)。

## 许可证

代码 [MIT](./LICENSE), `guides/` 下文档 CC BY 4.0 (每份文件自己标注)。
