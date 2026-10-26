# MCP (Model Context Protocol) 生产级指南

> CC BY 4.0 — 可自由转载，注明来源。
> 面向开发者，通用知识，不含项目业务细节。

---

## 1. 问题是什么

### 1.1 agent 怎么接外部能力

现代 AI agent 的核心循环很简单：LLM → 工具调用 → 结果回填 → 再判断。"工具"是 agent 能力的边界。

最朴素的做法是**本地函数注册**：把 Python 函数加到一个 dict，agent loop 直接 `impls[name](**args)` 调用。这能工作，但有三个痛点：

1. **进程边界**：工具逻辑必须在同一进程里。想接一个 Go 写的工具、或者一个独立的数据库服务？没有办法。
2. **权限无收敛**：本地函数能访问整个进程的文件系统、内存、网络。一个写文件工具和一个读文件工具的权限边界只靠开发者自律。
3. **描述格式碎片化**：OpenAI function calling 用一套 JSON Schema，Anthropic tool_use 用另一套，不同框架各搞一套适配层。

### 1.2 MCP 是什么

**MCP（Model Context Protocol）** 是 Anthropic 发布的开放协议，解决上面三个问题：

- **进程隔离**：工具（Server）跑在独立进程，Host/Client 通过协议调用。可以是本机子进程（stdio），也可以是远端服务（HTTP/SSE）。
- **权限收敛**：安全边界在 Server 侧实现，Client 只能请求，能不能做由 Server 说了算。
- **统一描述**：Server 用 JSON Schema 描述工具参数，所有 MCP Client 都能理解，不依赖特定 LLM 提供商。

**一句话**：MCP 是"agent 接外部能力"的标准规矩，协议管"怎么对话"，不管工具叫什么名字或做什么事。

---

## 2. 协议原理

### 2.1 三个角色

| 角色 | 职责 |
|---|---|
| **Host** | 运行 LLM 的程序（通常和 Client 在同一进程） |
| **Client** | 连接 Server，代表 Host 查询和调用工具 |
| **Server** | 实现工具，暴露给 Client，持有权限和业务逻辑 |

### 2.2 传输层（Transport）

协议本身不绑定传输方式，目前主要两种：

| Transport | 适用场景 |
|---|---|
| **stdio** | Server 是本机子进程，Client 通过标准输入输出通信。开发/学习首选，零网络配置。 |
| **HTTP/SSE** | Server 是远端服务或长期运行的进程。支持多 Client 连接，适合生产部署。 |

### 2.3 三个核心 Method（JSON-RPC 2.0）

这是 MCP 协议约束的全部对话规矩：

```
Client → Server: initialize
Server → Client: {capabilities: {...}, serverInfo: {...}}

Client → Server: tools/list
Server → Client: {tools: [{name, description, inputSchema}, ...]}

Client → Server: tools/call {name, arguments}
Server → Client: {content: [{type:"text", text:"..."}], isError: false}
```

**协议约束什么**：这三个 method 的调用顺序、消息格式、错误处理方式。

**协议不约束什么**：工具叫什么名字、参数叫什么、哪些参数必填、工具具体做什么——这些由 `inputSchema`（JSON Schema）描述，是 Server 自己定义的。

---

## 3. Server 侧：FastMCP 是怎么工作的

### 3.1 FastMCP 做了什么

手写一个符合 MCP 协议的 Server 需要：解析 JSON-RPC 请求、维护连接状态、把工具函数的 Python 签名翻译成 JSON Schema……

FastMCP 把这些全包了。你只需要：

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-server")

@mcp.tool()
def write_file(filename: str, content: str) -> dict:
    """把内容写入文件.
    
    Args:
        filename: 文件名
        content: 要写入的内容
    """
    # ... 业务逻辑
    return {"ok": True}

if __name__ == "__main__":
    mcp.run()   # 默认 stdio transport
```

FastMCP 自动做的事：
- 从 type hint 推导 `inputSchema`（`str` → `{"type":"string"}`）
- 从 docstring 提取 `description`（第一段）
- 实现 `initialize` / `tools/list` / `tools/call` 三个 method 的响应逻辑
- 把函数返回值序列化成 `content: [{type:"text", text:...}]`

### 3.2 stdio stdout 是协议管道——绝对不能 print

这是 stdio transport 的头号坑：**Server 进程的 stdout 就是 JSON-RPC 通道**。

任何 `print("调试信息")` 都会混进 JSON 流，Client 解析器会收到：

```
{"jsonrpc":"2.0","result":...}
调试信息
{"jsonrpc":"2.0","result":...}
```

第二行不是 JSON，解析崩溃。日志应该写到 **stderr** 或文件。

### 3.3 权限边界（破坏性工具的安全设计）

以"写文件"工具为例，权限收敛在 Server 侧：

```python
SANDBOX = Path(__file__).resolve().parent.parent / "notes_ai"

def _resolve_in_sandbox(filename: str) -> Path:
    # ① 语法层：拒绝路径分隔符和 .. —— 必须纯文件名
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"filename 不允许路径分隔符: {filename}")
    SANDBOX.mkdir(exist_ok=True)
    target = SANDBOX / filename
    # ② 真实路径层：resolve 后确认在沙箱内（防符号链接绕过）
    if target.resolve().parent != SANDBOX.resolve():
        raise ValueError(f"越出沙箱: {filename}")
    return target
```

Client 只能说"帮我写 `x.md`"，能不能写、写到哪，完全由 Server 说了算。

这是企业生产实践的微缩版——DB 凭据只在 Server 侧，Client 永远拿不到凭据本身。

---

## 4. 重点对照 1：手写 Client vs SDK Client

同样完成"调一个 MCP 工具"，手写和用 SDK 有什么区别？

### 4.1 手写 Client（理解协议用）

本质：起子进程 + 读写 stdin/stdout + 手工构造 JSON-RPC 消息：

```python
import subprocess, json

proc = subprocess.Popen(
    ["uv", "run", "python", "-m", "mcp_server.server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8",
)

def rpc(method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": id}
    if params:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())

# 1. initialize
rpc("initialize", {"protocolVersion": "2024-11-05",
                   "capabilities": {}, "clientInfo": {"name":"x","version":"0"}})

# 2. list tools
resp = rpc("tools/list")
print(resp["result"]["tools"])  # [{"name": "write_file", "inputSchema": {...}, ...}]

# 3. call tool
resp = rpc("tools/call", {"name": "write_file",
                           "arguments": {"filename": "x.md", "content": "hello"}})
print(resp["result"]["content"][0]["text"])  # {"ok": true, "path": "...", "bytes": 5}

proc.terminate()
```

**你负责**：进程生命周期管理、消息格式构造、错误处理、换行分帧（MCP stdio 用换行分隔 JSON）。

### 4.2 SDK Client（实际使用）

官方 mcp SDK 把上面的全部细节封装好：

```python
# async 版本（官方 SDK 原生是 async）
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="uv", args=["run", "python", "-m", "mcp_server.server"]
)

async def call_write_file():
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write,
                                 read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()                  # ← 自动构造握手消息
            tools = await session.list_tools()          # ← 自动解析响应
            result = await session.call_tool(
                "write_file", {"filename": "x.md", "content": "hello"}
            )
            return result.content[0].text               # ← 自动提取文本
```

**SDK 帮你做了**：
- 换行分帧与 JSON 解析
- initialize 握手协议细节（`protocolVersion` / `capabilities` 格式）
- 异常情况下 `isError=True` 的处理
- `async with` 确保进程 cleanup
- 超时保护（`read_timeout_seconds`）

**本质是同一件事**：手写和 SDK 走同样的三个 method，差的是鲁棒性和代码量。学习时可以手写一遍理解协议，生产用 SDK。

---

## 5. 重点对照 2：同步封装 vs 生产 async

### 5.1 为什么需要封装

官方 SDK 的 client 是 async：`ClientSession`、`stdio_client` 都是 `async with`，必须在 async 环境里运行。

如果你的 agent loop 是同步的（大多数早期实现是），直接用 SDK 会遇到"不能在同步代码里 await"的问题。

### 5.2 同步封装方案：asyncio.run

用 `asyncio.run` 把 async 代码关进同步函数：

```python
import asyncio
from datetime import timedelta
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class MCPClient:
    def __init__(self, command="uv",
                 args=None):
        self.command = command
        self.args = args or ["run", "python", "-m", "mcp_server.server"]

    def call_tool(self, name: str, arguments: dict) -> str:
        """纯同步接口，内部用 asyncio.run 执行 async 逻辑."""
        return asyncio.run(self._async_call(name, arguments))

    async def _async_call(self, name, arguments):
        params = StdioServerParameters(command=self.command, args=self.args)
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write,
                read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                return result.content[0].text if result.content else ""
```

**优点**：对外纯同步，不污染 agent loop；不需要把整个调用链改成 async。

**局限**：
- `asyncio.run` 每次调用都起一个新的 event loop，**不能并发**（一次只跑一个工具调用）
- 不能在已有 event loop（另一个 `asyncio.run`）里嵌套调用

### 5.3 生产 async（为什么企业是这样做的）

生产环境需要并发：一个 agent 可能同时连接 5 个 MCP Server，并行调用多个工具：

```python
async def run_parallel_tools(session, tool_calls):
    # 并行调用多个工具，不是串行等待
    tasks = [session.call_tool(tc.name, tc.args) for tc in tool_calls]
    results = await asyncio.gather(*tasks)
    return results
```

这要求整个调用链是 async 的——agent loop 是 async 函数，`ClientSession` 保持长连接（不每次起关），多工具调用用 `asyncio.gather` 并发。

**结论**：
- `asyncio.run` 同步封装 → 学习场景、单工具调用、已有同步 agent loop
- 原生 async → 生产场景、需要并发、多 Server 连接

async 是并发规模问题，**与 MCP 协议本身无关**——协议是同一套，只是执行模型不同。

---

## 6. 重点对照 3：每次起关 vs 生产常驻

### 6.1 当前实现：每次起关

每次 `call_tool` 都启动一个新的 Server 子进程，调用完就关掉：

```
call_tool("write_file", ...) 
  → subprocess.Popen(server.py)   # ~100-500ms 启动
  → initialize
  → tools/call
  → process.terminate()           # 关闭
```

**开销**：子进程启动 + Python 解释器 + FastMCP 初始化，每次 ~100-500ms。

**适用场景**：工具调用频率低（分钟级），学习/开发阶段。

### 6.2 生产常驻：连接池

生产场景里 Server 常驻运行，Client 保持长连接：

```python
# 启动时初始化，复用整个会话
session = await create_session(server_params)  # 起一次
await session.initialize()                      # 握手一次

# 每次调用复用同一个 session，无启动开销
result = await session.call_tool("write_file", ...)  # ~1-10ms
result = await session.call_tool("read_file", ...)   # ~1-10ms
```

更进一步，多 Server 场景用连接池管理：

```python
# 生产 MCP 客户端管理多个 Server 的连接
class MCPConnectionPool:
    sessions: dict[str, ClientSession]  # server_name → session
    
    async def call_tool(self, server_name, tool_name, args):
        session = self.sessions[server_name]
        return await session.call_tool(tool_name, args)
```

**生产差距对比**：

| 维度 | 每次起关 | 常驻连接池 |
|---|---|---|
| 调用延迟 | 100-500ms（含启动） | 1-10ms |
| 实现复杂度 | 低 | 中（需管理连接生命周期） |
| 并发支持 | ❌ | ✅ |
| 适用场景 | 学习/低频 | 生产/高频 |

---

## 7. 已知坑

### 7.1 pywin32 在 Windows + uv

**现象**：`import mcp` 报 `ImportError: No module named 'pywintypes'`。

**根因**：mcp 在 Windows 上依赖 pywin32。uv 装包不执行 pywin32 的 postinstall 脚本，导致 win32 目录没有被加入 Python 路径。

**修法 A（一次性）**：手动跑 postinstall：
```bash
uv run python venv/Lib/site-packages/pywin32_postinstall.py -install
```
问题：换机或重建 venv 后需重跑。

**修法 B（推荐，进 git）**：在 `import mcp` 之前运行时注入路径：
```python
import os, sys

_sp = os.path.join(sys.prefix, "Lib", "site-packages")
for _sub in ("win32", "win32/lib"):
    _p = os.path.join(_sp, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
_dll = os.path.join(_sp, "pywin32_system32")
if os.path.isdir(_dll):
    os.add_dll_directory(_dll)

# 现在可以正常 import
from mcp import ClientSession, ...
```

`isdir` 守卫：非 Windows 上这些目录不存在，代码是无副作用的空操作。进 git 后在任何机器上 import 即修复。

### 7.2 LLM 幻觉工具名

LLM 有时会生成不存在的工具名（如 `save_note` 代替 `write_file`）。

**处理**：在工具路由层加 KeyError 挡板，把错误回填给 LLM：
```python
if name not in local_tools and name not in mcp_tools:
    raise KeyError(f"未知工具: {name}，可用: {sorted(all_tool_names)}")
# 外层 except 捕获，把 error JSON 回填给 LLM 决策
```

不要让幻觉工具调用让 agent 崩溃——回填错误信息，让 LLM 有机会修正。

### 7.3 mcp SDK 版本 API 变化

mcp SDK 迭代较快，API 不稳定。以 `mcp==1.28.1` 为例：
- 超时参数：在 `ClientSession(read_timeout_seconds=timedelta(seconds=30))`，不在 `StdioServerParameters`
- 工具 schema 字段：`tool.inputSchema`（不是 `.schema`）
- 工具调用失败：SDK 不抛异常，返回 `CallToolResult(isError=True)`，需显式检查

建议：锁定版本号，升级前看 changelog。

---

## 8. 生产环境选型

### 8.1 什么时候引入 MCP

| 场景 | 建议 |
|---|---|
| 团队内多个 agent 共享同一组工具 | ✅ MCP server 统一暴露 |
| 工具需要独立权限控制（DB 凭据、文件系统边界） | ✅ MCP server 持有凭据 |
| 工具是非 Python 实现（Go/Rust/Node） | ✅ MCP 跨语言 |
| 单个 agent 自用、Python 工具、无共享需求 | 本地注册即可，MCP 是过度设计 |

### 8.2 transport 选型

| Transport | 适用 | 不适用 |
|---|---|---|
| stdio | 本机子进程、开发/调试 | 多客户端、跨机器 |
| HTTP/SSE | 远端服务、多客户端、生产部署 | 简单本地场景（复杂度高） |

### 8.3 本项目当前退化点

| 退化点 | 生产做法 | 评级 |
|---|---|---|
| 每次 `call_tool` 起/关 server 子进程（~200-500ms） | 常驻 server + 连接池（~1-10ms） | ⭐⭐ |
| 同步封装（asyncio.run，不能并发） | 原生 async client，并发调用多工具 | ⭐⭐ |
| 只有 stdio transport | HTTP/SSE 支持远端 server + 多 client | ⭐ |
| server 没有认证/鉴权 | API Key 或 mTLS | ⭐⭐ |

（⭐ 越多 = 离生产越远）

### 8.4 补齐路线（ROI 排序）

1. **常驻 server + 长连接**（高 ROI）：把 `ClientSession` 提到 agent 初始化时，不再每次起关。消除 500ms 延迟，代码改动小。
2. **原生 async client**（中 ROI）：agent loop 改 async，删掉 `asyncio.run` 封装。并发调用多工具，面向生产架构。
3. **HTTP/SSE transport**（低 ROI，除非有远端 server 需求）：把 server 改成 HTTP mode，client 用 `sse_client` 替代 `stdio_client`。

---

## 附录 A：术语词典

| 术语 | 解释 |
|---|---|
| **MCP** | Model Context Protocol，Anthropic 发布的 agent 工具接入协议 |
| **JSON-RPC 2.0** | 轻量级 RPC 协议，用 JSON 消息，无状态（每条消息独立） |
| **stdio transport** | 通过进程的 stdin/stdout 传输 JSON-RPC 消息的方式 |
| **Host** | 运行 LLM 的程序，控制 agent loop |
| **Client** | 代表 Host 连接 Server，发起工具查询和调用 |
| **Server** | 实现工具，持有权限边界，响应 Client 请求 |
| **inputSchema** | MCP Tool 对象的字段，是描述工具参数的 JSON Schema |
| **FastMCP** | mcp Python SDK 提供的 Server 快速构建库，自动把函数转成 MCP 工具 |
| **asyncio.run** | Python 标准库函数，启动一个新 event loop 运行 async 协程后返回结果 |
| **isError** | `CallToolResult.isError`，Server 返回错误时为 True，SDK 不抛异常 |

## 附录 B：进阶阅读

- MCP 官方规范：https://spec.modelcontextprotocol.io/
- mcp Python SDK：https://github.com/modelcontextprotocol/python-sdk
- FastMCP 文档：https://gofastmcp.com/
- MCP Server 示例集：https://github.com/modelcontextprotocol/servers
