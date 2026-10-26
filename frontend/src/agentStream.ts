// agent SSE 事件 → agent 消息的纯 reducer。
//
// 从 App.tsx 的 onEvent 抽出来: 事件协议是前后端接缝, 嵌在组件里就没法测,
// 结果后端 core/agent.py 改成发 llm_chunk 后前端还在等早已废弃的 answer,
// Web 聊天答案整个空掉都没人发现。抽成纯函数钉死协议。
//
// 答案文本的唯一来源是 llm_chunk 增量拼接 (后端流式吐 token);
// llm_start 标志新一轮 LLM 开始 → 清空上轮文本, 只保留最后一轮作为最终答案
// (中间轮是"我来查一下"这类工具前的思考, 不该混进最终答案)。
import type { StepEvent, Step } from "./api";

export type AgentMsg = { text: string; steps: Step[] };

export function applyStepEvent(msg: AgentMsg, ev: StepEvent): AgentMsg {
  switch (ev.type) {
    case "llm_start":
      // 新一轮 LLM: 清文本留 steps (工具记录整轮累积展示)
      return { ...msg, text: "" };

    case "llm_chunk":
      return { ...msg, text: msg.text + ev.chunk };

    case "tool_start":
      return { ...msg, steps: [...msg.steps, { name: ev.name }] };

    case "tool_end": {
      const steps = msg.steps.slice();
      for (let i = steps.length - 1; i >= 0; i--) {
        if (steps[i].name === ev.name && steps[i].result === undefined) {
          steps[i] = { ...steps[i], result: ev.result, ms: ev.latency_ms, ok: ev.success };
          break;
        }
      }
      return { ...msg, steps };
    }

    default:
      return msg;
  }
}
