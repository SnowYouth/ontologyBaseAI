"""test_agent_skill_catalog.py — L1 清单动态拼进当轮 system (热部署命门)。

跑法: uv run pytest tests/test_agent_skill_catalog.py -v
"""
import pytest

from core.runtime import agent
from agent.toolconfig import skills


@pytest.fixture
def env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(skills, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(skills, "_SKILLS_DIR", skills_dir)
    return skills_dir


def _make(skills_dir, name, desc):
    d = skills_dir / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n正文", encoding="utf-8")


def test_catalog_无skill返回空串(env):
    assert agent._build_skill_catalog() == ""


def test_catalog_有skill列出name和desc(env):
    _make(env, "obda", "查数据库")
    cat = agent._build_skill_catalog()
    assert "obda" in cat and "查数据库" in cat
    assert "load_skill" in cat  # 提示 agent 怎么用


def test_catalog_停用的不列(env):
    _make(env, "a", "AAA")
    _make(env, "b", "BBB")
    skills.set_enabled("b", False)
    cat = agent._build_skill_catalog()
    assert "AAA" in cat and "BBB" not in cat


def test_initial_messages_system含清单(env):
    _make(env, "obda", "查数据库")
    msgs = agent._build_initial_messages("你好", None)
    assert msgs[0]["role"] == "system"
    assert "查数据库" in msgs[0]["content"]
    # 原 system_prompt 仍在
    from core.config import settings
    assert settings.system_prompt in msgs[0]["content"]


def test_catalog_未焊死_两次调用间改动即刻反映(env):
    """热部署命门守卫: 连续两次 _build_initial_messages, 中间改启停, 第二次必须跟着变。

    证明 catalog 是每轮现算, 没有被写回 settings.system_prompt 焊死。
    """
    _make(env, "s1", "第一个技能")
    first = agent._build_initial_messages("q", None)[0]["content"]
    assert "第一个技能" in first
    skills.set_enabled("s1", False)      # 停用
    second = agent._build_initial_messages("q", None)[0]["content"]
    assert "第一个技能" not in second    # 焊死的话这里会 FAIL


def test_catalog_discover异常兜底返回空串(env, monkeypatch):
    """skill 坏了应"没有 skill"而非任务崩: enabled_catalog 抛异常时返回空串。"""
    def boom():
        raise OSError("磁盘炸了")
    monkeypatch.setattr(skills, "enabled_catalog", boom)
    assert agent._build_skill_catalog() == ""
