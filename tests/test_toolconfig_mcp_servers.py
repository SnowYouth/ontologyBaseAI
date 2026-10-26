"""test_toolconfig_mcp_servers.py — MCP server 实体 CRUD。

跑法: uv run pytest tests/test_toolconfig_mcp_servers.py -v
"""
import pytest

from agent.toolconfig import mcp_servers as ms


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_CONFIG_DIR", tmp_path)
    return tmp_path


def test_load_缺文件返回内置默认_notes_writer(cfg):
    servers = ms.load()
    assert len(servers) == 1
    assert servers[0]["name"] == "notes-writer"
    assert servers[0]["enabled"] is True
    # 缺文件时写盘, 二次读一致
    assert (cfg / "mcp_servers.json").exists()


def test_add_新增一条可读回(cfg):
    ms.load()  # 触发默认落盘
    ms.add({"name": "s2", "command": "uv", "args": ["run", "x"], "enabled": True})
    names = [s["name"] for s in ms.load()]
    assert "s2" in names


def test_add_重名报ValueError(cfg):
    ms.load()
    with pytest.raises(ValueError):
        ms.add({"name": "notes-writer", "command": "x", "args": [], "enabled": True})


def test_delete_按name删(cfg):
    ms.load()
    ms.delete("notes-writer")
    assert ms.load() == []


def test_set_enabled_翻状态(cfg):
    ms.load()
    ms.set_enabled("notes-writer", False)
    assert ms.load()[0]["enabled"] is False


def test_默认server的category是local(cfg):
    """★ 最小暴露 (2026-10-16): 内置那条虽然 enabled=True, category 必须是
    local —— 否则新 clone 零配置就在 serving profile 下发出 write_file。"""
    assert ms.load()[0]["category"] == "local"


def test_set_enabled_带category一并改(cfg):
    ms.load()
    ms.set_enabled("notes-writer", True, category="serving")
    assert ms.load()[0]["category"] == "serving"


def test_set_enabled_category为None时不动该字段(cfg):
    ms.load()
    ms.set_enabled("notes-writer", True, category="serving")
    ms.set_enabled("notes-writer", False)        # 只翻开关
    assert ms.load()[0]["category"] == "serving"


def test_load返回值可改而不污染内置默认(cfg, tmp_path, monkeypatch):
    """★ load() 必须 deepcopy (2026-10-16 当场栽过)。

    原先 `list(_DEFAULT)` 是浅拷贝, 里层 dict 共享 —— 缺文件时改返回值就把
    模块级 _DEFAULT 改了, 本进程后续 load() 全带着脏值。症状是跨文件的测试
    污染: test_web_app 把 category 改成 serving, 本文件读到的"内置默认"
    就成了 serving (单跑绿、连跑红)。
    """
    got = ms.load()
    got[0]["category"] = "serving"
    got[0]["enabled"] = False
    monkeypatch.setattr(ms, "_CONFIG_DIR", tmp_path / "another")
    fresh = ms.load()
    assert fresh[0]["category"] == "local"
    assert fresh[0]["enabled"] is True


def test_set_enabled_不存在报KeyError(cfg):
    ms.load()
    with pytest.raises(KeyError):
        ms.set_enabled("nope", False)


def test_enabled_servers_只返回启用的(cfg):
    ms.load()
    ms.add({"name": "off", "command": "x", "args": [], "enabled": False})
    names = [s["name"] for s in ms.enabled_servers()]
    assert names == ["notes-writer"]
