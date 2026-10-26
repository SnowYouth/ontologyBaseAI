"""core/runtime/agent.py 单元测试 — FakeLLM 编排预设响应序列, 不真打 LiteLLM.

FakeLLM 设计:
- queue: list[dict], 每个 dict 是一轮 LLM 应答的"脚本"
- 每次 complete() pop 一个 dict, 构造伪 ModelResponse 返回
- 字段: content / tool_calls / finish_reason / model (检测 fallback)
"""
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from litellm.types.utils import (
    ModelResponseStream, StreamingChoices, Delta,
    ChatCompletionDeltaToolCall, Function,
)


@dataclass
class _FakeToolCall:
    id: str
    function: SimpleNamespace = field(default=None)

    @classmethod
    def make(cls, call_id: str, name: str, arguments: str):
        return cls(
            id=call_id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )


@dataclass
class _FakeChoice:
    message: SimpleNamespace
    finish_reason: str


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeResponse:
    choices: list
    usage: _FakeUsage
    model: str = "anthropic/glm-5.1"


class FakeLLM:
    """按 queue 顺序返回预设响应. complete() 被调用 N 次必须有 N 个脚本."""

    def __init__(self):
        self.queue: list[dict] = []
        self.calls: list[dict] = []

    def push(self, *, content="", tool_calls=None, finish="stop",
             in_tok=100, out_tok=30, model="anthropic/glm-5.1"):
        """tool_calls 格式: [(call_id, name, arguments_json_str), ...]"""
        self.queue.append({
            "content": content,
            "tool_calls": tool_calls or [],
            "finish": finish,
            "in_tok": in_tok,
            "out_tok": out_tok,
            "model": model,
        })

    def __call__(self, messages, tools=None, stream=False):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.queue:
            raise AssertionError("FakeLLM queue 空, 但 complete() 被调用")
        spec = self.queue.pop(0)
        if stream:
            return iter(self._make_chunks(spec))
        # 非流式兜底 (若有测试仍用)
        tc_objs = [_FakeToolCall.make(cid, name, args) for cid, name, args in spec["tool_calls"]]
        msg = SimpleNamespace(content=spec["content"], tool_calls=tc_objs if tc_objs else None)
        return _FakeResponse(
            choices=[_FakeChoice(message=msg, finish_reason=spec["finish"])],
            usage=_FakeUsage(prompt_tokens=spec["in_tok"], completion_tokens=spec["out_tok"]),
            model=spec["model"],
        )

    def _make_chunks(self, spec):
        """把一轮脚本转成 litellm stream chunk 序列 (供 stream_chunk_builder 重组).
        content 拆两片模拟逐字; tool_calls 各出一个 delta chunk; 末尾 finish chunk。"""
        model = spec["model"]
        chunks = []
        content = spec["content"] or ""
        if content:
            mid = max(1, len(content) // 2)
            for piece in (content[:mid], content[mid:]):
                chunks.append(ModelResponseStream(
                    choices=[StreamingChoices(index=0, delta=Delta(content=piece),
                                              finish_reason=None)], model=model))
        for idx, (cid, name, args) in enumerate(spec["tool_calls"]):
            tc = ChatCompletionDeltaToolCall(
                id=cid, index=idx, type="function",
                function=Function(name=name, arguments=args))
            chunks.append(ModelResponseStream(
                choices=[StreamingChoices(index=0, delta=Delta(tool_calls=[tc]),
                                          finish_reason=None)], model=model))
        chunks.append(ModelResponseStream(
            choices=[StreamingChoices(index=0, delta=Delta(), finish_reason=spec["finish"])],
            model=model))
        return chunks


@pytest.fixture
def fake_llm(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr("core.runtime.agent.complete", llm)
    return llm


@pytest.fixture
def pin_model(monkeypatch):
    """把 agent 眼里"当前该用的模型"钉死 (fallback 比对的基准)。

    agent 走 model_config.active(), 所以钉的是那一层 —— 不钉的话测试会读到
    机主真实的 config/model.json (conftest 已把目录隔到 tmp, 这里再钉值,
    双保险且让测试意图自明)。
    """
    def _pin(model_name: str):
        monkeypatch.setattr(
            "core.runtime.agent.model_config.active",
            lambda: {"name": "pinned", "model": model_name,
                     "api_key": "sk-test", "base_url": ""})
    return _pin


@pytest.fixture
def fake_tools(monkeypatch):
    """替换 tools.get_all_tools 返回固定工具集。

    两个 fake 工具标 serving, 避免 profile 门控 (缺省 serving) 把它们剔除 ——
    本 fixture 测的是 agent loop 行为, 不是门控; 门控单独在 TestProfileGating 测。
    """
    def echo_tool(text: str) -> dict:
        return {"echoed": text}
    def fail_tool(x: str) -> dict:
        raise RuntimeError(f"故意失败: {x}")
    schemas = [
        {"type": "function", "function": {"name": "echo_tool", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "fail_tool", "description": "d", "parameters": {}}},
    ]
    impls = {"echo_tool": echo_tool, "fail_tool": fail_tool}
    monkeypatch.setattr("core.runtime.agent.get_all_tools", lambda: (schemas, impls))
    monkeypatch.setattr("core.runtime.agent.get_tool_categories",
                        lambda: {"echo_tool": "serving", "fail_tool": "serving"})
    return schemas, impls


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """每个测试独立 JSONL 日志文件."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("OBS_LOG_DIR", str(log_dir))
    # observability 模块级 _LOG_FILE 单例需要重置
    import core.observability
    monkeypatch.setattr("core.observability._LOG_FILE", None)
    return log_dir


def _read_jsonl(log_dir):
    """读最新 jsonl 文件返回 event 列表."""
    files = sorted(log_dir.glob("run-*.jsonl"))
    if not files:
        return []
    return [json.loads(line) for line in files[-1].read_text(encoding="utf-8").splitlines()]


class TestAgentLoop:
    def test_1_zero_iter_直接答案(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(content="hello world", finish="stop")
        result = run_agent("你好")
        assert result == "hello world"
        events = _read_jsonl(isolated_log)
        names = [e["event"] for e in events]
        assert "agent.start" in names
        assert "agent.iter" in names
        assert "agent.done" in names
        # 找 done event, exit_reason=stop
        done = [e for e in events if e["event"] == "agent.done"][0]
        assert done["payload"]["exit_reason"] == "stop"
        assert done["payload"]["n_iters"] == 1

    def test_2_两轮_一次工具调用(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        # iter 0: 调 echo_tool
        fake_llm.push(
            content="先调工具",
            tool_calls=[("call_1", "echo_tool", '{"text": "hi"}')],
            finish="tool_calls",
        )
        # iter 1: 最终答案
        fake_llm.push(content="最终答案: hi", finish="stop")
        result = run_agent("说 hi")
        assert "hi" in result
        events = _read_jsonl(isolated_log)
        tool_calls = [e for e in events if e["event"] == "agent.tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["payload"]["tool_name"] == "echo_tool"
        assert tool_calls[0]["payload"]["success"] is True

    def test_3_工具失败回填给_LLM(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(
            content="调 fail",
            tool_calls=[("c1", "fail_tool", '{"x": "y"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="工具失败, 我给你 fallback 答案", finish="stop")
        result = run_agent("做某事")
        assert "fallback" in result
        events = _read_jsonl(isolated_log)
        tc = [e for e in events if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        assert "RuntimeError" in tc["payload"]["error_type"]

    def test_4_未知工具名(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(
            content="调不存在",
            tool_calls=[("c1", "web_search", '{"q": "x"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="工具不存在, 我直接答", finish="stop")
        result = run_agent("找东西")
        assert "直接答" in result
        events = _read_jsonl(isolated_log)
        tc = [e for e in events if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        assert tc["payload"]["error_type"] == "KeyError"

    def test_5_arguments_JSON_烂(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(
            content="调",
            tool_calls=[("c1", "echo_tool", '{broken json')],
            finish="tool_calls",
        )
        fake_llm.push(content="JSON 烂了", finish="stop")
        result = run_agent("调")
        events = _read_jsonl(isolated_log)
        tc = [e for e in events if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        # JSONDecodeError 或 ValueError 都接受
        assert tc["payload"]["error_type"] in ("JSONDecodeError", "ValueError")

    def test_6_max_iters_触顶(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        from core.runtime.agent import run_agent
        # 每轮都返工具调, 永不收敛
        for i in range(15):
            fake_llm.push(
                content=f"iter {i}",
                tool_calls=[(f"c{i}", "echo_tool", '{"text": "x"}')],
                finish="tool_calls",
            )
        with pytest.raises(RuntimeError, match="max_iters"):
            run_agent("死循环", max_iters=3)
        events = _read_jsonl(isolated_log)
        done = [e for e in events if e["event"] == "agent.done"][0]
        assert done["payload"]["exit_reason"] == "max_iters"
        # max_iters 触顶不重复 log agent.error
        errors = [e for e in events if e["event"] == "agent.error"]
        assert len(errors) == 0

    def test_7_finish_length_有_tool_calls_继续(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(
            content="被截断但要调工具",
            tool_calls=[("c1", "echo_tool", '{"text": "x"}')],
            finish="length",
        )
        fake_llm.push(content="OK", finish="stop")
        result = run_agent("test")
        assert result == "OK"
        events = _read_jsonl(isolated_log)
        iters = [e for e in events if e["event"] == "agent.iter"]
        assert iters[0]["payload"]["finish_reason"] == "length"

    def test_8_finish_length_无_tool_calls_退出但记_truncated(
            self, fake_llm, fake_tools, isolated_log):
        """截断退出: 答案照样返回, 但 exit_reason 必须是 truncated 而非 stop.

        ★ 本测试 2026-10-20 重写过。旧版断言 exit_reason == "stop", 把缺陷写进了
        前提 —— 真实后果: CQ 跑批里 CQ-14/CQ-18 答案为空却记成正常收尾,
        人按面板判分时无从知道这是截断 (见下方 test_8b 的真实日志形态)。
        """
        from core.runtime.agent import run_agent
        # 补救轮也截断 (2026-10-21 起截断先补救一次, 见 test_8f/8g)
        fake_llm.push(content="部分答案", finish="length")
        fake_llm.push(content="部分答案被截断", finish="length")
        result = run_agent("test")
        # 内容不丢: 截断的半截答案仍比空字符串有用
        assert result == "部分答案被截断"
        events = _read_jsonl(isolated_log)
        done = [e for e in events if e["event"] == "agent.done"][0]
        assert done["payload"]["exit_reason"] == "truncated", (
            "截断必须与正常结束可区分, 否则基于 exit_reason 的统计全部失真"
        )

    def test_8b_截断且内容为空_仍记_truncated(self, fake_llm, fake_tools, isolated_log):
        """CQ-14/CQ-18 的真实形态: finish=length 且 content 为空 → 空答案.

        真实日志 (logs/run-20260919-191126.jsonl run#12 iter8 / run#15 iter4):
        finish_reason=length 且 has_tool_calls=False。修前这种 run 记成
        exit_reason=stop, 空答案看不出任何异常。
        """
        from core.runtime.agent import run_agent
        fake_llm.push(content="", finish="length")
        fake_llm.push(content="", finish="length")  # 补救轮也截断
        result = run_agent("test")
        assert result == ""
        events = _read_jsonl(isolated_log)
        done = [e for e in events if e["event"] == "agent.done"][0]
        assert done["payload"]["exit_reason"] == "truncated"

    def test_8c_正常结束仍记_stop(self, fake_llm, fake_tools, isolated_log):
        """反证: 别把所有退出都标成 truncated (那样同样丢失区分度)."""
        from core.runtime.agent import run_agent
        fake_llm.push(content="完整答案", finish="stop")
        run_agent("test")
        events = _read_jsonl(isolated_log)
        done = [e for e in events if e["event"] == "agent.done"][0]
        assert done["payload"]["exit_reason"] == "stop"

    def test_8d_截断时_done_事件带_truncated_标记(self, fake_llm, fake_tools, isolated_log):
        """on_event 的 done 事件要带 truncated=True, 供 CQ runner / 前端识别.

        没有这个标记, cq_runner 只能看到空 answer 和 error=None, 无法把
        "模型截断" 与 "模型真的无话可说" 分开。
        """
        from core.runtime.agent import run_agent
        seen: list[dict] = []
        fake_llm.push(content="半截", finish="length")
        fake_llm.push(content="半截", finish="length")  # 补救轮也截断
        run_agent("test", on_event=seen.append)
        done = [e for e in seen if e.get("type") == "done"]
        assert len(done) == 1
        assert done[0].get("truncated") is True

    def test_8e_正常结束的_done_事件不带_truncated(self, fake_llm, fake_tools, isolated_log):
        """反证 test_8d: 正常收尾不许挂 truncated 标记 (前端会误报)."""
        from core.runtime.agent import run_agent
        seen: list[dict] = []
        fake_llm.push(content="完整答案", finish="stop")
        run_agent("test", on_event=seen.append)
        done = [e for e in seen if e.get("type") == "done"]
        assert len(done) == 1
        assert not done[0].get("truncated")

    def test_8f_截断后补救一次_拿到完整答案(self, fake_llm, fake_tools, isolated_log):
        """CQ-13 真实形态 (2026-10-21): 工具都跑完了, 末轮 thinking 逐条枚举明细
        把 max_tokens 吃光 → finish=length 且 content 空。调大上限不治本 (中转强制
        thinking 关不掉), 补救 = 追加一条"直接成文"的 user 消息再要一次。"""
        from core.runtime.agent import run_agent
        fake_llm.push(content="", finish="length")
        fake_llm.push(content="完整答案", finish="stop")
        result = run_agent("test")
        assert result == "完整答案"
        nudge = fake_llm.calls[1]["messages"][-1]
        assert nudge["role"] == "user" and "长度上限" in nudge["content"]
        events = _read_jsonl(isolated_log)
        assert [e for e in events if e["event"] == "agent.truncation_salvage"]
        done = [e for e in events if e["event"] == "agent.done"][0]
        assert done["payload"]["exit_reason"] == "stop"

    def test_8g_补救只做一次_再截断仍记_truncated(self, fake_llm, fake_tools, isolated_log):
        """反证: 补救不许无限循环, 也不许把第二次截断洗成 stop。"""
        from core.runtime.agent import run_agent
        seen: list[dict] = []
        fake_llm.push(content="", finish="length")
        fake_llm.push(content="又半截", finish="length")
        result = run_agent("test", on_event=seen.append)
        assert result == "又半截"
        assert len(fake_llm.calls) == 2
        done = [e for e in seen if e.get("type") == "done"]
        assert done[0].get("truncated") is True

    def test_8h_半截内容随补救回传_不丢(self, fake_llm, fake_tools, isolated_log):
        """截断时已有的半截正文作为 assistant 消息带上, 让模型接着写而不是从头想。"""
        from core.runtime.agent import run_agent
        fake_llm.push(content="前半段", finish="length")
        fake_llm.push(content="后半段", finish="stop")
        run_agent("test")
        msgs = fake_llm.calls[1]["messages"]
        assert msgs[-2] == {"role": "assistant", "content": "前半段"}

    def test_8i_正常结束不触发补救(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(content="完整答案", finish="stop")
        run_agent("test")
        assert len(fake_llm.calls) == 1

    def test_9_fallback_发生时落_agent_llm_fallback(self, fake_llm, fake_tools, isolated_log, pin_model):
        from core.runtime.agent import run_agent
        # 期望模型钉死, 不依赖 .env / 机主 model.json (开发者本地可能配成 deepseek 跑通中转,
        # 那样 push 的 deepseek/deepseek-chat 反而 == 期望值, 不触发 fallback, 测试假阴性).
        pin_model("anthropic/glm-5.3")
        # 让 resp.model 返回 fallback 模型, 触发 fallback 检测
        fake_llm.push(
            content="answer",
            finish="stop",
            model="deepseek/deepseek-chat",  # actual ≠ 期望的 glm-5.3
        )
        run_agent("test")
        events = _read_jsonl(isolated_log)
        fb = [e for e in events if e["event"] == "agent.llm_fallback"]
        assert len(fb) == 1
        assert fb[0]["payload"]["actual"] == "deepseek/deepseek-chat"

    def test_10_档位前缀模型不误判为_fallback(self, fake_llm, fake_tools, isolated_log, pin_model):
        """中转平台的档位前缀 (g-/s-/b-/highspeed-) 只存在于请求侧, 回包是裸模型名。

        如 g-glm-5.3 的 resp.model == "glm-5.3", 裸子串比对会匹配失败 → 每轮误报 fallback,
        污染 observability。剥离档位前缀后再比对。
        """
        from core.runtime.agent import run_agent

        pin_model("anthropic/g-glm-5.3")
        fake_llm.push(content="answer", finish="stop", model="glm-5.3")
        run_agent("test")
        events = _read_jsonl(isolated_log)
        fb = [e for e in events if e["event"] == "agent.llm_fallback"]
        assert fb == [], f"档位前缀模型被误判为 fallback: {fb}"

    def test_12_fallback_比对跟随_model_config_非_settings(
            self, fake_llm, fake_tools, isolated_log, monkeypatch, pin_model):
        """★ 比对基准必须是**真正发出去的那个模型** (model_config.active),
        不是 settings.primary_model。

        前端换模型只改 model.json, settings 仍是启动期的旧值 —— 拿 settings 比对
        会让换模型后每一轮都误报 agent.llm_fallback, 把 observability 淹掉。
        """
        from core.runtime.agent import run_agent

        monkeypatch.setattr("core.runtime.agent.settings.primary_model",
                            "anthropic/stale-from-env")
        pin_model("anthropic/live-model")          # 前端改过的新模型
        fake_llm.push(content="answer", finish="stop", model="live-model")
        run_agent("test")
        events = _read_jsonl(isolated_log)
        fb = [e for e in events if e["event"] == "agent.llm_fallback"]
        assert fb == [], f"回包与 active 模型一致却报了 fallback: {fb}"
        start = [e for e in events if e["event"] == "agent.start"][0]
        assert start["payload"]["model"] == "anthropic/live-model"

    def test_11_档位前缀模型真_fallback_仍能检出(self, fake_llm, fake_tools, isolated_log, pin_model):
        """剥前缀不能剥成"什么都匹配": 真换了模型家族仍要报。"""
        from core.runtime.agent import run_agent

        pin_model("anthropic/g-glm-5.3")
        fake_llm.push(content="answer", finish="stop", model="deepseek-v4-pro")
        run_agent("test")
        events = _read_jsonl(isolated_log)
        fb = [e for e in events if e["event"] == "agent.llm_fallback"]
        assert len(fb) == 1


class TestRunAgentSmoke:
    """run_agent 基础回归保险 (原 TestAgentStream 里的 smoke, 流式删除后保留)。"""

    def test_run_agent_smoke_unchanged(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent

        fake_llm.push(content="hello", finish="stop")
        assert run_agent("你好") == "hello"


class FakeMCPClient:
    """假 MCP client: 带参构造 (command/args 记下但忽略), list 报 write_file, call 记账。"""

    call_log: list = []

    def __init__(self, command="uv", args=None):
        self.command = command
        self.args = args

    def list_tools_openai(self):
        return [{"type": "function", "function": {
            "name": "write_file",
            "description": "write notes",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }}]

    def call_tool(self, name, args):
        FakeMCPClient.call_log.append((name, args))
        return f'{{"ok": true, "called": "{name}"}}'


@pytest.fixture(autouse=True)
def fake_toolconfig(monkeypatch, tmp_path):
    """所有 agent 测试默认: 本地全开 (空配置目录) + MCP 一条 enabled server 走 FakeMCPClient。

    autouse: 现有 test_1~6 原本半集成起真 MCP 子进程, 这里统一换假的 (提速 + 去外部依赖)。
    需要自定义 MCP 行为的测试 (TestAgentConfigFiltering) 再各自 monkeypatch 覆盖。

    ★ 钉 deploy_profile="local" (2026-10-16): MCP 工具开始过 profile 门控后,
    "MCP 工具能到达 LLM"这类用例的结果就取决于 profile —— 而 profile 缺省
    serving、机主 .env 写的是 local, 不钉死就是本机绿 / CI 红。门控本身在
    TestMcpToolGating 里各自显式设 profile。
    """
    import core.runtime.agent as agent_mod
    from agent.toolconfig import tools as tc_tools
    from agent.toolconfig import mcp_servers as tc_mcp
    monkeypatch.setattr(tc_tools, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tc_mcp, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(agent_mod.settings, "deploy_profile", "local")
    # 默认一条 enabled server, 让 _merge_mcp_tools 实例化一次 client
    monkeypatch.setattr("core.runtime.agent.mcp_servers.enabled_servers",
                        lambda: [{"name": "notes-writer", "command": "uv",
                                  "args": ["x"], "enabled": True}])
    # 默认把 MCPClient 换成假的 (TestAgentMCPRouting 已各自 setattr, 覆盖幂等无害)
    monkeypatch.setattr("core.runtime.agent.MCPClient", FakeMCPClient)
    FakeMCPClient.call_log = []
    return tmp_path


class TestAgentMCPRouting:
    """run_agent 三分支路由 — 阶段 4 MCP Task 3.

    本地工具之外, MCP 工具 (write_file) 也能被调到; 幻觉工具名走 KeyError 回填。
    每个测试自己 monkeypatch core.runtime.agent.MCPClient = FakeMCPClient (不 import 真 client)。
    """

    def test_mcp_工具_路由到_call_tool(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        FakeMCPClient.call_log = []
        monkeypatch.setattr("core.runtime.agent.MCPClient", FakeMCPClient)
        from core.runtime.agent import run_agent
        # iter 0: 调 MCP 工具 write_file
        fake_llm.push(
            content="写文件",
            tool_calls=[("c1", "write_file", '{"filename": "x.md", "content": "hi"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="完成", finish="stop")
        result = run_agent("写个文件")
        assert result == "完成"
        # FakeMCPClient.call_tool 被调用过, 且参数透传
        assert FakeMCPClient.call_log == [("write_file", {"filename": "x.md", "content": "hi"})]
        events = _read_jsonl(isolated_log)
        tc = [e for e in events if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["tool_name"] == "write_file"
        assert tc["payload"]["success"] is True

    def test_本地工具_路由不变(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        FakeMCPClient.call_log = []
        monkeypatch.setattr("core.runtime.agent.MCPClient", FakeMCPClient)
        from core.runtime.agent import run_agent
        fake_llm.push(
            content="调本地",
            tool_calls=[("c1", "echo_tool", '{"text": "hi"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="done", finish="stop")
        result = run_agent("说 hi")
        assert result == "done"
        # 本地路由: MCP call_tool 不被碰
        assert FakeMCPClient.call_log == []
        # echo_tool 结果回填进 messages (本地直调)
        events = _read_jsonl(isolated_log)
        tc = [e for e in events if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["tool_name"] == "echo_tool"
        assert tc["payload"]["success"] is True

    def test_幻觉工具名_KeyError_回填LLM(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        FakeMCPClient.call_log = []
        monkeypatch.setattr("core.runtime.agent.MCPClient", FakeMCPClient)
        from core.runtime.agent import run_agent
        fake_llm.push(
            content="调幻觉",
            tool_calls=[("c1", "ghost_tool", '{"x": "y"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="fallback", finish="stop")
        result = run_agent("找东西")
        assert result == "fallback"
        # 既不在本地也不在 MCP → KeyError 被 except 捕获回填
        assert FakeMCPClient.call_log == []
        events = _read_jsonl(isolated_log)
        tc = [e for e in events if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        assert tc["payload"]["error_type"] == "KeyError"


class TestAgentConversationMemory:
    """run_agent 接入 conversation 共享历史 (跨任务记忆, 半进策略).

    需求:
    - run_agent(query, conversation=conv): 启动读历史进 messages, 结束写一对干净 Q&A
    - 半进: 中间工具调用细节不进 conversation, 只留 user(query)+assistant(answer)
    - 不传 conversation 时零回归 (老行为, 不写任何历史)
    - 历史插在 system message 之后、当前 query 之前
    """

    def test_不传conversation_零回归(self, fake_llm, fake_tools, isolated_log):
        """老调用方式 (无 conversation) 行为完全不变。"""
        from core.runtime.agent import run_agent
        fake_llm.push(content="答案", finish="stop")
        result = run_agent("问题")
        assert result == "答案"
        # 第一次 LLM 调用只有 system + user 两条 (没有历史注入)
        first_call_msgs = fake_llm.calls[0]["messages"]
        assert len(first_call_msgs) == 2
        assert first_call_msgs[0]["role"] == "system"
        assert first_call_msgs[1]["role"] == "user"
        assert first_call_msgs[1]["content"] == "问题"

    def test_传conversation_启动读历史进messages(self, fake_llm, fake_tools, isolated_log):
        """conversation 有历史时, 第一次 LLM 调用 messages 含历史 (在 system 后, query 前)。"""
        from core.runtime.agent import run_agent
        from core.conversation import Conversation
        conv = Conversation()
        conv.add_user("之前问的问题")
        conv.add_assistant("之前的回答")
        fake_llm.push(content="基于上下文的答案", finish="stop")
        run_agent("后续问题", conversation=conv)
        msgs = fake_llm.calls[0]["messages"]
        # system + 2条历史 + 当前 query = 4
        assert len(msgs) == 4
        assert msgs[0]["role"] == "system"
        assert msgs[1] == {"role": "user", "content": "之前问的问题"}
        assert msgs[2] == {"role": "assistant", "content": "之前的回答"}
        assert msgs[3] == {"role": "user", "content": "后续问题"}

    def test_传conversation_结束写一对干净QA(self, fake_llm, fake_tools, isolated_log):
        """半进: 结束后 conversation 只多 user(原query)+assistant(最终答案)。"""
        from core.runtime.agent import run_agent
        from core.conversation import Conversation
        conv = Conversation()
        # 跑一个调工具的任务 (验证中间工具消息不进 conversation)
        fake_llm.push(
            content="先调工具",
            tool_calls=[("c1", "echo_tool", '{"text": "hi"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="最终答案", finish="stop")
        run_agent("做某事", conversation=conv)
        msgs = conv.to_messages()
        # 半进: 只有干净 Q&A 两条, 没有 tool_calls / role=tool 消息
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "做某事"}
        assert msgs[1] == {"role": "assistant", "content": "最终答案"}

    def test_传conversation_连续两次任务累积(self, fake_llm, fake_tools, isolated_log):
        """连续两次 run_agent 共享同一 conversation, 第二次能看到第一次结果。"""
        from core.runtime.agent import run_agent
        from core.conversation import Conversation
        conv = Conversation()
        fake_llm.push(content="第一次答案", finish="stop")
        run_agent("第一个任务", conversation=conv)
        fake_llm.push(content="第二次答案", finish="stop")
        run_agent("第二个任务", conversation=conv)
        # 第二次调用时, messages 应含第一次的 Q&A
        second_call_msgs = fake_llm.calls[1]["messages"]
        assert {"role": "user", "content": "第一个任务"} in second_call_msgs
        assert {"role": "assistant", "content": "第一次答案"} in second_call_msgs
        assert {"role": "user", "content": "第二个任务"} in second_call_msgs

    def test_传conversation_max_iters触顶不写历史(self, fake_llm, fake_tools, isolated_log):
        """任务失败 (max_iters 触顶 raise) 时不污染 conversation。"""
        from core.runtime.agent import run_agent
        from core.conversation import Conversation
        conv = Conversation()
        for i in range(5):
            fake_llm.push(
                content=f"iter {i}",
                tool_calls=[(f"c{i}", "echo_tool", '{"text": "x"}')],
                finish="tool_calls",
            )
        with pytest.raises(RuntimeError, match="max_iters"):
            run_agent("死循环", conversation=conv, max_iters=2)
        # 失败任务不留半截历史
        assert conv.to_messages() == []


class TestAgentLLMError:
    def test_LLM_异常_顶层_catch_落_agent_error(self, monkeypatch, fake_tools, isolated_log):
        """LLM 抛异常 → run_agent 落 agent.error + re-raise."""
        import litellm
        def broken(*a, **kw):
            raise litellm.APIError(message="boom", model="x", llm_provider="test", status_code=500)
        monkeypatch.setattr("core.runtime.agent.complete", broken)
        from core.runtime.agent import run_agent
        with pytest.raises(litellm.APIError):
            run_agent("test")
        events = _read_jsonl(isolated_log)
        err = [e for e in events if e["event"] == "agent.error"]
        assert len(err) == 1
        assert err[0]["payload"]["phase"] == "llm_call"
        assert any(e["event"] == "llm.stream_error" for e in events)


class TestAgentOnEvent:
    """run_agent on_event hook — SSE 流式中间步骤呈现的后端 emit。

    需求:
    - on_event 不传 (None) 时行为零回归 (现有全部测试即保证)
    - 传入时: 每轮 LLM emit llm_start + llm_chunk*, 每个工具调用 emit
      tool_start + tool_end, 结束 emit done
    - tool_start 带 name/args; tool_end 带 name/result/latency_ms/success
    - 答案文本走 llm_chunk 增量拼 (前端 api.ts 消费), 终结事件 done 不带 text
    """

    def test_on_event_收到工具与答案事件序列(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        events = []
        fake_llm.push(
            content="先调工具",
            tool_calls=[("c1", "echo_tool", '{"text": "hi"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="最终答案 hi", finish="stop")
        result = run_agent("说 hi", on_event=events.append)
        assert result == "最终答案 hi"
        types = [e["type"] for e in events]
        # 只钉关键事件的相对顺序, 不写死全序列 (llm_chunk 条数随分片变)
        assert types[0] == "llm_start"
        assert types[-1] == "done"
        assert types.index("tool_start") < types.index("tool_end") < len(types) - 1
        # 答案文本靠 llm_chunk 拼出来 (SSE 契约: done 不带 text)
        chunks = "".join(e["chunk"] for e in events if e["type"] == "llm_chunk")
        assert "最终答案 hi" in chunks
        assert events[-1] == {"type": "done"}
        start = [e for e in events if e["type"] == "tool_start"][0]
        assert start["name"] == "echo_tool"
        assert "hi" in start["args"]
        end = [e for e in events if e["type"] == "tool_end"][0]
        assert end["name"] == "echo_tool"
        assert end["success"] is True
        assert isinstance(end["latency_ms"], int)
        assert "echoed" in end["result"] or "返回" in end["result"]

    def test_on_event_工具失败带success_false(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        events = []
        fake_llm.push(
            content="调 fail",
            tool_calls=[("c1", "fail_tool", '{"x": "y"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="兜底", finish="stop")
        run_agent("做某事", on_event=events.append)
        end = [e for e in events if e["type"] == "tool_end"][0]
        assert end["success"] is False

    def test_on_event_回调抛异常不炸loop(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        def boom(_ev):
            raise RuntimeError("回调炸了")
        fake_llm.push(
            content="调工具",
            tool_calls=[("c1", "echo_tool", '{"text": "hi"}')],
            finish="tool_calls",
        )
        fake_llm.push(content="答案", finish="stop")
        # 回调每次都抛, agent 仍正常跑完返回
        result = run_agent("说 hi", on_event=boom)
        assert result == "答案"

    def test_on_event_none_零回归(self, fake_llm, fake_tools, isolated_log):
        from core.runtime.agent import run_agent
        fake_llm.push(content="hello", finish="stop")
        # 不传 on_event, 与老行为一致
        assert run_agent("你好") == "hello"


class TestAgentConfigFiltering:
    """配置驱动过滤: 关掉的本地工具不进 schema; 停掉的 MCP server 不起。

    fake_toolconfig 是 autouse, 自动注入 (隔离配置目录 + 默认假 MCP)。
    这里在其基础上覆盖需要的部分。
    """

    def test_关掉的本地工具不进schema(self, fake_llm, fake_tools, isolated_log):
        # 关掉 echo_tool (写进被 autouse 指到 tmp_path 的配置目录)
        from agent.toolconfig import tools as tc_tools
        tc_tools.set_enabled("echo_tool", False)
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        # agent.start 落的 n_tools 不含被关工具 (fail_tool + write_file = 2, echo_tool 被排除)
        events = _read_jsonl(isolated_log)
        start = [e for e in events if e["event"] == "agent.start"][0]
        assert start["payload"]["n_tools"] == 2

    def test_无enabled_server时不起子进程(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        monkeypatch.setattr("core.runtime.agent.mcp_servers.enabled_servers", lambda: [])
        called = {"n": 0}
        class Boom:
            def __init__(self, **k): called["n"] += 1
        monkeypatch.setattr("core.runtime.agent.MCPClient", Boom)
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        assert called["n"] == 0   # 一个 enabled server 都没有 → MCPClient 从不实例化

    def test_坏的enabled_server不崩掉整轮_落mcp_server_skip(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """一台 enabled server 构造即抛异常 → 该 server 被跳过, run_agent 正常完成, 落 agent.mcp_server_skip."""
        monkeypatch.setattr("core.runtime.agent.mcp_servers.enabled_servers",
                             lambda: [{"name": "bad-server", "command": "not-exist",
                                       "args": [], "enabled": True}])

        class Boom:
            def __init__(self, **k):
                raise RuntimeError("子进程起不来")

        monkeypatch.setattr("core.runtime.agent.MCPClient", Boom)
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        result = run_agent("你好")
        assert result == "hi"
        events = _read_jsonl(isolated_log)
        skip_events = [e for e in events if e["event"] == "agent.mcp_server_skip"]
        assert len(skip_events) == 1
        assert skip_events[0]["payload"]["name"] == "bad-server"
        assert "子进程起不来" in skip_events[0]["payload"]["error"]
        # agent.error 不应被触发 (异常在 try 块之前的老代码里会逃逸到这里; 修复后不应有)
        assert [e for e in events if e["event"] == "agent.error"] == []

    def test_多server同名工具覆盖时落mcp_tool_name_conflict(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """两台 enabled server 都暴露 write_file → 后一台覆盖前一台时落 agent.mcp_tool_name_conflict."""
        monkeypatch.setattr("core.runtime.agent.mcp_servers.enabled_servers",
                             lambda: [
                                 {"name": "server-a", "command": "uv", "args": [], "enabled": True},
                                 {"name": "server-b", "command": "uv", "args": [], "enabled": True},
                             ])
        monkeypatch.setattr("core.runtime.agent.MCPClient", FakeMCPClient)
        FakeMCPClient.call_log = []
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        events = _read_jsonl(isolated_log)
        conflict_events = [e for e in events if e["event"] == "agent.mcp_tool_name_conflict"]
        assert len(conflict_events) == 1


class TestProfileGating:
    """_filter_local 的 profile 门控: serving 只留 serving 工具, local 全留。"""

    def _tools(self):
        def rf(path: str) -> dict:
            return {}
        def rq(query: str) -> dict:
            return {}
        schemas = [
            {"type": "function", "function": {"name": "read_file", "description": "d", "parameters": {}}},
            {"type": "function", "function": {"name": "rag_query", "description": "d", "parameters": {}}},
        ]
        impls = {"read_file": rf, "rag_query": rq}
        cats = {"read_file": "local", "rag_query": "serving"}
        return schemas, impls, cats

    def test_serving_只留serving工具(self, monkeypatch):
        import core.runtime.agent as agent
        schemas, impls, cats = self._tools()
        monkeypatch.setattr("core.runtime.agent.get_tool_categories", lambda: cats)
        monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
        out_schemas, out_impls = agent._filter_local(schemas, impls)
        assert set(out_impls) == {"rag_query"}
        assert [s["function"]["name"] for s in out_schemas] == ["rag_query"]

    def test_local_全留(self, monkeypatch):
        import core.runtime.agent as agent
        schemas, impls, cats = self._tools()
        monkeypatch.setattr("core.runtime.agent.get_tool_categories", lambda: cats)
        monkeypatch.setattr(agent.settings, "deploy_profile", "local")
        out_schemas, out_impls = agent._filter_local(schemas, impls)
        assert set(out_impls) == {"read_file", "rag_query"}

    def test_serving_叠加toolsjson再关掉一个(self, monkeypatch, tmp_path):
        # local profile 下仍可用 tools.json 单独关工具 (两过滤叠加)
        import core.runtime.agent as agent
        from agent.toolconfig import tools as tc
        monkeypatch.setattr(tc, "_CONFIG_DIR", tmp_path)
        tc.set_enabled("read_file", False)          # tools.json 关掉 read_file
        schemas, impls, cats = self._tools()
        monkeypatch.setattr("core.runtime.agent.get_tool_categories", lambda: cats)
        monkeypatch.setattr(agent.settings, "deploy_profile", "local")
        out_schemas, out_impls = agent._filter_local(schemas, impls)
        assert set(out_impls) == {"rag_query"}      # local 全留, 但 tools.json 又砍了 read_file


class TestMcpToolGating:
    """★ MCP 工具也必须过 profile + tools.json 双闸 (2026-10-16 审计第 2 条)。

    背景: `_filter_local` 的 docstring 写着"profile + tools.json 双重过滤, 只能
    更严" —— 这句对**本地工具**成立, 对**递给 LLM 的工具全集**不成立。
    `_merge_mcp_tools` 原先直接 `all_schemas.extend(srv_schemas)`, 既不查
    category 也不查 tools.json, `mcp_routes` 同一循环里建好 → `_dispatch_tool`
    照样放行。而 `mcp_servers._DEFAULT` 是 enabled=True 且缺文件自动落盘,
    于是一个新 clone 零配置就会把 write_file 发给 LLM, 而 `.env.example`
    向使用者承诺的是 serving 下"攻击者无法透过 agent 读源码"。

    ★ 断言下在**管道末端** (真正递给 complete() 的那个 tools 参数), 不在
    `_merge_mcp_tools` 这个函数上 —— 这个洞能活下来正是因为老测试钉的是
    `_filter_local` 这个函数, 没有一条断言"LLM 实际收到了什么"。
    [[memory: 单测的边界就是它 mock 的那一层]]
    """

    def _servers(self, monkeypatch, *servers):
        monkeypatch.setattr("core.runtime.agent.mcp_servers.enabled_servers",
                            lambda: list(servers))
        monkeypatch.setattr("core.runtime.agent.MCPClient", FakeMCPClient)
        FakeMCPClient.call_log = []

    def _srv(self, name="notes-writer", **kw):
        return {"name": name, "command": "uv", "args": ["x"], "enabled": True, **kw}

    def _tool_names_sent_to_llm(self, fake_llm):
        """LLM 真收到的工具名集合 (管道末端: complete(tools=...) 的实参)。"""
        return {t["function"]["name"] for t in fake_llm.calls[0]["tools"]}

    def test_serving下未标serving的server不暴露工具(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """★ 核心: serving profile + server 无 category → write_file 到不了 LLM。

        缺省 category="local" 与本地工具同口径 (拿不准就藏)。
        """
        import core.runtime.agent as agent
        monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
        self._servers(monkeypatch, self._srv())
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        assert "write_file" not in self._tool_names_sent_to_llm(fake_llm)

    def test_serving下不起未放行server的子进程(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """最小暴露: 过滤要发生在起子进程**之前**, 不是拉到 schema 再丢掉。

        先起进程再过滤同样能让断言变绿, 但那等于 serving 下仍 spawn 第三方
        进程 —— 真正要收敛的东西没收敛。
        """
        import core.runtime.agent as agent
        monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
        monkeypatch.setattr("core.runtime.agent.mcp_servers.enabled_servers",
                            lambda: [self._srv()])
        spawned = {"n": 0}

        class CountingClient(FakeMCPClient):
            def __init__(self, **kw):
                spawned["n"] += 1
                super().__init__(**kw)

        monkeypatch.setattr("core.runtime.agent.MCPClient", CountingClient)
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        assert spawned["n"] == 0, "serving 下仍起了未放行 server 的子进程"

    def test_serving下显式标serving的server仍可用(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """门控不是"serving 下一律禁 MCP": 显式标 serving 的仍放行。

        否则这就不是门控而是开关, 使用者在 serving 下接外部 MCP 没有途径。
        """
        import core.runtime.agent as agent
        monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
        self._servers(monkeypatch, self._srv(category="serving"))
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        assert "write_file" in self._tool_names_sent_to_llm(fake_llm)

    def test_local下照旧全放行(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """★ 零回归: 机主本机就是 local profile, 行为必须和改动前一模一样。"""
        import core.runtime.agent as agent
        monkeypatch.setattr(agent.settings, "deploy_profile", "local")
        self._servers(monkeypatch, self._srv())
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        assert "write_file" in self._tool_names_sent_to_llm(fake_llm)

    def test_toolsjson关掉的mcp工具不进schema(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """tools.json 对 MCP 工具同样生效 (原先只管本地工具)。"""
        from agent.toolconfig import tools as tc_tools
        tc_tools.set_enabled("write_file", False)
        self._servers(monkeypatch, self._srv())
        from core.runtime.agent import run_agent
        fake_llm.push(content="hi", finish="stop")
        run_agent("你好")
        assert "write_file" not in self._tool_names_sent_to_llm(fake_llm)

    def _llm_hallucinates_write_file(self, fake_llm):
        fake_llm.push(content="偷偷调", finish="tool_calls",
                      tool_calls=[("c1", "write_file",
                                   '{"filename": "x.md", "content": "hi"}')])
        fake_llm.push(content="完成", finish="stop")

    def test_profile门控掉的mcp工具调不到(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """★ 不止不给 schema: 路由表里也不许有。

        只过滤 schema 不过滤 mcp_routes 的话, LLM 凭记忆/提示词硬报这个名字
        仍能调到 —— 不给 schema 只是"不告诉它", 不是"它做不到"。
        """
        import core.runtime.agent as agent
        monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
        self._servers(monkeypatch, self._srv())
        from core.runtime.agent import run_agent
        self._llm_hallucinates_write_file(fake_llm)
        run_agent("写个文件")
        assert FakeMCPClient.call_log == [], "被门控掉的 MCP 工具仍被真正执行了"

    def test_toolsjson关掉的mcp工具也调不到(
            self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """★ 两个闸都要管到 routes, 不止 schema。

        上一条走的是 profile 闸 —— 它在 spawn 之前就把整个 server 挡了,
        routes 自然是空的, 所以它**钉不住** tools.json 这一层。
        (变异反证: 把 tools.json 过滤只作用于 all_schemas 而 routes 用原始
        列表, 上一条照样绿, 只有这一条会红。)
        """
        from agent.toolconfig import tools as tc_tools
        tc_tools.set_enabled("write_file", False)
        self._servers(monkeypatch, self._srv())
        from core.runtime.agent import run_agent
        self._llm_hallucinates_write_file(fake_llm)
        run_agent("写个文件")
        assert FakeMCPClient.call_log == [], \
            "tools.json 关掉的 MCP 工具只是没进 schema, 仍能被调到"

    def test_默认server缺省不在serving暴露(self, monkeypatch, tmp_path):
        """★ 内置默认那条 (enabled=True, 缺文件自动落盘) 在 serving 下不放行。

        这是"新 clone 零配置"的那条路径: 使用者没写过任何配置, 不该凭空
        获得一个发给 LLM 的写文件工具。
        """
        import core.runtime.agent as agent
        from agent.toolconfig import mcp_servers as tc_mcp
        monkeypatch.setattr(tc_mcp, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
        assert agent._servers_allowed_by_profile(tc_mcp.enabled_servers()) == []


class TestAgentHistoryTruncation:
    """history_* 三项配置真被消费 (2026-10-16 接线)。

    ★ 背景: _build_initial_messages 原先调无参 to_messages(), 三个配置项
    定义在 core/config.py、被启动横幅打印、被 config.loaded 记录, 但生产侧
    零消费 —— 翻过全部历史日志, 带 history.truncated 的那些全部出自 pytest。
    这几条钉的就是"配置到行为"这一段, 缺了它改回无参调用不会有测试变红。
    """

    def _conv_with_rounds(self, n: int):
        from core.conversation import Conversation
        conv = Conversation()
        for i in range(n):
            conv.add_user(f"q{i}")
            conv.add_assistant(f"a{i}")
        return conv

    def test_max_rounds生效_老历史被砍掉(self, fake_llm, fake_tools,
                                        isolated_log, monkeypatch):
        from core.config import settings
        from core.runtime.agent import run_agent
        monkeypatch.setattr(settings, "history_truncate_enabled", True)
        monkeypatch.setattr(settings, "history_max_rounds", 2)
        monkeypatch.setattr(settings, "history_max_chars", 100000)

        conv = self._conv_with_rounds(10)
        fake_llm.push(content="答案", finish="stop")
        run_agent("新问题", conversation=conv)

        sent = fake_llm.calls[0]["messages"]
        assert {"role": "user", "content": "q0"} not in sent, "最老的轮次没被砍"
        assert {"role": "user", "content": "q9"} in sent, "最近的轮次不该丢"
        assert {"role": "user", "content": "新问题"} in sent
        # system + 2 轮 (4 条) + 当前 query
        assert len(sent) == 6, sent

    def test_max_chars生效(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        from core.config import settings
        from core.runtime.agent import run_agent
        monkeypatch.setattr(settings, "history_truncate_enabled", True)
        monkeypatch.setattr(settings, "history_max_rounds", 200)
        monkeypatch.setattr(settings, "history_max_chars", 1000)

        from core.conversation import Conversation
        conv = Conversation()
        for i in range(10):
            conv.add_user("x" * 300)
            conv.add_assistant("y" * 300)
        fake_llm.push(content="答案", finish="stop")
        run_agent("新问题", conversation=conv)

        sent = fake_llm.calls[0]["messages"]
        hist = [m for m in sent
                if m["role"] != "system" and m["content"] != "新问题"]
        assert sum(len(m["content"]) for m in hist) <= 1000, "chars 上限没生效"
        assert hist, "不该把历史砍空"

    def test_开关关掉则不截断(self, fake_llm, fake_tools, isolated_log, monkeypatch):
        """★ 反证: 关掉开关必须退回全量历史, 否则"生效"是假的。"""
        from core.config import settings
        from core.runtime.agent import run_agent
        monkeypatch.setattr(settings, "history_truncate_enabled", False)
        monkeypatch.setattr(settings, "history_max_rounds", 2)

        conv = self._conv_with_rounds(10)
        fake_llm.push(content="答案", finish="stop")
        run_agent("新问题", conversation=conv)

        sent = fake_llm.calls[0]["messages"]
        assert {"role": "user", "content": "q0"} in sent, "关掉开关却仍截断了"
        assert len(sent) == 22, sent          # system + 20 条 + query

    def test_截断不反向污染conversation(self, fake_llm, fake_tools,
                                        isolated_log, monkeypatch):
        """★ 带参 to_messages 返回拷贝, 砍掉的历史不许从 conversation 里真消失。

        无参调用返回的是内部 list 引用 (见 to_messages docstring), 接线时若图
        省事原地切片, 用户的会话历史会被静默吃掉 —— 磁盘上的 session 也跟着少。
        """
        from core.config import settings
        from core.runtime.agent import run_agent
        monkeypatch.setattr(settings, "history_truncate_enabled", True)
        monkeypatch.setattr(settings, "history_max_rounds", 2)
        monkeypatch.setattr(settings, "history_max_chars", 100000)

        conv = self._conv_with_rounds(10)
        fake_llm.push(content="答案", finish="stop")
        run_agent("新问题", conversation=conv)

        # 原历史 20 条仍在 + 本轮新增一对 Q&A
        assert len(conv.to_messages()) == 22
        assert {"role": "user", "content": "q0"} in conv.to_messages()


class TestToolCallSuccessSemantics:
    """`agent.tool_call.success` 要反映"工具这次干成了没有", 不是"有没有抛异常"。

    ★ 2026-10-16 审计第 8 条: 翻遍历史日志, success=True 压倒性多数而 False 极
    罕见, 同期 sql.error / obqc.hard_block 都数量可观; 真流量里不少 sql.error 和
    obqc.hard_block **紧跟着 success=True**。根因是本项目工具的约定是把失败当
    **返回值** ({"error": ...} / {"blocked": true}) 回填给 LLM 自我纠正
    (见 guides/LLM-工具调用与Agent协议 "失败也必须回填"), 而 _execute_tool_call
    只在 except 分支翻 False。于是 `agent.py:5` 那条"错误路径必落
    tool_call success=false"的硬约定, 对最常见的失败形态是假的。

    断言全部下在**最终出口**上 (JSONL 载荷 / on_event 事件), 不测判定函数的
    返回值 —— 上一轮 MCP 双闸的洞正是因为只钉了过滤函数内部 (见
    [[declared-control-scope-audit]])。
    """

    def _fake_tools_returning(self, monkeypatch, result: dict):
        """装一个"不抛异常、但返回失败结构"的工具 (本项目工具的真实形态)。"""
        def soft_fail_tool() -> dict:
            return result
        schemas = [{"type": "function",
                    "function": {"name": "soft_fail_tool", "description": "d",
                                 "parameters": {}}}]
        impls = {"soft_fail_tool": soft_fail_tool}
        monkeypatch.setattr("core.runtime.agent.get_all_tools", lambda: (schemas, impls))
        monkeypatch.setattr("core.runtime.agent.get_tool_categories",
                            lambda: {"soft_fail_tool": "serving"})

    def _run_once(self, fake_llm):
        fake_llm.push(content="调工具",
                      tool_calls=[("c1", "soft_fail_tool", "{}")],
                      finish="tool_calls")
        fake_llm.push(content="收到", finish="stop")

    def test_工具返回error键_success为false(self, fake_llm, isolated_log, monkeypatch):
        """sql.error / 各工具 {"error": ...} 这条路径 —— 占真实失败的大头。"""
        from core.runtime.agent import run_agent
        self._fake_tools_returning(monkeypatch, {"error": "ProgrammingError: no such table"})
        self._run_once(fake_llm)
        run_agent("查点东西")
        tc = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        assert tc["payload"]["error_type"] == "tool_error"

    def test_OBQC硬拦_success为false(self, fake_llm, isolated_log, monkeypatch):
        """execute_sql 被 OBQC 硬拦返回 {"blocked": true} —— SQL 根本没跑。"""
        from core.runtime.agent import run_agent
        self._fake_tools_returning(monkeypatch, {
            "blocked": True,
            "violations": [{"kind": "no_edge", "detail": "x"}],
            "hints": ["换条路"], "skipped": [],
        })
        self._run_once(fake_llm)
        run_agent("跨表查")
        tc = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        assert tc["payload"]["error_type"] == "obqc_blocked"

    def test_成功结果仍为true(self, fake_llm, isolated_log, monkeypatch):
        """★ 反证: 不许一刀切判死, 正常结果必须还是 True 且不带 error_type。"""
        from core.runtime.agent import run_agent
        self._fake_tools_returning(monkeypatch, {"rows": [{"a": 1}], "n": 1})
        self._run_once(fake_llm)
        run_agent("正常查")
        tc = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is True
        assert "error_type" not in tc["payload"]

    def test_error键为None不算失败(self, fake_llm, isolated_log, monkeypatch):
        """★ 边界: ontology_join 成功时也带 "error": None 键 —— 按键存在判会全误判。"""
        from core.runtime.agent import run_agent
        self._fake_tools_returning(monkeypatch, {"error": None, "hops": 2})
        self._run_once(fake_llm)
        run_agent("找路径")
        tc = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is True

    def test_抛异常仍记原异常名(self, fake_llm, fake_tools, isolated_log):
        """★ 回归: 真抛异常那条老路径的 error_type 不许被新判据顶掉。"""
        from core.runtime.agent import run_agent
        fake_llm.push(content="调 fail",
                      tool_calls=[("c1", "fail_tool", '{"x": "y"}')],
                      finish="tool_calls")
        fake_llm.push(content="算了", finish="stop")
        run_agent("做某事")
        tc = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.tool_call"][0]
        assert tc["payload"]["success"] is False
        assert tc["payload"]["error_type"] == "RuntimeError"

    def test_前端事件与日志同口径(self, fake_llm, isolated_log, monkeypatch):
        """两个出口不许各说各话: on_event 的 success 也要跟着翻。

        前端 (agentStream.ts: ok=ev.success) 和 CQ 跑批 (cq_runner:126 t["ok"])
        都消费这个字段, 只修 JSONL 等于只修了给我看的那一半。
        """
        from core.runtime.agent import run_agent
        self._fake_tools_returning(monkeypatch, {"error": "boom"})
        self._run_once(fake_llm)
        events = []
        run_agent("查点东西", on_event=events.append)
        end = [e for e in events if e["type"] == "tool_end"][0]
        assert end["success"] is False


class TestErrorPhaseClassification:
    """`agent.error.phase` 要说清错在哪一步, 不许恒 unknown。

    ★ 2026-10-16 审计第 8 条: 历史日志里的 agent.error **全部** phase=unknown。
    根因是 `litellm.APIError` 不是 litellm 具体异常的基类 (实测
    Timeout/RateLimitError/NotFoundError/InternalServerError/AuthenticationError/
    APIConnectionError 对它 issubclass 全 False), 所以 isinstance 判据永不命中。
    原测试能过, 只因为它直接构造 `litellm.APIError` —— 真 provider 不抛那个类。

    修法改成**由调用点报阶段**而不是从异常类型猜: 真日志里还有
    UnicodeEncodeError (GBK 控制台) / OSError / AttributeError (空流),
    这些按类型永远猜不出阶段, 而代码知道自己当时在跑哪一段。
    """

    def _broken_complete(self, monkeypatch, exc):
        def broken(*a, **kw):
            raise exc
        monkeypatch.setattr("core.runtime.agent.complete", broken)

    @pytest.mark.parametrize("make_exc", [
        lambda: __import__("litellm").Timeout(message="m", model="x", llm_provider="p"),
        lambda: __import__("litellm").RateLimitError(message="m", llm_provider="p", model="x"),
        lambda: __import__("litellm").NotFoundError(message="m", model="x", llm_provider="p"),
        lambda: __import__("litellm").InternalServerError(message="m", llm_provider="p", model="x"),
        lambda: __import__("litellm").AuthenticationError(message="m", llm_provider="p", model="x"),
        lambda: __import__("litellm.exceptions", fromlist=["MidStreamFallbackError"])
                .MidStreamFallbackError(message="m", model="x", llm_provider="p",
                                        original_exception=RuntimeError("x")),
    ], ids=["Timeout", "RateLimit", "NotFound", "InternalServer", "Auth", "MidStreamFallback"])
    def test_真provider异常归到llm_call(self, monkeypatch, fake_tools, isolated_log, make_exc):
        """这六个是真日志里出现过/provider 真会抛的类型, 一个都不许落 unknown。"""
        from core.runtime.agent import run_agent
        exc = make_exc()
        self._broken_complete(monkeypatch, exc)
        with pytest.raises(type(exc)):
            run_agent("test")
        err = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.error"]
        assert len(err) == 1
        assert err[0]["payload"]["phase"] == "llm_call", \
            f"{type(exc).__name__} 落到了 {err[0]['payload']['phase']}"

    def test_非provider异常也归llm_call(self, monkeypatch, fake_tools, isolated_log):
        """UnicodeEncodeError (GBK 控制台打字时炸) 真杀过几个 run —— 它也错在 LLM 这一步。

        按异常类型猜永远猜不到; 由调用点报阶段才能对。
        """
        from core.runtime.agent import run_agent
        self._broken_complete(monkeypatch, UnicodeEncodeError("gbk", "x", 0, 1, "illegal"))
        with pytest.raises(UnicodeEncodeError):
            run_agent("test")
        err = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.error"][0]
        assert err["payload"]["phase"] == "llm_call"

    def test_空流AttributeError归llm_call(self, monkeypatch, fake_tools, isolated_log):
        """stream_chunk_builder([]) 返回 None → .choices 炸 (审计第 13 条)。

        它发生在收流之后、重组阶段, 仍属 llm_call; 真日志里有两次。
        """
        from core.runtime.agent import run_agent
        monkeypatch.setattr("core.runtime.agent.complete", lambda *a, **kw: iter([]))
        with pytest.raises(Exception):
            run_agent("test")
        err = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.error"][0]
        assert err["payload"]["phase"] == "llm_call"

    def test_工具层之外的异常不冒认llm_call(self, fake_llm, fake_tools,
                                            isolated_log, monkeypatch):
        """★ 反证: 不许让 phase 变成另一个常量。

        工具回填后、下一轮 LLM 之前写回 conversation 时炸 → 不是 llm_call。
        """
        from core.runtime.agent import run_agent

        class Boom:
            def to_messages(self, **kw):
                return []
            def add_user(self, *a):
                raise ValueError("写回炸了")
            def add_assistant(self, *a):
                pass

        fake_llm.push(content="答案", finish="stop")
        with pytest.raises(ValueError):
            run_agent("test", conversation=Boom())
        err = [e for e in _read_jsonl(isolated_log) if e["event"] == "agent.error"][0]
        assert err["payload"]["phase"] != "llm_call"
        assert err["payload"]["error_type"] == "ValueError"
