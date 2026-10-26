import { Fragment, useEffect, useState } from "react";
import {
  listProposals, reviewProposal, processSignalsStream, applyProposal,
  listRelationHealth, runCqRegression, proposeRetireFromHealth,
  type Proposal, type RelationHealth,
} from "../api";
import OntoModal from "./OntoModal";
import {
  proposalSummary, probeBadge, probeDetail, directionBadge,
  applyDisabled, applyTooltip, sortHealth, cqRegressionMessage,
  requiresReviewLabel, reviewLabelError, type ProbeDetail,
} from "../proposalView";

/** 探针证据展开行: checks (过/不过 + 阈值) + evidence 原始数字。 */
function ProbeDetailRow({ id, d }: { id: number; d: ProbeDetail }) {
  return (
    <tr className="pp-probe-row">
      <td colSpan={9} id={`pp-probe-${id}`}>
        <div className="pp-probe-detail">
          <span className="pp-probe-verdict">verdict: {d.verdict}</span>
          {d.checks.length > 0 && (
            <ul className="pp-probe-checks">
              {d.checks.map((c) => (
                <li key={c.name} className={c.ok ? "pp-ok" : "pp-bad"}>
                  <span aria-label={c.ok ? "通过" : "不通过"}>{c.ok ? "✓" : "✗"}</span>
                  {" "}<code>{c.name}</code> {c.detail}
                </li>
              ))}
            </ul>
          )}
          {d.evidence.length > 0 && (
            <dl className="pp-probe-evidence">
              {d.evidence.map(([k, v]) => (
                <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>
              ))}
            </dl>
          )}
        </div>
      </td>
    </tr>
  );
}

// ProposalsPanel — 自演化提议审批 (spec §8 / plan Task 8)。
// 表格: ID/kind/direction/摘要/原因/探针红绿灯/墓碑/状态/操作。
// 底部: 边健康观察折叠区 (只读, suspect 边降序, 无操作按钮)。
//
// 两个触发按钮对应飞轮的两个半环:
//   "处理信号" = 增边半环 (失败信号 → 探针 → add 提议)
//   "扫 CQ 回归" = 淘汰半环 (verdict 下降归因 + mismatch 累计 → retire 提议)

export default function ProposalsPanel() {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [health, setHealth] = useState<RelationHealth[]>([]);
  const [reviewFilter, setReviewFilter] = useState<string>("");
  const [processing, setProcessing] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [applyingId, setApplyingId] = useState<number | null>(null);
  const [retiringVia, setRetiringVia] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [infoMsg, setInfoMsg] = useState<string | null>(null);
  const [healthOpen, setHealthOpen] = useState(false);
  const [probeOpenId, setProbeOpenId] = useState<number | null>(null);
  // 通过一条新增边要先填 label (后端硬要求), 弹框收集
  const [labelFor, setLabelFor] = useState<Proposal | null>(null);
  const [labelDraft, setLabelDraft] = useState("");

  function reload() {
    listProposals(reviewFilter || undefined)
      .then(setProposals)
      .catch((e) => setErrMsg(String(e.message ?? e)));
  }
  function reloadHealth() {
    listRelationHealth().then(setHealth).catch(() => { /* 面板不炸 */ });
  }
  useEffect(() => { reload(); reloadHealth(); }, [reviewFilter]);

  async function onProcess() {
    setProcessing(true); setErrMsg(null); setInfoMsg(null);
    try {
      // 流式: start 事件一到就报"正在处理 N 条", 不让人对着转圈干等
      const out = await processSignalsStream(10, (pending) =>
        setInfoMsg(`正在处理 ${pending} 条信号 (探针要连真库, 稍候)...`));
      setInfoMsg(`处理 ${out.processed} 条信号: 新提议 ${out.proposals_created}, ` +
                 `跳过 ${out.skipped}, 墓碑命中 ${out.tombstone_hits}`);
      reload(); reloadHealth();
    } catch (e) { setErrMsg((e as Error).message); setInfoMsg(null); }
    finally { setProcessing(false); }
  }

  async function onScanCq() {
    setScanning(true); setErrMsg(null); setInfoMsg(null);
    try {
      const { text, isWarning } = cqRegressionMessage(await runCqRegression());
      if (isWarning) setErrMsg(text); else setInfoMsg(text);
      reload(); reloadHealth();
    } catch (e) { setErrMsg((e as Error).message); }
    finally { setScanning(false); }
  }

  async function onReview(p: Proposal, status: "approved" | "rejected",
                          label = "") {
    setErrMsg(null);
    try {
      await reviewProposal(p.id, status, "", label);
      reload();
    } catch (e) { setErrMsg((e as Error).message); }
  }

  /** 点"通过": 新增边先弹框收 label, 其余直接提交。 */
  function onApprove(p: Proposal) {
    if (requiresReviewLabel(p)) { setLabelDraft(""); setLabelFor(p); return; }
    onReview(p, "approved");
  }

  async function onConfirmLabel() {
    if (!labelFor) return;
    const err = reviewLabelError(labelFor, labelDraft);
    if (err) { setErrMsg(err); return; }
    const p = labelFor;
    setLabelFor(null);
    await onReview(p, "approved", labelDraft.trim());
  }

  async function onApply(p: Proposal) {
    setApplyingId(p.id); setErrMsg(null); setInfoMsg(null);
    try {
      const out = await applyProposal(p.id);
      if (out.ok) setInfoMsg(`#${p.id} 已落地: ${out.detail}`);
      else {
        const blocks = out.new_hard_blocks?.length
          ? ` (新增 hard_block: ${out.new_hard_blocks.join(", ")})` : "";
        setErrMsg(`#${p.id} 落地被拒: ${out.detail}${blocks}`);
      }
      reload(); reloadHealth();
    } catch (e) { setErrMsg((e as Error).message); }
    finally { setApplyingId(null); }
  }

  async function onRetireFromHealth(h: RelationHealth) {
    setRetiringVia(h.via); setErrMsg(null); setInfoMsg(null);
    try {
      const out = await proposeRetireFromHealth(h);
      if (out.ok) setInfoMsg(`已提 retire 提议: ${h.source} → ${h.target} (待人审)`);
      else setErrMsg(out.detail ?? "未生成提议");
      reload(); reloadHealth();
    } catch (e) { setErrMsg((e as Error).message); }
    finally { setRetiringVia(null); }
  }

  return (
    <div>
      <div className="onto-toolbar">
        <button className="onto-btn" onClick={onProcess} disabled={processing}>
          {processing ? "处理中…" : "⚙ 处理信号"}
        </button>
        <button className="onto-btn" onClick={onScanCq} disabled={scanning}
                title="扫两轮 CQ 结果做归因; 需先在 CQ 面板逐条打分, 否则空转">
          {scanning ? "扫描中…" : "♻ 扫 CQ 回归"}
        </button>
        <select className="onto-input" value={reviewFilter}
                onChange={(e) => setReviewFilter(e.target.value)}>
          <option value="">全部状态</option>
          <option value="pending">待审</option>
          <option value="approved">已批准</option>
          <option value="rejected">已拒绝</option>
        </select>
      </div>

      {errMsg && <div className="onto-error">{errMsg}</div>}
      {infoMsg && <div className="onto-info">{infoMsg}</div>}

      <table className="onto-table">
        <thead><tr>
          <th>ID</th><th>方向</th><th>提议内容</th><th>原因</th>
          <th>探针</th><th>墓碑</th><th>审批</th><th>落地</th><th>操作</th>
        </tr></thead>
        <tbody>
          {proposals.map((p) => {
            const detail = probeDetail(p);
            const open = detail !== null && probeOpenId === p.id;
            return (
            <Fragment key={p.id}>
            <tr>
              <td>#{p.id}</td>
              <td>
                <span className={`pp-dir pp-dir-${p.direction}`}>
                  {directionBadge(p.direction)}
                </span>
                <span className="pp-kind">{p.kind}</span>
              </td>
              <td title={p.payload_json}>{proposalSummary(p)}</td>
              <td className="pp-reason">{p.reason}</td>
              <td>
                {detail
                  ? <button className="pp-probe-toggle" aria-expanded={open}
                            aria-controls={`pp-probe-${p.id}`}
                            title={open ? "收起探针证据" : "展开探针证据"}
                            onClick={() => setProbeOpenId(open ? null : p.id)}>
                      {probeBadge(p)} {open ? "▾" : "▸"}
                    </button>
                  : probeBadge(p)}
              </td>
              <td>
                {p.tombstone_hit
                  ? <span title={p.tombstone_note ?? ""}>⚠️</span>
                  : "—"}
              </td>
              <td>{p.review_status}</td>
              <td>
                <span className={`pp-patch pp-patch-${p.patch_status}`}>
                  {p.patch_status}
                </span>
              </td>
              <td>
                {p.review_status === "pending" && (
                  <>
                    <button className="onto-btn" onClick={() => onApprove(p)}>通过</button>
                    <button className="onto-btn onto-btn-danger" onClick={() => onReview(p, "rejected")}>拒绝</button>
                  </>
                )}
                {p.review_status === "approved" && p.patch_status === "pending" && (
                  <button className="onto-btn" onClick={() => onApply(p)}
                          disabled={applyDisabled(p) || applyingId === p.id}
                          title={applyTooltip(p)}>
                    {applyingId === p.id ? "回归中…" : "Apply"}
                  </button>
                )}
              </td>
            </tr>
            {open && <ProbeDetailRow id={p.id} d={detail} />}
            </Fragment>
            );
          })}
          {proposals.length === 0 && (
            <tr><td colSpan={9} className="pp-empty">暂无提议 (先跑查询积累失败信号, 再点"处理信号")</td></tr>
          )}
        </tbody>
      </table>

      <div className="pp-health">
        <button className="pp-health-toggle" onClick={() => setHealthOpen(!healthOpen)}>
          {healthOpen ? "▼" : "▶"} 边健康观察 ({health.length})
        </button>
        <span className="pp-health-hint">这些边反复出现在失败场景, 可提 retire 交人审</span>
        {healthOpen && (
          <table className="onto-table">
            <thead><tr>
              <th>边</th><th>use_count</th><th>suspect_count</th><th>last_suspect_at</th>
              <th>操作</th>
            </tr></thead>
            <tbody>
              {sortHealth(health).map((h) => (
                <tr key={`${h.source}|${h.target}|${h.via}`}>
                  <td>{h.source} → {h.target} <span className="pp-via">{h.via}</span></td>
                  <td>{h.use_count}</td>
                  <td>{h.suspect_count}</td>
                  <td>{h.last_suspect_at ?? "—"}</td>
                  <td>
                    <button className="onto-btn onto-btn-danger"
                            onClick={() => onRetireFromHealth(h)}
                            disabled={retiringVia === h.via}
                            title="生成 retire 提议进人审队列; 落地仍需 approve + 回归门">
                      {retiringVia === h.via ? "提交中…" : "提 retire"}
                    </button>
                  </td>
                </tr>
              ))}
              {health.length === 0 && (
                <tr><td colSpan={5} className="pp-empty">暂无可疑边</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {labelFor && (
        <OntoModal title={`通过 #${labelFor.id} — 给这条边写个 label`}
                   onClose={() => setLabelFor(null)}>
          <div className="pp-label-hint">
            {proposalSummary(labelFor)} —— 概念层和 agent 靠 label 从多条候选边里
            选路, 空 label 只能靠列名前缀猜。写一句说清业务语义的短语。
          </div>
          <div className="onto-field"><label>label *</label>
            <input className="onto-input" value={labelDraft} autoFocus
                   placeholder="如: 订单的变更记录"
                   onChange={(e) => setLabelDraft(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter") onConfirmLabel(); }} /></div>
          <button className="onto-btn" onClick={onConfirmLabel}
                  disabled={!labelDraft.trim()}>确认通过</button>
          <button className="onto-btn" onClick={() => setLabelFor(null)}>取消</button>
        </OntoModal>
      )}
    </div>
  );
}
