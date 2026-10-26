import { useEffect, useState } from "react";
import { getOntoRelations, postOntoRelation, deleteOntoRelation, flipOntoRelation,
  getOntoEntities, REL_TYPES, type OntoRelation, type OntoEntity } from "../api";
import OntoModal from "./OntoModal";
import { usePaged, Pager, PAGE_SIZES } from "./Pager";

// via 由后端从 col 派生 `{source}.{source_col}={target}.{target_col}`, 前端只读预览。
function deriveVia(r: { source: string; target: string; source_col?: string | null; target_col?: string | null }): string {
  if (!r.source_col || !r.target_col) return "";
  return `${r.source}.${r.source_col}=${r.target}.${r.target_col}`;
}

export default function RelationsPanel(
  { filterEntity, onFilterConsumed }: { filterEntity: string; onFilterConsumed: () => void },
) {
  const [rels, setRels] = useState<OntoRelation[]>([]);
  const [entities, setEntities] = useState<OntoEntity[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [q, setQ] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const [edit, setEdit] = useState<OntoRelation | null>(null);
  const [addDraft, setAddDraft] = useState<OntoRelation | null>(null);
  const [pendingDel, setPendingDel] = useState<OntoRelation | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  async function reload() {
    const [rs, es] = await Promise.all([getOntoRelations(), getOntoEntities()]);
    setRels(rs); setEntities(es);
  }
  useEffect(() => { reload(); }, []);

  const kw = q.trim().toLowerCase();
  const shown = rels
    .filter((r) => !filter || r.source === filter || r.target === filter)
    .filter((r) => !kw || `${r.source} ${r.target} ${r.via ?? ""} ${r.label ?? ""}`
      .toLowerCase().includes(kw))
    .slice()
    // 裸边(label空)置顶; 同组内按 source→target→source_col→target_col 稳序
    // (via 派生自 col, 用 col 当 key 等价且 col 是定位主键)。
    .sort((a, b) => (a.label ? 1 : 0) - (b.label ? 1 : 0)
      || `${a.source}|${a.target}|${a.source_col ?? ""}|${a.target_col ?? ""}`
        .localeCompare(`${b.source}|${b.target}|${b.source_col ?? ""}|${b.target_col ?? ""}`));
  const { slice, page, setPage, pageCount, total, start } = usePaged(shown, pageSize);

  // 从实体 Tab 跳转来的预筛: 消费一次即清父状态并回第 1 页。
  // onFilterConsumed 由父 useCallback 稳定, 安全入依赖 (修 review Minor 闭包陷阱)。
  useEffect(() => {
    if (filterEntity) { setFilter(filterEntity); setPage(1); onFilterConsumed(); }
  }, [filterEntity, onFilterConsumed]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function saveEdit() {
    if (!edit) return;
    try { await postOntoRelation(edit); setEdit(null); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function saveAdd() {
    if (!addDraft) return;
    if (!addDraft.source || !addDraft.target) { setErrMsg("source/target 必选"); return; }
    if (!addDraft.source_col || !addDraft.target_col) { setErrMsg("source_col/target_col 必填 (via 从 col 派生)"); return; }
    try { await postOntoRelation(addDraft); setAddDraft(null); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function flip(r: OntoRelation) {
    try { await flipOntoRelation(r.source, r.target, r.source_col ?? "", r.target_col ?? ""); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  async function confirmDelete() {
    const r = pendingDel; if (!r) return;
    setPendingDel(null);
    try { await deleteOntoRelation(r.source, r.target, r.source_col ?? "", r.target_col ?? ""); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }

  return (
    <div>
      <div className="onto-toolbar">
        <label style={{ fontSize: 12, color: "#888" }}>按实体筛选</label>
        <select className="onto-input" value={filter}
                onChange={(e) => { setFilter(e.target.value); setPage(1); }}>
          <option value="">全部</option>
          {entities.map((e) => <option key={e.id} value={e.id}>{e.id}</option>)}
        </select>
        <input className="onto-search" placeholder="搜索 source/target/via/label" value={q}
               onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <span style={{ fontSize: 12, color: "#888" }}>共 {total} 条 · 裸边置顶</span>
        <button className="onto-btn" onClick={() => setAddDraft({
          source: "", target: "", via: "", label: "", type: "UNKNOWN",
          source_col: "", target_col: "", bridge_count: null,
        })}>+ 新增关系</button>
      </div>

      <table className="onto-table">
        <thead><tr><th>source</th><th>→ target</th><th>via</th><th>label</th><th>type</th><th>来源</th><th>桥数</th><th></th></tr></thead>
        <tbody>
          {slice.map((r) => (
            <tr key={`${r.source}|${r.target}|${r.source_col ?? ""}|${r.target_col ?? ""}`} className={r.label ? "" : "bare"}>
              <td>{r.source}</td>
              <td>→ {r.target}</td>
              <td>{r.via}</td>
              <td>{r.label || <span style={{ color: "#c0392b" }}>(裸边)</span>}</td>
              <td>{r.type ?? ""}</td>
              <td>{r.origin ?? ""}</td>
              <td>{r.bridge_count ?? ""}</td>
              <td>
                <button className="onto-btn" onClick={() => setEdit({ ...r })}>编辑</button>
                <button className="onto-btn" onClick={() => flip(r)}>改方向</button>
                <button className="onto-btn onto-btn-danger" onClick={() => setPendingDel(r)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager page={page} pageCount={pageCount} total={total} start={start} shown={slice.length}
             pageSize={pageSize} onPage={setPage} onSize={(s) => { setPageSize(s); setPage(1); }} />

      {edit && (
        <OntoModal title={`${edit.source} → ${edit.target} (via ${edit.via})`} onClose={() => setEdit(null)}>
          <div className="onto-field"><label>label</label>
            <input className="onto-input" value={edit.label ?? ""}
                   onChange={(e) => setEdit({ ...edit, label: e.target.value })} /></div>
          <div className="onto-field"><label>type</label>
            <select className="onto-input" value={edit.type ?? "UNKNOWN"}
                    onChange={(e) => setEdit({ ...edit, type: e.target.value })}>
              {REL_TYPES.map(([v, zh]) => <option key={v} value={v}>{zh} ({v})</option>)}
            </select></div>
          <div className="onto-field"><label>source_col *</label>
            <input className="onto-input" value={edit.source_col ?? ""} required
                   onChange={(e) => setEdit({ ...edit, source_col: e.target.value })} /></div>
          <div className="onto-field"><label>target_col *</label>
            <input className="onto-input" value={edit.target_col ?? ""} required
                   onChange={(e) => setEdit({ ...edit, target_col: e.target.value })} /></div>
          <div className="onto-field"><label>via (自动派生, 只读)</label>
            <input className="onto-input" value={deriveVia(edit)} readOnly
                   style={{ color: "#888", backgroundColor: "#f5f5f5" }} /></div>
          <button className="onto-btn" onClick={saveEdit}
                  disabled={!edit.source_col || !edit.target_col}>保存</button>
          <button className="onto-btn" onClick={() => setEdit(null)}>取消</button>
        </OntoModal>
      )}

      {addDraft && (
        <OntoModal title="新增关系" onClose={() => setAddDraft(null)}>
          <div className="onto-field"><label>source (选实体)</label>
            <select className="onto-input" value={addDraft.source}
                    onChange={(e) => setAddDraft({ ...addDraft, source: e.target.value })}>
              <option value="">— 选择 —</option>
              {entities.map((en) => <option key={en.id} value={en.id}>{en.id}</option>)}
            </select></div>
          <div className="onto-field"><label>target (选实体)</label>
            <select className="onto-input" value={addDraft.target}
                    onChange={(e) => setAddDraft({ ...addDraft, target: e.target.value })}>
              <option value="">— 选择 —</option>
              {entities.map((en) => <option key={en.id} value={en.id}>{en.id}</option>)}
            </select></div>
          <div className="onto-field"><label>label</label>
            <input className="onto-input" value={addDraft.label ?? ""}
                   onChange={(e) => setAddDraft({ ...addDraft, label: e.target.value })} /></div>
          <div className="onto-field"><label>type</label>
            <select className="onto-input" value={addDraft.type ?? "UNKNOWN"}
                    onChange={(e) => setAddDraft({ ...addDraft, type: e.target.value })}>
              {REL_TYPES.map(([v, zh]) => <option key={v} value={v}>{zh} ({v})</option>)}
            </select></div>
          <div className="onto-field"><label>source_col *</label>
            <input className="onto-input" value={addDraft.source_col ?? ""} required
                   onChange={(e) => setAddDraft({ ...addDraft, source_col: e.target.value })} /></div>
          <div className="onto-field"><label>target_col *</label>
            <input className="onto-input" value={addDraft.target_col ?? ""} required
                   onChange={(e) => setAddDraft({ ...addDraft, target_col: e.target.value })} /></div>
          <div className="onto-field"><label>via (自动派生, 只读)</label>
            <input className="onto-input" value={deriveVia(addDraft)} readOnly
                   style={{ color: "#888", backgroundColor: "#f5f5f5" }} /></div>
          <button className="onto-btn" onClick={saveAdd}
                  disabled={!addDraft.source_col || !addDraft.target_col}>保存</button>
          <button className="onto-btn" onClick={() => setAddDraft(null)}>取消</button>
        </OntoModal>
      )}

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除关系</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel.source} → {pendingDel.target}</b> (via {pendingDel.via})？
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
