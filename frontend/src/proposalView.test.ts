// proposalView 纯函数单测 (ProposalsPanel 展示逻辑)。
import { describe, it, expect } from "vitest";
import {
  proposalSummary, probeBadge, probeDetail, directionBadge,
  applyDisabled, applyTooltip, sortHealth, cqRegressionMessage,
  requiresReviewLabel, reviewLabelError, recoveredNote,
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
    apply_journal_json: null,
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

  it("manual (人工取证) 不许显示成 ❔ —— 那是'探针坏了'的样子", () => {
    expect(probeBadge(prop({ probe_result_json: '{"verdict":"manual"}' }))).toBe("✋");
  });
});

describe("probeDetail — 探针证据展开 (审计 #6: 人据此填 label 并对落库负责)", () => {
  // 真库 #5 的形状 (数值脱敏前后同构)
  const pass = JSON.stringify({
    verdict: "pass",
    checks: [
      { name: "inclusion", ok: true, detail: "inclusion_rate=0.8230 >= 0.5" },
      { name: "null_rate", ok: false, detail: "null_rate=0.6000 <= 0.5" },
    ],
    evidence: { inclusion_rate: 0.8229895797702377, null_rate: 0.0, src_rows: 81842 },
  });

  it("checks 原样带出 (名称/通过与否/阈值说明)", () => {
    const d = probeDetail(prop({ probe_result_json: pass }))!;
    expect(d.verdict).toBe("pass");
    expect(d.checks).toEqual([
      { name: "inclusion", ok: true, detail: "inclusion_rate=0.8230 >= 0.5" },
      { name: "null_rate", ok: false, detail: "null_rate=0.6000 <= 0.5" },
    ]);
  });

  it("evidence 小数保留 4 位, 整数原样", () => {
    const d = probeDetail(prop({ probe_result_json: pass }))!;
    expect(d.evidence).toEqual([
      ["inclusion_rate", "0.8230"], ["null_rate", "0"], ["src_rows", "81842"],
    ]);
  });

  it("manual 只有 evidence 没 checks 也能展开 (字段不固定, 按 key 通吃)", () => {
    const d = probeDetail(prop({ probe_result_json: JSON.stringify({
      verdict: "manual", evidence: { join_rows_live: 750 } }) }))!;
    expect(d.checks).toEqual([]);
    expect(d.evidence).toEqual([["join_rows_live", "750"]]);
  });

  it("无探针 / 坏 JSON → null (面板不给展开入口)", () => {
    expect(probeDetail(prop({ probe_result_json: null }))).toBeNull();
    expect(probeDetail(prop({ probe_result_json: "not-json" }))).toBeNull();
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

describe("recoveredNote — 崩溃恢复导致的 failed 要和普通 failed 区分开", () => {
  const recovered = (from_phase: string) => prop({
    patch_status: "failed",
    apply_journal_json: JSON.stringify({
      phase: "recovered", from_phase, run_id: "r1", recovered_at: "2026-10-26T10:00:00",
    }),
  });

  it("applying 中断 → 提示已按快照回滚", () => {
    const n = recoveredNote(recovered("applying"));
    expect(n).toContain("2026-10-26T10:00:00");
    expect(n).toContain("回滚");
  });

  it("claimed 中断 → 提示本体没被改过", () => {
    const n = recoveredNote(recovered("claimed"));
    expect(n).toContain("未改动");
  });

  it("普通 failed (journal 已清空) → null", () => {
    expect(recoveredNote(prop({ patch_status: "failed" }))).toBeNull();
  });

  it("journal 是进行中态 (claimed/applying) 不算恢复 → null", () => {
    expect(recoveredNote(prop({
      patch_status: "applied",
      apply_journal_json: JSON.stringify({ phase: "applying" }),
    }))).toBeNull();
  });

  it("坏 JSON 不抛 → null", () => {
    expect(recoveredNote(prop({ patch_status: "failed", apply_journal_json: "{bad" }))).toBeNull();
  });
});
