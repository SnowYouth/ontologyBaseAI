# 安全说明

## 这个项目的安全定位

ontologyBaseAI 是一个**单用户本地开发工具**, 不是多租户服务。默认部署假设是
"只监听 localhost, 使用者就是机器所有者"。在这个假设下没有鉴权是可接受的;
一旦离开这个假设, 下面的边界会立刻变成真实漏洞。

## 已知边界 (设计如此, 不是 bug)

### 1. MCP server 管理 = 本机任意命令执行 ⚠️

最重要的一条。信任链:

```
前端"添加 MCP server"表单
  → POST /config/mcp_servers   (无鉴权, 不校验 command)
  → 原样写入 agent/config/mcp_servers.json
  → 下一轮 run_agent 对每台 enabled server spawn 子进程跑 command + args
```

能访问设置面板的人 = 能在本机执行任意命令的人。填
`command: "cmd", args: ["/c", ...]` 即可删文件、下后门、提权。

**做下列任一动作前, 必须先加防护**:

- `--host 0.0.0.0` 或绑定任何非 loopback 地址
- 反向代理 / 端口转发 / 内网穿透暴露该端口
- 部署到共享服务器或公网
- 让同网段其他设备 (手机等) 访问

**防护二选一或都做**:

1. **命令白名单** — `command` 只允许预批准值 (如仅 `uv`), 其他后端直接 400。
   最小成本闸门。
2. **鉴权** — 未通过身份验证不得触达任何 `/config/*` 写端点。

### 2. Agent 工具有真实副作用

agent 可以执行 SQL、读写文件。LLM 会被输入文本欺骗 (prompt injection), 这一点
无法靠提示词消除 —— 防线必须落在代码/OS/容器层。背景阅读:
[`guides/Agent沙箱与Prompt注入-安全认知指南.md`](guides/Agent沙箱与Prompt注入-安全认知指南.md)。

### 2b. `DEPLOY_PROFILE` 门控: 它管什么, 不管什么

`DEPLOY_PROFILE=serving` 收敛的是**agent 能看见哪些工具**, 两种来源同一套闸:

- 本地工具按 `register_tool(category=...)` 标注, 只放行 `serving` 的
  (`read_file` / `list_dir` 这类内省工具因此不进 agent)
- MCP server 按 `mcp_servers.json` 里的 `category` 放行, 缺省 `local`;
  被挡掉的**连子进程都不起**

两道闸叠加, 都只能更严: `config/tools.json` 里手动关掉的工具 (含 MCP 工具)
在任何 profile 下都不加载。

**它不管的**: `/config/*` 这些管理端点本身。门控拦不住上面第 1 条的 RCE ——
改 `mcp_servers.json` 的那个端点无鉴权, 攻击者可以直接把自己加的 server 标成
`category: "serving"`。**profile 门控不是鉴权的替代品**, 它降低的是
"LLM 被注入后能摸到什么"的上限, 不是"人能通过 HTTP 做什么"的上限。

> 历史: 2026-10-16 前 MCP 工具完全绕过这道闸 —— `serving` 下照样把
> `write_file` 发给 LLM, 而 `.env.example` 当时承诺的是"攻击者无法透过 agent
> 读源码"。那句话当时只对本地工具成立 (`_filter_local` 只过本地工具, 而递给
> LLM 的是合并 MCP 之后的全集)。**声明过的安全控制有洞, 比没有这个控制更糟** ——
> 使用者会按那句承诺来决定部署方式。门控现在的回归测试在
> `tests/test_agent.py::TestMcpToolGating`, 断言下在"真正递给 LLM 的那个
> schema 列表"上, 而不是某个过滤函数的返回值。

### 3. 数据库凭据与 LLM key 走 `.env`

`.env` 已在 `.gitignore` 中, 不要提交。仓库里只有 `.env.example` 占位值。
日志 (`config.loaded` 事件) 只记 key **是否填了**, 不记实际值。

### 4. SQL 执行

`execute_sql` 工具对接真实库。接生产库前请使用只读账号 —— 本项目不替你做
权限约束。

出口挂着 OBQC 校验 (对照本体拦 JOIN/列/枚举/规则违规), 但**那是纠错机制, 不是
安全边界** —— 它防的是 LLM 写出业务错误的 SQL, 不防恶意 SQL。权限约束只能靠
数据库账号。

### 5. 本体库含真实表名

`capabilities/ontology/ontology.db` 映射你的真实库结构 (表名、列名、枚举码值),
已在 `.gitignore` 中。公开分享前注意: 本体本身就是一份 schema 情报。
`scripts/init_ontology.py` 建的是脱敏的图书馆域示例本体, 与真库无关。

## 报告漏洞

这是个人学习/实验项目, 没有正式的安全响应流程, 也没有 SLA。

发现问题请开 **GitHub Issue**。如果你认为某个问题不适合公开披露, 请在 Issue 里
只写"发现一个安全问题, 想私下沟通", 不要贴细节, 我会回复联系方式。

请不要对不属于你的部署做测试。

## 支持范围

只维护 `main` 分支的最新状态, 不对历史版本发安全补丁。
