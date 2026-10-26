// proposalView — ProposalsPanel 的纯展示逻辑 (拆出来供 vitest 单测, 不含 React)。
import type { Proposal, RelationHealth, CqRegressionOut } from "./api";

/** payload_json 摘要: 'a.X ↔ b.X' 形态 (解析失败回退原文前 40 字)。 */
export function proposalSummary(p: Proposal): string {
  try {
    const d = JSON.parse(p.payload_json) as {
      source?: string; target?: string; source_col?: string;
      target_col?: string; col_pair?: string;
    };
    if (d.col_pair) return `${d.source} ?? ${d.col_pair}`;
    if (d.source && d.target) {
      const l = d.source_col ?? "?";
      const r = d.target_col ?? "?";
      return `${d.source}.${l} ↔ ${d.target}.${r}`;
    }
  } catch { /* 坏 JSON 走兜底 */ }
  return p.payload_json.slice(0, 40);
}

/** 探针红绿灯: probe_result_json 里的 verdict。 */
export function probeBadge(p: Proposal): "🟢" | "🔴" | "❔" | "—" {
  if (!p.probe_result_json) return "—";  // retire 提议不走探针
  try {
    const d = JSON.parse(p.probe_result_json) as { verdict?: string };
    if (d.verdict === "pass") return "🟢";
    if (d.verdict === "fail") return "🔴";
    return "❔";
  } catch { return "❔"; }
}

/** direction 徽标文案。 */
export function directionBadge(d: Proposal["direction"]): string {
  return d === "retire" ? "淘汰" : "新增";
}

/** Apply 按钮是否可用。
 *
 * ★ relation_col 自 2026-09-18 起支持 apply (软删 via 指认的那条现有边),
 * 但 payload 必须带 via —— 没有 via 就定位不到边, 后端会拒 (拒绝静默 no-op),
 * 前端先禁掉, 免得人点了才知道。
 */
export function applyDisabled(p: Proposal): boolean {
  return p.kind === "relation_col" && !payloadVia(p);
}

/** Apply 按钮 tooltip (禁用时解释原因)。 */
export function applyTooltip(p: Proposal): string {
  if (applyDisabled(p)) return "提议缺 via, 定位不到要淘汰的边";
  return p.direction === "retire"
    ? "软删这条边并跑 CQ 回归门"
    : "落库并跑 CQ 回归门";
}

/** payload_json 里的 via (坏 JSON / 缺字段 → 空串)。 */
function payloadVia(p: Proposal): string {
  try {
    return (JSON.parse(p.payload_json) as { via?: string }).via ?? "";
  } catch { return ""; }
}

/** 通过这条提议时是否必须填 label。
 *
 * ★ 只有"新增一条边"要 (2026-09-19): 它 apply 时建新行, label 空就是裸边,
 * 概念层/agent 靠 get_join_path 返回的 label 选边时没区分度。probe 产的
 * payload 从不带 label —— 人审是唯一有人在场、又正在判业务语义的时刻。
 * retire 软删的是现有边 (label 早在库里), 不该逼人重填。
 */
export function requiresReviewLabel(p: Proposal): boolean {
  return p.direction === "add" && p.kind === "relation";
}

/** 人审 label 输入校验: 通过返回 null, 否则返回人话错误 (与后端 strip 同口径)。 */
export function reviewLabelError(p: Proposal, label: string): string | null {
  if (!requiresReviewLabel(p)) return null;
  if (!label.trim()) {
    return "新增边必须填 label —— 概念层/agent 靠它选边, 空 label 只能靠列名前缀猜。";
  }
  return null;
}

/** 健康观察折叠区排序键: suspect 降序, 同分按 use 升序 (高频主干边优先展示)。 */
export function sortHealth(rows: RelationHealth[]): RelationHealth[] {
  return [...rows].sort((a, b) =>
    b.suspect_count - a.suspect_count || a.use_count - b.use_count);
}

/** 淘汰半环扫描结果文案。
 *
 * ★ warning 排在计数前面: 没打分时这三个数必然全 0, 单看数字会被读成
 * "本体很健康", 实际是归因压根没素材。空转要说出来。
 */
export function cqRegressionMessage(
    out: CqRegressionOut): { text: string; isWarning: boolean } {
  const counts = `可疑边 ${out.suspects_updated}, ` +
                 `mismatch 达阈值 ${out.mismatch_thresholds_hit}, ` +
                 `淘汰提议 ${out.retire_proposals_created}`;
  return out.warning
    ? { text: `⚠ ${out.warning} | ${counts}`, isWarning: true }
    : { text: `扫描完成: ${counts} ` +
              `(本轮 ${out.scored_current}/${out.total_current} 条已打分)`,
        isWarning: false };
}
