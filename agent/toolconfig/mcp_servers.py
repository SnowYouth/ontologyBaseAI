"""agent/toolconfig/mcp_servers.py — MCP server 实体 CRUD (真相就在配置)。

一条 server = {name, command, args, enabled, category}。停一个 = enabled 翻
False, agent 遍历时跳过 (子进程根本不起)。缺文件时落内置默认 (老行为不变)。

category 是可见性层级 (local / serving), 由 agent 侧的 profile 门控消费 ——
serving 档位下只加载 category="serving" 的 server。老配置文件没这个键, 读到时
按 "local" 算 (最小暴露), 不需要迁移。
"""
import copy
import json
from pathlib import Path

from core.observability import log_event

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# category: 可见性层级, 与本地工具同口径 (agent/tools/base.py::register_tool)。
# local = 只在 local profile 加载; serving = 上线档位也加载。配置里没写这个键
# 的 server 按 local 处理 (最小暴露), 所以内置这条 notes-writer 虽然
# enabled=True, 在 serving profile 下也不会把 write_file 发给 LLM。
_DEFAULT = [{
    "name": "notes-writer",
    "command": "uv",
    "args": ["run", "python", "-m", "agent.mcp_server.server"],
    "enabled": True,
    "category": "local",
}]


def _path() -> Path:
    return _CONFIG_DIR / "mcp_servers.json"


def _write(servers: list[dict]) -> None:
    _CONFIG_DIR.mkdir(exist_ok=True)
    _path().write_text(json.dumps(servers, ensure_ascii=False, indent=2), encoding="utf-8")


def load() -> list[dict]:
    """读 config/mcp_servers.json; 缺文件 → 写入并返回内置默认。

    ★ 返回 deepcopy: 原先是 `list(_DEFAULT)` —— 浅拷贝, 里层 dict 还是同一个
    对象。调用方改一下返回值 (如 set_enabled 在缺文件时改 category) 就把
    模块级 _DEFAULT 改了, 污染本进程后续所有 load()。实测跑
    test_web_app + test_toolconfig_mcp_servers 时后者读到前者写的 "serving"。
    """
    p = _path()
    if not p.exists():
        _write(_DEFAULT)
        return copy.deepcopy(_DEFAULT)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log_event("toolconfig.load_error", {"file": "mcp_servers.json", "error": str(e)[:200]})
        return copy.deepcopy(_DEFAULT)


def enabled_servers() -> list[dict]:
    return [s for s in load() if s.get("enabled", False)]


def add(server: dict) -> None:
    servers = load()
    if any(s["name"] == server["name"] for s in servers):
        raise ValueError(f"MCP server 重名: {server['name']}")
    servers.append(server)
    _write(servers)
    log_event("toolconfig.mcp_add", {"name": server["name"]})


def delete(name: str) -> None:
    servers = [s for s in load() if s["name"] != name]
    _write(servers)
    log_event("toolconfig.mcp_delete", {"name": name})


def set_enabled(name: str, enabled: bool, category: str | None = None) -> None:
    """翻启停; category 非 None 时一并改可见性层级 (None = 不动该字段)。"""
    servers = load()
    for s in servers:
        if s["name"] == name:
            s["enabled"] = enabled
            if category is not None:
                s["category"] = category
            _write(servers)
            log_event("toolconfig.mcp_set", {"name": name, "enabled": enabled,
                                             "category": s.get("category", "local")})
            return
    raise KeyError(f"MCP server 不存在: {name}")
