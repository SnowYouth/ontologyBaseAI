"""tools/base.py 单元测试 — Tool 抽象 + 装饰器 + safe_path 边界."""
import os
import pytest

from agent.tools.base import PROJECT_ROOT, safe_path
from agent.tools.base import _is_blocked


class TestIsBlocked:
    """路径黑名单: 硬底线 (代码内置) + 配置追加。缺省往拦截倒。"""

    def test_env_文件命中硬底线(self):
        assert _is_blocked(".env") is True

    def test_env_带后缀命中硬底线(self):
        assert _is_blocked(".env.local") is True

    def test_git_目录命中硬底线(self):
        assert _is_blocked(".git/config") is True
        assert _is_blocked(".git") is True

    def test_任意dotfile命中硬底线(self):
        assert _is_blocked(".hidden") is True
        assert _is_blocked("sub/.secret") is True

    def test_普通文件不命中(self):
        assert _is_blocked("corpus/foo.md") is False
        assert _is_blocked("core/config.py") is False

    def test_点和双点导航符不误判(self):
        # "." (list_dir 默认列项目根) 和 ".." 是路径导航符, 不是 dotfile, 不该拦
        assert _is_blocked(".") is False
        assert _is_blocked("notes/../corpus") is False

    def test_配置追加项命中(self, tmp_path, monkeypatch):
        cfg = tmp_path / "path_blacklist.json"
        cfg.write_text('["secret_dir"]', encoding="utf-8")
        monkeypatch.setattr("agent.tools.base._BLACKLIST_CONFIG_PATH", cfg)
        assert _is_blocked("secret_dir/x.txt") is True

    def test_配置缺失仍拦硬底线(self, tmp_path, monkeypatch):
        cfg = tmp_path / "nonexistent.json"
        monkeypatch.setattr("agent.tools.base._BLACKLIST_CONFIG_PATH", cfg)
        assert _is_blocked(".env") is True          # 硬底线仍在
        assert _is_blocked("corpus/foo.md") is False

    def test_配置坏json仍拦硬底线(self, tmp_path, monkeypatch):
        cfg = tmp_path / "path_blacklist.json"
        cfg.write_text("{ 坏 json", encoding="utf-8")
        monkeypatch.setattr("agent.tools.base._BLACKLIST_CONFIG_PATH", cfg)
        assert _is_blocked(".env") is True          # fail-safe: 坏配置不放开硬底线


class TestRuntimeArtifactBlock:
    """运行时产物目录 (2026-10-21): agent 读到上一轮 CQ 结果就能抄答案。

    真实事故: 2026-10-20 CQ-5 库没初始化, agent 转去 read_file 读
    kg_private/cq_result_current.json, 凑出一篇带表格的长答案且 error=None。
    """

    @pytest.mark.parametrize("p", [
        "kg_private/cq_result_current.json",
        "kg_private",
        "logs/run-20260919-191126.jsonl",
        "sessions/abc.jsonl",
        "kg_private\\cq_baselines\\r1.json",
        # 10-21 端到端复现: 拦掉 kg_private 后 agent 转去读这里的 CQ 重跑产物
        "notes_ai/_cq_rerun_2026-10-21.json",
    ])
    def test_运行时产物命中(self, p):
        assert _is_blocked(p) is True

    @pytest.mark.parametrize("p", [
        "corpus/logs/运维手册.md",      # 只拦项目根下的那几个, 非首段同名不误伤
        "logs_readme.md",              # 精确匹配, 不按前缀
        "core/session_store.py",
    ])
    def test_非首段或前缀相似不误伤(self, p):
        assert _is_blocked(p) is False

    def test_safe_path_拒绝读旧CQ结果(self):
        with pytest.raises(ValueError, match="敏感路径"):
            safe_path("kg_private/cq_result_current.json")

    def test_safe_path_大小写变体也拦(self):
        # Windows 大小写不敏感: 原始串不命中, 靠 realpath 解出的真实段兜住
        if os.name != "nt":
            pytest.skip("大小写不敏感文件系统才有此绕过")
        with pytest.raises(ValueError, match="敏感路径"):
            safe_path("KG_PRIVATE/cq_set.json")

    def test_配置清空也拦(self, tmp_path, monkeypatch):
        cfg = tmp_path / "path_blacklist.json"
        cfg.write_text("[]", encoding="utf-8")
        monkeypatch.setattr("agent.tools.base._BLACKLIST_CONFIG_PATH", cfg)
        assert _is_blocked("kg_private/cq_set.json") is True


class TestSafePath:
    def test_POSIX_穿越_抛(self):
        with pytest.raises(ValueError, match="越出项目根"):
            safe_path("../../etc/passwd")

    # 下面两条只在 Windows 有意义: safe_path 用 os.path, 平台语义不同。
    # POSIX 下反斜杠和 "D:" 都是合法文件名字符, 不构成穿越 —— normpath 不回溯,
    # 结果仍落在项目根内, 所以不抛才是对的 (不是漏洞, 那串在 Linux 上本就不是
    # 穿越向量)。断言绑死平台, 非 Windows 跳过。
    @pytest.mark.skipif(os.name != "nt", reason="反斜杠穿越只在 Windows 语义下成立")
    def test_Windows_穿越_抛(self):
        with pytest.raises(ValueError, match="越出项目根"):
            safe_path("..\\..\\windows\\system32")

    @pytest.mark.skipif(os.name != "nt", reason="盘符只在 Windows 语义下是绝对路径")
    def test_跨盘符_抛(self):
        # D 盘绝对路径, realpath 后不可能 startswith 项目根
        with pytest.raises(ValueError, match="越出项目根"):
            safe_path("D:\\foo.txt")

    def test_正常相对路径_通过(self):
        result = safe_path("corpus/foo.md")
        assert result.startswith(PROJECT_ROOT)
        assert result.endswith(os.path.join("corpus", "foo.md"))

    def test_中段含双点_最终在内_通过(self):
        result = safe_path("notes/../corpus/foo.md")
        # 最终归一到 PROJECT_ROOT/corpus/foo.md
        assert result.startswith(PROJECT_ROOT)
        assert "corpus" in result
        assert ".." not in result

    def test_读env被黑名单拦(self):
        with pytest.raises(ValueError, match="敏感路径"):
            safe_path(".env")

    def test_读git被黑名单拦(self):
        with pytest.raises(ValueError, match="敏感路径"):
            safe_path(".git/config")

    def test_普通路径不受黑名单影响(self):
        # 回归: 正常文件仍通过 (Task 1 的黑名单不误伤业务路径)
        result = safe_path("corpus/foo.md")
        assert result.startswith(PROJECT_ROOT)

    def test_软链指向env被拦(self, tmp_path, monkeypatch):
        # 防线二纵深: 项目根内一个不带点的软链指向 .env, realpath 解开后应被黑名单拦。
        # (Windows 建软链需权限/开发者模式, 建不了则跳过)
        link = os.path.join(PROJECT_ROOT, "innocent_link_test.txt")
        target = os.path.join(PROJECT_ROOT, ".env")
        try:
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("本机无法创建符号链接 (需权限/开发者模式)")
        try:
            with pytest.raises(ValueError, match="敏感路径"):
                safe_path("innocent_link_test.txt")
        finally:
            if os.path.lexists(link):
                os.remove(link)


from agent.tools.base import Tool, register_tool, get_all_tools, _REGISTRY


@pytest.fixture(autouse=True)
def _clear_registry():
    """每个测试隔离 _REGISTRY: clear 模式保留对象身份, 防 import 引用陷阱.

    plan 给的 monkeypatch.setattr 版本会替换 tools.base._REGISTRY 绑定到新 dict,
    但测试文件顶部 `from tools.base import _REGISTRY` 拿到的是旧 dict 引用,
    装饰器内部通过模块 globals 写新 dict, 测试断言读旧 dict, 永远空 → fail.
    用 clear() 原地改对象内容, 所有引用都看到一致状态.
    """
    import agent.tools.base as tb
    saved = dict(tb._REGISTRY)
    tb._REGISTRY.clear()
    yield
    tb._REGISTRY.clear()
    tb._REGISTRY.update(saved)


class TestRegisterTool:
    def test_装饰器从_func_自动生成_schema(self):
        @register_tool
        def my_tool(query: str, top_k: int = 5) -> dict:
            """这是工具用途的第一段.

            Args:
                query: 检索词
                top_k: 返回数
            """
            return {"ok": True}

        tools = _REGISTRY
        assert "my_tool" in tools
        t = tools["my_tool"]
        assert isinstance(t, Tool)
        assert t.name == "my_tool"
        assert t.description == "这是工具用途的第一段."
        assert t.parameters["type"] == "object"
        assert t.parameters["properties"]["query"]["type"] == "string"
        assert t.parameters["properties"]["top_k"]["type"] == "integer"
        # top_k 有默认值 → 不在 required
        assert t.parameters["required"] == ["query"]

    def test_无_type_hint_抛(self):
        with pytest.raises(ValueError, match="缺 type hint"):
            @register_tool
            def bad_tool(query):
                """docstring."""
                return {}

    def test_无_docstring_抛(self):
        with pytest.raises(ValueError, match="缺 docstring"):
            @register_tool
            def bad_tool(query: str) -> dict:
                return {}

    def test_装饰器返回原函数(self):
        @register_tool
        def my_tool(q: str) -> dict:
            """doc."""
            return {"q": q}

        assert my_tool(q="hello") == {"q": "hello"}

    def test_不支持的_type_hint_抛(self):
        from datetime import datetime
        with pytest.raises(ValueError, match="不支持的 type hint"):
            @register_tool
            def bad_tool(when: datetime) -> dict:
                """doc."""
                return {}

    def test_裸用装饰器_category缺省local(self):
        @register_tool
        def bare_tool(x: str) -> dict:
            """裸用工具."""
            return {}
        assert _REGISTRY["bare_tool"].category == "local"

    def test_带参装饰器_category_serving(self):
        @register_tool(category="serving")
        def biz_tool(x: str) -> dict:
            """业务工具."""
            return {}
        assert _REGISTRY["biz_tool"].category == "serving"

    def test_带参装饰器仍返回原函数(self):
        @register_tool(category="serving")
        def biz_tool(x: str) -> dict:
            """业务工具."""
            return {"x": x}
        assert biz_tool(x="hi") == {"x": "hi"}


class TestGetAllTools:
    def test_返回_schemas_和_impls(self):
        @register_tool
        def tool_a(x: str) -> dict:
            """工具 A."""
            return {"a": x}

        @register_tool
        def tool_b(y: int = 1) -> dict:
            """工具 B."""
            return {"b": y}

        schemas, impls = get_all_tools()
        assert len(schemas) == 2
        # OpenAI 格式: {"type": "function", "function": {...}}
        assert all(s["type"] == "function" for s in schemas)
        names = {s["function"]["name"] for s in schemas}
        assert names == {"tool_a", "tool_b"}
        assert set(impls.keys()) == {"tool_a", "tool_b"}
        assert impls["tool_a"](x="hi") == {"a": "hi"}

    def test_空_registry_返回空(self):
        schemas, impls = get_all_tools()
        assert schemas == []
        assert impls == {}

    def test_get_tool_categories_返回name到category映射(self):
        @register_tool
        def local_one(x: str) -> dict:
            """本地工具."""
            return {}

        @register_tool(category="serving")
        def serving_one(x: str) -> dict:
            """业务工具."""
            return {}

        from agent.tools.base import get_tool_categories
        cats = get_tool_categories()
        assert cats["local_one"] == "local"
        assert cats["serving_one"] == "serving"
