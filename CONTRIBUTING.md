# 贡献指南

欢迎 issue 和 PR。这是个人学习/实验项目, 下面是让改动能顺利合并的几条约定。

项目的核心是**本体链路** (本体语义层 → agent 拼 SQL → OBQC 校验 → 自演化飞轮),
README 有全貌。动本体 / OBQC / selfevolve 这几块前建议先读 `guides/OBDA-GUIDE.md`
和 `guides/SELF-EVOLVING-KNOWLEDGE-GUIDE.md` —— 那里讲清了为什么这么设计。

## 环境

- **Python 3.12** (锁定; 3.13/3.14 兼容性踩过坑)
- **[uv](https://docs.astral.sh/uv/)** 管依赖, 命令一律 `uv run xxx`, 别直接 `python` / `pytest`
- 要动 Web 前端再加 **Node 20.19+ / 22.12+** (vite 8 engines 要求) 和 **pnpm**
  (仓库只锁 `pnpm-lock.yaml`)
- **Linux / Windows 都能开发**; macOS 需要 **14+ 且 Apple Silicon** —— 上游 torch
  只发 `macosx_14_0_arm64` 轮子, 既无 x86_64 也无 sdist, Intel Mac 装不上

```bash
git clone <your-fork> ontologyBaseAI
cd ontologyBaseAI
uv sync
cp .env.example .env     # 填上你自己的 PRIMARY_API_KEY / PRIMARY_BASE_URL
uv run python scripts/init_ontology.py   # 建种子本体库
```

跑测试不需要 `.env`、不需要 embedding 模型、也不需要真实数据库:

```bash
uv run pytest -q                  # 后端 (当前 1041 绿)
cd frontend && pnpm test          # 前端 (当前 182 绿)
```

## TDD 是硬规则

本项目的测试不是补充材料, 是开发顺序本身:

1. **先写测试, 跑到红** —— 确认它真的测到了东西 (测试写错了也会"绿")
2. 再写实现, 跑到绿
3. 重构, 保持绿

修 bug 同理: 先写一个能复现 bug 的失败测试, 再修。没有失败测试的 bug 修复
很难证明问题真的存在、也很难防它回来。

CI (`.github/workflows/ci.yml`) 会跑后端 + 前端全量测试和冷启动验证, 全绿才合并。

## 提交

提交信息用 `类型(范围): 说明` —— 看 `git log` 就是现成样例:

```
fix(coldstart): 空 ontology.db 不再阻断建库
fix(test): db_sources 接线测试补 os.environ 隔离 — .env 迁移后同名变量会盖测试值
docs(env): .env.example 数据源段注释重写
```

常用类型: `feat` / `fix` / `test` / `docs` / `refactor` / `chore`。
说明写**为什么**, 不只是改了什么 —— 半年后回头看 diff 能看出改了啥, 看不出动机。

> 注: 仓库所有者在 `main` 上直接提交, 那是单人工作流。
> 外部贡献请走 fork + PR。

## 什么不要提进来

`.gitignore` 已经挡了, 但还是说明一下原因:

- `.env` —— 真实 key 和数据库凭据
- `capabilities/ontology/*.db` —— 本体库从 `scripts/init_ontology.py` 建, 不是版本化产物
- `models/` `chroma_db/` —— 本地模型和向量库, 几百 MB 到 GB 级
- `logs/` `sessions/` —— 运行时产物, 可能含真实查询内容
- `corpus/` 下任何内容 —— 这是 RAG 输入目录, 内容是**使用者自己的知识库**,
  项目不替使用者决定索引什么。别往里加"示例语料"再提交

**公开文档里不要出现**: 公司内网地址 (用 `your-llm-proxy.example.com` 占位)、
真实表名/库名/客户数据、具体模型名写死 (会漂, 指向 `.env.example` 即可)。

## 文档约定

- `guides/` —— 通用技术指南, 不含项目业务细节, CC BY 4.0, 可独立传播。
  新增请在文末带上 CC BY 声明 (照现有文件抄)。**本体那几份是主线**, 底座能力
  (RAG / agent loop / MCP) 的文档轻化处理。
- `README.md` —— 对外门面, 以本体链路为主体

## 安全

动 MCP server 管理、工具执行、SQL 相关代码前, 先读 [`SECURITY.md`](SECURITY.md)。
简短版: 设置面板目前等于本机任意命令执行, 安全性完全依赖"只监听 localhost"
这个部署假设。别在没加白名单或鉴权的前提下让它对外可达。

发现安全问题请看 `SECURITY.md` 的报告方式, 不要直接在公开 issue 贴可利用细节。
