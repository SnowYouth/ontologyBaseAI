"""core/model_config.py — 模型候选清单 + 激活项 (真相在 config/model.json)。

为什么不住在 core/config.py 里:
    `settings` 是启动期一次性加载的 pydantic 单例, 改 .env 必须重启进程。
    前端要能改模型并**下一轮对话即生效**, 取值路径就不能走 settings ——
    complete() 每次调用现读这里 (文件 I/O 在 LLM 网络调用面前可忽略)。

与 agent/toolconfig/*.py 的分工:
    那边管"能力开关"且配置文件进 git; 这里带 api_key, 文件**不进 git**
    (.gitignore 挡 config/model.json), 所以单独一个目录, 别混进 agent/config/。

缺文件时的语义 (刻意):
    用 .env 的 PRIMARY_* 合成一条名为 default 的候选返回, 但 **不落盘** ——
    落盘等于把密钥复制出第二份。只有用户真点保存 (add/update/set_active)
    才建文件, 届时把种子一并固化进去。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.config import is_placeholder_api_key, settings
from core.observability import log_event

# 顶层 config/ (core/ 的父目录下), 不是 agent/config/
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_SEED_NAME = "default"
# name 要进 URL path (PUT /config/model/{name}), 只许字母数字下划线连字符
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _path() -> Path:
    return _CONFIG_DIR / "model.json"


def _seed() -> dict:
    """.env 的 PRIMARY_* → 一条候选。只在内存里用, 调用方决定是否落盘。"""
    return {
        "active": _SEED_NAME,
        "models": [{
            "name": _SEED_NAME,
            "model": settings.primary_model,
            "api_key": settings.primary_api_key,
            "base_url": settings.primary_base_url,
        }],
    }


def _write(data: dict) -> None:
    _CONFIG_DIR.mkdir(exist_ok=True)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load() -> dict:
    """{"active": 名字, "models": [{name, model, api_key, base_url}]}。

    缺文件 / 坏 JSON / 空清单 → .env 种子 (不落盘)。坏 JSON 落一条 log 事件:
    静默回退会让"我明明配过"变成查不出的怪事。
    """
    p = _path()
    if not p.exists():
        return _seed()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log_event("model_config.load_error", {"error": str(e)[:200]})
        return _seed()
    if not isinstance(data, dict) or not data.get("models"):
        log_event("model_config.load_error", {"error": "models 为空或结构非法"})
        return _seed()
    return data


def active() -> dict:
    """当前生效的那条候选 (complete() 的取值入口)。

    active 指向不存在的名字 → 回退第一条 + 响亮 log。手工编辑文件时一个笔误
    不该让整个对话功能瘫掉, 但也不能静默。
    """
    data = load()
    models = data["models"]
    name = data.get("active", "")
    for e in models:
        if e.get("name") == name:
            return e
    log_event("model_config.active_missing",
              {"active": str(name)[:50], "fallback": models[0].get("name", "")})
    return models[0]


def _validate_new(entry: dict, existing: list[dict]) -> None:
    name = (entry.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"模型配置名 '{name}' 非法: 只允许字母/数字/下划线/连字符")
    if any(e.get("name") == name for e in existing):
        raise ValueError(f"模型配置名重复: {name}")
    if not (entry.get("model") or "").strip():
        raise ValueError("model 不能为空 (litellm 格式 provider/model)")


def add(entry: dict) -> None:
    """追加一条候选; 不改 active (新增不等于要用它)。"""
    data = load()
    _validate_new(entry, data["models"])
    data["models"].append({
        "name": entry["name"].strip(),
        "model": entry["model"].strip(),
        "api_key": entry.get("api_key", ""),
        "base_url": (entry.get("base_url") or "").strip(),
    })
    _write(data)
    log_event("model_config.add", {"name": entry["name"].strip()})


def update(name: str, model: str | None = None, api_key: str | None = None,
           base_url: str | None = None) -> None:
    """read-modify-write 单条。None = 该字段不动。

    ★ api_key 的空串也按"不动"处理 (不是 None 才不动): 前端拿到的是掩码串,
    用户不改 key 时提交空值, 空若解释成"清空"则每次改 base_url 都洗掉 key。
    base_url 相反 —— 空有明确语义 (走 provider 官方端点), 空就是空。
    """
    data = load()
    for e in data["models"]:
        if e.get("name") != name:
            continue
        if model is not None and model.strip():
            e["model"] = model.strip()
        if api_key:                      # 空串/None 都不动
            e["api_key"] = api_key
        if base_url is not None:         # 空串 = 真清空
            e["base_url"] = base_url.strip()
        _write(data)
        log_event("model_config.update", {"name": name, "key_changed": bool(api_key)})
        return
    raise KeyError(f"模型配置不存在: {name}")


def set_active(name: str) -> None:
    data = load()
    if not any(e.get("name") == name for e in data["models"]):
        raise KeyError(f"模型配置不存在: {name}")
    data["active"] = name
    _write(data)
    log_event("model_config.set_active", {"name": name})


def delete(name: str) -> None:
    """删一条。删 active 被拒 (删掉正在用的 = 下一句对话没模型可用)。"""
    data = load()
    if not any(e.get("name") == name for e in data["models"]):
        raise KeyError(f"模型配置不存在: {name}")
    if data.get("active") == name:
        raise ValueError(f"'{name}' 正在使用中, 请先切换到其它模型再删")
    data["models"] = [e for e in data["models"] if e.get("name") != name]
    _write(data)
    log_event("model_config.delete", {"name": name})


def mask_key(key: str) -> str:
    """sk-REPLACE-ME → sk-••••ME。短 key (≤8) 全掩: 露 5 个字符等于露大半。"""
    k = key or ""
    if not k:
        return ""
    if len(k) <= 8:
        return "•" * 8
    return f"{k[:3]}••••{k[-2:]}"


def list_status() -> list[dict]:
    """给前端的列表: 不含明文 key, 只给掩码 + 是否已配 + 是否激活。"""
    data = load()
    cur = data.get("active", "")
    return [{
        "name": e.get("name", ""),
        "model": e.get("model", ""),
        "base_url": e.get("base_url", ""),
        "api_key_masked": mask_key(e.get("api_key", "")),
        "api_key_set": not is_placeholder_api_key(e.get("api_key", "")),
        "active": e.get("name") == cur,
    } for e in data["models"]]


def get_key(name: str) -> str:
    """明文 key。唯一一个回明文的出口 (前端"看明文"按钮), 端点侧另有 profile 闸。"""
    for e in load()["models"]:
        if e.get("name") == name:
            return e.get("api_key", "")
    raise KeyError(f"模型配置不存在: {name}")
