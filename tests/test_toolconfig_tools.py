"""test_toolconfig_tools.py — 本地工具启停状态 CRUD。

用 monkeypatch 把 _CONFIG_DIR 指到 tmp_path 隔离磁盘。
跑法: uv run pytest tests/test_toolconfig_tools.py -v
"""
import pytest

from agent.toolconfig import tools


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_CONFIG_DIR", tmp_path)
    return tmp_path


def test_load_缺文件返回空dict(cfg):
    assert tools.load() == {}


def test_filter_enabled_缺省全开(cfg):
    # 配置为空 → 所有注册工具都视为启用
    assert tools.filter_enabled(["a", "b", "c"]) == {"a", "b", "c"}


def test_set_enabled_关掉一个后filter排除它(cfg):
    tools.set_enabled("b", False)
    assert tools.filter_enabled(["a", "b", "c"]) == {"a", "c"}


def test_set_enabled_落盘可读回(cfg):
    tools.set_enabled("x", False)
    assert tools.load() == {"x": {"enabled": False}}


def test_list_status_以注册表为准_死配置忽略(cfg):
    tools.set_enabled("gone", False)   # gone 不在注册表 → 死配置
    tools.set_enabled("a", False)
    status = tools.list_status(["a", "b"])
    assert status == [{"name": "a", "enabled": False},
                      {"name": "b", "enabled": True}]


def test_load_坏json不炸回退空dict(cfg):
    (cfg / "tools.json").write_text("{ 坏", encoding="utf-8")
    assert tools.load() == {}
