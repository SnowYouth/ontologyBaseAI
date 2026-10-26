"""web/app.py — 多会话对话后端 (前端 MVP).

- 一会话一 JSON 文件 (SessionStore), _cache 缓存 Conversation 实例。
- 全局串行锁 _agent_lock: 任意时刻只有一个 run_agent 跑 (绕开 MCP 并发/同会话撕裂)。
- steps 展示/LLM 分离: 磁盘存 steps, 喂 LLM (_get_conv) 剥离。
- 标题: 首轮后台线程轻量裸 LLM 调用 fire-and-forget, read-modify-write 落盘。
CORS 放行本地 dev 任意端口 (Vite 端口漂移, 见 add_middleware 注释)。
"""
import sys

# Web 是新进程入口 (uvicorn), 跟 main.py 一样切 UTF-8 (Windows GBK + emoji 会 500)
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime
import json
import queue
import re
import threading
import uuid
from contextlib import asynccontextmanager

from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.runtime.agent import run_agent
from core.runtime.bootstrap import init_agent_runtime
from core.config import settings, default_source
from core.conversation import Conversation
from core.runtime import cq_runner
from core import model_config
from core.llm import complete
from core.observability import log_event
from core.ontology_view import get_ontology, get_ontology_path, set_ontology, find_join_paths
from core.session_store import SessionStore
from agent.toolconfig import mcp_servers as mcp_config
from agent.toolconfig import tools as tool_config
from agent.toolconfig import skills as skill_config
from core.runtime.agent import _allowed_by_profile, _servers_allowed_by_profile
from agent.tools import get_all_tools
from agent.tools.sql_exec import execute_sql, list_table_columns

_store = SessionStore(settings.sessions_dir)
_cache: dict[str, Conversation] = {}     # {session_id: Conversation} (剥 steps 的纯对话)
_agent_lock = threading.Lock()            # 全局串行: 一次只跑一个 run_agent


def _strip_steps(messages: list[dict]) -> list[dict]:
    """喂 LLM 前剥掉 assistant 消息的 steps 字段 (防 context 膨胀)。"""
    out = []
    for m in messages:
        out.append({"role": m["role"], "content": m["content"]})
    return out


def _get_conv(sid: str) -> Conversation:
    """按 sid 拿 Conversation (缓存优先, 缺则读盘); 会话不存在抛 KeyError。"""
    if sid not in _cache:
        data = _store.load(sid)  # 不存在 → KeyError
        conv = Conversation()
        conv.load_from(_strip_steps(data["messages"]))
        _cache[sid] = conv
    return _cache[sid]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_agent_runtime()
    yield


app = FastAPI(lifespan=lifespan)

# 本地 dev: 放行 localhost/127.0.0.1 任意端口 (Vite 端口被占会漂到 5174/5175…,
# 写死单端口会被 CORS 拦; 正则一劳永逸。仅本地学习项目, 无外网暴露风险)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


class AgentIn(BaseModel):
    message: str
    session_id: str


class ToolToggle(BaseModel):
    enabled: bool


class McpServerIn(BaseModel):
    name: str
    command: str
    args: list[str]
    enabled: bool = True
    # 与本地工具同口径的可见性层级; 缺省 local = 最小暴露 (serving profile 下
    # 不加载)。显式写 "serving" 才在上线档位放行。
    category: str = "local"


class McpServerToggle(BaseModel):
    enabled: bool
    # None = 不动该字段 (PATCH 语义)。照 EntityEditIn 那次的教训来:
    # 用 `str = "local"` 会让前端每次点开关都把 category 悄悄洗回 local。
    category: str | None = None


class ModelIn(BaseModel):
    # model_config 是 pydantic BaseModel 的保留属性名, 字段叫 model 会撞
    # "protected namespaces" 警告 → 显式放开 model_ 前缀保护。
    model_config = ConfigDict(protected_namespaces=())

    name: str
    model: str
    api_key: str = ""
    base_url: str = ""


class ModelPatch(BaseModel):
    """PATCH 语义: None = 该字段不动 (api_key 的空串也当"不动", 见端点注释)。"""
    model_config = ConfigDict(protected_namespaces=())

    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class ModelActivate(BaseModel):
    name: str


class SkillCreate(BaseModel):
    name: str
    content: str


class SkillBody(BaseModel):
    content: str


class EntityIn(BaseModel):
    id: str
    displayName: str = ""
    domain: str = ""
    maps_to: str = ""
    tables: list[str] = []
    attributes: list[dict] = []
    summary: str = ""
    data_source: str = ""   # 新建没有"旧值"概念, 空串走 default_source() 兜底


class EntityExtraIn(BaseModel):
    """entity extra_json 白名单血肉键固定槽位; extra=forbid 锁死不许加 key。

    ⚠️ 封闭语义: UI 每次保存都按此重建血肉, 白名单外的键会被清除。
    将来新增血肉键须三处同步: 此模型字段 + 下方 _EXTRA_WHITELIST + ontology_store,
    漏一处会导致 UI 保存把新键抹掉。
    (query_hints 已于 2026-08-03 废除; identityKeywords/scenarioKeywords 已于
     2026-08-04 按语义方向重划为 recallKeywords/disambiguateFrom;
     cocVerified/inSpec 已于 2026-08-05 废除 (运行时零消费的旧一代治理位)。)
    """
    model_config = ConfigDict(extra="forbid")
    recallKeywords: list[str] = []       # 正向召回信号 (旧 identityKeywords ∪ includes)
    disambiguateFrom: list[str] = []     # 负向消歧边界 (旧 scenarioKeywords.excludes 升格)


class AttributeIn(BaseModel):
    """attributes_json 9 键白名单固定槽位; extra=forbid 锁死不许加键。

    ⚠️ 封闭语义: UI 保存按整列表替换重建, 白名单外的键会被清除。
    kind/type/desc_source 封闭可选值 (Literal, 与真库现状一致), 越界 422。

    ⚠️ Literal 隐含假设: 三个封闭集合只覆盖真库现有值 (2026-07 快照 kind=pk/enum/fk/
    attr/bizkey/time, type=bigint/varchar/datetime/int/char, desc_source 已归一无 inferred)。
    若未来 importer/铺量写入集合外的新值, 该实体经编辑器一保存必 422; 扩集合时
    须同步: 此处 Literal + 前端 api.ts 的 ATTR_KINDS/ATTR_TYPES/ATTR_DESC_SOURCES。
    (valueSet/inSpec/note/_valueName 已于 2026-08-05 废除: 运行时零消费, note 真陷阱
    已并入 desc, _valueName 是 desc 缩略冗余。)
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    kind: Literal["fk", "enum", "attr", "pk", "time", "bizkey"]
    type: Literal["bigint", "varchar", "datetime", "int", "char"]
    desc: str = ""
    desc_source: Literal["code", "infer", "convention"] = "code"
    disambiguation: str = ""
    hasEnum: bool | None = None
    sensitive: bool | None = None


class EntityEditIn(BaseModel):
    """entity PUT 只收可编标量 + 可选 extra (2 白名单血肉键) + 可选 attributes (整列表替换)。

    data_source: None=不动 (照 attributes 的先例), 带值=覆盖。不能用 str="" 默认值
    —— model_dump() 会恒含该键, 前端 (Task13 之前) 不传时就会把旧值静默清空
    (PUT 是前端唯一编辑路径, api.ts putOntoEntity)。
    """
    displayName: str = ""
    domain: str = ""
    summary: str = ""
    maps_to: str = ""
    tables: list[str] = []
    extra: EntityExtraIn | None = None
    attributes: list[AttributeIn] | None = None   # None=不动 (老语义), 带值=整列表替换
    data_source: str | None = None   # None=不动, 带值=覆盖


class DomainIn(BaseModel):
    code: str
    color: str = ""
    displayOrder: int = 0
    description: str = ""


class RelationIn(BaseModel):
    source: str
    target: str
    type: Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "UNKNOWN"] = "UNKNOWN"
    label: str = ""
    source_col: str = Field(min_length=1)
    target_col: str = Field(min_length=1)
    bridge_count: int | None = None
    origin: Literal["manual", "bridge"] = "manual"


class EnumCodeIn(BaseModel):
    code: str
    name: str = ""
    desc: str = ""


class EnumIn(BaseModel):
    entity: str
    field: str
    name: str = ""
    spec_ref: str = ""
    codes: list[EnumCodeIn] = []


class RelationFlipIn(BaseModel):
    source: str
    target: str
    source_col: str = Field(min_length=1)
    target_col: str = Field(min_length=1)


class VerdictIn(BaseModel):
    verdict: Literal["pass", "partial", "fail"]
    note: str = ""


class CqCriteriaIn(BaseModel):
    """四类判据, 与现有考题集结构逐字一致 (封闭语义, 不许加键)。"""
    model_config = ConfigDict(extra="forbid")
    点名: str = ""
    走边: str = ""
    值集: str = ""
    断腿: str = ""


class CqCaseIn(BaseModel):
    """一条考题; extra=forbid 封闭语义 (同 EntityExtraIn 约定), 防塞野键。

    ★ turns 的校验拦的是一个静默失败: cq_runner.run_one_cq 对 multi_turn 走
    `for turn in case.get("turns") or []`, 没给 turns 就循环零次 —— answer
    恒为空串、不抛异常、结果里也没有任何线索。前端建一条 multi_turn 忘填
    轮次, 跑出来就是"空答案"且无从排查。故在入口判死 (422)。
    """
    model_config = ConfigDict(extra="forbid")
    id: str
    question: str
    kind: Literal["single", "multi_turn"] = "single"
    turns: list[str] | None = None
    criteria: CqCriteriaIn

    @model_validator(mode="after")
    def _检查turns与kind匹配(self):
        if self.kind == "multi_turn":
            turns = self.turns or []
            if len(turns) < 2 or any(not t.strip() for t in turns):
                raise ValueError("kind=multi_turn 必须给 ≥2 条非空 turns "
                                 "(否则跑起来循环零次、答案恒空且不报错)")
        elif self.turns:
            raise ValueError("kind=single 不接受 turns (多轮请用 kind=multi_turn)")
        else:
            self.turns = None   # 归一: 空列表也存成 null, 与既有考题集一致
        return self


class FilterRuleIn(BaseModel):
    entity: str
    col: str
    op: Literal["=", "!=", ">", "<", ">=", "<=", "IN", "IS NULL", "IS NOT NULL"]
    value: str = ""
    mode: Literal["hard", "soft"]
    origin: Literal["source_code", "manual", "ai"]


class FilterRuleDeleteIn(BaseModel):
    entity: str
    col: str
    op: str
    value: str = ""


class ConceptConditionIn(BaseModel):
    entity: str
    col: str = ""
    op: Literal["=", "!=", ">", "<", ">=", "<=", "IN", "IS NULL", "IS NOT NULL", ""] = "="
    value: str | None = None
    seq: int = 0
    # 受控表达式 (spec §4): kind 白名单 = 写时严; agg 字段白名单守门。
    kind: Literal["column", "agg"] = "column"
    agg_fn: Literal["", "COUNT", "SUM", "AVG", "MAX", "MIN"] = ""
    agg_target: str = ""
    group_by_col: str = ""
    having_op: Literal["", ">", ">=", "<", "<=", "=", "!="] = ""
    having_value: str = ""

    @model_validator(mode="after")
    def _check_agg(self):
        """kind='agg' 时聚合四件套必填 (写时严, 业务层补 Literal 拦不住的"非空")。"""
        if self.kind == "agg":
            if not self.agg_fn:
                raise ValueError("kind='agg' 需 agg_fn (COUNT/SUM/AVG/MAX/MIN)")
            if not self.group_by_col:
                raise ValueError("kind='agg' 需 group_by_col")
            if not self.having_op:
                raise ValueError("kind='agg' 需 having_op")
            if self.having_value == "":
                raise ValueError("kind='agg' 需 having_value (聚合断言必须有比较)")
            if self.agg_fn != "COUNT" and not self.agg_target:
                raise ValueError(f"{self.agg_fn} 需 agg_target 物理列")
        return self


class ConceptIn(BaseModel):
    id: str
    display_name: str
    anchor_entity: str
    summary: str = ""
    origin: Literal["manual", "ai"]
    conditions: list[ConceptConditionIn] = []


class ConceptDeleteIn(BaseModel):
    id: str


# ---------- 会话 CRUD ----------

@app.get("/sessions")
def list_sessions():
    return _store.list_meta()


@app.post("/sessions")
def create_session():
    return _store.create()


@app.get("/sessions/{sid}")
def get_session(sid: str):
    try:
        return _store.load(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在")


@app.delete("/sessions/{sid}")
def delete_session(sid: str):
    try:
        _store.delete(sid)
    except PermissionError:
        # Windows: 会话正在被 worker 写 (文件占用)
        raise HTTPException(status_code=409, detail="会话正在生成中, 稍后再删")
    _cache.pop(sid, None)
    return {"ok": True}


# ---------- 对话 ----------

@app.post("/agent/stream")
def agent_stream_endpoint(body: AgentIn):
    log_event("web.agent_stream_request",
              {"session_id": body.session_id, "message": body.message[:200]})
    # 锁外先校验会话存在 (不存在直接 404, 不占锁)
    try:
        _get_conv(body.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在")

    q: queue.Queue = queue.Queue()

    def worker():
        steps: list[dict] = []

        def on_event(ev):
            if ev["type"] == "tool_start":
                steps.append({"name": ev["name"], "result": None,
                              "ms": None, "ok": None})
            elif ev["type"] == "tool_end":
                for s in reversed(steps):
                    if s["name"] == ev["name"] and s["result"] is None:
                        s["result"] = ev["result"]
                        s["ms"] = ev["latency_ms"]
                        s["ok"] = ev["success"]
                        break
            q.put(ev)

        with _agent_lock:
            try:
                conv = _get_conv(body.session_id)
                is_first = len(conv) == 0
                run_agent(body.message, conversation=conv, on_event=on_event)
                _persist(body.session_id, conv, steps)
                q.put({"type": "done"})
            except Exception as e:
                log_event("web.agent_stream_error",
                          {"error": f"{type(e).__name__}: {e}"})
                q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
                return
        # 锁外: 首轮生成标题 (不阻塞)
        if is_first:
            _gen_title_async(body.session_id, body.message)

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            ev = q.get()
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev["type"] in ("done", "error"):
                break

    return StreamingResponse(sse(), media_type="text/event-stream")


def _persist(sid: str, conv: Conversation, steps: list[dict]) -> None:
    """把本轮新增的一对 Q&A (assistant 带 steps) 追加进磁盘历史。

    run_agent._write_back 每轮只 add_user + add_assistant 两条, 所以 conv.to_dict()
    的最后两条就是本轮新增。磁盘 messages 已含全部历史 (带 steps), 直接追加新一对,
    无需按 index 回填 —— 简单且不会错位。任务失败 (未写回) 时 conv 无新增, 不落盘。
    """
    conv_msgs = conv.to_dict()          # [{role, content}, ...] 完整对话
    data = _store.load(sid)
    n_old = len(data["messages"])
    new_pair = conv_msgs[n_old:]        # 本轮新增 (通常 2 条: user + assistant)
    if not new_pair:
        return                           # 无新增 (任务失败 run_agent 没写回), 不动磁盘
    # 给本轮 assistant 消息挂 steps (供前端展示)
    if steps and new_pair[-1]["role"] == "assistant":
        new_pair = new_pair[:-1] + [{**new_pair[-1], "steps": steps}]
    data["messages"] = data["messages"] + new_pair
    _store.save(sid, data)


def _gen_title_async(sid: str, first_msg: str) -> None:
    """后台线程 fire-and-forget 生成标题; 失败回退首条消息截断。"""
    def work():
        title = first_msg[:20]  # 回退默认值
        try:
            resp = complete(messages=[
                {"role": "system",
                 "content": "用不超过15字概括这句话的主题, 只输出标题本身, 不要引号。"},
                {"role": "user", "content": first_msg},
            ])
            title = resp.choices[0].message.content.strip()[:20] or title
        except Exception as e:
            log_event("session.title_failed",
                      {"id": sid, "error": f"{type(e).__name__}: {e}"})
        # read-modify-write: 只改 title, 不覆盖 messages
        try:
            data = _store.load(sid)
            data["title"] = title
            _store.save(sid, data)
        except KeyError:
            pass  # 会话已被删

    threading.Thread(target=work, daemon=True).start()


# ---------- 工具 / MCP 配置管理 ----------

@app.get("/config/tools")
def get_config_tools():
    # 面板与 agent 共用防线一(profile 门控): serving 下只列 category=serving 的
    # 工具, local 工具不进面板(agent 本就不加载, 列出来会误导)。启停仍由 tools.json
    # 管, 不在这里过 —— 手动关掉的 serving 工具要留在面板(enabled=false)才能再打开。
    _, impls = get_all_tools()
    visible = _allowed_by_profile(impls)
    return tool_config.list_status(sorted(visible))


@app.get("/config/sources")
def get_config_sources():
    """前端实体编辑器的 data_source 下拉选项 = 本机 DB_SOURCES 配的源名。

    ★ 2026-10-15: 原先前端无法编辑 data_source, 新使用者配好自己的库也没法把
    实体指过去。这个端点给 UI 提供可选源清单 (而不是让用户手敲源名敲错)。
    """
    return {"sources": list(settings.db_sources)}


@app.put("/config/tools/{name}")
def put_config_tool(name: str, body: ToolToggle):
    tool_config.set_enabled(name, body.enabled)
    return {"ok": True}


@app.get("/config/mcp_servers")
def get_config_mcp_servers():
    """面板列全部 server, 但带上 agent 本轮是否真会加载它 (active)。

    ★ 与本地工具面板那条注释同一个教训: serving profile 下某个 server 显示
    "已启用"而 agent 根本不加载它, 是在误导人。但 MCP 这里**不能像本地工具
    那样直接不列** —— 那样使用者就没有途径把它改成 serving。所以全列,
    用 active 字段把"配置启用"和"本轮真加载"分开讲。
    """
    servers = mcp_config.load()
    allowed = {s["name"] for s in _servers_allowed_by_profile(
        [s for s in servers if s.get("enabled", False)])}
    return [{**s, "category": s.get("category", "local"),
             "active": s["name"] in allowed} for s in servers]


@app.post("/config/mcp_servers")
def post_config_mcp_server(body: McpServerIn):
    try:
        mcp_config.add(body.model_dump())
    except ValueError:
        raise HTTPException(status_code=409, detail="MCP server 重名")
    return {"ok": True}


@app.delete("/config/mcp_servers/{name}")
def delete_config_mcp_server(name: str):
    mcp_config.delete(name)
    return {"ok": True}


@app.put("/config/mcp_servers/{name}")
def put_config_mcp_server(name: str, body: McpServerToggle):
    """启停 + 可见性层级。category 省略 = 不动 (PATCH 语义, 同 /config/model)。

    ★ category 必须有写路径: 只读的话 serving 部署下没有任何途径启用一个
    MCP server, 门控就从"最小暴露"退化成"一律禁用"。
    """
    if body.category is not None and body.category not in ("local", "serving"):
        raise HTTPException(status_code=400,
                            detail="category 只能是 local 或 serving")
    try:
        mcp_config.set_enabled(name, body.enabled, category=body.category)
    except KeyError:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    return {"ok": True}


# ---------- 模型配置管理 (候选清单 + 激活项; 真相在 config/model.json) ----------
#
# 为什么不是改 .env: settings 是启动期单例, 改 .env 要重启后端。这几个端点写的是
# config/model.json, core/llm.py 每次调用现读 —— 改完下一轮对话即生效。
#
# ★ key 一律只出掩码, 明文走单独的 /key 端点且挂 profile 闸 (见那个端点的注释)。

@app.get("/config/model")
def get_config_model():
    return model_config.list_status()


@app.post("/config/model")
def post_config_model(body: ModelIn):
    try:
        model_config.add(body.model_dump())
    except ValueError as e:
        # 重名 → 409 (跟 MCP server 同口径); 名字非法/model 空 → 400
        code = 409 if "重复" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e))
    return {"ok": True}


# ★ 声明顺序要紧: active 必须在 /{name} 之前, 否则 FastAPI 把 "active"
# 当成 name 匹配到下面那条路由 (测试 test_active_路由不被name路由抢走 守着)。
@app.put("/config/model/active")
def put_config_model_active(body: ModelActivate):
    try:
        model_config.set_active(body.name)
    except KeyError:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return {"ok": True}


@app.put("/config/model/{name}")
def put_config_model(name: str, body: ModelPatch):
    # 字段省略 (None) = 不动该字段; api_key 空串同样按"不动"处理 ——
    # 前端表单里 key 框显示的是掩码, 用户不改 key 时提交空值。
    try:
        model_config.update(name, model=body.model, api_key=body.api_key,
                            base_url=body.base_url)
    except KeyError:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/config/model/{name}")
def delete_config_model(name: str):
    try:
        model_config.delete(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))   # 删 active 被拒
    return {"ok": True}


@app.get("/config/model/{name}/key")
def get_config_model_key(name: str):
    """明文 api_key (前端"看明文"按钮)。

    ★ 唯一回明文的出口, 只在 DEPLOY_PROFILE=local 放行。缺省 profile 是 serving,
    所以默认关闭 (fail-safe)。理由: web 端没有任何认证, 且 CORS 放行
    localhost 任意端口 —— 这个端点等于"本机任何进程/任何页面都能取走 key"。
    本机开发时这是可接受的便利, 部署形态下不是。
    """
    if settings.deploy_profile != "local":
        log_event("model_config.key_peek_denied", {"name": name,
                                                   "profile": settings.deploy_profile})
        raise HTTPException(status_code=403,
                            detail="明文 key 仅在 DEPLOY_PROFILE=local 下可读")
    try:
        key = model_config.get_key(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    log_event("model_config.key_peek", {"name": name})
    return {"api_key": key}


# ---------- Skill 配置管理 (只读: 列表/正文/启停) ----------

@app.get("/config/skills")
def get_config_skills():
    return skill_config.list_status()


@app.get("/config/skills/{name}")
def get_config_skill_body(name: str):
    try:
        return {"name": name, "content": skill_config.read_body(name)}
    except KeyError:
        raise HTTPException(status_code=404, detail="skill 不存在")


@app.get("/config/skills/{name}/full")
def get_config_skill_full(name: str):
    # 编辑用: 返回含 frontmatter 的完整 SKILL.md (read_body 剥了 frontmatter, 回写会挂校验)。
    try:
        return {"name": name, "content": skill_config.read_full(name)}
    except KeyError:
        raise HTTPException(status_code=404, detail="skill 不存在")


@app.put("/config/skills/{name}")
def put_config_skill(name: str, body: ToolToggle):
    # 决策: 沿袭 tools 的宽松语义, PUT 不校验 name 是否真实存在 (不 404)。
    # 理由: 真相在文件系统, config 只记状态; 写下的死配置由 list_status/enabled_catalog
    # 以 discover() 为准自动忽略 (同 tools.py)。GET body 做 404 是因为它真要读文件拿不到,
    # PUT 只写状态无此约束。两者语义差异是刻意的, 非疏漏。
    skill_config.set_enabled(name, body.enabled)
    return {"ok": True}


@app.post("/config/skills")
def post_config_skill(body: SkillCreate):
    try:
        skill_config.create(body.name, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.put("/config/skills/{name}/body")
def put_config_skill_body(name: str, body: SkillBody):
    try:
        skill_config.update_body(name, body.content)
    except KeyError:
        raise HTTPException(status_code=404, detail="skill 不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/config/skills/{name}")
def delete_config_skill(name: str):
    try:
        skill_config.delete(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="skill 不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# ---------- 本体图 (Task4: 读接口从单例, 写接口落 SQLite + 热重载) ----------

def _reload_ontology() -> None:
    """写库后热重载单例, 让读接口/agent 立刻看到新本体 (复用 set_ontology 加载路径)。"""
    set_ontology(settings.obda_ontology_path)


def _ontology_or_404():
    onto = get_ontology()
    if onto is None:
        raise HTTPException(status_code=404, detail="本体未加载")
    return onto


@app.get("/ontology/graph")
def get_ontology_graph():
    """域分组的实体(节点)+关系(边)+域元数据, 喂前端力导向图 (同行接口1)。

    消费方: frontend/src/components/GraphPanel.tsx ("网络图" tab, 2026-09-21 接上)。

    节点带 dataSource: 前端靠它判跨库边 (lib↔lib2 不能拼进一条 SQL,
    图上画虚线区分)。domain 判不出这个 —— "读者账户" 域恰好等于 lib2 库
    是巧合不是判据。
    """
    onto = _ontology_or_404()
    nodes = [
        {"id": e["id"], "displayName": e.get("displayName", e["id"]),
         "domain": e.get("domain", ""), "summary": e.get("summary", ""),
         "dataSource": e.get("data_source", "")}
        for e in onto.entities.values()
    ]
    seen: set = set()
    edges = []
    for adj in onto.adjacency.values():
        for r in adj:
            key = (r["source"], r["target"], r.get("via"))
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "source": r["source"], "target": r["target"],
                "via": r.get("via"), "label": r.get("label"), "type": r.get("type"),
            })
    return {"nodes": nodes, "edges": edges, "domains": onto.domains}


@app.get("/ontology/domains")
def get_ontology_domains():
    """域列表 (同行接口2)。"""
    return _ontology_or_404().domains


@app.get("/ontology/catalogs")
def get_ontology_catalogs():
    """检索卡片 (同行接口3); keywords 读 recallKeywords (迁移后多数实体有值)。

    ⚠️ 2026-09-18 核实: 前端尚无消费方 (graph 已于 2026-09-21 接上, 本端点仍无)。
    """
    onto = _ontology_or_404()
    return [
        {"id": e["id"], "displayName": e.get("displayName", e["id"]),
         "domain": e.get("domain", ""), "summary": e.get("summary", ""),
         "keywords": e.get("recallKeywords", [])}
        for e in onto.entities.values()
    ]


@app.get("/ontology/entities")
def get_ontology_entities():
    """实体全字段 (含 attributes/extra_json 血肉), 供编辑器只读区展示。"""
    onto = _ontology_or_404()
    return list(onto.entities.values())


@app.get("/ontology/relations")
def get_ontology_relations():
    """全部关系 (含 JOIN 字段对), 按 (source,target,via) 去重 (adjacency 双向记)。"""
    onto = _ontology_or_404()
    seen: set = set()
    out = []
    for adj in onto.adjacency.values():
        for r in adj:
            key = (r["source"], r["target"], r.get("via"))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source": r["source"], "target": r["target"], "via": r.get("via"),
                "label": r.get("label"), "type": r.get("type"),
                "source_col": r.get("source_col"), "target_col": r.get("target_col"),
                "bridge_count": r.get("bridge_count"),
                "origin": r.get("origin"),
            })
    return out


@app.get("/ontology/enums")
def get_ontology_enums():
    """全部 enum_value, 由单例 value_sets (tuple-key) 摊平成列表。"""
    onto = _ontology_or_404()
    return [
        {"entity": k[0], "field": k[1], "name": v.get("name"),
         "spec_ref": v.get("spec_ref"), "codes": v.get("codes", [])}
        for k, v in onto.value_sets.items()
    ]


@app.get("/filter-rules")
def get_filter_rules():
    """全部存活 filter_rule (管理读)。由单例 filter_rules (entity->[...]) 摊平。"""
    onto = _ontology_or_404()
    rules = []
    for entity, rs in onto.filter_rules.items():
        for r in rs:
            rules.append({"entity": entity, "col": r["col"], "op": r["op"],
                          "value": r.get("value"), "mode": r["mode"], "origin": r["origin"]})
    return {"rules": rules}


def _with_store(fn):
    """打开 OntologyStore 跑 fn(store) → close → 热重载单例 (写接口共用样板)。"""
    from capabilities.ontology.ontology_store import OntologyStore
    store = OntologyStore(settings.obda_ontology_path)
    try:
        fn(store)
    finally:
        store.close()
    _reload_ontology()


@app.post("/filter-rules")
def post_filter_rule(body: FilterRuleIn):
    """新增/更新一条 filter_rule (幂等键 entity,col,op,value)。mode/op/origin 走 Literal 白名单, 非法 422。
    普通 op 传空值 → store 写时守门抛 ValueError → 400 (照 post_config_skill 范式)。"""
    try:
        _with_store(lambda s: s.upsert_filter_rule(body.model_dump()))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/filter-rules")
def delete_filter_rule(body: FilterRuleDeleteIn):
    """软删一条 filter_rule (四键定位)。value 可空走 body 表达 None。"""
    _with_store(lambda s: s.delete_filter_rule(body.entity, body.col, body.op, body.value))
    return {"ok": True}


@app.get("/concepts")
def get_concepts():
    """全部存活概念 + 内嵌存活条件 (管理读)。摊 onto.concepts 单例 (dict)。"""
    onto = _ontology_or_404()
    return {"concepts": list(onto.concepts.values())}


@app.post("/concepts")
def post_concept(body: ConceptIn):
    """组合保存一个概念 + 全部条件 (幂等键 id)。origin 走 Literal 白名单, 非法 422。
    存后 valid 自动回 0 (改定义须重校验)。库里有、body 没传的旧条件级联软删。"""
    payload = body.model_dump()
    conditions = payload.pop("conditions")
    _with_store(lambda s: s.save_concept_with_conditions(payload, conditions))
    return {"ok": True}


@app.delete("/concepts")
def delete_concept_ep(body: ConceptDeleteIn):
    """软删概念 + 级联软删其条件。"""
    _with_store(lambda s: s.delete_concept(body.id))
    return {"ok": True}


def _validate_one_concept(concept: dict, onto, cols_cache: dict) -> dict:
    """校验单个概念; 涉及实体逐个取物理表列组 table_cols_map, 连通性走 find_join_paths。"""
    from capabilities.ontology.validate_concepts import validate_concept
    table_cols_map: dict = {}
    for eid in [concept["anchor_entity"], *(c["entity"] for c in concept["conditions"])]:
        if eid in table_cols_map:
            continue
        ent = onto.entities.get(eid, {})
        tables = ent.get("tables") or []
        source = ent.get("data_source") or default_source()
        if not tables:
            table_cols_map[eid] = set()
            continue
        key = (tables[0], source)
        try:
            if key not in cols_cache:
                cols_cache[key] = list_table_columns(tables[0], source)
            table_cols_map[eid] = cols_cache[key]
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "概念校验取列失败 %s@%s: %s: %s", tables[0], source, type(e).__name__, e)
            table_cols_map[eid] = set()   # 兜底空集, 校验器判列不存在红灯
    return validate_concept(concept, onto,
                            table_cols_map, lambda a, b: find_join_paths(onto, a, b))


@app.post("/concepts/validate")
def post_validate_concepts():
    """一键校验全部存活概念, 回写各 concept.valid, 返回每概念 {id, checks, ok}。"""
    onto = _ontology_or_404()
    cols_cache: dict = {}
    results = [_validate_one_concept(c, onto, cols_cache) for c in onto.concepts.values()]
    _with_store(lambda s: [s.set_concept_valid(r["id"], 1 if r["ok"] else 0) for r in results])
    return {"results": results}


def _make_run_sql(entity: str, source: str):
    """包 execute_sql 成校验器要的 run_sql: 正常吞掉, 有 error 键→抛 (跑不通=抛)。"""
    def run_sql(sql: str) -> None:
        res = execute_sql(sql, entity=entity, source=source)
        if "error" in res:
            raise RuntimeError(res["error"])
    return run_sql


def _validate_one_rule(rule: dict, onto, cols_cache: dict) -> dict:
    """校验单条 rule; 取列/组装注入点异常 → 兜底 table_access 红灯卡 (不外抛)。"""
    from capabilities.ontology.validate_filter_rules import validate_filter_rule
    entity = rule["entity"]
    ent = onto.entities.get(entity, {})
    tables = ent.get("tables") or []
    source = ent.get("data_source") or default_source()
    base = {"entity": entity, "col": rule["col"], "op": rule["op"], "value": rule.get("value")}
    if not tables:
        return {**base, "checks": [{"name": "table_access", "ok": False,
                "detail": f"实体 {entity} 无物理表"}], "ok": False}
    table = tables[0]
    key = (table, source)
    try:
        if key not in cols_cache:
            cols_cache[key] = list_table_columns(table, source)
        table_cols = cols_cache[key]
    except Exception as e:
        return {**base, "checks": [{"name": "table_access", "ok": False,
                "detail": f"取列失败 {table}@{source}: {type(e).__name__}: {e}"}], "ok": False}
    res = validate_filter_rule(rule, onto, table_cols, _make_run_sql(entity, source))
    return {**base, "checks": res["checks"], "ok": res["ok"]}


def _flatten_check_result(vr: dict) -> dict:
    """校验器单条结果 (checks 数组) → filter_rule_check 落盘 row (三列 + detail 拼接)。

    三检查 name 固定 column_exists/value_in_enum/sql_runs; value_in_enum 若 skipped=True
    存 None (三态: 该字段无枚举登记 ≠ 校验通过), 否则 1/0。detail = 各红灯 detail 拼接。
    """
    by_name = {c["name"]: c for c in vr.get("checks", [])}

    def _tri(name: str):
        c = by_name.get(name)
        if c is None:
            return None
        if c.get("skipped"):
            return None
        return 1 if c["ok"] else 0

    def _bin(name: str):
        c = by_name.get(name)
        return None if c is None else (1 if c["ok"] else 0)

    details = [c["detail"] for c in vr.get("checks", []) if c.get("detail")]
    return {
        "entity": vr["entity"], "col": vr["col"],
        "op": vr.get("op", ""), "value": vr.get("value"),
        "column_exists": _bin("column_exists"),
        "value_in_enum": _tri("value_in_enum"),
        "sql_runs": _bin("sql_runs"),
        "detail": "; ".join(details),
    }


@app.post("/filter-rules/validate")
def post_validate_filter_rules():
    """一键校验全部存活 filter_rule, 每条三检查红绿灯 + 落盘一批快照 (append-only)。"""
    onto = _ontology_or_404()
    cols_cache: dict = {}
    results = []
    for entity, rs in onto.filter_rules.items():
        for rule in rs:
            results.append(_validate_one_rule({**rule, "entity": entity}, onto, cols_cache))

    run_id = uuid.uuid4().hex
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    rows = [_flatten_check_result(r) for r in results]
    from capabilities.ontology.ontology_store import OntologyStore
    store = OntologyStore(settings.obda_ontology_path)
    try:
        store.insert_check_run(run_id, checked_at, rows)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("filter_rule_check 落盘失败 (non-fatal): %s", e)
    finally:
        store.close()
    # 落盘不热重载单例 (只写历史表, 不改本体)
    return {"results": results, "run_id": run_id, "checked_at": checked_at}


@app.get("/filter-rules/check-history")
def get_filter_rule_check_history(limit: int = 2):
    """最近 limit 个校验 run (倒序, 最近在前), 供前端 current/previous 对比。"""
    from capabilities.ontology.ontology_store import OntologyStore
    store = OntologyStore(settings.obda_ontology_path)
    try:
        runs = store.load_check_runs(limit=limit)
    finally:
        store.close()
    return {"runs": runs}


@app.post("/ontology/entity")
def post_ontology_entity(body: EntityIn):
    """新建实体 (id 不存在才整行插入)。命中已存在 id → 409, 改标量走 PUT load-merge。
    否则 upsert_entity 的 ON CONFLICT 整行覆盖会抹 attributes/extra_json 血肉
    (T3 防抹血肉哲学的另一入口, 前端虽只 PUT, 端点不能留抹血肉口子)。"""
    def mutate(s):
        try:
            s.load_entity(body.id)
        except KeyError:
            s.upsert_entity(body.model_dump())   # 不存在 → 新建合法
            return
        raise HTTPException(status_code=409, detail="实体已存在, 改标量请用 PUT")
    _with_store(mutate)
    return {"ok": True}


# entity 固定列: 从 store 导入 public 名, 不再内联复制 (消除三处同步陷阱)。
from capabilities.ontology.ontology_store import ENTITY_FIXED_KEYS as _ENTITY_FIXED_KEYS
# extra_json 白名单血肉键 (与 EntityExtraIn 字段对应)。query_hints 已废除(→ filter_rule 表);
# cocVerified/inSpec 已于 2026-08-05 废除 (运行时零消费的旧一代治理位)。
_EXTRA_WHITELIST = ("recallKeywords", "disambiguateFrom")

_MAX_ATTRS = 20   # 与 capabilities.ontology.validate_semantics._MAX_ATTRS 口径一致


def _attr_row(a: AttributeIn) -> dict:
    """AttributeIn → 落库 dict: name/kind/type/desc_source 恒写, 其余省缺省
    (空串/None 不写此键, 与真库现状一致)。"""
    row = {"name": a.name.strip(), "kind": a.kind, "type": a.type,
           "desc_source": a.desc_source}
    for key, val in (("desc", a.desc.strip()),
                     ("disambiguation", a.disambiguation.strip())):
        if val:
            row[key] = val
    for key, val in (("hasEnum", a.hasEnum), ("sensitive", a.sensitive)):
        if val is not None:
            row[key] = val
    return row


def _check_attr_rows(rows: list[dict], enum_fields: set[str]) -> None:
    """name 非空/同列表查重 + 条数上限 + hasEnum 断链 → 400 (中文 detail)。"""
    if len(rows) > _MAX_ATTRS:
        raise HTTPException(status_code=400,
                            detail=f"字段数 {len(rows)} 超上限 {_MAX_ATTRS}")
    seen: set[str] = set()
    for r in rows:
        if not r["name"]:
            raise HTTPException(status_code=400, detail="有属性 name 为空")
        if r["name"] in seen:
            raise HTTPException(status_code=400, detail=f"属性 name 重复: {r['name']}")
        seen.add(r["name"])
    broken = [r["name"] for r in rows if r.get("hasEnum") and r["name"] not in enum_fields]
    if broken:
        raise HTTPException(
            status_code=400,
            detail=f"字段 {', '.join(broken)} 标了有枚举码表但码表无码值, 请先到枚举 Tab 建码表")


@app.put("/ontology/entity/{entity_id}")
def put_ontology_entity(entity_id: str, body: EntityEditIn):
    """load-merge-write: 读旧行 → 覆盖标量 → (若带 extra) 2 白名单键重建血肉
    → (若带 attributes) 校验后整列表替换, 否则原样保留 (防抹血肉)。"""
    dump = body.model_dump()
    extra = dump.pop("extra")            # None 或 2 键 dict
    dump.pop("attributes")               # 走 body.attributes 整列表替换 (_attr_row 逐条落库)
    if dump.get("data_source") is None:
        dump.pop("data_source")          # 不传 = 不动, 保留旧值 (防前端未传时清空)
    attr_rows = ([_attr_row(a) for a in body.attributes]
                 if body.attributes is not None else None)

    def mutate(s):
        try:
            merged = s.load_entity(entity_id)   # 旧行 (extra_json 平铺)
        except KeyError:
            raise HTTPException(status_code=404, detail="实体不存在")
        merged.update(dump)                     # 覆盖标量
        if attr_rows is not None:
            _check_attr_rows(attr_rows, s.enum_fields(entity_id))
            merged["attributes"] = attr_rows
        if extra is not None:
            # 封闭语义: 清掉所有旧血肉键 (白名单外一并清), 再按 2 键重建。
            for k in list(merged.keys()):
                if k not in _ENTITY_FIXED_KEYS:
                    del merged[k]
            for k in _EXTRA_WHITELIST:
                merged[k] = extra[k]            # recallKeywords/disambiguateFrom, 恒为 list
        s.upsert_entity(merged)

    _with_store(mutate)
    return {"ok": True}


@app.delete("/ontology/entity/{entity_id}")
def delete_ontology_entity(entity_id: str):
    _with_store(lambda s: s.delete_entity_cascade(entity_id))
    return {"ok": True}


@app.post("/ontology/domain")
def post_ontology_domain(body: DomainIn):
    _with_store(lambda s: s.upsert_domain(body.model_dump()))
    return {"ok": True}


@app.delete("/ontology/domain/{code}")
def delete_ontology_domain(code: str):
    """软删域 + 级联把挂靠实体 domain 置空 (归未分类), 单事务 all-or-nothing。"""
    _with_store(lambda s: s.delete_domain_cascade(code))
    return {"ok": True}


@app.post("/ontology/relation")
def post_ontology_relation(body: RelationIn):
    _with_store(lambda s: s.upsert_relation(body.model_dump()))
    # via 由 store 从 col 派生, 回传供前端预览
    from capabilities.ontology.ontology_store import _derive_via
    return {"ok": True, "via": _derive_via(body.source, body.source_col, body.target, body.target_col)}


@app.delete("/ontology/relation")
def delete_ontology_relation(source: str, target: str,
                             source_col: str, target_col: str):
    _with_store(lambda s: s.delete_relation(source, target, source_col, target_col))
    return {"ok": True}


@app.post("/ontology/relation/flip")
def post_ontology_relation_flip(body: RelationFlipIn):
    """改方向: 一操作内删旧边 + 建 source/target(及col)互换的新边, via/label/type 保留。

    via 不再手编 —— 按 body 的 source_col/target_col 派生旧 via 定位删除,
    new_edge 的 via 由 store 按翻转后的 col 派生 (翻转后 via 变 B.y=A.x)。
    """
    onto = _ontology_or_404()
    old = None
    for adj in onto.adjacency.values():
        for r in adj:
            if (r["source"], r["target"]) == (body.source, body.target) and \
               r.get("source_col") == body.source_col and \
               r.get("target_col") == body.target_col:
                old = r
                break
        if old:
            break
    if old is None:
        raise HTTPException(status_code=404, detail="关系不存在")
    # 基于 old 展开覆盖: type/label/bridge_count 及任意 extra 键 (crossDomain/note
    # 等跨域断腿信号) 原样保留, 只翻转 source/target 与 source_col/target_col。
    # via 不带 (store 按新 col 派生)。
    new_edge = {**old,
                "source": old["target"], "target": old["source"],
                "source_col": old.get("target_col"), "target_col": old.get("source_col")}
    new_edge.pop("via", None)

    _with_store(lambda s: s.flip_relation(
        body_source=body.source, body_target=body.target,
        body_source_col=body.source_col, body_target_col=body.target_col,
        new_edge=new_edge))
    return {"ok": True}


@app.post("/ontology/enum")
def post_ontology_enum(body: EnumIn):
    """upsert 整张码表 (codes 全量覆盖)。"""
    ev = {"name": body.name, "spec_ref": body.spec_ref,
          "codes": [c.model_dump() for c in body.codes]}
    _with_store(lambda s: s.upsert_enum_value(body.entity, body.field, ev))
    return {"ok": True}


@app.delete("/ontology/enum")
def delete_ontology_enum(entity: str, field: str):
    """软删整张码表。"""
    _with_store(lambda s: s.delete_enum_value(entity, field))
    return {"ok": True}


# ---------- 验证回路 B (CQ) ----------

@app.get("/cq/set")
def get_cq_set():
    try:
        cases = cq_runner.load_cq_set()
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"考题集读取失败: {e}")
    return {"cases": cases}


@app.get("/cq/results")
def get_cq_results():
    return {
        "current": cq_runner.read_results(cq_runner.CUR_PATH),
        "previous": cq_runner.read_results(cq_runner.PREV_PATH),
    }


@app.put("/cq/verdict/{cq_id}")
def put_cq_verdict(cq_id: str, body: VerdictIn):
    ok = cq_runner.set_verdict(cq_id, body.verdict, body.note)
    if not ok:
        raise HTTPException(status_code=404, detail="结果里找不到该 CQ (先跑一轮?)")
    return {"ok": True}


@app.post("/cq/case")
def post_cq_case(body: CqCaseIn):
    """新建考题。id 撞 → 409。"""
    try:
        cq_runner.upsert_case(body.model_dump(), create_only=True)
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        # 考题集被手改坏时: 400 + 原始行列号, 别让 JSONDecodeError 穿成 500
        # (前端只会显示 "Failed to fetch", 用户无从知道要去修哪一行)
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.put("/cq/case/{cq_id}")
def put_cq_case(cq_id: str, body: CqCaseIn):
    """改考题。路径 id 与 body.id 必须一致; 不存在 → 404。"""
    if cq_id != body.id:
        raise HTTPException(status_code=400,
                            detail=f"路径 id {cq_id} 与 body.id {body.id} 不一致")
    try:
        cases = {c["id"] for c in cq_runner.load_cq_set()}
        if cq_id not in cases:
            raise HTTPException(status_code=404, detail=f"考题 {cq_id} 不存在")
        cq_runner.upsert_case(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/cq/case/{cq_id}")
def delete_cq_case(cq_id: str):
    """删考题; 不存在 → 404。"""
    try:
        ok = cq_runner.delete_case(cq_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"考题 {cq_id} 不存在")
    return {"ok": True}


@app.post("/cq/run")
def run_cq_endpoint():
    """批量重跑 CQ, SSE 逐条推; 复用 _agent_lock 串行锁。跑前 current→previous。"""
    q: queue.Queue = queue.Queue()

    def worker():
        with _agent_lock:
            try:
                cases = cq_runner.load_cq_set()
            except (FileNotFoundError, ValueError) as e:
                q.put({"type": "error", "error": f"考题集读取失败: {e}"})
                return
            try:
                cq_runner.rotate_current_to_previous()
                results = []
                for case in cases:
                    q.put({"type": "case_start", "id": case["id"]})
                    r = cq_runner.run_one_cq(case)
                    results.append(r)
                    q.put({"type": "case_done", **r})
                cq_runner.save_current(
                    results, get_ontology_path() or settings.obda_ontology_path,
                    datetime.datetime.now().isoformat(timespec="seconds"))
                q.put({"type": "done", "n": len(results)})
            except Exception as e:
                log_event("web.cq_run_error",
                          {"error": f"{type(e).__name__}: {e}"})
                q.put({"type": "error",
                       "error": f"{type(e).__name__}: {e}"})
                return

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            ev = q.get()
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev["type"] in ("done", "error"):
                break

    return StreamingResponse(sse(), media_type="text/event-stream")


# ---------- 自演化 (Task 7): 提议管理 + 冷路径触发 + 健康观察 ----------

class ProposalReviewIn(BaseModel):
    review_status: Literal["approved", "rejected"]
    review_note: str = ""
    # ★ approve 一条新增边时必填 (2026-09-19): probe 产的 payload 不带 label,
    # 不在这里补就落库成裸边。详见 _review_label_into_payload。
    label: str = ""


class HealthRetireIn(BaseModel):
    """边健康面板发起 retire: 三元组定位边 (与 relation 表 UNIQUE 同口径)。"""
    source: str
    target: str
    via: str
    reason: str = ""


def _selfevolve_store():
    """打开本体 store (自演化读写共用; 不走 _with_store 因 process/apply 有自己的事务节奏)。"""
    from capabilities.ontology.ontology_store import OntologyStore
    return OntologyStore(settings.obda_ontology_path)


@app.get("/selfevolve/proposals")
def list_selfevolve_proposals(review_status: str | None = None):
    """提议列表, 可按 review_status 过滤。"""
    store = _selfevolve_store()
    try:
        proposals = store.list_proposals(review_status)
    finally:
        store.close()
    return {"proposals": proposals}


def _requires_review_label(proposal: dict) -> bool:
    """这条提议 approve 时是否必须带 label。

    只有"新增一条边"需要 —— 它 apply 时走 upsert_relation 建新行, label 空
    就是裸边。retire 是软删一条**现有**边, label 早在库里; reject 不落库。
    """
    return proposal.get("direction") == "add" and proposal.get("kind") == "relation"


def _review_label_into_payload(store, proposal: dict, label: str) -> None:
    """把人审填的 label 合进 payload_json (其余字段不动)。

    为什么 label 落在人审这一刻而不是 probe 或 apply:
    - probe 只能按列名拼草稿, 产出的是"有 label 但无信息量"的边, 比裸边更坏
      —— 从此看不出哪条是待补的, 且雷同护栏会拿这些草稿互相误拦。
    - apply 那时没人在场, 空了只能拒绝, 飞轮直接断。
    - 人审正在判这条边的业务语义 (走不走得通), 顺手写一句成本最低、质量最高。
      R10 那轮的 label 也正是人工真库复核定的。
    """
    try:
        payload = json.loads(proposal.get("payload_json") or "{}")
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"提议 payload_json 解析失败, 无法写入 label: {exc}") from exc
    payload["label"] = label.strip()
    store.patch_proposal_payload(proposal["id"], payload)


@app.patch("/selfevolve/proposals/{proposal_id}/review")
def review_selfevolve_proposal(proposal_id: int, body: ProposalReviewIn):
    """人审: approved / rejected + 备注 (+ 新增边必填 label)。"""
    store = _selfevolve_store()
    try:
        proposals = store.list_proposals()
        target = next((p for p in proposals if p["id"] == proposal_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")
        if body.review_status == "approved" and _requires_review_label(target):
            if not body.label.strip():
                raise HTTPException(
                    status_code=400,
                    detail="批准新增边必须填 label —— 落库后概念层/agent 靠 "
                           "get_join_path 返回的 label 选边, 空 label 只能靠列名前缀猜。"
                           "请写一句能说清业务语义的短语 (如 '订单的变更记录')。")
            _review_label_into_payload(store, target, body.label)
        store.patch_proposal_review(proposal_id, body.review_status,
                                    reviewed_by="human",
                                    review_note=body.review_note)
    finally:
        store.close()
    return {"ok": True}


def _safe_table_ident(table: str) -> str:
    """表名白名单校验: 只允许 [A-Za-z_][A-Za-z0-9_]*, 否则 ValueError。

    COUNT(*) 那句必须拼表名 (占位符只能绑值, 绑不了标识符), 所以在拼之前把关。
    表名当前来自本体 entity id (不是外部直接输入), 但本体可通过提议落地 / CRUD
    写入新实体 —— "上游可信"这个假设不该只存在于一条拼接语句的隐含前提里。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        raise ValueError(f"非法表名 (只允许字母/数字/下划线): {table!r}")
    return table


@app.post("/selfevolve/process")
def process_selfevolve_signals(limit: int = 10):
    """触发一次冷路径 (同步阻塞版)。

    ★ 保留不动: 5 处调用方 (含 verify_flywheel_full_turn / verify_fuel_decoupling
    两个端到端取证脚本) 依赖它返回 JSON。人在前端点按钮走
    /selfevolve/process-stream (流式, 不干等)。
    """
    store = _selfevolve_store()
    try:
        return _run_process_signals(store, limit)
    finally:
        store.close()


def _process_deps(store) -> tuple:
    """冷路径要的两个回调 (run_sql / get_index_info) + entities/relations。

    同步版与流式版共用 —— 两份平行接线是这个项目栽过的坑 (定义写好调用点漏了),
    真库访问逻辑只能有一处。
    """
    from agent.tools.sql_exec import _connections

    raw = store.load_raw()
    entities = {e["id"]: e for e in raw["entities"]}
    relations = raw["relationships"]

    def run_sql(sql: str, source: str) -> list[dict]:
        conn = _connections.get(source)
        if conn is None:
            raise ValueError(f"data source '{source}' not found")
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    def get_index_info(table: str, _role: str) -> dict:
        """物理 PK/UNIQUE/行数 (MySQL; 分库中间件的 TABLE_ROWS 全 0 → COUNT(*)).

        _role 参数是 probe_relation 传来的角色标记 ("source"/"target"), 不是
        真实 data_source. 真实 data_source 从 entities dict 里读取
        (兜底 DB_SOURCES 首源, 见 core.config.default_source)。
        """
        data_source = (entities.get(table) or {}).get("data_source") or default_source()
        conn = _connections.get(data_source)
        if conn is None:
            raise ValueError(f"data source '{data_source}' for entity '{table}' not found")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                "AND INDEX_NAME='PRIMARY'", (table,))
            pk_cols = [r["COLUMN_NAME"] for r in cur.fetchall()]
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                "AND COLUMN_KEY='UNI'", (table,))
            unique_cols = [r["COLUMN_NAME"] for r in cur.fetchall()]
            cur.execute(f"SELECT COUNT(*) AS n FROM `{_safe_table_ident(table)}`")
            row_count = cur.fetchone()["n"]
        return {"pk_cols": pk_cols, "unique_cols": unique_cols,
                "row_count": row_count, "data_source": data_source}

    return run_sql, get_index_info, entities, relations


def _run_process_signals(store, limit: int) -> dict:
    from capabilities.ontology.selfevolve import signals as signals_mod

    run_sql, get_index_info, entities, relations = _process_deps(store)
    return signals_mod.process_pending_signals(
        store, run_sql, get_index_info, entities, relations, limit=limit)


@app.post("/selfevolve/process-stream")
def process_selfevolve_signals_stream(limit: int = 10):
    """冷路径 SSE 版: 立刻回 start 事件, 后台线程跑探针 (A1 修阻塞)。

    探针要连真库逐对跑 COUNT/包含率, 信号一多就是几十秒起步 —— 同步版期间
    前端只能干等且无从知道是在跑还是挂了。照 /cq/run 的惯例做成 SSE。

    事件: start (带待处理信号数) → done (带统计) | error。
    ★ 后台线程里抛的异常必须转成 error 事件: HTTP 头早发出去了, 再抛只会让
    前端看到连接莫名断开 (又一个"安静的失败")。
    """
    q: queue.Queue = queue.Queue()

    def worker():
        store = _selfevolve_store()
        try:
            pending = len(store.list_unprocessed_signals(limit))
            q.put({"type": "start", "pending": pending})
            q.put({"type": "done", **_run_process_signals(store, limit)})
        except Exception as e:
            log_event("web.selfevolve_process_error",
                      {"error": f"{type(e).__name__}: {e}"})
            q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            store.close()

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            ev = q.get()
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev["type"] in ("done", "error"):
                break

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/selfevolve/proposals/{proposal_id}/apply")
def apply_selfevolve_proposal(proposal_id: int):
    """落地: apply_proposal (含守卫/分发) + run_regression 回归门。"""
    from capabilities.ontology.selfevolve.apply import (
        apply_proposal, rollback_apply, run_regression, snapshot_baseline)

    store = _selfevolve_store()
    # ★ try 之前初始化: 异常若发生在 snapshot_baseline/apply_proposal 之前或之中,
    #   except 块引用它们会 NameError —— 等于用一个更难查的异常替换掉原异常。
    out: dict | None = None
    run_id = ""
    extra: dict = {}
    try:
        proposals = store.list_proposals()
        match = [p for p in proposals if p["id"] == proposal_id]
        if not match:
            raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")
        proposal = match[0]
        # baseline 快照 (apply 前拷 current)
        run_id = snapshot_baseline()
        proposal["regression_run_id"] = run_id
        out = apply_proposal(proposal, store)
        if not out.get("ok"):
            return out
        # 回归门 (真跑 CQ; 单测里被 mock)
        reg = run_regression(store, proposal)
        # ★ 闸的两处收窄都不许静默 (判据见 apply._diff_new_hard_blocks):
        #   uncovered_cqs    — baseline 没跑过, 这轮判不了 (修法: 先 /cq/run 刷 baseline)
        #   unrelated_blocks — 硬拦了但违规类型与边无因果 (LLM 非确定性噪音)
        # 不透出的话, "闸放行"一个信号会同时代表"真没坏"和"根本没看/判不到",
        # 人无从分辨 —— 等于没有闸。
        #   truncated_cqs    — 撞 max_tokens 没跑完, 零违规不可信 (判不了)
        uncovered = reg.get("uncovered_cqs") or []
        unrelated = reg.get("unrelated_blocks") or []
        truncated = reg.get("truncated_cqs") or []
        #   no_db_evidence_cqs — 没一次 execute_sql 成功, 零违规只因没查库 (判不了)
        no_db = reg.get("no_db_evidence_cqs") or []
        extra = {"uncovered_cqs": uncovered, "unrelated_blocks": unrelated,
                 "truncated_cqs": truncated, "no_db_evidence_cqs": no_db}
        notes = []
        if uncovered:
            notes.append(
                f"以下 CQ 不在 baseline 里, 这轮回归门没能守住 (判不了, 未计入): "
                f"{', '.join(uncovered)} —— 先跑一轮 /cq/run 刷新 baseline 再 apply")
        if unrelated:
            notes.append(
                f"以下 CQ 本轮硬拦了, 但违规类型与 relation 增删无因果 "
                f"(未计入定罪): {', '.join(unrelated)} —— 多为 agent 猜错列名/"
                f"枚举值这类非确定性噪音, 值得单独看一眼日志")
        if truncated:
            notes.append(
                f"以下 CQ 撞 max_tokens 被截断, 没跑完 (判不了, 未计入): "
                f"{', '.join(truncated)} —— 闸看的是 tool_events 里的违规, 截断的"
                f"运行可能没走到那句会被拦的 SQL, 它的\"零违规\"不可信; "
                f"调大 MAX_TOKENS 后重跑 /cq/run 再 apply")
        if no_db:
            notes.append(
                f"以下 CQ 没有一次 execute_sql 成功 (判不了, 未计入): "
                f"{', '.join(no_db)} —— 没查到库自然零违规, 先确认数据源已连上")
        if notes:
            extra["warning"] = "; ".join(notes)
        if not reg["regression_ok"]:
            # ★ 闸拒绝 = 改动不得生效: 回滚已落库的边 + 热重载, 否则闸形同虚设
            rollback_apply(store, out.get("rollback_snapshot") or {})
            store.patch_proposal_patch_status(
                proposal_id, "failed", regression_run_id=run_id,
                regression_ok=0)
            return {"ok": False,
                    "detail": f"回归门拒绝 (改动已回滚): "
                              f"新增 hard_block {reg['new_hard_blocks']}",
                    "new_hard_blocks": reg["new_hard_blocks"], **extra}
        store.patch_proposal_patch_status(
            proposal_id, "applied", regression_run_id=run_id, regression_ok=1)
    except HTTPException:
        raise  # 404 等业务异常原样抛 (裸 except 会把它吞成 200+ok=False)
    except Exception as e:
        # ★ 闸"判都没判"也必须回滚, 否则是 2026-09-17"闸拒绝不回滚"的第二个版本:
        #   那次是判了不回滚, 这次是坏 JSON / LLM 异常 / 真库断连 / cq_set 运行中
        #   被改 → run_regression 抛异常直穿 500, 而 apply_proposal 已把边落库、
        #   patch_status 置成 applied、本体热重载过。没判过的闸 = 没有闸, 改动
        #   不得生效。
        rollback_apply(store, (out or {}).get("rollback_snapshot") or {})
        store.patch_proposal_patch_status(
            proposal_id, "failed", regression_run_id=run_id, regression_ok=0)
        return {"ok": False,
                "detail": f"回归门异常 (改动已回滚): {e}",
                "regression_error": str(e)}
    finally:
        store.close()
    out.pop("rollback_snapshot", None)
    return {**out, **extra}


@app.post("/selfevolve/cq-regression")
def run_selfevolve_cq_regression():
    """淘汰半环触发: 扫两轮 CQ 结果 → verdict 下降归因 + mismatch 累计。

    ★ 时序: 必须在人于 CQ 面板逐条打分**之后**调, 不能挂 /cq/run 结尾 ——
    save_current 落盘时 verdict=None, 那一刻跑归因必然空转。

    返回值带诊断字段 (scored_current/scored_previous/total_current/warning):
    没打分时要**响亮**说出来, 否则 suspects_updated=0 会被误读成"本体健康"。
    """
    from capabilities.ontology.selfevolve.signals import process_cq_regression

    store = _selfevolve_store()
    try:
        out = process_cq_regression(store)
    finally:
        store.close()
    out.update(_cq_scoring_diagnosis())
    return out


def _cq_scoring_diagnosis() -> dict:
    """统计两轮 CQ 的打分覆盖情况, 空转时给一句人话 warning。"""
    cur = cq_runner.read_results(cq_runner.CUR_PATH) or {"results": []}
    prev = cq_runner.read_results(cq_runner.PREV_PATH) or {"results": []}
    scored_cur = sum(1 for r in cur["results"] if r.get("verdict"))
    scored_prev = sum(1 for r in prev["results"] if r.get("verdict"))
    total_cur = len(cur["results"])

    warning = None
    if total_cur == 0:
        warning = "本轮 CQ 结果为空 (还没跑过 CQ?), 归因无素材"
    elif scored_cur == 0 or scored_prev == 0:
        warning = (f"verdict 归因需要两轮都打分, 当前本轮 {scored_cur}/{total_cur} 条、"
                   f"上轮 {scored_prev} 条已打分 → 该路径空转 "
                   f"(mismatch 累计不受影响, 照常统计)")
    return {"scored_current": scored_cur, "scored_previous": scored_prev,
            "total_current": total_cur, "warning": warning}


@app.get("/selfevolve/health")
def get_selfevolve_health():
    """边健康观察 (spec §8.1 R-4): suspect_count>0 的行, 按 suspect DESC。只读。"""
    store = _selfevolve_store()
    try:
        rows = store.list_suspect_relations(min_suspect=1)
    finally:
        store.close()
    return {"health": [
        {"source": r["source"], "target": r["target"], "via": r["via"],
         "use_count": r["use_count"], "suspect_count": r["suspect_count"],
         "last_suspect_at": r.get("last_suspect_at")}
        for r in rows]}


@app.post("/selfevolve/health/retire")
def propose_retire_from_health(body: HealthRetireIn):
    """可疑边 → retire 提议 (人工发起, 2026-09-18)。

    health 面板原先纯只读, 可疑边攒够了只能手工去库里塞提议。这里把"观察"接到
    人审队列 —— 但**只生成提议**, 落地照走 approve → apply → 回归门, 不绕闸。

    ★ 与 apply 同一道守卫: via 在 relation 表查无此行直接 404。不然生成一条
    注定 apply 失败的提议, 只是把静默失败推后一步。
    """
    from capabilities.ontology.selfevolve.signals import _tombstone_lookup

    store = _selfevolve_store()
    try:
        row = store._conn.execute(
            "SELECT status FROM relation WHERE source=? AND target=? AND via=?",
            (body.source, body.target, body.via)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"边不存在于 relation 表: {body.via}")
        if row["status"] == 0:
            return {"ok": False, "detail": f"边已是软删状态, 无需淘汰: {body.via}"}

        for p in store.list_proposals():
            if p["direction"] != "retire" or p["review_status"] not in (
                    "pending", "approved"):
                continue
            if json.loads(p["payload_json"] or "{}").get("via") == body.via:
                return {"ok": False,
                        "detail": f"已有待处理的 retire 提议 (#{p['id']}), 不重复生成"}

        tomb_hit, tomb_note = _tombstone_lookup(
            store, body.source, body.target, body.via)
        health = store.get_relation_health(body.source, body.target, body.via)
        suspect = (health or {}).get("suspect_count", 0)
        pid = store.insert_proposal(
            "relation_col",
            {"source": body.source, "target": body.target, "via": body.via},
            reason=body.reason or f"人工从边健康面板发起 (suspect_count={suspect})",
            proposed_by="human_health_panel", direction="retire",
            tombstone_hit=tomb_hit, tombstone_note=tomb_note)
    finally:
        store.close()
    return {"ok": True, "proposal_id": pid, "tombstone_hit": tomb_hit}
