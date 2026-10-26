"""core/runtime/agent.py — Agent loop 主体.

设计:
1. 裸 while 循环, 不引框架; 控制流就这 4 个函数 (run_agent + 3 helper)
2. 所有错误路径必须落 JSONL (硬约定): agent.error / agent.llm_fallback / tool_call success=false
   —— success 按"工具这次干成了没有"判, 抛异常和**返回** {"error"}/{"blocked"}
   都算失败 (见 _result_failure_kind; 2026-10-16 前只认抛异常, 该字段恒 True)
3. CC 预算硬: run_agent ≤ 11 (全局上限, 实测持平) / _execute_tool_call ≤ 6 /
   _llm_step ≤ 9 / _exit_reason ≤ 3 —— 判据在 tests/test_agent_complexity.py
4. 退出原因三分: stop (正常) / truncated (撞 max_tokens) / max_iters (耗尽)
   —— 见 _exit_reason

流程:
  agent.start
  for iter in range(max_iters):
      msg, finish = _llm_step(...)  # 真流式收流, 边收边打
      agent.iter / agent.llm_fallback?
      if _exit_reason(msg, finish): agent.done(stop|truncated); return
      for tc in msg.tool_calls:
          _execute_tool_call → agent.tool_call
  agent.done(max_iters); raise RuntimeError
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

import litellm

from core import model_config
from core.config import settings
from core.llm import complete
from core.mcp_client import MCPClient
from core.observability import log_event
from agent.toolconfig import mcp_servers
from agent.toolconfig import skills as skill_config
from agent.toolconfig import tools as tool_config
from agent.tools import get_all_tools
from agent.tools.base import get_tool_categories


# agent.error 的 phase 取值: 由调用点推进的游标, 不是从异常类型反猜的分类。
#
# ★ 2026-10-16 审计: 原 `_classify_error_phase` 判 `isinstance(e, litellm.APIError)`,
# 而 litellm.APIError **不是** litellm 具体异常的基类 (Timeout / RateLimitError /
# NotFoundError / InternalServerError / AuthenticationError / APIConnectionError
# 对它 issubclass 全 False, 它们只共有 openai.OpenAIError 这个祖先), 于是历史
# 日志里的 agent.error 的 phase 全是 unknown。原测试过, 是因为它直接构造
# litellm.APIError —— 真 provider 永不抛的那个类。
#
# 不改成"换个更大的基类"是因为真日志里还有 UnicodeEncodeError (GBK 控制台)、
# OSError、AttributeError (空流重组) —— 按异常类型永远猜不出阶段, 而代码知道
# 自己当时在跑哪一段。新增阶段就在循环里推进游标, 不必维护异常类型清单。
# 没有 "setup" 这一档: 工具装配和 messages 组装在 try **之外** (函数开头),
# 那里炸了压根不落 agent.error —— 那是另一个缺口, 不在本次范围内, 别在这里
# 放一个永不出现的取值假装覆盖了。
_PHASE_LLM_CALL = "llm_call"    # 调 LLM: 发请求、收流、stream_chunk_builder 重组
_PHASE_TOOL_EXEC = "tool_exec"  # 跑工具 (_execute_tool_call 自己兜异常, 这里只兜它兜不住的)
_PHASE_FINALIZE = "finalize"    # 收尾: 写回 conversation / emit done


def run_agent(query: str, max_iters: int | None = None, conversation=None,
              on_event: Callable[[dict], None] | None = None) -> str:
    """跑一次 agent 循环, 返回最终自然语言答案.

    Args:
        query: 本次任务 / 问题.
        max_iters: loop 轮数上限, None 用 settings.max_iters.
        conversation: 可选 core.conversation.Conversation. 传入则:
            - 启动: 把已有历史注入 messages (在 system 之后, 当前 query 之前),
              让 agent 看到此前的 /ask、普通对话、上次 /agent 的 Q&A.
            - 成功结束: 半进策略, 只写一对干净 Q&A (add_user(query)+add_assistant(answer)),
              中间的工具调用细节不进历史 (留口子: 以后要换"完整 tool 链"只动这里).
            - 任务失败 (max_iters 触顶 / LLM 异常): 不写历史, 不污染对话.
            None 时保持老行为 (不读不写, 单次无记忆任务).

    Raises:
        RuntimeError: max_iters 触顶
        litellm.APIError / 其他: LLM 调用失败 (agent.error 已落, re-raise 给 main)
    """
    schemas, impls = _filter_local(*get_all_tools())
    all_schemas, mcp_routes = _merge_mcp_tools(schemas)
    iters_cap = max_iters or settings.max_iters
    messages = _build_initial_messages(query, conversation)
    log_event("agent.start", {
        "query": query[:200],
        "model": model_config.active()["model"],
        "max_iters": iters_cap,
        "n_tools": len(all_schemas),
    })

    t0 = time.monotonic()
    current_iter = 0
    # phase 由**调用点**推进, 不从异常类型猜 (见 _PHASE_* 常量上方的说明)。
    # 初值 llm_call: try 块里第一件事就是调 LLM, 之前的异常根本到不了这个 except。
    phase = _PHASE_LLM_CALL

    try:
        for i in range(iters_cap):
            current_iter = i
            phase = _PHASE_LLM_CALL
            msg, finish = _llm_step(messages, all_schemas, i, on_event=on_event)
            msg, finish = _salvage_truncation(msg, finish, messages, all_schemas, i, on_event)

            reason = _exit_reason(msg, finish)
            if reason:
                phase = _PHASE_FINALIZE
                _log_done(i + 1, t0, reason)
                answer = msg.content or ""
                _write_back(conversation, query, answer)
                _emit(on_event, _done_event(reason))
                return answer

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [_serialize_tool_call(tc) for tc in (msg.tool_calls or [])],
            })
            phase = _PHASE_TOOL_EXEC
            for tc in msg.tool_calls:
                result_msg = _execute_tool_call(tc, impls, mcp_routes, iter_idx=i, on_event=on_event)
                messages.append(result_msg)

        _log_done(iters_cap, t0, "max_iters")
        _log_max_iters_signal(query, iters_cap, messages)
        raise RuntimeError(
            f"agent loop 超过 max_iters={iters_cap}. "
            f"请简化任务 / 给更具体的 query / 或调大 AGENT_MAX_ITERS 后重试."
        )
    except RuntimeError:
        raise
    except Exception as e:
        log_event("agent.error", {
            "phase": phase,
            "iter": current_iter,
            "error_type": type(e).__name__,
            "error_msg": str(e)[:200],
        })
        raise


def _build_skill_catalog() -> str:
    """把启用 skill 的 name+description 拼成 L1 清单文本 (每轮动态, 热部署命门)。

    无 skill → 空串。每次新 run_agent 重读 config/skills.json → 前端启停下一轮即生效。

    ⚠️ 焊死红线: 本函数结果只在 _build_initial_messages 现算拼接, **禁止写回**
       settings.system_prompt —— 否则首轮就把当时的清单焊死, 之后前端启停不生效。
    ⚠️ 兜底: skill 是增强能力, 坏了应"没有 skill"而非"任务崩"。任何异常吞掉返回
       空串 (discover 读磁盘可能抛), 绝不让异常冒泡到 run_agent (它在 try 块外)。
    """
    try:
        cat = skill_config.enabled_catalog()
    except Exception as e:
        log_event("agent.skill_catalog_error", {"error_type": type(e).__name__, "error_msg": str(e)[:100]})
        return ""
    if not cat:
        return ""
    lines = [f"- {s['name']}: {s['description']}" for s in cat]
    return "\n\n## 可用技能 (调 load_skill(name) 调出正文再据此行动)\n" + "\n".join(lines)


def _build_initial_messages(query: str, conversation) -> list[dict]:
    """组装首轮 messages: system[0] + 已有历史 + 当前 query.

    conversation=None → 老行为 (无历史, system + query 两条).
    传入 → 把此前的 /ask、普通对话、上次 /agent 的 Q&A 注入 (system 后, query 前),
    让 agent 跨任务可见上下文.

    截断按 settings.history_* 三项生效 (2026-10-16 接线): 此前这里调的是无参
    to_messages(), 于是 history_truncate_enabled / history_max_rounds /
    history_max_chars 三个配置项**从未被消费过** —— main.py 启动横幅照着打印
    "on", config.loaded 也记了, 但历史日志里的 history.truncated 全部来自
    pytest, 真实对话一次没触发。
    开关关掉时仍走无参调用, 保持"返回内部引用"的老语义 (见 to_messages docstring)。
    """
    if conversation is None:
        history = []
    elif settings.history_truncate_enabled:
        history = conversation.to_messages(
            max_rounds=settings.history_max_rounds,
            max_chars=settings.history_max_chars,
        )
    else:
        history = list(conversation.to_messages())
    return [
        {"role": "system", "content": settings.system_prompt + _build_skill_catalog()},
        *history,
        {"role": "user", "content": query},
    ]


def _write_back(conversation, query: str, answer: str) -> None:
    """半进策略: 任务成功后把一对干净 Q&A 写回 conversation (无 conversation 则 no-op).

    只写 user(原 query) + assistant(最终答案), 中间工具调用链不进历史 —— 防 context 膨胀.
    留口子: 以后要换"完整 tool 链记忆", 只改这一个函数 (extend 整条 messages 尾段).
    """
    if conversation is not None:
        conversation.add_user(query)
        conversation.add_assistant(answer)


def _emit(on_event, event: dict) -> None:
    """安全 emit: 回调抛异常绝不炸 agent loop (web 端队列满/断连保护)。"""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception as e:
        log_event("agent.emit_error", {"error_type": type(e).__name__, "error_msg": str(e)[:100]})


def _log_max_iters_signal(query: str, iters_cap: int, messages: list) -> None:
    """★ 自演化埋点: max_iters 触顶 → failure_signal。静默不抛。"""
    try:
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("max_iters", {
            "query": query[:200],
            "iters": iters_cap,
            "last_tool": messages[-1].get("name", "") if messages else "",
        })
    except Exception:
        pass


def _strip_tier_prefix(name: str) -> str:
    """剥掉中转平台的档位前缀, 得到用于比对的裸模型名。

    部分中转平台把同一模型按档位重复上架 (g-/s-/b-/highspeed- 等前缀),
    前缀只在请求侧存在, 回包的 resp.model 是裸名 ("g-glm-5.3" → "glm-5.3")。
    不剥前缀直接子串比对会永远匹配失败, 导致每轮误报 agent.llm_fallback。
    """
    for prefix in ("highspeed-", "local-", "free-", "g-", "s-", "b-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _llm_step(messages, schemas, iter_idx, on_event=None):
    """跑一次 LLM 调用 (真流式), 返回 (message, finish_reason).

    副作用:
    - print "[LLM] 思考中..." + 边收 content delta 边逐字打 (中间轮和末轮都打)
    - stream chunk 用 litellm.stream_chunk_builder 重组成与非流式同构的 resp
    - agent.iter event 落 JSONL (无 token 字段)
    - fallback 检测 (resp.model) 落 agent.llm_fallback
    - 流式异常落 llm.stream_error
    - on_event: 发送 llm_start / llm_chunk 事件 (SSE 流)
    """
    print("\n[LLM] 思考中...", end="", flush=True)
    _emit(on_event, {"type": "llm_start"})

    t0 = time.monotonic()
    chunks = []
    printed_any = False
    try:
        stream = complete(messages=messages, tools=schemas, stream=True)
        for chunk in stream:
            chunks.append(chunk)
            piece = getattr(chunk.choices[0].delta, "content", None)
            if piece:
                if not printed_any:
                    print()  # 收到首个文本增量, 离开"思考中"换行
                    printed_any = True
                print(piece, end="", flush=True)
                _emit(on_event, {"type": "llm_chunk", "chunk": piece})
    except Exception as e:
        log_event("llm.stream_error", {
            "iter": iter_idx, "error_type": type(e).__name__, "error_msg": str(e)[:200]})
        raise
    print()  # 无条件收尾换行: "[LLM] 思考中..." 这行必被终结, 无论本轮有无流式文字。
    #        纯 tool_calls 轮 (printed_any=False) 也要换行, 否则下一行 "[工具] 调用..."
    #        会拼上去; 有文字时恰好当正文末尾换行, 不多打空行 (统一两种情况)。
    latency = int((time.monotonic() - t0) * 1000)

    resp = litellm.stream_chunk_builder(chunks, messages=messages)
    msg = resp.choices[0].message
    finish = resp.choices[0].finish_reason

    actual = getattr(resp, "model", None)
    # 比对基准是**真正发出去的那个模型** (model_config.active), 不是 settings ——
    # 前端换模型只改 config/model.json, settings 还是启动期的旧值, 拿它比对会让
    # 换模型后每轮都误报 fallback, 把 observability 淹掉。
    # 期望值是 litellm 格式 "provider/model", 取模型名部分跟 resp.model 子串比较
    # (resp.model 回的是裸模型名, 不带 provider 前缀)。
    expected = model_config.active()["model"]
    expected_name = _strip_tier_prefix(expected.split("/")[-1])
    if actual and expected_name not in actual:
        log_event("agent.llm_fallback", {
            "iter": iter_idx,
            "primary": expected,
            "actual": actual,
            "llm_latency_ms": latency,
        })

    log_event("agent.iter", {
        "iter": iter_idx,
        "finish_reason": finish,
        "has_tool_calls": bool(msg.tool_calls),
        "n_tool_calls": len(msg.tool_calls or []),
        "llm_latency_ms": latency,
    })

    return msg, finish


def _exit_reason(msg, finish: str) -> str | None:
    """本轮该不该退出: 返回退出原因, None = 继续 loop. CC 预算 ≤3.

    两种退出都是"模型不再要工具了", 但必须分开记:
      - "stop"      正常收尾, 答案完整
      - "truncated" 撞 max_tokens 被切断 (finish_reason=length), 答案可能是半截或空

    ★ 2026-10-20: 原 `_should_exit` 收了 finish 参数却从不读它, 两者都记成 stop。
    后果不是日志不好看 —— CQ 跑批里有答案为空、error=None、exit_reason=stop 的条目,
    判分的人看不出是截断 (真实日志里每次 length 命中的 has_tool_calls 都是 False,
    即全都被静默当成了正常收尾)。
    """
    if msg.tool_calls:
        return None
    return "truncated" if finish == "length" else "stop"


_SALVAGE_NUDGE = (
    "你上一条回复超出了长度上限被截断, 用户什么都没看到。数据已经查齐, "
    "不要再逐条核数或展开推理, 直接用已有的工具结果写出最终回答: "
    "先给结论, 明细只列关键几条, 其余给汇总数。"
)


def _salvage_truncation(msg, finish, messages, schemas, iter_idx, on_event):
    """末轮被 max_tokens 截断 (无 tool_calls + finish=length) → 追加一条催成文的
    user 消息再要**一次**; 其余情况原样返回。CC 预算 ≤3。

    ★ 2026-10-21 CQ-13: 每条 SQL 都成功, 丢的是最后成文 —— 中转强制开 thinking
    (thinking/reasoning_effort 参数都被拒), 末轮 thinking 逐条枚举明细行把 token
    上限吃光, content 为空。调大上限只是把截断点往后推 (thinking 会跟着涨),
    所以在循环里补一次: 半截正文作为 assistant 带上 (空的不带, Anthropic 拒空
    assistant 消息), 再追加 _SALVAGE_NUDGE。只补一次: 第二次仍截断就照实记 truncated,
    不许洗成 stop。补救轮仍带 tools —— 历史里有 tool_use 块, 不带 tools 的请求会被拒。
    """
    if msg.tool_calls or finish != "length":
        return msg, finish
    log_event("agent.truncation_salvage", {
        "iter": iter_idx, "partial_len": len(msg.content or "")})
    if msg.content:
        messages.append({"role": "assistant", "content": msg.content})
    messages.append({"role": "user", "content": _SALVAGE_NUDGE})
    return _llm_step(messages, schemas, iter_idx, on_event=on_event)


def _allowed_by_profile(names):
    """按 deploy_profile 过滤工具名 (防线一: profile 门控, 不含 tools.json 启停)。

    serving profile 只留 category=serving 的工具; local profile 全留。
    映射里查不到 category 的工具 → 缺省 "local" (最小暴露: 拿不准就藏)。
    单独抽出是因为管理面板要"只过 profile、不过启停"—— 手动关掉的工具仍要显示。
    """
    if settings.deploy_profile != "serving":
        return set(names)
    cats = get_tool_categories()
    return {n for n in names if cats.get(n, "local") == "serving"}


def _filter_local(schemas, impls):
    """按 profile + tools.json 双重过滤**本地注册表的**工具 (叠加, 只能更严)。

    过滤顺序:
    1. profile 门控 (防线一): serving profile 只留 category=serving 的工具;
       local profile 全留。剔除的工具连 schema 都不给 LLM, 它调了也是未知工具。
    2. tools.json 启停 (原有): 手动关掉的再砍一刀。
    映射里查不到 category 的工具 → 缺省 "local" (最小暴露: 拿不准就藏)。

    ★ 函数名里的 local 指"本地注册表来源", 不是 profile 的 local 档位 ——
    它管不到 MCP 工具。"递给 LLM 的工具全集都过了双闸"这个命题, 要
    本函数 + `_merge_mcp_tools` 两边都成立才成立 (2026-10-16 前只有这边
    成立, 于是 serving 下 MCP 的 write_file 照样发出去)。
    """
    allowed_by_profile = _allowed_by_profile(impls)
    enabled = tool_config.filter_enabled(list(impls))
    keep = allowed_by_profile & enabled

    schemas = [s for s in schemas if s["function"]["name"] in keep]
    impls = {k: v for k, v in impls.items() if k in keep}
    return schemas, impls


def _servers_allowed_by_profile(servers: list[dict]) -> list[dict]:
    """按 deploy_profile 过滤 MCP server (防线一, 与本地工具同口径)。

    serving profile 只留 category="serving" 的 server; local 全留。
    配置里没写 category → 缺省 "local" (最小暴露: 拿不准就藏), 所以
    `_DEFAULT` 那条 notes-writer 在 serving 下自动不暴露, 不必动它的 enabled。

    ★ 过滤必须在起子进程**之前**: serving 的立意是最小暴露, "先 spawn 第三方
    进程再把 schema 丢掉"不算收敛。
    """
    if settings.deploy_profile != "serving":
        return list(servers)
    return [s for s in servers if s.get("category", "local") == "serving"]


def _merge_mcp_tools(schemas):
    """遍历放行的 MCP server, 各起一个 client 拉工具, 过双闸后合并 schema。

    返回 (all_schemas, mcp_routes)。mcp_routes: 工具名 → 该工具所属 server 的 client。
    一个放行的 server 都没有 → 不起任何子进程 (空短路)。

    ★ 2026-10-16: MCP 工具原先既不过 profile 门控也不过 tools.json ——
    `_filter_local` 的"双重过滤"只覆盖本地工具, 而递给 LLM 的是合并后的全集。
    后果: serving profile 下仍发出 write_file, 且 `tools.json` 关不掉它。
    现在两闸对两种工具来源一视同仁:
      闸一 (profile): server 级, 在 spawn 之前 (`_servers_allowed_by_profile`)
      闸二 (tools.json): 工具级, 拉到 schema 之后 (名字要起进程才知道)
    schema 和 mcp_routes **同一套判据**: 只过滤 schema 的话, LLM 凭提示词硬报
    工具名仍能调到 —— 不给 schema 只是"不告诉它", 不是"它做不到"。
    """
    all_schemas = list(schemas)
    mcp_routes: dict = {}
    for server in _servers_allowed_by_profile(mcp_servers.enabled_servers()):
        try:
            client, srv_schemas = _load_one_server_tools(server)
        except Exception as e:
            log_event("agent.mcp_server_skip", {"name": server.get("name"), "error": str(e)[:200]})
            continue
        srv_schemas = _mcp_schemas_enabled(srv_schemas, server)
        all_schemas.extend(srv_schemas)
        for s in srv_schemas:
            _name = s["function"]["name"]
            if _name in mcp_routes:
                log_event("agent.mcp_tool_name_conflict", {"tool": _name, "server": server.get("name")})
            mcp_routes[_name] = client
    return all_schemas, mcp_routes


def _mcp_schemas_enabled(srv_schemas: list[dict], server: dict) -> list[dict]:
    """闸二: 按 tools.json 砍掉手动关掉的 MCP 工具 (缺省全开, 同本地工具)。"""
    names = [s["function"]["name"] for s in srv_schemas]
    keep = tool_config.filter_enabled(names)
    dropped = [n for n in names if n not in keep]
    if dropped:
        log_event("agent.mcp_tool_disabled",
                  {"server": server.get("name"), "tools": dropped})
    return [s for s in srv_schemas if s["function"]["name"] in keep]


def _load_one_server_tools(server: dict):
    """起一个 MCPClient 拉它的工具 schema, 返回 (client, schemas)。"""
    client = MCPClient(command=server["command"], args=server["args"])
    return client, client.list_tools_openai()


def _dispatch_tool(name, args, impls, mcp_routes) -> str:
    """三分支路由: 幻觉→KeyError / 本地直调 / MCP 跨进程 (按 mcp_routes 找 client)。"""
    if name not in impls and name not in mcp_routes:
        raise KeyError(f"未知工具: {name}. 可用本地: {list(impls)}, MCP: {sorted(mcp_routes)}")
    if name in impls:
        return json.dumps(impls[name](**args), ensure_ascii=False)
    return mcp_routes[name].call_tool(name, args)


def _execute_tool_call(tc, impls, mcp_routes, iter_idx, on_event=None):
    """执行单个 tool_call, 返回 role=tool 的回填消息. 三分支路由委托 _dispatch_tool. 失败也回填."""
    name = tc.function.name
    args_raw = tc.function.arguments or ""
    args_summary = args_raw[:200]
    print(f"[工具] 调用 {name}({args_summary})")
    _emit(on_event, {"type": "tool_start", "name": name, "args": args_summary})

    t0 = time.monotonic()
    error_type = None
    try:
        content = _dispatch_tool(name, json.loads(args_raw), impls, mcp_routes)
        latency = int((time.monotonic() - t0) * 1000)
        error_type = _result_failure_kind(content)
        print(f"   -> {_summarize_content(content)} ({latency} ms)")
    except Exception as e:
        error_type = type(e).__name__
        content = json.dumps({"error": f"{error_type}: {e}"}, ensure_ascii=False)
        latency = int((time.monotonic() - t0) * 1000)
        print(f"   [失败] ({error_type}: {e}), 回填给 LLM 处理")

    success = error_type is None
    payload = {
        "iter": iter_idx,
        "tool_name": name,
        "args_summary": args_summary,
        "tool_latency_ms": latency,
        "success": success,
    }
    if error_type:
        payload["error_type"] = error_type
    log_event("agent.tool_call", payload)

    _emit(on_event, {
        "type": "tool_end", "name": name,
        "result": _summarize_content(content),
        "latency_ms": latency, "success": success,
        **_extract_used_edges(content),
        **_extract_obqc_violations(content),
    })
    return {"role": "tool", "tool_call_id": tc.id, "content": content}


def _result_failure_kind(content: str) -> str | None:
    """工具**返回值**里的失败形态 → error_type 标签; 成功返回 None。

    ★ 2026-10-16 审计: 本项目工具的约定是"失败也要包成合法 content 回填"
    (让 LLM 下一轮自我纠正), 所以绝大多数失败根本不抛异常 —— 只看 except 分支
    的话 success 恒 True。历史日志取证: success=False 极少, 而同期 sql.error 与
    obqc.hard_block 大量出现, 真流量里不少 sql.error 紧跟着 success=True。

    两种形态:
      {"error": ...}        工具自报失败 (sql.error / 实体不存在 / 非 SELECT ...)
      {"blocked": true}     OBQC 硬拦 (SQL 压根没跑, 不是"跑出了空结果")
    `"error": None` 不算失败 —— get_concept_join 等工具成功时也带这个键
    (`ontology_join.py:174` 无条件透传 `result.get("error")`), 按键存在判会全误判。
    解析不了的 content 不猜 (MCP 工具返回任意文本), 按成功记。
    """
    try:
        data = json.loads(content)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("blocked"):
        return "obqc_blocked"
    if data.get("error") is not None:
        return "tool_error"
    return None


def _summarize_content(content: str) -> str:
    """把 JSON 字符串结果转 dict 后交给 _summarize_result; 解析不了给空 dict 不炸."""
    try:
        return _summarize_result(json.loads(content))
    except Exception:
        return "返回非 dict"


def _extract_used_edges(content: str) -> dict:
    """从工具结果提取 _used_edges (自演化链路透传, spec §3.2)。

    只有 execute_sql 结果含此字段; 其余工具返回空 dict (tool_end 事件不多加字段)。
    """
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "_used_edges" in data:
            return {"used_edges": data["_used_edges"]}
    except Exception:
        pass
    return {}


def _extract_obqc_violations(content: str) -> dict:
    """从工具结果提取 blocked 响应里的 violations (自演化 spec §5.3)。

    execute_sql 被 OBQC 硬拦时返回 {blocked, violations, hints, skipped},
    摘要只留一行 → mismatch 累计需要 violations 原文, 同 used_edges 模式透传。
    """
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("blocked") and data.get("violations"):
            return {"violations": data["violations"]}
    except Exception:
        pass
    return {}


def _summarize_result(result) -> str:
    """根据 result 结构出一行摘要 (CLI 给用户看)."""
    if not isinstance(result, dict):
        return "返回非 dict"
    if "error" in result:
        return f"返回 error: {str(result['error'])[:50]}"
    if "hits" in result:
        return f"返回 {result.get('n', 0)} 个 chunk"
    if "items" in result:
        return f"返回 {result.get('n', 0)} 个 item"
    if "content" in result:
        suffix = ", 截断" if result.get("truncated") else ""
        return f"返回 {result.get('total_lines', '?')} 行{suffix}"
    return "返回 dict"


def _serialize_tool_call(tc) -> dict:
    """tool_call 对象 → OpenAI 风格 dict (回塞 messages 用)."""
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        },
    }


def _done_event(reason: str) -> dict:
    """done 事件载荷。truncated 只在截断时出现, 正常收尾不挂这个键。

    下游 (cq_runner / 前端 api.ts) 靠它把"模型被切断"与"模型真的答完了"分开 ——
    两者都表现为 error=None, 光看 answer 认不出来 (空答案尤其)。
    单独抽成函数而非在 run_agent 里写分支: run_agent CC 已持平全局上限 11。
    """
    ev = {"type": "done"}
    if reason == "truncated":
        ev["truncated"] = True
    return ev


def _log_done(n_iters, t0, exit_reason):
    log_event("agent.done", {
        "n_iters": n_iters,
        "total_latency_ms": int((time.monotonic() - t0) * 1000),
        "exit_reason": exit_reason,
    })
