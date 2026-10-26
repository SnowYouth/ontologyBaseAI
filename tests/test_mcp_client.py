"""tests/test_mcp_client.py — MCPClient 同步封装测试.

这组测试起真 server 子进程 (uv run python -m agent.mcp_server.server), 不 mock,
端到端验证 MCP 协议桥接. 每个用例 ~2-5 秒 (subprocess 启动), 打 slow 标记但不 skip.
"""
import json
import os

import pytest

from core.mcp_client import MCPClient

# 项目根 (tests/ 的父目录), 用于清理 notes_ai/ 产物
PROJECT_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
NOTES_AI = os.path.join(PROJECT_ROOT, "notes_ai")


@pytest.mark.slow
def test_list_tools_openai_返回正确schema格式():
    mcp = MCPClient()
    schemas = mcp.list_tools_openai()

    assert isinstance(schemas, list)
    assert len(schemas) >= 1

    wf = next((s for s in schemas if s["function"]["name"] == "write_file"), None)
    assert wf is not None, "应含 name==write_file 的工具"

    # 结构 == {"type":"function","function":{name,description,parameters}}
    assert wf["type"] == "function"
    assert set(wf["function"].keys()) == {"name", "description", "parameters"}

    props = wf["function"]["parameters"]["properties"]
    assert "filename" in props
    assert "content" in props


@pytest.mark.slow
def test_call_tool_同步返回结果文本():
    mcp = MCPClient()
    result_text = mcp.call_tool(
        "write_file",
        {"filename": "task2-smoke.md", "content": "hello from test"},
    )

    assert isinstance(result_text, str)
    # server 返回 {"ok":True,"path":...,"bytes":...} 序列化文本
    assert "ok" in result_text or "path" in result_text

    # 清理 notes_ai/task2-smoke* 产物
    if os.path.isdir(NOTES_AI):
        for fn in os.listdir(NOTES_AI):
            if fn.startswith("task2-smoke"):
                try:
                    os.remove(os.path.join(NOTES_AI, fn))
                except OSError:
                    pass


@pytest.mark.slow
def test_call_tool_错误工具名_返回error文本不抛():
    mcp = MCPClient()
    result_text = mcp.call_tool("not_a_real_tool", {})
    assert isinstance(result_text, str)
    assert "error" in result_text.lower()
