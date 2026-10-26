import { useEffect, useState } from "react";
import { getOntoEnums, postOntoEnum, deleteOntoEnum,
  type OntoEnum, type OntoEnumCode } from "../api";
import OntoModal from "./OntoModal";
import { usePaged, Pager, PAGE_SIZES } from "./Pager";

export default function EnumsPanel() {
  const [enums, setEnums] = useState<OntoEnum[]>([]);
  const [draft, setDraft] = useState<OntoEnum | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [origCount, setOrigCount] = useState(0);     // 进入编辑时的码值数 (检测误删)
  const [pendingShrink, setPendingShrink] = useState(false);  // 码值变少二次确认
  const [pendingDel, setPendingDel] = useState<OntoEnum | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  function reload() { getOntoEnums().then(setEnums); }
  useEffect(() => { reload(); }, []);

  const kw = q.trim().toLowerCase();
  const filtered = enums.filter((e) =>
    !kw || e.entity.toLowerCase().includes(kw) || e.field.toLowerCase().includes(kw)
    || (e.name ?? "").toLowerCase().includes(kw));
  const { slice, page, setPage, pageCount, total, start } = usePaged(filtered, pageSize);

  function startNew() {
    setIsNew(true);
    setOrigCount(0);
    setDraft({ entity: "", field: "", name: "", spec_ref: "", codes: [] });
  }
  function startEdit(e: OntoEnum) {
    setIsNew(false);
    const codes = e.codes ?? [];
    setOrigCount(codes.length);
    setDraft({ ...e, codes: codes.map((c) => ({ ...c })) });
  }

  function setCode(i: number, patch: Partial<OntoEnumCode>) {
    if (!draft) return;
    const codes = draft.codes.slice();
    codes[i] = { ...codes[i], ...patch };
    setDraft({ ...draft, codes });
  }
  function addCode() {
    if (draft) setDraft({ ...draft, codes: [...draft.codes, { code: "", name: "", desc: "" }] });
  }
  function delCode(i: number) {
    if (draft) setDraft({ ...draft, codes: draft.codes.filter((_, j) => j !== i) });
  }

  // POST /ontology/enum 是 codes 整表覆盖: 码值数比进来时少 → 会永久删掉那些码值,
  // 弹二次确认防误删 (修 review Minor: enum 整表覆盖无确认)。
  async function doSave() {
    if (!draft) return;
    try { await postOntoEnum(draft); setDraft(null); reload(); }
    catch (e) { setErrMsg((e as Error).message); }
  }
  function save() {
    if (!draft || !draft.entity.trim() || !draft.field.trim()) return;
    if (draft.codes.length < origCount) { setPendingShrink(true); return; }
    doSave();
  }
  async function confirmDelete() {
    const e = pendingDel; if (!e) return;
    setPendingDel(null);
    try { await deleteOntoEnum(e.entity, e.field); reload(); }
    catch (er) { setErrMsg((er as Error).message); }
  }

  return (
    <div>
      <div className="onto-toolbar">
        <button className="onto-btn" onClick={startNew}>➕ 新建码表</button>
        <input className="onto-search" placeholder="搜索 entity / field / 名称" value={q}
               onChange={(e) => { setQ(e.target.value); setPage(1); }} />
      </div>

      {draft && (
        <OntoModal title={isNew ? "新建码表" : `编辑码表 · ${draft.entity}.${draft.field}`}
                   onClose={() => setDraft(null)}>
          <div className="onto-field">
            <label>entity {isNew ? "" : "(主键不可改)"}</label>
            <input className="onto-input" value={draft.entity} disabled={!isNew}
                   onChange={(e) => setDraft({ ...draft, entity: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>field {isNew ? "" : "(主键不可改)"}</label>
            <input className="onto-input" value={draft.field} disabled={!isNew}
                   onChange={(e) => setDraft({ ...draft, field: e.target.value })} />
          </div>
          <div className="onto-field">
            <label>名称</label>
            <input className="onto-input" value={draft.name}
                   onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </div>
          <label style={{ fontSize: 12, color: "#888" }}>码值 ({draft.codes.length})</label>
          <table className="onto-table">
            <thead><tr><th>code</th><th>name</th><th>desc</th><th></th></tr></thead>
            <tbody>
              {draft.codes.map((c, i) => (
                <tr key={i}>
                  <td><input className="onto-input" value={c.code}
                             onChange={(e) => setCode(i, { code: e.target.value })} /></td>
                  <td><input className="onto-input" value={c.name}
                             onChange={(e) => setCode(i, { name: e.target.value })} /></td>
                  <td><input className="onto-input" value={c.desc}
                             onChange={(e) => setCode(i, { desc: e.target.value })} /></td>
                  <td><button className="onto-btn onto-btn-danger" onClick={() => delCode(i)}>删</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="onto-btn" onClick={addCode}>➕ 加一条码值</button>
          <div style={{ marginTop: 10 }}>
            <button className="onto-btn" onClick={save}
                    disabled={!draft.entity.trim() || !draft.field.trim()}>保存</button>
            <button className="onto-btn" onClick={() => setDraft(null)}>取消</button>
          </div>
        </OntoModal>
      )}

      <table className="onto-table">
        <thead><tr><th>entity</th><th>field</th><th>名称</th><th>码值数</th><th></th></tr></thead>
        <tbody>
          {slice.map((e) => (
            <tr key={`${e.entity}|${e.field}`}>
              <td>{e.entity}</td><td>{e.field}</td><td>{e.name}</td><td>{e.codes?.length ?? 0}</td>
              <td>
                <button className="onto-btn" onClick={() => startEdit(e)}>编辑</button>
                <button className="onto-btn onto-btn-danger" onClick={() => setPendingDel(e)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager page={page} pageCount={pageCount} total={total} start={start} shown={slice.length}
             pageSize={pageSize} onPage={setPage} onSize={(s) => { setPageSize(s); setPage(1); }} />

      {pendingShrink && draft && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingShrink(false)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">码值减少确认</div>
            <div className="cfg-confirm-msg">
              码值数从 <b>{origCount}</b> 变为 <b>{draft.codes.length}</b>。保存将整表覆盖，
              被删的码值无法恢复。确定继续？
            </div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-ghost" onClick={() => setPendingShrink(false)}>取消</button>
              <button className="cfg-btn-danger" onClick={() => { setPendingShrink(false); doSave(); }}>仍保存</button>
            </div>
          </div>
        </div>
      )}
      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除码表</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel.entity}.{pendingDel.field}</b> 整张码表？
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
