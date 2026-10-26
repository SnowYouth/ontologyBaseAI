// agent SSE 事件 → agent 消息的纯 reducer 测试。
//
// 背景 (真 bug): 后端 core/agent.py 在 SSE 修复轮改成发 llm_start/llm_chunk/done,
// 不再发 answer, 但 App.tsx 的 onEvent 仍只认 answer → Web 聊天答案全空。
// 抽成纯函数后钉死协议, 后端再改事件形态时这里会响亮变红。
import { describe, it, expect } from "vitest";
import { applyStepEvent, type AgentMsg } from "./agentStream";

const empty: AgentMsg = { text: "", steps: [] };

describe("applyStepEvent — 答案文本 (真 bug 回归)", () => {
  it("llm_chunk 增量拼进 text (后端唯一的答案来源)", () => {
    let m = empty;
    m = applyStepEvent(m, { type: "llm_chunk", chunk: "最终" });
    m = applyStepEvent(m, { type: "llm_chunk", chunk: "答案" });
    expect(m.text).toBe("最终答案");
  });

  it("空 chunk 不破坏已拼文本", () => {
    let m = applyStepEvent(empty, { type: "llm_chunk", chunk: "abc" });
    m = applyStepEvent(m, { type: "llm_chunk", chunk: "" });
    expect(m.text).toBe("abc");
  });

  it("llm_start 清空上一轮残留文本 (多轮 LLM: 工具轮的思考文本不该留在最终答案里)", () => {
    let m = applyStepEvent(empty, { type: "llm_chunk", chunk: "先调工具" });
    m = applyStepEvent(m, { type: "llm_start" });
    m = applyStepEvent(m, { type: "llm_chunk", chunk: "最终答案" });
    expect(m.text).toBe("最终答案");
  });

  it("llm_start 不动已收集的 steps (工具记录跨轮保留)", () => {
    let m = applyStepEvent(empty, { type: "tool_start", name: "rag_query", args: "{}" });
    m = applyStepEvent(m, { type: "llm_start" });
    expect(m.steps).toHaveLength(1);
    expect(m.steps[0].name).toBe("rag_query");
  });
});

describe("applyStepEvent — 工具步骤", () => {
  it("tool_start 追加一条待完成 step", () => {
    const m = applyStepEvent(empty, { type: "tool_start", name: "rag_query", args: "{}" });
    expect(m.steps).toEqual([{ name: "rag_query" }]);
  });

  it("tool_end 回填最近一条同名未完成 step", () => {
    let m = applyStepEvent(empty, { type: "tool_start", name: "rag_query", args: "{}" });
    m = applyStepEvent(m, {
      type: "tool_end", name: "rag_query", result: "3 chunks", latency_ms: 10, success: true,
    });
    expect(m.steps[0]).toEqual({ name: "rag_query", result: "3 chunks", ms: 10, ok: true });
  });

  it("同名工具多次调用: tool_end 回填最近一条未完成的, 不覆盖已完成的", () => {
    let m = applyStepEvent(empty, { type: "tool_start", name: "sql", args: "a" });
    m = applyStepEvent(m, {
      type: "tool_end", name: "sql", result: "第一次", latency_ms: 5, success: true,
    });
    m = applyStepEvent(m, { type: "tool_start", name: "sql", args: "b" });
    m = applyStepEvent(m, {
      type: "tool_end", name: "sql", result: "第二次", latency_ms: 7, success: false,
    });
    expect(m.steps).toHaveLength(2);
    expect(m.steps[0].result).toBe("第一次");
    expect(m.steps[1].result).toBe("第二次");
    expect(m.steps[1].ok).toBe(false);
  });

  it("孤儿 tool_end (无匹配 tool_start) 不炸也不凭空造 step", () => {
    const m = applyStepEvent(empty, {
      type: "tool_end", name: "ghost", result: "x", latency_ms: 1, success: true,
    });
    expect(m.steps).toEqual([]);
  });

  it("工具失败 ok=false 被如实记录", () => {
    let m = applyStepEvent(empty, { type: "tool_start", name: "fail", args: "{}" });
    m = applyStepEvent(m, {
      type: "tool_end", name: "fail", result: "报错", latency_ms: 2, success: false,
    });
    expect(m.steps[0].ok).toBe(false);
  });
});

describe("applyStepEvent — 不可变性与完整链路", () => {
  it("不修改传入的 msg (React state 安全)", () => {
    const before: AgentMsg = { text: "x", steps: [{ name: "a" }] };
    const snapshot = JSON.stringify(before);
    applyStepEvent(before, { type: "llm_chunk", chunk: "y" });
    applyStepEvent(before, { type: "tool_start", name: "b", args: "{}" });
    expect(JSON.stringify(before)).toBe(snapshot);
  });

  it("完整一轮: llm_start→chunk→tool→llm_start→chunk 得到最终答案+工具记录", () => {
    const events = [
      { type: "llm_start" as const },
      { type: "llm_chunk" as const, chunk: "我来查" },
      { type: "tool_start" as const, name: "sql", args: "{}" },
      { type: "tool_end" as const, name: "sql", result: "5 行", latency_ms: 12, success: true },
      { type: "llm_start" as const },
      { type: "llm_chunk" as const, chunk: "共 5 " },
      { type: "llm_chunk" as const, chunk: "个客户" },
    ];
    const m = events.reduce(applyStepEvent, empty);
    expect(m.text).toBe("共 5 个客户");
    expect(m.steps).toEqual([{ name: "sql", result: "5 行", ms: 12, ok: true }]);
  });
});
