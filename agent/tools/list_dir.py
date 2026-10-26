"""agent/tools/list_dir.py — 列项目内目录.

Note: 不用 `from __future__ import annotations` — base._type_to_json_schema 用
`annotation in _TYPE_MAP` 直接比对类型对象, future import 会让注解变字符串匹配失败.
Python 3.12 原生支持 `list[dict]` 运行时注解, 无需 future.
"""
import os

from agent.tools.base import register_tool, safe_path


@register_tool(category="local")
def list_dir(path: str = ".") -> dict:
    """列出项目内某个目录下的文件和子目录.

    适用: 想探索项目结构、找某个文件叫啥时.
    路径限制在项目根目录内, 隐藏文件 (.env / .git / .__pycache__) 自动跳过.

    Args:
        path: 项目相对路径, 默认 "." 列项目根

    Returns:
        {"items": [{"name": "main.py", "type": "file", "size": 12345}, ...], "n": 8}
        type ∈ {"file", "dir"}; size 单位字节, dir 类型 size=0.
        非目录返回 {"error": "not a directory: <path>"}.
    """
    abs_path = safe_path(path)
    if not os.path.isdir(abs_path):
        return {"error": f"not a directory: {path}"}
    items: list[dict] = []
    for name in sorted(os.listdir(abs_path)):
        if name.startswith("."):
            continue
        full = os.path.join(abs_path, name)
        if os.path.isdir(full):
            items.append({"name": name, "type": "dir", "size": 0})
        else:
            items.append({"name": name, "type": "file", "size": os.path.getsize(full)})
    return {"items": items, "n": len(items)}
