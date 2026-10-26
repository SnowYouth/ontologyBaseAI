import { useEffect, useState } from "react";
import {
  getFilterRules, postFilterRule, deleteFilterRule, validateFilterRules,
  getFilterRuleCheckHistory,
  type FilterRule, type FilterRuleValidateResult,
  type FilterRuleCheckRun, type FilterRuleCheckRow,
} from "../api";
import OntoModal from "./OntoModal";
import { usePaged, Pager, PAGE_SIZES } from "./Pager";

const OPS = ["=", "!=", ">", "<", ">=", "<=", "IN", "IS NULL", "IS NOT NULL"];
const rkey = (r: { entity: string; col: string; op: string; value: string | null }) =>
  `${r.entity}|${r.col}|${r.op}|${r.value ?? ""}`;

type DiffMark = "reg" | "fixed" | "same" | "added" | "removed";

// 四键匹配两个 run 的 results, 给 current 每条标 diff; previous 独有的标 removed。
function diffRuns(cur: FilterRuleCheckRun | null, prev: FilterRuleCheckRun | null):
    { row: FilterRuleCheckRow; mark: DiffMark }[] {
  const key = (r: FilterRuleCheckRow) => `${r.entity}|${r.col}|${r.op}|${r.value ?? ""}`;
  const prevMap = new Map((prev?.results ?? []).map((r) => [key(r), r]));
  const out: { row: FilterRuleCheckRow; mark: DiffMark }[] = [];
  for (const row of cur?.results ?? []) {
    const p = prevMap.get(key(row));
    let mark: DiffMark;
    if (!p) mark = "added";
    else if (p.ok && !row.ok) mark = "reg";
    else if (!p.ok && row.ok) mark = "fixed";
    else mark = "same";
    prevMap.delete(key(row));
    out.push({ row, mark });
  }
  // reg 置顶, 其余保序
  out.sort((a, b) => (a.mark === "reg" ? -1 : 0) - (b.mark === "reg" ? -1 : 0));
  return out;
}

export default function FilterRulesPanel() {
  const [rules, setRules] = useState<FilterRule[]>([]);
  const [draft, setDraft] = useState<FilterRule | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [pendingDel, setPendingDel] = useState<FilterRule | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, FilterRuleValidateResult>>({});
  const [checking, setChecking] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const [checkRuns, setCheckRuns] = useState<FilterRuleCheckRun[]>([]);
  const [histLimit, setHistLimit] = useState(5);
  const [expandedRun, setExpandedRun] = useState<string | null>(null);

  function reload() { getFilterRules().then(setRules).catch((e) => setErrMsg(String(e.message ?? e))); }
  async function refreshHistory(limit = histLimit) {
    try { setCheckRuns(await getFilterRuleCheckHistory(limit)); } catch { /* 忽略, 面板不炸 */ }
  }
  useEffect(() => { reload(); refreshHistory(5); }, []);

  const kw = q.trim().toLowerCase();
  const filtered = rules.filter((r) =>
    !kw || r.entity.toLowerCase().includes(kw) || r.col.toLowerCase().includes(kw));
  const { slice, page, setPage, pageCount, total, start } = usePaged(filtered, pageSize);

  function startNew() {
    setIsNew(true);
    setDraft({ entity: "", col: "", op: "=", value: "", mode: "hard", origin: "manual" });
  }
  function startEdit(r: FilterRule) { setIsNew(false); setDraft({ ...r }); }

  async function save() {
    if (!draft || !draft.entity.trim() || !draft.col.trim()) return;
    try { await postFilterRule(draft); setDraft(null); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function confirmDelete() {
    const r = pendingDel; if (!r) return;
    setPendingDel(null);
    try { await deleteFilterRule(r); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function onCheck() {
    setChecking(true); setErrMsg(null);
    try {
      const res = await validateFilterRules();
      const m: Record<string, FilterRuleValidateResult> = {};
      for (const v of res) m[rkey(v)] = v;
      setVerdicts(m);
      await refreshHistory();
    } catch (e) { setErrMsg((e as Error).message); }
    finally { setChecking(false); }
  }
  async function onHistLimitChange(n: number) {
    setHistLimit(n);
    await refreshHistory(n);
  }

  const cur = checkRuns[0] ?? null;
  const prev = checkRuns[1] ?? null;
  const diff = diffRuns(cur, prev);
  const green = diff.filter((d) => d.row.ok).length;
  const red = diff.length - green;
  const turnedRed = diff.filter((d) => d.mark === "reg").length;
  const turnedGreen = diff.filter((d) => d.mark === "fixed").length;

  const MARK_STYLE: Record<DiffMark, { icon: string; cls: string }> = {
    reg: { icon: "🔴", cls: "frc-reg" },
    fixed: { icon: "🟢", cls: "frc-fixed" },
    same: { icon: "⚪", cls: "" },
    added: { icon: "➕", cls: "frc-added" },
    removed: { icon: "➖", cls: "frc-removed" },
  };

  return (
    <div>
      <div className="onto-toolbar">
        <button className="onto-btn" onClick={startNew}>➕ 新建规则</button>
        <input className="onto-search" placeholder="搜索 entity / col" value={q}
               onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <button className="onto-btn" onClick={onCheck} disabled={checking}>
          {checking ? "校验中…" : "✓ 一键校验"}
        </button>
      </div>

      {draft && (
        <OntoModal title={isNew ? "新建规则" : `编辑规则 · ${draft.entity}.${draft.col}`}
                   onClose={() => setDraft(null)}>
          <div className="onto-field">
            <label>entity {isNew ? "" : "(键不可改)"}</label>
            <input className="onto-input" value={draft.entity} disabled={!isNew}
                   onChange={(e) => setDraft({ ...draft, entity: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>col {isNew ? "" : "(键不可改)"}</label>
            <input className="onto-input" value={draft.col} disabled={!isNew}
                   onChange={(e) => setDraft({ ...draft, col: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>op {isNew ? "" : "(键不可改)"}</label>
            <select className="onto-input" value={draft.op} disabled={!isNew}
                    onChange={(e) => setDraft({ ...draft, op: e.target.value })}>
              {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>
          <div className="onto-field">
            <label>value {isNew ? "" : "(键不可改)"}</label>
            <input className="onto-input" value={draft.value ?? ""} disabled={!isNew}
                   onChange={(e) => setDraft({ ...draft, value: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>mode <span className="onto-hint">hard=硬过滤(必加WHERE) · soft=软提示(引导LLM)</span></label>
            <select className="onto-input" value={draft.mode}
                    onChange={(e) => setDraft({ ...draft, mode: e.target.value as FilterRule["mode"] })}>
              <option value="hard">hard</option>
              <option value="soft">soft</option>
            </select>
          </div>
          <div className="onto-field">
            <label>origin <span className="onto-hint">规则来源: 源码扫出 / 人工登记 / AI 建议</span></label>
            <select className="onto-input" value={draft.origin}
                    onChange={(e) => setDraft({ ...draft, origin: e.target.value as FilterRule["origin"] })}>
              <option value="source_code">source_code</option>
              <option value="manual">manual</option>
              <option value="ai">ai</option>
            </select>
          </div>
          <div style={{ marginTop: 10 }}>
            <button className="onto-btn" onClick={save}
                    disabled={!draft.entity.trim() || !draft.col.trim()}>保存</button>
            <button className="onto-btn" onClick={() => setDraft(null)}>取消</button>
          </div>
        </OntoModal>
      )}

      <table className="onto-table">
        <thead><tr>
          <th>校验</th><th>entity</th><th>col</th><th>op</th><th>value</th>
          <th>mode</th><th>origin</th><th></th>
        </tr></thead>
        <tbody>
          {slice.map((r) => {
            const v = verdicts[rkey(r)];  // rkey 内已有 value ?? "" 归一
            return (
              <tr key={rkey(r)}>
                <td title={v ? v.checks.filter((c) => !c.ok).map((c) => c.detail).join("; ") : ""}>
                  {v ? (v.ok ? "🟢" : "🔴") : "—"}
                </td>
                <td>{r.entity}</td><td>{r.col}</td><td>{r.op}</td>
                <td>{r.value ?? ""}</td><td>{r.mode}</td><td>{r.origin}</td>
                <td>
                  <button className="onto-btn" onClick={() => startEdit(r)}>编辑</button>
                  <button className="onto-btn onto-btn-danger" onClick={() => setPendingDel(r)}>删除</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <Pager page={page} pageCount={pageCount} total={total} start={start} shown={slice.length}
             pageSize={pageSize} onPage={setPage} onSize={(s) => { setPageSize(s); setPage(1); }} />

      {cur && (
        <div style={{ marginTop: 20 }}>
          <div className="frc-summary">
            本次 {diff.length} 条 | 绿 {green} 红 {red}
            {prev && ` | 较上次: ${turnedRed} 转红 ${turnedGreen} 转绿`}
            <span className="frc-time"> ({cur.checked_at})</span>
          </div>
          <ul className="frc-list">
            {diff.map(({ row, mark }) => (
              <li key={`${row.entity}|${row.col}|${row.op}|${row.value ?? ""}`}
                  className={MARK_STYLE[mark].cls}>
                {MARK_STYLE[mark].icon} {row.ok ? "✅" : "❌"} {row.entity}.{row.col} {row.op} {row.value ?? ""}
                {!row.ok && row.detail && <span className="frc-detail"> — {row.detail}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="frc-history" style={{ marginTop: 24 }}>
        <div className="frc-history-head">
          <b>校验历史</b>
          <label style={{ marginLeft: 12, fontWeight: "normal" }}>
            最近
            <select className="onto-input frc-hist-limit" value={histLimit}
                    onChange={(e) => onHistLimitChange(Number(e.target.value))}>
              {[5, 10, 20, 50].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            次
          </label>
        </div>
        {checkRuns.length === 0 ? (
          <div className="frc-empty">还没有校验记录 — 点上方"一键校验"生成第一批快照。</div>
        ) : (
          <ul className="frc-runlist">
            {checkRuns.map((run) => {
              const g = run.results.filter((r) => r.ok).length;
              const rd = run.results.length - g;
              const open = expandedRun === run.run_id;
              return (
                <li key={run.run_id} className="frc-run">
                  <div className="frc-run-head"
                       onClick={() => setExpandedRun(open ? null : run.run_id)}>
                    <span className="frc-run-toggle">{open ? "▼" : "▶"}</span>
                    <span className="frc-time">{run.checked_at}</span>
                    <span className="frc-run-stat">
                      {run.results.length} 条 · 绿 {g} 红 {rd}
                    </span>
                  </div>
                  {open && (
                    <ul className="frc-list frc-run-detail">
                      {run.results.map((row) => (
                        <li key={`${row.entity}|${row.col}|${row.op}|${row.value ?? ""}`}>
                          {row.ok ? "✅" : "❌"} {row.entity}.{row.col} {row.op} {row.value ?? ""}
                          {!row.ok && row.detail && <span className="frc-detail"> — {row.detail}</span>}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除规则</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel.entity}.{pendingDel.col} {pendingDel.op} {pendingDel.value ?? ""}</b>？
            </div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-ghost" onClick={() => setPendingDel(null)}>取消</button>
              <button className="cfg-btn-danger" onClick={confirmDelete}>删除</button>
            </div>
          </div>
        </div>
      )}
      {errMsg && (
        <div className="cfg-confirm-backdrop" onClick={() => setErrMsg(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">操作失败</div>
            <div className="cfg-confirm-msg">{errMsg}</div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-ghost" onClick={() => setErrMsg(null)}>知道了</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
