"""agent/tools/read_file.py — 读项目内文件 (按行分页, 跟 Claude Code Read tool 同款).

设计:
1. 路径必须在 PROJECT_ROOT 内 (safe_path 拦)
2. offset/limit 分页, limit 上限 2000 (LLM 一次塞 2000 行已经够吃 token)
3. truncated 字段告诉 agent 是否还有, 让 LLM 决定要不要继续读

Note: 不用 `from __future__ import annotations` — base._type_to_json_schema 用
`annotation in _TYPE_MAP` 直接比对类型对象, future import 会让注解变字符串匹配失败.
Python 3.12 原生支持运行时注解, 无需 future.
"""
from agent.tools.base import register_tool, safe_path


@register_tool(category="local")
def read_file(path: str, offset: int = 0, limit: int = 200) -> dict:
    """读项目内文件的若干行 (按行分页, 跟 Claude Code Read tool 同款).

    适用: 想看某个文件完整内容时. rag_query 给的是片段, read_file 给完整或分页全文.
    路径限制在项目根目录内 (越出抛 ValueError 回填给你).

    Args:
        path: 项目相对路径或绝对路径, 必须在项目根内
        offset: 起始行号 (0-based)
        limit: 读多少行, 默认 200, 上限 2000

    Returns:
        {"content": "...", "total_lines": 412, "start": 0, "end": 200, "truncated": true}
        truncated=true 表示后面还有, 你可以再调一次 offset=200 继续读, 或换 rag_query.
    """
    abs_path = safe_path(path)
    capped_limit = min(max(limit, 1), 2000)
    with open(abs_path, encoding="utf-8") as f:
        lines = f.readlines()
    total = len(lines)
    end = min(offset + capped_limit, total)
    content = "".join(lines[offset:end])
    return {
        "content": content,
        "total_lines": total,
        "start": offset,
        "end": end,
        "truncated": end < total,
    }
