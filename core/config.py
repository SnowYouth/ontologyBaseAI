"""
config: 项目所有配置的单一入口。

阶段 1 债务清算第 1 轮改造:
- 老的 `class Settings:` (无校验) 换成 `Settings(BaseSettings)` (pydantic-settings)
- 启动时强校验: 必填非空 / 数值范围 / prompt 文件存在非空
- 任何配置不合规 -> 启动 ValidationError 崩 (fail-fast)
- 系统 prompt 和 RAG user 模板从 prompts/*.md 加载, 不再从 .env 读
- 调用方接口不变: `from core.config import settings`, 字段名全保留
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.observability import log_event

# 项目根目录 (core/config.py 的父目录的父目录)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------- 多数据源配置 (2026-09-24 spec): DB_SOURCES + DB_<NAME>_* ----------

_DIALECT_DEFAULT_PORT = {"mysql": 3306, "oracle": 1521}
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class DbSourceConfig:
    """单个数据源的连接配置。源名本身是 settings.db_sources 的 dict 键。

    dbname: mysql → database, oracle → service_name (.env 统一叫 DB_<NAME>_NAME,
    少一个方言专属概念)。
    """
    host: str = ""
    port: int = 0
    user: str = ""
    password: str = ""
    dbname: str = ""
    dialect: str = "mysql"


def _parse_db_sources(
    raw: str, env: dict[str, str] | None = None
) -> dict[str, DbSourceConfig]:
    """DB_SOURCES="lib,crm" + 每源 DB_<NAME>_{HOST,PORT,USER,PASSWORD,NAME,TYPE}
    → {源名小写: DbSourceConfig}。

    env 缺省 = 真实 os.environ 覆盖 .env 文件值 (与 pydantic-settings 同优先级;
    pydantic 的 extra="ignore" 读不到任意名变量, per-source 变量只能自己解析)。
    非法值 raise ValueError —— 本函数在 model_validator 里跑, pydantic 会包成
    ValidationError 启动即炸 (fail-fast)。
    host 留空**不炸**: 降级语义归 bootstrap (2026-09-24 冷启动教训)。
    """
    if env is None:
        env = {k: v for k, v in dotenv_values(".env").items() if v is not None}
        env.update(os.environ)
    names = [n.strip() for n in (raw or "").split(",") if n.strip()]
    sources: dict[str, DbSourceConfig] = {}
    for name in names:
        if not _SOURCE_NAME_RE.match(name):
            raise ValueError(
                f"DB_SOURCES 源名 '{name}' 非法: 只允许字母/数字/下划线")
        key = name.lower()
        if key in sources:
            raise ValueError(f"DB_SOURCES 源名重复 (不区分大小写): {name}")
        prefix = f"DB_{key.upper()}_"
        dialect = env.get(prefix + "TYPE", "mysql").strip().lower()
        if dialect not in _DIALECT_DEFAULT_PORT:
            raise ValueError(
                f"数据源 {name}: {prefix}TYPE 非法 '{dialect}', 只支持 mysql/oracle")
        port_raw = env.get(prefix + "PORT", "").strip()
        try:
            port = int(port_raw) if port_raw else _DIALECT_DEFAULT_PORT[dialect]
        except ValueError:
            raise ValueError(f"数据源 {name}: {prefix}PORT 不是整数 '{port_raw}'")
        if not (0 < port < 65536):
            raise ValueError(f"数据源 {name}: {prefix}PORT 越界 ({port})")
        sources[key] = DbSourceConfig(
            host=env.get(prefix + "HOST", "").strip(),
            port=port,
            user=env.get(prefix + "USER", "").strip(),
            password=env.get(prefix + "PASSWORD", ""),
            dbname=env.get(prefix + "NAME", "").strip(),
            dialect=dialect,
        )
    return sources


def _read_prompt(filename: str) -> str:
    """读 prompts/<filename> 并返回字符串。

    路径选择: cwd/prompts/ 目录存在则用 cwd (测试场景 monkeypatch.chdir 用这条),
    否则用 PROJECT_ROOT/agent/prompts/ (生产场景; prompts/ 已随装配层搬进 agent/)。
    一旦选定目录, 在该目录内找不到文件 / 文件为空一律 fail-fast — 不再回退到另一个目录,
    避免"部分阴影"的语义陷阱 (用户给了 cwd/prompts/ 就该完整覆盖, 缺哪个就报哪个)。

    Windows 编码: 显式 UTF-8, 防 GBK 默认导致中文乱码。
    """
    cwd_prompts = Path.cwd() / "prompts"
    base = cwd_prompts if cwd_prompts.is_dir() else PROJECT_ROOT / "agent" / "prompts"
    path = base / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Prompt 文件为空: {path}")
    return text


class Settings(BaseSettings):
    """所有配置项的容器, pydantic-settings 自动从 .env 加载 + 校验。

    校验规则:
    - api_key: 必填非空 (空字符串 -> ValidationError)
    - 数值字段: gt / ge / le 约束 (top_k > 0, threshold ∈ [0,1] 等)
    - prompts: 不从 env 读, 启动时从 prompts/*.md 加载 (model_validator)

    扩展时:
    - 加新字段: 直接加, 写 Field(...) 约束
    - 加新约束: 改字段上的 Field, 老调用方不感知
    - 不要在这里加运行时逻辑 (那是调用方的事)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env 里 Settings 没声明的字段静默丢弃 (默认 forbid 会崩);
                         # 注意: DEFAULT_SYSTEM_PROMPT 这种"被 validator 覆盖"的情况
                         # 走的是字段填充路径, 不是 extra 拦的, 别混淆
    )

    # ---------- LLM: 单模型 (主备已于 2026-10-15 砍掉) ----------
    # PRIMARY_MODEL 用 litellm 格式 "provider/model" (如 anthropic/claude-sonnet-5-5).
    # base_url 留空 = 走 provider 官方端点; 填 = 走自定义中转 (complete() 传 api_base).
    #
    # ★ 这三个字段是**冷启动种子**: core/llm.py 的取值路径走 core/model_config.py
    #   (config/model.json), 那个文件缺失时才拿这里的值合成一条候选。前端设置面板
    #   改模型写的是 model.json, 不回写 .env —— 改完 .env 要重启, 而面板要热生效。
    primary_model: str = Field(
        default="anthropic/claude-sonnet-5-5", alias="PRIMARY_MODEL", min_length=1)
    primary_api_key: str = Field(default="", alias="PRIMARY_API_KEY")
    primary_base_url: str = Field(default="", alias="PRIMARY_BASE_URL")

    # ---------- RAG ----------
    rag_score_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    rag_top_k: int = Field(default=3, gt=0)
    chunk_size: int = Field(default=300, gt=0)
    chunk_overlap: int = Field(default=50, ge=0)
    chroma_persist_dir: str = Field(default="chroma_db", min_length=1)

    # ---------- RAG: Rerank (1.5) ----------
    rag_use_reranker: bool = False
    rag_rerank_candidates: int = Field(default=30, gt=0)
    # rerank 阈值不加范围约束: -1.0 是"未校准"哨兵 (main.py 拦), 真值在 [0,1] 区间
    # 但允许其他值用于实验, pydantic 不替 main.py 拦
    rag_rerank_threshold: float = -1.0

    # ---------- RAG: Lost-in-middle 重排 (Round 2 ②) ----------
    rag_reorder_enabled: bool = True
    rag_reorder_min_chunks: int = Field(default=5, ge=2)

    # ---------- History 截断 (Round 2 ④) ----------
    history_truncate_enabled: bool = True
    history_max_rounds: int = Field(default=10, ge=2, le=200)
    history_max_chars: int = Field(default=20000, ge=1000)

    # ---------- LLM 行为参数 (去 agent_ 前缀: 三模式合一后无 agent 专属概念) ----------
    max_iters: int = Field(default=15, alias="MAX_ITERS", gt=0, le=50)
    max_tokens: int = Field(default=4096, alias="MAX_TOKENS", gt=0)
    request_timeout: int = Field(default=45, alias="REQUEST_TIMEOUT", gt=0, le=600)
    # system_prompt 由 model_validator 从 prompts/agent_system.md 加载, 不从 env 读
    system_prompt: str = ""

    # ---------- 阶段 6 OBDA + 多数据源 (2026-09-24): 源清单/数量/方言全可配 ----------
    db_sources_raw: str = Field(default="", alias="DB_SOURCES")
    # 派生字段的 alias 挂一个永不存在的 env 名: pydantic-settings 大小写不敏感
    # 按**字段名**匹配 env, 裸名 db_sources 会被 DB_SOURCES 命中并 json.loads
    # → SettingsError 启动即崩 (计划评审发现 1, 已实证复现)。
    db_sources: dict[str, DbSourceConfig] = Field(
        default_factory=dict, alias="DB_SOURCES_PARSED")

    obda_ontology_path: str = Field(default="capabilities/ontology/ontology.db", min_length=1)
    # ontology_usage_prompt 由 model_validator 加载, 不从 env 读 (跟其它 4 份 prompt 同机制)
    ontology_usage_prompt: str = ""

    # ---------- 阶段 3: Web (FastAPI 前台) ----------
    # sessions_dir: 会话 JSONL 落盘目录 (相对项目根), 不进 git. web/app.py 仍用。
    sessions_dir: str = Field(default="sessions", min_length=1)

    # ---------- 工具信任边界: 部署 profile ----------
    # local   — 开发机: 加载全部工具 (含 read_file/list_dir 等内省工具)
    # serving — 上线:   只加载 category=serving 的业务工具 (dev 工具根本不进 agent)
    # 缺省 serving = 最小暴露: 不配 = 当上线看, fail-safe。开发要读代码须显式写 local。
    deploy_profile: str = Field(default="serving")

    @model_validator(mode="after")
    def _load_prompts_from_files(self) -> "Settings":
        """启动时把仍在用的 prompts/*.md 加载到字段。

        只剩两份: agent_system.md (agent 系统提示) + ontology_usage.md (OBDA 用法),
        由 model_validator 加载。system.md/rag_user.md 已随三模式合一废弃删除。
        """
        self.system_prompt = _read_prompt("agent_system.md")
        self.ontology_usage_prompt = _read_prompt("ontology_usage.md")
        return self

    @model_validator(mode="after")
    def _normalize_deploy_profile(self) -> "Settings":
        """非法 DEPLOY_PROFILE 归一到 serving (fail-safe: 拼错不该意外全开)。"""
        if self.deploy_profile not in ("local", "serving"):
            log_event("config.deploy_profile_invalid",
                      {"got": self.deploy_profile[:50], "fallback": "serving"})
            self.deploy_profile = "serving"
        return self

    @model_validator(mode="after")
    def _parse_db_sources_validator(self) -> "Settings":
        """DB_SOURCES=lib,crm + DB_<NAME>_* → settings.db_sources。"""
        self.db_sources = _parse_db_sources(self.db_sources_raw)
        return self


# .env.example 里发的占位值 (照抄没改 = 还没配 key)。小写比较。
_PLACEHOLDER_API_KEYS = ("sk-replace-me",)


def is_placeholder_api_key(key: str) -> bool:
    """key 是空/占位值(没真配)吗? 判据收口在这, 别在调用点各写一份。

    判据跟 `.env.example` 的占位值绑死 —— 改那边的占位值, 这里跟着加。
    `填` 前缀是老形态, 留着兼容手填过的 .env。
    """
    k = (key or "").strip()
    if not k:
        return True
    return k.lower() in _PLACEHOLDER_API_KEYS or k.startswith("填")


# 模块级单例, 调用方 `from core.config import settings` 用这个
settings = Settings()  # type: ignore[call-arg]  # pydantic 字段默认值已覆盖, IDE 误报

# Observability: 启动加载成功后落一条事件 (不记 api_key 实际值, 只记是否填了)
log_event(
    "config.loaded",
    {
        "api_key_set": not is_placeholder_api_key(settings.primary_api_key),
        "primary_model": settings.primary_model,
        "rag_top_k": settings.rag_top_k,
        "rag_score_threshold": settings.rag_score_threshold,
        "rag_use_reranker": settings.rag_use_reranker,
        "rag_rerank_threshold": settings.rag_rerank_threshold,
        "rag_reorder_enabled": settings.rag_reorder_enabled,
        "history_truncate_enabled": settings.history_truncate_enabled,
        "prompts_loaded": ["agent_system.md", "ontology_usage.md"],
    },
)


def default_source() -> str:
    """缺 data_source 时的兜底源 = DB_SOURCES 首源; 未配置则空串。

    ★ 2026-10-15: 原先 7 处散写写死的真库名(脱敏后是示例名 'lib')。改成取首源后
    本机行为不变(本机 DB_SOURCES 首源恰是原写死的那个), 但新使用者的库不会被
    悄悄塞进一个在它的 DB_SOURCES 里不存在的源名。
    """
    return next(iter(settings.db_sources), "")
