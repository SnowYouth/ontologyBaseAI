"""agent/tools/base.py — Tool 抽象 + 装饰器 + 路径安全 helper.

为啥这么设计:
1. Tool 是 dataclass 不是 class — 只是个数据容器, 行为在 agent loop 里
2. @register_tool 装饰器 = 副作用注册, 减少手动维护 registry 的心智压力
3. 自动 schema 从 type hint + docstring 推, 让"加新工具"成本 = 写一个函数
4. safe_path 用 realpath 而不是 abspath — 解开符号链接, 防穿越
"""
from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from core.observability import log_event

# 项目根目录 (agent/tools/base.py 上溯三层)
PROJECT_ROOT = os.path.abspath(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def safe_path(path: str) -> str:
    """把 path 规范成 PROJECT_ROOT 内的绝对路径; 越出抛 ValueError.

    两道校验:
    1. 路径穿越: realpath 归一后必须在 PROJECT_ROOT 内 (老逻辑)
    2. 敏感路径黑名单: .env/.git/dotfile 等 (防线二, 见 _is_blocked)
    两道都过才返回。任一不过抛 ValueError, 由 agent loop 回填给 LLM。

    边界处理:
    - 绝对路径 (D:\\foo): os.path.join 直接用绝对部分, realpath 后 startswith fail
    - 中段含 .. (a/../b): realpath 归一化掉
    - 符号链接: realpath 解开, 真实路径若在外 → fail; 若仍在根内, 解析后的真实
      相对路径也会再过一次黑名单 (防"根内不带点的软链指向 .env"绕过, 见下)
    - 大小写: Windows realpath 返回真实大小写, 自动一致
    """
    p = os.path.realpath(os.path.join(PROJECT_ROOT, path))
    # 允许 p == PROJECT_ROOT (例如 list_dir(".") 列项目根)
    if p != PROJECT_ROOT and not p.startswith(PROJECT_ROOT + os.sep):
        raise ValueError(f"path 越出项目根: {path}")
    # 黑名单双查: 原始 path + realpath 解析后的真实相对路径。
    # 后者堵住"项目根内软链指向 .env"—— 软链名本身不带点(不命中), 但解析后真实段
    # 是 .env, 必须拦。密钥保护是不可谈判底线, 软链不能成为绕过口。
    real_rel = os.path.relpath(p, PROJECT_ROOT)
    if _is_blocked(path) or _is_blocked(real_rel):
        raise ValueError(f"敏感路径, 拒绝访问: {path}")
    return p


# ============ 路径黑名单 (防线二: 纵深防御) ============
# 硬底线: 代码内置, 不可被配置文件删减。配置只能"追加"更多黑名单, 永远不能
# "决定要不要拦这些"。理由 (反直觉但关键): config/path_blacklist.json 会被删、
# 写坏、清空; 若把"拦不拦 .env"交给配置, 一次手误就让中转密钥裸奔 —— 正是本轮
# 要消灭的 fail-open。密钥/凭据保护是不可谈判的底线, 所以硬编码在此, 配置动不了。
_HARDCODED_BLACKLIST = (".env", ".git")  # 前缀匹配: .env 覆盖 .env.local 等

# 运行时产物目录 (2026-10-21): 只匹配**首段**, 且精确等于 (不按前缀)。
# 真实事故: 2026-10-20 CQ-5 库没初始化, agent 转去读 kg_private/cq_result_current.json
# (上一轮的旧答案) 凑出一篇像样的长答案, error=None —— 对 agent 来说这些是"抄答案"的
# 捷径, 没有正当读取理由。同样硬编码: 清空配置不能把考卷答案重新暴露给它。
# notes_ai: 10-21 端到端复现时, 拦掉 kg_private 后 agent 转去读这里的 CQ 重跑产物。
_RUNTIME_ARTIFACT_DIRS = ("kg_private", "logs", "sessions", "notes_ai")

_BLACKLIST_CONFIG_PATH = os.path.join(
    PROJECT_ROOT, "agent", "config", "path_blacklist.json")


def _load_blacklist_extra() -> list[str]:
    """读 config/path_blacklist.json 的追加黑名单项 (list[str])。

    fail-safe: 文件缺失 / 坏 JSON / 结构非 list → 返回空列表 (只用硬底线),
    绝不因配置问题放开硬底线。坏配置落 log 但不炸。
    """
    try:
        with open(_BLACKLIST_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log_event("blacklist.load_error", {"error": str(e)[:200]})
        return []


def _is_blocked(path: str) -> bool:
    """判断相对路径是否命中黑名单 (硬底线 + 配置追加)。

    命中规则: 首段 ∈ _RUNTIME_ARTIFACT_DIRS 即 True; 否则路径按 / 和 \\ 拆成段, 任意一段:
    - 以硬底线前缀开头 (.env / .env.local / .git 都命中)
    - 或以 "." 开头 (任意 dotfile/dotdir, 和 list_dir 跳隐藏文件对齐)
    - 或等于/前缀匹配配置追加项
    只要有一段命中即 True。
    例外: "." 和 ".." 是路径导航符 (list_dir(".") 列项目根), 不是隐藏文件, 跳过。
    """
    extra = _load_blacklist_extra()
    segments = [s for s in path.replace("\\", "/").split("/") if s and s != "."]
    if segments and segments[0] in _RUNTIME_ARTIFACT_DIRS:
        return True
    for seg in segments:
        if seg in (".", ".."):                        # 路径导航符, 非 dotfile
            continue
        if seg.startswith("."):                       # 所有 dotfile (含 .env/.git)
            return True
        if any(seg.startswith(h) for h in _HARDCODED_BLACKLIST):
            return True
        if any(seg == e or seg.startswith(e) for e in extra):
            return True
    return False


# ============ Tool 抽象 ============

@dataclass
class Tool:
    """工具元数据 + 执行函数."""
    name: str                   # = func.__name__
    description: str            # docstring 第一段
    parameters: dict            # OpenAI JSON Schema
    func: Callable[..., Any]
    category: str = "local"     # local=仅本地可见 / serving=上线也可见 (缺省最小暴露)


_REGISTRY: dict[str, Tool] = {}


# Python type → JSON Schema 映射. 第一版只支持这 6 类.
# 故意不支持 Pydantic / 自定义 class — YAGNI, 阶段 3 加复杂工具再扩.
_TYPE_MAP: dict[Any, dict] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    dict: {"type": "object"},
    list[str]: {"type": "array", "items": {"type": "string"}},
}


def _type_to_json_schema(annotation: Any) -> dict:
    """Python type hint → OpenAI JSON Schema; 不认识的抛 ValueError."""
    if annotation in _TYPE_MAP:
        return _TYPE_MAP[annotation]
    raise ValueError(f"不支持的 type hint: {annotation!r} (支持 str/int/float/bool/dict/list[str])")


def _build_tool_from_func(func: Callable, category: str = "local") -> Tool:
    """从函数签名 + docstring 构造 Tool。category 决定工具可见性层级."""
    doc = inspect.getdoc(func)
    if not doc or not doc.strip():
        raise ValueError(f"{func.__name__} 缺 docstring")

    sig = inspect.signature(func)
    properties: dict = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if param.annotation is inspect.Parameter.empty:
            raise ValueError(f"{func.__name__}.{name} 缺 type hint")
        properties[name] = _type_to_json_schema(param.annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)

    description = doc.split("\n\n")[0].strip()
    return Tool(
        name=func.__name__,
        description=description,
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
        category=category,
    )


def register_tool(func: Callable | None = None, *, category: str = "local"):
    """装饰器: 把 Python 函数注册成 LLM 工具。可带参也可裸用。

    两种用法:
      @register_tool                      # 裸用, category 缺省 "local"
      @register_tool(category="serving")  # 带参, 显式指定可见性层级

    category 语义 (信任边界, 见 core/agent.py::_filter_local):
      local   — 仅 DEPLOY_PROFILE=local 时加载 (开发内省工具, 如 read_file)
      serving — 两种 profile 都加载 (业务工具, 如 rag_query/execute_sql)
    缺省 local = 最小暴露: 忘标的工具上线自动藏起来, 不裸奔 (fail-safe)。

    实现: func 为 None 表示带参调用 (@register_tool(category=...)), 返回一个
    真正的装饰器闭包等第二次调用拿到 func; func 非 None 表示裸用, 直接注册。

    要求函数: 参数有 type hint / 有 docstring / 返回可 JSON 序列化。
    Returns: 原函数 (不包装, 只为副作用注册)。
    """
    def _decorate(f: Callable) -> Callable:
        tool = _build_tool_from_func(f, category=category)
        _REGISTRY[tool.name] = tool
        return f

    if func is None:            # 带参: @register_tool(category="serving")
        return _decorate
    return _decorate(func)      # 裸用: @register_tool


def _to_openai_schema(t: Tool) -> dict:
    """Tool → OpenAI function-calling schema dict."""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }


def get_all_tools() -> tuple[list[dict], dict[str, Callable]]:
    """返回 (OpenAI schemas, name → func dict), 给 agent loop 喂 LLM 用."""
    schemas = [_to_openai_schema(t) for t in _REGISTRY.values()]
    impls = {t.name: t.func for t in _REGISTRY.values()}
    return schemas, impls


def get_tool_categories() -> dict[str, str]:
    """返回 name → category 映射, 给 agent 层做 profile 门控用。

    独立于 get_all_tools (后者签名是硬合同, web/main 都解包两元组, 不动)。
    """
    return {t.name: t.category for t in _REGISTRY.values()}
