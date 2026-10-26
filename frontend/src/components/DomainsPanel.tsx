import { useEffect, useState } from "react";
import { getOntoDomains, postOntoDomain, deleteOntoDomain, getOntoEntities,
  type OntoDomain } from "../api";
import OntoModal from "./OntoModal";
import { usePaged, Pager, PAGE_SIZES } from "./Pager";

export default function DomainsPanel() {
  const [domains, setDomains] = useState<OntoDomain[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [editing, setEditing] = useState<OntoDomain | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [pendingDel, setPendingDel] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  async function reload() {
    const [ds, es] = await Promise.all([getOntoDomains(), getOntoEntities()]);
    setDomains(ds);
    const c: Record<string, number> = {};
    for (const e of es) {
      const dom = e.domain;
      if (!dom) continue;  // 未分类实体不挂任何域, 不计入 counts (避免幽灵 "" key)
      c[dom] = (c[dom] ?? 0) + 1;
    }
    setCounts(c);
  }
  useEffect(() => { reload(); }, []);

  const kw = q.trim().toLowerCase();
  const filtered = domains.filter((d) =>
    !kw || d.code.toLowerCase().includes(kw) || (d.description ?? "").toLowerCase().includes(kw));
  const { slice, page, setPage, pageCount, total, start } = usePaged(filtered, pageSize);

  function startNew() {
    setIsNew(true);
    setEditing({ code: "", color: "#4CAF50", displayOrder: 0, description: "" });
  }
  function startEdit(d: OntoDomain) { setIsNew(false); setEditing({ ...d }); }

  async function save() {
    if (!editing || !editing.code.trim()) return;
    try {
      await postOntoDomain({ ...editing, code: editing.code.trim() });
      setEditing(null); reload();
    } catch (e) { setErrMsg((e as Error).message); }
  }
  async function confirmDelete() {
    const code = pendingDel; if (!code) return;
    setPendingDel(null);
    try { await deleteOntoDomain(code); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }

  return (
    <div>
      <div className="onto-toolbar">
        <button className="onto-btn" onClick={startNew}>➕ 新建域</button>
        <input className="onto-search" placeholder="搜索 code / 描述" value={q}
               onChange={(e) => { setQ(e.target.value); setPage(1); }} />
      </div>

      {editing && (
        <OntoModal title={isNew ? "新建域" : `编辑域 · ${editing.code}`} onClose={() => setEditing(null)}>
          <div className="onto-field">
            <label>code {isNew ? "(建后不可改名)" : "(主键, 不可改)"}</label>
            <input className="onto-input" value={editing.code} disabled={!isNew}
                   onChange={(e) => setEditing({ ...editing, code: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>颜色</label>
            <input className="onto-input" value={editing.color}
                   onChange={(e) => setEditing({ ...editing, color: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>显示顺序</label>
            <input className="onto-input" type="number" value={editing.displayOrder}
                   onChange={(e) => setEditing({ ...editing, displayOrder: Number(e.target.value) })} />
          </div>
          <div className="onto-field">
            <label>描述</label>
            <input className="onto-input" value={editing.description}
                   onChange={(e) => setEditing({ ...editing, description: e.target.value })} />
          </div>
          <button className="onto-btn" onClick={save} disabled={!editing.code.trim()}>保存</button>
          <button className="onto-btn" onClick={() => setEditing(null)}>取消</button>
        </OntoModal>
      )}

      <table className="onto-table">
        <thead><tr><th>code</th><th>颜色</th><th>描述</th><th>挂靠实体</th><th></th></tr></thead>
        <tbody>
          {slice.map((d) => (
            <tr key={d.code}>
              <td>{d.code}</td>
              <td><span style={{ display: "inline-block", width: 14, height: 14,
                background: d.color, borderRadius: 3, verticalAlign: "middle" }} /> {d.color}</td>
              <td>{d.description}</td>
              <td>{counts[d.code] ?? 0}</td>
              <td>
                <button className="onto-btn" onClick={() => startEdit(d)}>编辑</button>
                <button className="onto-btn onto-btn-danger" onClick={() => setPendingDel(d.code)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager page={page} pageCount={pageCount} total={total} start={start} shown={slice.length}
             pageSize={pageSize} onPage={setPage} onSize={(s) => { setPageSize(s); setPage(1); }} />

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除域</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel}</b>？<b>{counts[pendingDel] ?? 0}</b> 个实体将归为未分类。
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
