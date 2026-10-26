/**
 * mcpView.ts — MCP server 面板的纯逻辑 (可见性层级的展示判据)。
 *
 * 跟 McpPanel.tsx 分开是为了可测: 这里不碰 React/fetch, 同 modelView.ts /
 * proposalView.ts 的分工。
 *
 * 背景 (2026-10-16 审计第 2 条): MCP 工具原先完全绕过 profile 门控 —— serving
 * 档位下仍把 write_file 发给 LLM。修复后 server 带 category, 面板要能把
 * "配置启用"(enabled) 和"本轮真加载"(active) 分开讲, 否则 serving 部署下
 * 开关显示"已启用"而 agent 根本不加载它, 是在误导人。
 */
import type { McpServer } from "./api";

/** 开关开着、但当前 profile 不放行它 —— 面板要显式说明这个落差。 */
export function isGatedOff(s: McpServer): boolean {
  return s.enabled && !s.active;
}

/**
 * 一条 server 的状态文案。
 *
 * 三种状态而不是两种: 关掉的 (人自己关的, 不用解释)、启用且生效、
 * 启用但被档位挡掉 (唯一需要解释的那个)。
 */
export function statusText(s: McpServer): string {
  if (!s.enabled) return "已停用";
  if (!s.active) return "已启用但本轮不加载 (serving 档位下 local 层级不暴露)";
  return "已启用";
}
