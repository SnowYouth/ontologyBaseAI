"""core/mcp_client.py — MCP client 同步封装.

设计:
1. 官方 SDK 的 client 是 async; 这里用 asyncio.run 包成纯同步, agent loop 碰不到 async
2. 每次 list_tools / call_tool 起一次 server 子进程, 用完关闭 (asyncio.run + 上下文管理器)
3. 协议层完全交给 SDK (零协议坑), 只桥接同步
4. pywin32 运行时注入 (顶部, import mcp 之前): Windows+uv 装 pywin32 不跑 postinstall,
   import mcp 链会炸; 运行时注入 win32 目录修复, 进 git 即根治, 非 Windows 静默跳过
"""
import os
import sys

_sp = os.path.join(sys.prefix, "Lib", "site-packages")
for _sub in ("win32", "win32/lib"):
    _p = os.path.join(_sp, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
_dll = os.path.join(_sp, "pywin32_system32")
if os.path.isdir(_dll):
    os.add_dll_directory(_dll)

import asyncio
import json
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# server 无响应时的读超时 (防永久阻塞). mcp==1.28.1 把超时放在
# ClientSession.__init__ 的 read_timeout_seconds (datetime.timedelta), 不在
# StdioServerParameters; call_tool 也接受同名 per-call 覆盖.
_READ_TIMEOUT = timedelta(seconds=30)


class MCPClient:
    """MCP server 的纯同步 client 封装. 每次调用起/关一次 server 子进程."""

    def __init__(self, command: str = "uv", args: list[str] | None = None):
        self.command = command
        self.args = args or ["run", "python", "-m", "agent.mcp_server.server"]

    def list_tools_openai(self) -> list[dict]:
        """起 server → initialize → tools/list → 转 OpenAI function schema → 关."""
        return self._run(self._list_tools)

    def call_tool(self, name: str, args: dict) -> str:
        """起 server → initialize → tools/call → 抽 result.content[].text → 关.

        错误不抛, 转成错误文本返回 (让 agent 把错误回填 LLM).
        """
        try:
            return self._run(lambda session: self._call_tool(session, name, args))
        except Exception as e:
            return f'{{"error": "{type(e).__name__}: {e}"}}'

    def _run(self, coro_fn):
        """asyncio.run 包 stdio_client + ClientSession 上下文 (每次起关)."""
        async def _main():
            params = StdioServerParameters(command=self.command, args=self.args)
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=_READ_TIMEOUT
                ) as session:
                    await session.initialize()
                    return await coro_fn(session)
        return asyncio.run(_main())

    async def _list_tools(self, session) -> list[dict]:
        resp = await session.list_tools()
        return [self._to_openai(t) for t in resp.tools]

    async def _call_tool(self, session, name, args) -> str:
        resp = await session.call_tool(name, args)
        # resp.content 是 list[TextContent/...]; 抽 text 拼接.
        parts = [c.text for c in resp.content if hasattr(c, "text")]
        text = "".join(parts) if parts else str(resp.content)
        # 错误工具名 / 工具内异常时 SDK 返回 isError=True 的 CallToolResult (不抛),
        # content 文本不一定含 "error" 字样; 显式包成 error JSON, 让 agent 能识别.
        if getattr(resp, "isError", False):
            return f'{{"error": {json.dumps(text, ensure_ascii=False)}}}'
        return text

    @staticmethod
    def _to_openai(tool) -> dict:
        """MCP Tool → OpenAI function schema (与 agent/tools/base.py::_to_openai_schema 同构)."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
