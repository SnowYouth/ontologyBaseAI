"""agent/toolconfig/tools.py — 本地工具启停状态 (真相在注册表, 这里只记状态)。

约定: 配置里没有某个工具名 = 视为启用 (缺省全开)。
只记 "我手动关了谁", 新注册工具不改配置也立即可用。
"""
import json
from pathlib import Path

from core.observability import log_event

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _path() -> Path:
    return _CONFIG_DIR / "tools.json"


def load() -> dict:
    """读 config/tools.json; 缺文件/坏 JSON → {} (全缺省=全开)。"""
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log_event("toolconfig.load_error", {"file": "tools.json", "error": str(e)[:200]})
        return {}


def set_enabled(name: str, enabled: bool) -> None:
    """read-modify-write: 只改一个工具的 enabled, 落盘。"""
    data = load()
    data[name] = {"enabled": enabled}
    _CONFIG_DIR.mkdir(exist_ok=True)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("toolconfig.tool_set", {"name": name, "enabled": enabled})


def filter_enabled(all_names: list[str]) -> set[str]:
    """给注册表全量名, 返回该启用的集合。名字不在配置 → 视为 True。"""
    data = load()
    return {n for n in all_names if data.get(n, {}).get("enabled", True)}


def list_status(all_names: list[str]) -> list[dict]:
    """[{name, enabled}], 以 all_names (注册表) 为准, 死配置项忽略。"""
    data = load()
    return [{"name": n, "enabled": data.get(n, {}).get("enabled", True)}
            for n in all_names]
