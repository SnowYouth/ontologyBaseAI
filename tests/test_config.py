"""
测 core/config.py 的 pydantic-settings 校验 + prompts 文件加载。

策略: 每个测试用 monkeypatch 临时改 env + tmp_path 临时改 prompts 路径,
保证测试隔离, 不污染真实 .env / prompts/。
"""

import pytest


# ============ 测试隔离: 每个测试跑完把 core.config 还原 ============


@pytest.fixture(autouse=True)
def _restore_config_after_test():
    """每个测试跑完, 把 core.config 还原成真实项目根的状态。

    本文件大量 importlib.reload(core.config) 会改变模块级单例 settings;
    跑完必须还原, 否则后续 tests/test_*.py 拿到的可能是 tmp_path 残留版本。
    """
    yield
    import importlib
    import core.config
    importlib.reload(core.config)


# ============ 工具: 给一个能跑通的最小 env ============


def _set_valid_env(monkeypatch, **overrides):
    """设置一组刚好能通过 pydantic 校验的 env, 然后调用方可以 override 单项触发坏值。"""
    defaults = {
        "PRIMARY_API_KEY": "sk-test-key-1234567890",
        "PRIMARY_BASE_URL": "https://example.com",
        "MAX_TOKENS": "2048",
        "RAG_SCORE_THRESHOLD": "0.4",
        "RAG_TOP_K": "3",
        "CHUNK_SIZE": "300",
        "CHUNK_OVERLAP": "50",
        "CHROMA_PERSIST_DIR": "chroma_db",
        "RAG_USE_RERANKER": "false",
        "RAG_RERANK_CANDIDATES": "30",
        "RAG_RERANK_THRESHOLD": "0.50",
        # Round 2 三模式
        "RAG_REORDER_ENABLED": "true",
        "RAG_REORDER_MIN_CHUNKS": "5",
        "HISTORY_TRUNCATE_ENABLED": "true",
        "HISTORY_MAX_ROUNDS": "10",
        "HISTORY_MAX_CHARS": "20000",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


def _make_prompts_dir(
    tmp_path,
    system_content="你是助手",
    rag_content="资料: {context}\n问: {query}",
    agent_system_content="你是 agent, 调工具查事实, 不编造。",
    ontology_usage_content="OBDA 规则: 先查目录点名, 断腿就回问。",
):
    """在 tmp_path 下建一个 prompts/ 目录, 写入四个 md 文件。返回 prompts 目录路径。

    阶段 2 Task 4 加 agent_system.md 加载, fixture 同步建;
    阶段 6 Task 8 加 ontology_usage.md 加载, fixture 同步建。
    """
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "system.md").write_text(system_content, encoding="utf-8")
    (prompts_dir / "rag_user.md").write_text(rag_content, encoding="utf-8")
    (prompts_dir / "agent_system.md").write_text(agent_system_content, encoding="utf-8")
    (prompts_dir / "ontology_usage.md").write_text(ontology_usage_content, encoding="utf-8")
    return prompts_dir


# ============ 测试 ============


def test_config_loads_with_valid_env(monkeypatch, tmp_path):
    """正常 env + 正常 prompts 目录 -> Settings() 不抛, 字段值正确。"""
    _set_valid_env(monkeypatch)
    _make_prompts_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    # 重新 import config 触发新的实例化 (Settings 是模块级单例)
    import importlib
    import core.config
    importlib.reload(core.config)

    from core.config import settings
    assert settings.primary_api_key == "sk-test-key-1234567890"
    assert settings.rag_top_k == 3
    assert settings.rag_score_threshold == 0.4


def test_config_negative_top_k_raises(monkeypatch, tmp_path):
    """RAG_TOP_K=-1 -> ValidationError (gt=0 约束)。"""
    _set_valid_env(monkeypatch, RAG_TOP_K="-1")
    _make_prompts_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:
        import importlib
        import core.config
        importlib.reload(core.config)
    assert "rag_top_k" in str(exc_info.value).lower() or "RAG_TOP_K" in str(exc_info.value)


def test_config_threshold_out_of_range_raises(monkeypatch, tmp_path):
    """RAG_SCORE_THRESHOLD=2.0 -> ValidationError (le=1 约束)。"""
    _set_valid_env(monkeypatch, RAG_SCORE_THRESHOLD="2.0")
    _make_prompts_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:
        import importlib
        import core.config
        importlib.reload(core.config)
    assert "rag_score_threshold" in str(exc_info.value).lower() or "RAG_SCORE_THRESHOLD" in str(exc_info.value)


def test_config_missing_prompt_file_raises(monkeypatch, tmp_path):
    """prompts/system.md 不存在 -> 启动 FileNotFoundError 或 ValidationError。"""
    _set_valid_env(monkeypatch)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    # 故意只建 rag_user.md, 不建 system.md
    (prompts_dir / "rag_user.md").write_text("dummy {context} {query}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:
        import importlib
        import core.config
        importlib.reload(core.config)
    assert "system.md" in str(exc_info.value) or "prompt" in str(exc_info.value).lower()


def test_config_empty_prompt_file_raises(monkeypatch, tmp_path):
    """prompts/agent_system.md 存在但为空 -> 启动错误。"""
    _set_valid_env(monkeypatch)
    _make_prompts_dir(tmp_path, agent_system_content="")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:
        import importlib
        import core.config
        importlib.reload(core.config)
    assert "agent_system.md" in str(exc_info.value) or "empty" in str(exc_info.value).lower() or "为空" in str(exc_info.value)


# ============ Round 2 (Task 5) 新增 3 个 ============


def test_config_round2_新字段默认值(monkeypatch, tmp_path):
    """Round 2 加的 10 个字段, 缺省时按 spec 默认值。"""
    _set_valid_env(monkeypatch)
    _make_prompts_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    import importlib
    import core.config
    importlib.reload(core.config)

    from core.config import settings
    assert settings.rag_reorder_enabled is True
    assert settings.rag_reorder_min_chunks == 5
    assert settings.history_truncate_enabled is True
    assert settings.history_max_rounds == 10
    assert settings.history_max_chars == 20000


def test_config_round2_边界越界_max_rounds_过大报错(monkeypatch, tmp_path):
    """HISTORY_MAX_ROUNDS=999 -> 超过 le=200 上限, ValidationError。"""
    _set_valid_env(monkeypatch, HISTORY_MAX_ROUNDS="999")
    _make_prompts_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:
        import importlib
        import core.config
        importlib.reload(core.config)
    assert "history_max_rounds" in str(exc_info.value).lower() or "HISTORY_MAX_ROUNDS" in str(exc_info.value)


# ============ LLM 行为参数 (去 agent_ 前缀) ============


class TestAgentSettings:
    """LLM 行为参数字段 (max_iters/max_tokens/request_timeout, 已去 agent_ 前缀)."""

    def test_默认值(self, monkeypatch):
        """无相关环境变量时, 默认值应符合 spec."""
        # 注: import litellm 会自动 load_dotenv() 把 .env 灌进 os.environ,
        # _env_file=None 只挡文件不挡 os.environ, 故相关 env 也须显式删.
        for var in ["PRIMARY_MODEL", "MAX_ITERS", "MAX_TOKENS",
                    "REQUEST_TIMEOUT", "PRIMARY_BASE_URL"]:
            monkeypatch.delenv(var, raising=False)
        # _env_file=None: 测"代码里写死的默认值", 不能被本地 .env 污染.
        from core.config import Settings
        s = Settings(_env_file=None)
        assert s.primary_model == "anthropic/claude-sonnet-5-5"
        assert s.max_iters == 15
        assert s.max_tokens == 4096
        assert s.request_timeout == 45

    def test_env_覆盖(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_MODEL", "deepseek/deepseek-v4-pro")
        monkeypatch.setenv("MAX_ITERS", "5")
        from core.config import Settings
        s = Settings()
        assert s.primary_model == "deepseek/deepseek-v4-pro"
        assert s.max_iters == 5

    def test_max_iters_范围_必须正整数(self, monkeypatch):
        import pydantic
        monkeypatch.setenv("MAX_ITERS", "0")
        from core.config import Settings
        with pytest.raises(pydantic.ValidationError):
            Settings()

    def test_max_tokens_范围_必须正整数(self, monkeypatch):
        import pydantic
        monkeypatch.setenv("MAX_TOKENS", "-1")
        from core.config import Settings
        with pytest.raises(pydantic.ValidationError):
            Settings()

    # ============ system_prompt (原 agent_system_prompt) 加载 ============

    def test_system_prompt_启动期加载(self):
        """system_prompt 由 model_validator 从 prompts/agent_system.md 读."""
        from core.config import Settings
        s = Settings()
        assert s.system_prompt
        assert len(s.system_prompt) > 100  # 不为空, 至少几行
        assert "工具" in s.system_prompt or "tool" in s.system_prompt.lower()

    def test_agent_system_md_文件不存在_fail_fast(self, tmp_path, monkeypatch):
        """如果 prompts/agent_system.md 不存在, Settings 实例化崩."""
        # 把 cwd 切到一个只有 ontology_usage.md 但无 agent_system.md 的目录
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "ontology_usage.md").write_text("x", encoding="utf-8")
        # agent_system.md 故意缺
        # 显式 setenv: chdir 到 tmp_path 后 .env (相对 cwd) 读不到, 不补 env 会卡在 OBDA 校验,
        # 走不到 prompt 加载阶段, 测不到 agent_system.md 的 fail-fast
        _set_valid_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        # _read_prompt 先看 cwd/prompts/ 存在则用 cwd, 这里 cwd/prompts/ 在, 但缺 agent_system.md → 应抛
        from core.config import Settings
        with pytest.raises(FileNotFoundError, match="agent_system.md"):
            Settings()


# ============ 阶段 6 (Task 8) 新增 OBDA 字段 ============


def test_config_obda_默认值(monkeypatch, tmp_path):
    """OBDA 相关字段缺省时按 spec 默认值 (ontology_path 有默认)。"""
    _set_valid_env(monkeypatch)
    # OBDA_ONTOLOGY_PATH 没进 _set_valid_env defaults, 但 litellm 的 load_dotenv 会把
    # .env 里的值灌进 os.environ (见 memory litellm-dotenv-provider-magic), 污染"取代码
    # 默认值"的断言。显式清掉才是真测"环境无此变量 → 取代码默认值"。
    monkeypatch.delenv("OBDA_ONTOLOGY_PATH", raising=False)
    _make_prompts_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    import importlib
    import core.config
    importlib.reload(core.config)

    from core.config import settings
    assert settings.obda_ontology_path == "capabilities/ontology/ontology.db"


def test_config_ontology_usage_prompt_启动期加载(monkeypatch, tmp_path):
    """ontology_usage_prompt 由 model_validator 从 prompts/ontology_usage.md 读, 跟另外 4 份 prompt 同一套机制。"""
    _set_valid_env(monkeypatch)
    _make_prompts_dir(tmp_path, ontology_usage_content="测试 OBDA 规则内容")
    monkeypatch.chdir(tmp_path)

    import importlib
    import core.config
    importlib.reload(core.config)

    from core.config import settings
    assert settings.ontology_usage_prompt == "测试 OBDA 规则内容"


def test_config_missing_ontology_usage_prompt_fail_fast(monkeypatch, tmp_path):
    """prompts/ontology_usage.md 不存在 -> 启动 FileNotFoundError (跟其它 4 份 prompt 一致的 fail-fast)。"""
    _set_valid_env(monkeypatch)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "system.md").write_text("x", encoding="utf-8")
    (prompts_dir / "rag_user.md").write_text("{context}{query}", encoding="utf-8")
    (prompts_dir / "agent_system.md").write_text("x", encoding="utf-8")
    # 故意不建 ontology_usage.md
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Exception) as exc_info:
        import importlib
        import core.config
        importlib.reload(core.config)
    assert "ontology_usage.md" in str(exc_info.value) or "prompt" in str(exc_info.value).lower()


# ============ 工具信任边界: deploy_profile ============


class TestDeployProfile:
    """DEPLOY_PROFILE 缺省 serving; 非法值归一 serving (fail-safe)。"""

    def test_缺省_serving(self, monkeypatch):
        monkeypatch.delenv("DEPLOY_PROFILE", raising=False)
        from core.config import Settings
        s = Settings(_env_file=None)
        assert s.deploy_profile == "serving"

    def test_env_设为_local(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_PROFILE", "local")
        from core.config import Settings
        s = Settings(_env_file=None)
        assert s.deploy_profile == "local"

    def test_非法值归一为_serving(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_PROFILE", "locl")   # 拼错
        from core.config import Settings
        s = Settings(_env_file=None)
        assert s.deploy_profile == "serving"           # fail-safe


# ============ LLM 配置统一: 主/备两套 (统一自四套老前缀) ============


class TestPrimaryBackupConfig:
    """LLM 配置字段 (单模型; 主备已于 2026-10-15 砍掉)."""

    def test_模型字段默认值(self, monkeypatch, tmp_path):
        # 清掉可能污染的 env, 用默认值
        # (import litellm 自动 load_dotenv 会把 .env 灌进 os.environ, 故须逐个删)
        for k in ["PRIMARY_MODEL", "PRIMARY_API_KEY", "PRIMARY_BASE_URL"]:
            monkeypatch.delenv(k, raising=False)
        # _env_file=None: 测"代码写死的默认值", 不被本地 .env 污染
        from core.config import Settings
        s = Settings(_env_file=None)
        # 默认值必须是官方可用的真模型名: 带中转档位前缀 (g-/s-/b-) 的名字
        # 外部使用者拿官方 key 照抄会 404
        assert s.primary_model == "anthropic/claude-sonnet-5-5"
        assert s.primary_base_url == ""   # 默认空 = 官方端点

    def test_backup_四字段已移除(self, monkeypatch):
        """主备砍掉后这四个字段不该再存在 —— 留着会让人以为还能配备用。"""
        monkeypatch.setenv("BACKUP_MODEL", "deepseek/deepseek-v4-pro")
        monkeypatch.setenv("BACKUP_ENABLED", "true")
        import importlib, core.config
        importlib.reload(core.config)
        s = core.config.settings
        for f in ("backup_model", "backup_api_key", "backup_base_url", "backup_enabled"):
            assert not hasattr(s, f), f"backup 字段残留: {f}"

    def test_模型字段读env(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_MODEL", "anthropic/some-model")
        monkeypatch.setenv("PRIMARY_API_KEY", "pk-123")
        monkeypatch.setenv("PRIMARY_BASE_URL", "https://mid.example.com")
        import importlib, core.config
        importlib.reload(core.config)
        s = core.config.settings
        assert s.primary_model == "anthropic/some-model"
        assert s.primary_api_key == "pk-123"
        assert s.primary_base_url == "https://mid.example.com"

    def test_行为字段去agent前缀(self, monkeypatch):
        monkeypatch.setenv("MAX_ITERS", "20")
        monkeypatch.setenv("MAX_TOKENS", "8192")
        monkeypatch.setenv("REQUEST_TIMEOUT", "60")
        import importlib, core.config
        importlib.reload(core.config)
        s = core.config.settings
        assert s.max_iters == 20
        assert s.max_tokens == 8192
        assert s.request_timeout == 60

    def test_死prompt字段已移除(self):
        import importlib, core.config
        importlib.reload(core.config)
        s = core.config.settings
        assert not hasattr(s, "default_system_prompt")
        assert not hasattr(s, "rag_user_template")

    def test_system_prompt_仍加载(self):
        """system_prompt (原 agent_system_prompt) 从 prompts/agent_system.md 加载."""
        import importlib, core.config
        importlib.reload(core.config)
        s = core.config.settings
        assert s.system_prompt
        assert len(s.system_prompt) > 100


# ============ 多数据源配置 (DB_SOURCES, 2026-09-24 spec) ============


def test_parse_多源混方言():
    """DB_SOURCES=lib,crm + 每源 DB_<NAME>_*: mysql 默认 3306, oracle 默认 1521。"""
    from core.config import _parse_db_sources
    env = {
        "DB_LIB_HOST": "127.0.0.1", "DB_LIB_NAME": "library",
        "DB_LIB_USER": "ro", "DB_LIB_PASSWORD": "pw",
        "DB_CRM_HOST": "203.0.113.10", "DB_CRM_TYPE": "oracle",
        "DB_CRM_NAME": "ORCLPDB1", "DB_CRM_USER": "crm", "DB_CRM_PASSWORD": "pw2",
    }
    srcs = _parse_db_sources("lib,crm", env)
    assert set(srcs) == {"lib", "crm"}
    assert srcs["lib"].dialect == "mysql" and srcs["lib"].port == 3306
    assert srcs["crm"].dialect == "oracle" and srcs["crm"].port == 1521
    assert srcs["crm"].dbname == "ORCLPDB1"


def test_parse_源名大小写归一():
    """DB_SOURCES 写 Lib, 键归一小写 (对上本体 entity.data_source 的惯例小写)。"""
    from core.config import _parse_db_sources
    assert "lib" in _parse_db_sources("Lib", {"DB_LIB_HOST": "h"})


def test_parse_空清单_零源():
    from core.config import _parse_db_sources
    assert _parse_db_sources("", {}) == {}
    assert _parse_db_sources("  ", {}) == {}


def test_parse_非法源名炸():
    import pytest
    from core.config import _parse_db_sources
    with pytest.raises(ValueError, match="源名"):
        _parse_db_sources("lib-1", {})


def test_parse_重复源名炸_不区分大小写():
    import pytest
    from core.config import _parse_db_sources
    with pytest.raises(ValueError, match="重复"):
        _parse_db_sources("lib,LIB", {})


def test_parse_非法TYPE炸():
    import pytest
    from core.config import _parse_db_sources
    with pytest.raises(ValueError, match="TYPE"):
        _parse_db_sources("lib", {"DB_LIB_TYPE": "postgres"})


def test_parse_端口非整数炸():
    import pytest
    from core.config import _parse_db_sources
    with pytest.raises(ValueError, match="PORT"):
        _parse_db_sources("lib", {"DB_LIB_PORT": "abc"})


def test_parse_端口越界炸():
    import pytest
    from core.config import _parse_db_sources
    with pytest.raises(ValueError, match="PORT"):
        _parse_db_sources("lib", {"DB_LIB_PORT": "70000"})


def test_parse_全字段缺省不炸():
    """host 留空必须能构造 (降级交给 bootstrap, 同 2026-09-24 冷启动教训)。"""
    from core.config import _parse_db_sources
    s = _parse_db_sources("lib", {})["lib"]
    assert (s.host, s.user, s.dbname) == ("", "", "")


def test_settings_db_sources_接线(monkeypatch, tmp_path):
    """Settings 集成: DB_SOURCES + DB_<NAME>_* 真进 settings.db_sources。

    两层隔离都必须:
    1. chdir(tmp_path) 切走真 .env (dotenv_values(".env") 是 cwd 相对);
    2. 逐个 delenv 本测试涉及的 DB_* —— 别处 `import litellm` 会自动
       load_dotenv() 把真 .env 灌进 os.environ, 而解析层的 env.update(os.environ)
       让进程环境变量优先于 .env 文件。只做第 1 层时, 机主 .env 里同名的
       DB_LIB2_PORT=8901 会盖掉本测试想验的 oracle 默认端口 1521
       (2026-10-13 .env 迁移实测: 迁移前键名叫 LIB2_DB_PORT 撞不上所以绿,
       迁移后一撞就红 —— 不是生产代码的问题, 是这条测试没隔离干净)。
    """
    monkeypatch.chdir(tmp_path)
    for k in ("DB_SOURCES", "DB_LIB_HOST", "DB_LIB_PORT", "DB_LIB_USER",
              "DB_LIB_PASSWORD", "DB_LIB_NAME", "DB_LIB_TYPE",
              "DB_LIB2_HOST", "DB_LIB2_PORT", "DB_LIB2_USER",
              "DB_LIB2_PASSWORD", "DB_LIB2_NAME", "DB_LIB2_TYPE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_SOURCES", "lib,lib2")
    monkeypatch.setenv("DB_LIB_HOST", "h1")
    monkeypatch.setenv("DB_LIB2_HOST", "h2")
    monkeypatch.setenv("DB_LIB2_TYPE", "oracle")
    from core.config import Settings
    s = Settings(_env_file=None)
    assert set(s.db_sources) == {"lib", "lib2"}
    assert s.db_sources["lib"].host == "h1"
    assert s.db_sources["lib2"].dialect == "oracle"
    assert s.db_sources["lib2"].port == 1521


def test_settings_无DB_SOURCES_零源(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_SOURCES", raising=False)
    from core.config import Settings
    s = Settings(_env_file=None)
    assert s.db_sources == {}


def test_settings_非法TYPE_启动即炸(monkeypatch, tmp_path):
    import pytest
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_SOURCES", raising=False)
    monkeypatch.setenv("DB_SOURCES", "lib")
    monkeypatch.setenv("DB_LIB_TYPE", "sqlite")
    from pydantic import ValidationError
    from core.config import Settings
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_default_source_取首源(monkeypatch, tmp_path):
    """default_source() = DB_SOURCES 首源 (dict 插入顺序即 DB_SOURCES 书写顺序)。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_SOURCES", raising=False)
    monkeypatch.setenv("DB_SOURCES", "lib,lib2")
    monkeypatch.setenv("DB_LIB_HOST", "h1")
    monkeypatch.setenv("DB_LIB2_HOST", "h2")
    import core.config
    s = core.config.Settings(_env_file=None)
    monkeypatch.setattr(core.config, "settings", s)
    assert core.config.default_source() == "lib"


def test_default_source_无配置返回空串(monkeypatch, tmp_path):
    """未配置 DB_SOURCES 时 default_source() 返回空串, 不编造源名。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_SOURCES", raising=False)
    import core.config
    s = core.config.Settings(_env_file=None)
    monkeypatch.setattr(core.config, "settings", s)
    assert core.config.default_source() == ""


def test_settings_环境有DB_SOURCES_不撞派生字段名(monkeypatch, tmp_path):
    """回归 (计划评审发现 1, 本机复现过): pydantic-settings 大小写不敏感按
    **字段名**匹配 env — 派生字段裸叫 db_sources 会被 DB_SOURCES 命中并
    json.loads("lib,lib2") → SettingsError 启动即崩。派生字段必须挂一个
    永不存在的 alias, 这条测试锁住该形态。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_SOURCES", "lib,lib2")
    monkeypatch.setenv("DB_LIB_HOST", "h1")
    from core.config import Settings
    s = Settings(_env_file=None)  # 不炸即过
    assert s.db_sources["lib"].host == "h1"


# ============ api_key 占位符检测 (与 .env.example 的占位值对齐) ============


class Test占位符检测:
    """`.env.example` 发的是 PRIMARY_API_KEY=sk-REPLACE-ME, 但旧判据只认
    startswith("填") —— 照抄 .env.example 没改 key 时检测不出来, 要等真正
    发请求才炸在 getaddrinfo。判据收口到 is_placeholder_api_key(), config
    的 api_key_set 与 main.py 的启动前置检查共用, 两边不会再漂。
    """

    def test_识别env_example的占位值(self):
        from core.config import is_placeholder_api_key
        assert is_placeholder_api_key("sk-REPLACE-ME") is True

    def test_识别中文占位值_兼容老形态(self):
        from core.config import is_placeholder_api_key
        assert is_placeholder_api_key("填你的key") is True

    def test_空值算占位(self):
        from core.config import is_placeholder_api_key
        assert is_placeholder_api_key("") is True
        assert is_placeholder_api_key("   ") is True

    def test_真key不算占位(self):
        from core.config import is_placeholder_api_key
        assert is_placeholder_api_key("sk-test-key-1234567890") is False

    def test_大小写与空白不影响判断(self):
        from core.config import is_placeholder_api_key
        assert is_placeholder_api_key("  sk-replace-me  ") is True

    def test_api_key_set_对占位值为False(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_API_KEY", "sk-REPLACE-ME")
        import importlib, core.config
        importlib.reload(core.config)
        assert core.config.is_placeholder_api_key(core.config.settings.primary_api_key) is True
