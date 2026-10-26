import { describe, it, expect } from "vitest";
import { isGatedOff, statusText } from "./mcpView";
import type { McpServer } from "./api";

function srv(over: Partial<McpServer> = {}): McpServer {
  return {
    name: "notes-writer", command: "uv", args: ["x"],
    enabled: true, category: "local", active: true, ...over,
  };
}

describe("isGatedOff — 配置启用 vs 本轮真加载", () => {
  it("启用且生效 → false", () => {
    expect(isGatedOff(srv())).toBe(false);
  });

  it("★ 启用但被档位挡掉 → true (serving 下的 local server)", () => {
    expect(isGatedOff(srv({ active: false }))).toBe(true);
  });

  it("自己关掉的 → false (不需要解释, 不是落差)", () => {
    // 关掉的 server active 也是 false, 但那是人自己关的, 提示它反而噪音。
    expect(isGatedOff(srv({ enabled: false, active: false }))).toBe(false);
  });
});

describe("statusText", () => {
  it("三种状态各自不同文案", () => {
    expect(statusText(srv())).toBe("已启用");
    expect(statusText(srv({ enabled: false, active: false }))).toBe("已停用");
    expect(statusText(srv({ active: false }))).toContain("本轮不加载");
  });

  it("★ 被挡掉时文案要说清原因, 不只说'未加载'", () => {
    // 人要据此知道该去改 category 还是改 DEPLOY_PROFILE。
    const t = statusText(srv({ active: false }));
    expect(t).toContain("serving");
    expect(t).toContain("local");
  });
});
