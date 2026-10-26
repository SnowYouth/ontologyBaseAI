// proposalView 纯函数单测 (ProposalsPanel 展示逻辑)。
import { describe, it, expect } from "vitest";
import {
  proposalSummary, probeBadge, directionBadge,
  applyDisabled, applyTooltip, sortHealth, cqRegressionMessage,
  requiresReviewLabel, reviewLabelError,
} from "./proposalView";
import type { Proposal, RelationHealth, CqRegressionOut } from "./api";

function prop(patch: Partial<Proposal>): Proposal {
  return {
    id: 1, kind: "relation", direction: "add",
    payload_json: "{}", reason: "",
    review_status: "pending", patch_status: "pending",
    proposed_at: "t", proposed_by: "probe",
    probe_result_json: null, regression_ok: null,
    tombstone_hit: 0, tombstone_note: null,
    ...patch,
  };
}

describe("proposalSummary — payload 摘要", () => {
  it("relation add: 'a.X ↔ b.X' 形态", () => {
    const p = prop({ payload_json: JSON.stringify({
      source: "lib_loan", target: "lib_reader",
      source_col: "READER_ID", target_col: "MEMBER_ID" }) });
    expect(proposalSummary(p)).toBe("lib_loan.READER_ID ↔ lib_reader.MEMBER_ID");
  });

  it("relation_col retire: 走 col_pair 形态", () => {
    const p = prop({ payload_json: JSON.stringify({
      source: "a", target: "b", col_pair: "X=Y" }),
      kind: "relation_col", direction: "retire" });
    expect(proposalSummary(p)).toBe("a ?? X=Y");
  });

  it("坏 JSON 兜底不炸, 截 40 字", () => {
    const p = prop({ payload_json: "x".repeat(60) });
    expect(proposalSummary(p)).toHaveLength(40);
  });
});

describe("probeBadge — 探针红绿灯", () => {
  it("pass 绿 / fail 红 / unknown 问号 / 无探针横杠", () => {
    expect(probeBadge(prop({ probe_result_json: '{"verdict":"pass"}' }))).toBe("🟢");
    expect(propose("fail")).toBe("🔴");
    expect(propose("unknown")).toBe("❔");
    expect(probeBadge(prop({ probe_result_json: null }))).toBe("—");
    function propose(v: string) {
      return probeBadge(prop({ probe_result_json: JSON.stringify({ verdict: v }) }));
    }
  });

  it("坏 JSON → 问号不炸", () => {
    expect(probeBadge(prop({ probe_result_json: "not-json" }))).toBe("❔");
  });
});

describe("directionBadge / applyDisabled", () => {
  it("retire=淘汰 add=新增", () => {
    expect(directionBadge("retire")).toBe("淘汰");
    expect(directionBadge("add")).toBe("新增");
  });

  it("relation_col 现在可以 Apply (2026-09-18 起支持软删真边)", () => {
    const p = prop({
      kind: "relation_col", direction: "retire",
      payload_json: JSON.stringify({ source: "a", target: "b", via: "a.X=b.X" }),
    });
    expect(applyDisabled(p)).toBe(false);
    expect(applyTooltip(p)).not.toContain("手动");
  });

  it("★ relation_col 缺 via 仍禁用 — 定位不到边, apply 必失败", () => {
    const p = prop({
      kind: "relation_col", direction: "retire",
      payload_json: JSON.stringify({ source: "a", target: "b" }),
    });
    expect(applyDisabled(p)).toBe(true);
    expect(applyTooltip(p)).toContain("via");
  });

  it("relation add 照常可 Apply", () => {
    const ok = prop({});
    expect(applyDisabled(ok)).toBe(false);
    expect(applyTooltip(ok)).not.toContain("手动");
  });
});

describe("sortHealth — 观察区排序", () => {
  it("suspect 降序; 同 suspect 按 use 升序 (高频边先看到分母)", () => {
    const rows: RelationHealth[] = [
      { source: "a", target: "b", via: "v1", use_count: 99, suspect_count: 1, last_suspect_at: null },
      { source: "c", target: "d", via: "v2", use_count: 5, suspect_count: 3, last_suspect_at: null },
      { source: "e", target: "f", via: "v3", use_count: 10, suspect_count: 1, last_suspect_at: null },
    ];
    const sorted = sortHealth(rows);
    expect(sorted.map((r) => r.source)).toEqual(["c", "e", "a"]);
  });

  it("不改原数组", () => {
    const rows: RelationHealth[] = [
      { source: "a", target: "b", via: "v1", use_count: 1, suspect_count: 1, last_suspect_at: null },
      { source: "c", target: "d", via: "v2", use_count: 1, suspect_count: 9, last_suspect_at: null },
    ];
    sortHealth(rows);
    expect(rows[0].source).toBe("a");
  });
});

describe("cqRegressionMessage — 淘汰半环结果文案", () => {
  function out(patch: Partial<CqRegressionOut>): CqRegressionOut {
    return {
      suspects_updated: 0, mismatch_thresholds_hit: 0,
      retire_proposals_created: 0,
      scored_current: 5, scored_previous: 5, total_current: 5,
      warning: null, ...patch,
    };
  }

  it("正常情况: 报三个计数, 不带告警", () => {
    const { text, isWarning } = cqRegressionMessage(out({
      suspects_updated: 2, retire_proposals_created: 1 }));
    expect(text).toContain("2");
    expect(text).toContain("1");
    expect(isWarning).toBe(false);
  });

  it("★ 空转时必须把 warning 顶上来, 不能只显示一串 0", () => {
    const { text, isWarning } = cqRegressionMessage(out({
      scored_current: 0, scored_previous: 0,
      warning: "verdict 归因需要两轮都打分, 当前本轮 0/11 条、上轮 0 条已打分 → 该路径空转" }));
    expect(isWarning).toBe(true);
    expect(text).toContain("空转");
  });

  it("有告警但也有产出: 两段都要在", () => {
    const { text, isWarning } = cqRegressionMessage(out({
      retire_proposals_created: 1, scored_previous: 0,
      warning: "上轮 0 条已打分 → 该路径空转" }));
    expect(isWarning).toBe(true);
    expect(text).toContain("空转");
    expect(text).toContain("淘汰提议 1");
  });
});

describe("requiresReviewLabel — 通过时是否必须填 label", () => {
  it("relation add: 必填 (probe 产的 payload 不带 label, 不填就落库成裸边)", () => {
    expect(requiresReviewLabel(prop({ kind: "relation", direction: "add" })))
      .toBe(true);
  });

  it("relation retire: 不要求 (软删现有边, label 早在库里)", () => {
    expect(requiresReviewLabel(prop({ kind: "relation", direction: "retire" })))
      .toBe(false);
  });

  it("relation_col retire: 不要求", () => {
    expect(requiresReviewLabel(
      prop({ kind: "relation_col", direction: "retire" }))).toBe(false);
  });
});

describe("reviewLabelError — 人审输入校验 (点通过前拦一道)", () => {
  const addProp = prop({ kind: "relation", direction: "add" });

  it("空串报错", () => {
    expect(reviewLabelError(addProp, "")).toContain("label");
  });

  it("★ 纯空白等于没填 (与后端 strip 口径一致)", () => {
    expect(reviewLabelError(addProp, "   ")).toBeTruthy();
  });

  it("填了就放行", () => {
    expect(reviewLabelError(addProp, "订单的变更记录")).toBeNull();
  });

  it("retire 方向空 label 也放行", () => {
    expect(reviewLabelError(prop({ direction: "retire" }), "")).toBeNull();
  });
});
