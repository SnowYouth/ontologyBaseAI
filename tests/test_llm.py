"""core/llm.py 单元测试 — mock litellm.completion, 不真打网络."""
import os

import pytest


class TestLLMNoiseSuppression:
    def test_env_var_已设(self):
        """LITELLM_LOCAL_MODEL_COST_MAP 必须在 import litellm 之前已设."""
        import core.llm  # noqa
        assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"

    def test_suppress_debug_info_已开(self):
        import core.llm  # noqa
        import litellm
        assert litellm.suppress_debug_info is True

    def test_litellm_logger_level_warning(self):
        import logging
        import core.llm  # noqa
        assert logging.getLogger("LiteLLM").level == logging.WARNING


class TestComplete:
    def _reload(self, monkeypatch, tmp_path):
        """reload core.config/model_config/llm 以吃进本测试设的 env。

        ★ reload(core.model_config) 会把模块级 _CONFIG_DIR 重置回真实的
        项目 config/ —— conftest 的隔离 fixture 在 reload 前就设好了, 拦不住
        重新执行模块体。所以 reload 之后必须再钉一次 tmp 目录, 否则测试会
        读写机主真实的 config/model.json。
        """
        import importlib, core.config, core.llm, core.model_config
        importlib.reload(core.config)
        importlib.reload(core.model_config)
        core.model_config._CONFIG_DIR = tmp_path
        importlib.reload(core.llm)
        return core.llm

    def test_primary_model_直传(self, monkeypatch, tmp_path):
        """没有 config/model.json 时, 模型三要素走 .env 的 PRIMARY_* 种子。"""
        monkeypatch.setenv("PRIMARY_MODEL", "anthropic/glm-5.1")
        llm = self._reload(monkeypatch, tmp_path)
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert called["model"] == "anthropic/glm-5.1"

    def test_primary_base_url_非空传_api_base(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRIMARY_MODEL", "anthropic/glm-5.1")
        monkeypatch.setenv("PRIMARY_API_KEY", "pk-1")
        monkeypatch.setenv("PRIMARY_BASE_URL", "https://mid.example.com")
        llm = self._reload(monkeypatch, tmp_path)
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert called["api_base"] == "https://mid.example.com"
        assert called["api_key"] == "pk-1"

    def test_primary_base_url_空_不传_api_base(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRIMARY_MODEL", "deepseek/deepseek-v4-pro")
        monkeypatch.setenv("PRIMARY_BASE_URL", "")
        llm = self._reload(monkeypatch, tmp_path)
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert "api_base" not in called

    def test_无_fallbacks_参数(self, monkeypatch, tmp_path):
        """主备已砍 (litellm 1.89.3 fallbacks + 流式必崩)。单模型挂就直接报错
        暴露, 不静默退化 —— 中转抽风时备用摆烂寒暄更难查。"""
        llm = self._reload(monkeypatch, tmp_path)
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert "fallbacks" not in called

    def test_model_json_覆盖_env(self, monkeypatch, tmp_path):
        """config/model.json 存在时, 它是真相源, .env 只当种子。"""
        import json
        monkeypatch.setenv("PRIMARY_MODEL", "anthropic/from-env")
        llm = self._reload(monkeypatch, tmp_path)
        import core.model_config as mc
        (mc._CONFIG_DIR / "model.json").write_text(json.dumps({
            "active": "ui",
            "models": [{"name": "ui", "model": "anthropic/from-json",
                        "api_key": "sk-json", "base_url": "https://json.example.com"}],
        }), encoding="utf-8")
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert called["model"] == "anthropic/from-json"
        assert called["api_key"] == "sk-json"
        assert called["api_base"] == "https://json.example.com"

    def test_切换active_下一次调用即生效(self, monkeypatch, tmp_path):
        """★ 热生效的核心判据: 不重启进程、不 reload 模块, 改完下一次
        complete() 就用新模型。settings 做不到这点 (启动期单例), 所以
        complete() 必须每次现读 model_config。"""
        llm = self._reload(monkeypatch, tmp_path)
        import core.model_config as mc
        mc.add({"name": "a", "model": "anthropic/model-a",
                "api_key": "sk-a", "base_url": ""})
        mc.add({"name": "b", "model": "anthropic/model-b",
                "api_key": "sk-b", "base_url": ""})
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")

        mc.set_active("a")
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert called["model"] == "anthropic/model-a"
        assert called["api_key"] == "sk-a"

        mc.set_active("b")      # 中途换模型, 不重启任何东西
        llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert called["model"] == "anthropic/model-b"
        assert called["api_key"] == "sk-b"

    def test_max_tokens_timeout_透传(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAX_TOKENS", "1234")
        monkeypatch.setenv("REQUEST_TIMEOUT", "33")
        llm = self._reload(monkeypatch, tmp_path)
        called = {}
        monkeypatch.setattr("core.llm.litellm.completion",
                            lambda **kw: called.update(kw) or "mock")
        llm.complete(messages=[{"role": "user", "content": "x"}])
        assert called["max_tokens"] == 1234
        assert called["timeout"] == 33

    def test_不传_tools_时_kwargs_无_tools(self, monkeypatch):
        called = {}
        def fake_completion(**kwargs):
            called.update(kwargs)
            return None
        import core.llm
        monkeypatch.setattr("core.llm.litellm.completion", fake_completion)
        core.llm.complete(messages=[{"role": "user", "content": "x"}])
        assert "tools" not in called

    def test_传_tools_时_kwargs_有_tools(self, monkeypatch):
        called = {}
        def fake_completion(**kwargs):
            called.update(kwargs)
            return None
        import core.llm
        monkeypatch.setattr("core.llm.litellm.completion", fake_completion)
        core.llm.complete(
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "t"}}],
        )
        assert "tools" in called
        assert called["tools"][0]["function"]["name"] == "t"
