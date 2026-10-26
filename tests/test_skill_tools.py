"""test_skill_tools.py — load_skill 工具。

跑法: uv run pytest tests/test_skill_tools.py -v
"""
import pytest

from agent.toolconfig import skills
from agent.tools.base import _REGISTRY
from agent.tools.skill_tools import load_skill


@pytest.fixture
def env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(skills, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(skills, "_SKILLS_DIR", skills_dir)
    d = skills_dir / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示\n---\n\n这是 demo 正文", encoding="utf-8")
    return skills_dir


def test_load_skill_返回正文(env):
    r = load_skill("demo")
    assert r["name"] == "demo"
    assert "这是 demo 正文" in r["content"]


def test_load_skill_不存在抛KeyError(env):
    # agent loop 的 try 会捕获并回填 error dict, 这里只验抛出
    with pytest.raises(KeyError):
        load_skill("nope")


def test_load_skill_注册为serving():
    assert "load_skill" in _REGISTRY
    assert _REGISTRY["load_skill"].category == "serving"
