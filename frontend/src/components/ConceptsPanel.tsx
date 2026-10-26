import { useEffect, useState } from "react";
import {
  getConcepts, postConcept, deleteConcept, validateConcepts, getOntoEntities,
  type Concept, type ConceptCondition, type ConceptValidateResult, type OntoEntity,
} from "../api";
import OntoModal from "./OntoModal";
import { previewSql } from "../conceptSqlPreview";
import { usePaged, Pager, PAGE_SIZES } from "./Pager";

const OPS = ["=", "!=", ">", "<", ">=", "<=", "IN", "IS NULL", "IS NOT NULL"];
const AGG_FNS = ["COUNT", "SUM", "AVG", "MAX", "MIN"];
const HAVING_OPS = [">", ">=", "<", "<=", "=", "!="];

const emptyDraft = (): Concept => ({
  id: "", display_name: "", anchor_entity: "", summary: "",
  origin: "manual", valid: 0, conditions: [],
});

export default function ConceptsPanel() {
  const [concepts, setConcepts] = useState<Concept[]>([]);
  const [entities, setEntities] = useState<OntoEntity[]>([]);
  const [draft, setDraft] = useState<Concept | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [pendingDel, setPendingDel] = useState<Concept | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, ConceptValidateResult>>({});
  const [checking, setChecking] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  function reload() {
    getConcepts().then(setConcepts).catch((e) => setErrMsg(String(e.message ?? e)));
  }
  useEffect(() => {
    reload();
    getOntoEntities().then(setEntities).catch(() => { /* 下拉空不炸面板 */ });
  }, []);

  const entityIds = entities.map((e) => e.id).sort();
  const kw = q.trim().toLowerCase();
  const filtered = concepts.filter((c) =>
    !kw || c.id.toLowerCase().includes(kw) || c.display_name.toLowerCase().includes(kw));
  const { slice, page, setPage, pageCount, total, start } = usePaged(filtered, pageSize);

  function startNew() { setIsNew(true); setDraft(emptyDraft()); }
  function startEdit(c: Concept) {
    setIsNew(false);
    setDraft({ ...c, conditions: c.conditions.map((cc) => ({ ...cc })) });
  }

  function setCond(i: number, patch: Partial<ConceptCondition>) {
    if (!draft) return;
    const conditions = draft.conditions.map((cc, j) => (j === i ? { ...cc, ...patch } : cc));
    setDraft({ ...draft, conditions });
  }
  function addCond() {
    if (!draft) return;
    setDraft({ ...draft, conditions: [...draft.conditions,
      { entity: "", col: "", op: "=", value: "", seq: draft.conditions.length,
        kind: "column", agg_fn: "", agg_target: "", group_by_col: "",
        having_op: "", having_value: "" }] });
  }
  function removeCond(i: number) {
    if (!draft) return;
    setDraft({ ...draft, conditions: draft.conditions.filter((_, j) => j !== i) });
  }

  async function save() {
    if (!draft || !draft.id.trim() || !draft.display_name.trim() || !draft.anchor_entity.trim()) return;
    try { await postConcept(draft); setDraft(null); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function confirmDelete() {
    const c = pendingDel; if (!c) return;
    setPendingDel(null);
    try { await deleteConcept({ id: c.id }); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function onCheck() {
    setChecking(true); setErrMsg(null);
    try {
      const res = await validateConcepts();
      const m: Record<string, ConceptValidateResult> = {};
      for (const v of res) m[v.id] = v;
      setVerdicts(m);
      reload();
    } catch (e) { setErrMsg((e as Error).message); }
    finally { setChecking(false); }
  }

  function markOf(c: Concept): { icon: string; title: string } {
    const v = verdicts[c.id];
    if (v) {
      const bad = v.checks.filter((k) => !k.ok).map((k) => k.detail).filter(Boolean).join("; ");
      return { icon: v.ok ? "🟢" : "🔴", title: bad };
    }
    return { icon: c.valid ? "🟢" : "🔴", title: c.valid ? "" : "未校验 / 断腿" };
  }

  const canSave = !!draft && !!draft.id.trim() && !!draft.display_name.trim()
    && !!draft.anchor_entity.trim();

  return (
    <div>
      <div className="onto-toolbar">
        <button className="onto-btn" onClick={startNew}>➕ 新建概念</button>
        <input className="onto-search" placeholder="搜索 id / 概念名" value={q}
               onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <button className="onto-btn" onClick={onCheck} disabled={checking}>
          {checking ? "校验中…" : "✓ 一键校验"}
        </button>
      </div>

      {draft && (
        <OntoModal title={isNew ? "新建概念" : `编辑概念 · ${draft.id}`}
                   onClose={() => setDraft(null)} wide>
          <div className="onto-field">
            <label>id {isNew ? "(概念机器名, 同 entity 风格)" : "(键不可改)"}</label>
            <input className="onto-input" value={draft.id} disabled={!isNew}
                   onChange={(e) => setDraft({ ...draft, id: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>概念名</label>
            <input className="onto-input" value={draft.display_name}
                   onChange={(e) => setDraft({ ...draft, display_name: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>anchor 实体 <span className="onto-hint">概念的枢纽实体 (星型 JOIN 中心)</span></label>
            <select className="onto-input" value={draft.anchor_entity}
                    onChange={(e) => setDraft({ ...draft, anchor_entity: e.target.value })}>
              <option value="">— 选实体 —</option>
              {entityIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </div>
          <div className="onto-field">
            <label>summary <span className="onto-hint">目录点名提示 (LLM 看这句决定要不要展开)</span></label>
            <input className="onto-input" value={draft.summary}
                   onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>origin</label>
            <select className="onto-input" value={draft.origin}
                    onChange={(e) => setDraft({ ...draft, origin: e.target.value as Concept["origin"] })}>
              <option value="manual">manual</option>
              <option value="ai">ai</option>
            </select>
          </div>

          <div className="onto-field">
            <label>条件 <span className="onto-hint">满足全部条件 (AND); 挂不同实体走 relation 边</span></label>
            {draft.conditions.map((cc, i) => (
              <div className="onto-cond-card" key={i}>
                <div className="onto-cond-card-head">
                  <select className="onto-input" value={cc.kind ?? "column"}
                          onChange={(e) => setCond(i, { kind: e.target.value as "column" | "agg" })}>
                    <option value="column">列过滤</option>
                    <option value="agg">聚合断言</option>
                  </select>
                  <select className="onto-input" value={cc.entity}
                          onChange={(e) => setCond(i, { entity: e.target.value })}>
                    <option value="">— 选实体 —</option>
                    {entityIds.map((id) => <option key={id} value={id}>{id}</option>)}
                  </select>
                  <span className="onto-cond-spacer" />
                  <button className="onto-btn onto-btn-danger" onClick={() => removeCond(i)}>删</button>
                </div>
                {(cc.kind ?? "column") === "column" ? (
                  <div className="onto-cond-row">
                    <input className="onto-input" placeholder="col" value={cc.col}
                           onChange={(e) => setCond(i, { col: e.target.value })} />
                    <select className="onto-input" value={cc.op}
                            onChange={(e) => setCond(i, { op: e.target.value })}>
                      {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                    <input className="onto-input" placeholder="value" value={cc.value ?? ""}
                           onChange={(e) => setCond(i, { value: e.target.value })} />
                  </div>
                ) : (
                  <>
                    <div className="onto-cond-row">
                      <span className="onto-hint">先过滤(可空):</span>
                      <input className="onto-input" placeholder="col" value={cc.col}
                             onChange={(e) => setCond(i, { col: e.target.value })} />
                      <select className="onto-input" value={cc.op}
                              onChange={(e) => setCond(i, { op: e.target.value })}>
                        {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                      <input className="onto-input" placeholder="value" value={cc.value ?? ""}
                             onChange={(e) => setCond(i, { value: e.target.value })} />
                    </div>
                    <div className="onto-cond-row">
                      <select className="onto-input" value={cc.agg_fn ?? ""}
                              onChange={(e) => setCond(i, { agg_fn: e.target.value })}>
                        <option value="">— 聚合 —</option>
                        {AGG_FNS.map((f) => <option key={f} value={f}>{f}</option>)}
                      </select>
                      <input className="onto-input" placeholder="目标列 (* 或列名)" value={cc.agg_target ?? ""}
                             onChange={(e) => setCond(i, { agg_target: e.target.value })} />
                      <span className="onto-hint">按</span>
                      <input className="onto-input" placeholder="分组列" value={cc.group_by_col ?? ""}
                             onChange={(e) => setCond(i, { group_by_col: e.target.value })} />
                      <select className="onto-input" value={cc.having_op ?? ""}
                              onChange={(e) => setCond(i, { having_op: e.target.value })}>
                        <option value="">— 比较 —</option>
                        {HAVING_OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                      <input className="onto-input" placeholder="阈值" value={cc.having_value ?? ""}
                             onChange={(e) => setCond(i, { having_value: e.target.value })} />
                    </div>
                  </>
                )}
              </div>
            ))}
            <button className="onto-btn" onClick={addCond}>+ 添加条件</button>
          </div>

          <div className="onto-field">
            <label>SQL 预览 <span className="onto-hint">条件片段示意, 随编辑实时变化</span></label>
            <div className="onto-sql-preview">{previewSql(draft)}</div>
          </div>

          <div style={{ marginTop: 10 }}>
            <button className="onto-btn" onClick={save} disabled={!canSave}>保存</button>
            <button className="onto-btn" onClick={() => setDraft(null)}>取消</button>
          </div>
        </OntoModal>
      )}

      <table className="onto-table">
        <thead><tr>
          <th>校验</th><th>id</th><th>概念名</th><th>anchor</th>
          <th>条件数</th><th>origin</th><th></th>
        </tr></thead>
        <tbody>
          {slice.map((c) => {
            const mk = markOf(c);
            return (
              <tr key={c.id}>
                <td title={mk.title}>{mk.icon}</td>
                <td>{c.id}</td><td>{c.display_name}</td><td>{c.anchor_entity}</td>
                <td>{c.conditions.length}</td><td>{c.origin}</td>
                <td>
                  <button className="onto-btn" onClick={() => startEdit(c)}>编辑</button>
                  <button className="onto-btn onto-btn-danger"
                          onClick={() => setPendingDel(c)}>删除</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <Pager page={page} pageCount={pageCount} total={total} start={start} shown={slice.length}
             pageSize={pageSize} onPage={setPage} onSize={(s) => { setPageSize(s); setPage(1); }} />

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除概念</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel.id}</b> (及其 {pendingDel.conditions.length} 条条件)？
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
