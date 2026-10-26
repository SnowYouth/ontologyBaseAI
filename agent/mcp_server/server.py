"""agent/mcp_server/server.py — FastMCP server, 暴露 write_file 工具到 notes_ai/ 沙箱.

设计:
1. 跑在独立子进程, 通过 stdio 说 JSON-RPC 2.0 协议
2. write_file 只准写 SANDBOX (notes_ai/), 权限收敛在 server 一处
3. 时间戳防重名: 永不覆盖, 每次沉淀是新文件
4. server 端禁止 print 到 stdout (stdout 是 JSON-RPC 管道, 污染会让 client 解析炸)
"""
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# 沙箱 = 项目根/notes_ai (server.py 在 agent/mcp_server/ 下, parent.parent.parent = 项目根)
SANDBOX = Path(__file__).resolve().parent.parent.parent / "notes_ai"

mcp = FastMCP("notes-writer")


def _resolve_in_sandbox(filename: str) -> Path:
    """把 filename 解析成 SANDBOX 内的安全目标路径; 越界抛 ValueError.

    三层防护 (比读文件的 safe_path 更严, 写比读危险):
    ① 语法层: 拒绝路径分隔符和 .. — 必须纯文件名
    ② 自动建沙箱目录
    ③ 真实路径层: resolve 后 parent 必须正好是 SANDBOX (防符号链接绕过)
    时间戳防重名: stem_YYYYMMDD_HHMMSS.ext, 同秒撞名加 -1/-2.
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"filename 只能是纯文件名, 不准带路径: {filename}")
    SANDBOX.mkdir(exist_ok=True)
    stem, ext = os.path.splitext(filename)
    ts = time.strftime("%Y%m%d_%H%M%S")
    target = SANDBOX / f"{stem}_{ts}{ext}"
    n = 1
    while target.exists():
        target = SANDBOX / f"{stem}_{ts}-{n}{ext}"
        n += 1
    if target.resolve().parent != SANDBOX.resolve():
        raise ValueError(f"越出沙箱: {filename}")
    return target


@mcp.tool()
def write_file(filename: str, content: str) -> dict:
    """把内容写入 notes_ai/ 沙箱目录下的文件, 用于沉淀知识/笔记.

    Args:
        filename: 文件名 (只允许纯文件名, 不允许路径分隔符, 如 "rag-notes.md")
        content: 要写入的文本内容
    Returns:
        {"ok": bool, "path": str, "bytes": int}
    """
    target = _resolve_in_sandbox(filename)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target), "bytes": len(content.encode("utf-8"))}


if __name__ == "__main__":
    mcp.run()
