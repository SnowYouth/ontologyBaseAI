"""验证 5 个真实工具的 category 标注正确 (真实注册表, 非 mock)。"""
from agent.tools import get_all_tools  # noqa: F401  触发全部工具注册
from agent.tools.base import get_tool_categories


def test_五个工具category标注正确():
    cats = get_tool_categories()
    assert cats["read_file"] == "local"
    assert cats["list_dir"] == "local"
    assert cats["rag_query"] == "serving"
    assert cats["get_entity_cards"] == "serving"
    assert cats["execute_sql"] == "serving"


def test_serving_profile下只剩business工具(monkeypatch):
    """端到端: serving profile 下 _filter_local 剔除 read_file/list_dir。"""
    import core.runtime.agent as agent
    from agent.tools import get_all_tools
    monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
    schemas, impls = agent._filter_local(*get_all_tools())
    names = set(impls)
    assert "read_file" not in names
    assert "list_dir" not in names
    assert "rag_query" in names
    assert "execute_sql" in names
