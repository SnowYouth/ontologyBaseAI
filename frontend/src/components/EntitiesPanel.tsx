import { useEffect, useState } from "react";
import { getOntoEntities, putOntoEntity, postOntoEntity, deleteOntoEntity, getOntoDomains, getOntoEnums,
  getSources,
  type OntoEntity, type OntoDomain, type OntoEntityExtra,
  type OntoAttribute } from "../api";
import OntoModal from "./OntoModal";
import EntityExtraEditor from "./EntityExtraEditor";
import AttributesEditor, { attrsInvalid } from "./AttributesEditor";
import { usePaged, Pager, PAGE_SIZES } from "./Pager";

type Draft = {
  displayName: string; domain: string; summary: string; maps_to: string; tables: string;
  data_source: string;
};

function extraFromEntity(e: OntoEntity): OntoEntityExtra {
  return {
    recallKeywords: (e.recallKeywords as string[]) ?? [],
    disambiguateFrom: (e.disambiguateFrom as string[]) ?? [],
  };
}

// 提交前归一各字符串列表: 逐条 trim + 去空 (行内编辑可能改出空串/首尾空格)。
const cleanList = (xs: string[]) => xs.map((x) => x.trim()).filter(Boolean);
function normalizeExtra(x: OntoEntityExtra): OntoEntityExtra {
  return {
    recallKeywords: cleanList(x.recallKeywords),
    disambiguateFrom: cleanList(x.disambiguateFrom),
  };
}

function attrsFromEntity(e: OntoEntity): OntoAttribute[] {
  return ((e.attributes ?? []) as Record<string, unknown>[]).map((a) => ({
    name: (a.name as string) ?? "", kind: (a.kind as string) ?? "attr",
    type: (a.type as string) ?? "varchar", desc: (a.desc as string) ?? "",
    desc_source: (a.desc_source as string) ?? "code",
    disambiguation: (a.disambiguation as string) ?? "",
    hasEnum: (a.hasEnum as boolean | null) ?? null,
    sensitive: (a.sensitive as boolean | null) ?? null,
  }));
}

// 提交前归一: 字符串 trim。三态原样。
function normalizeAttrs(items: OntoAttribute[]): OntoAttribute[] {
  return items.map((a) => ({
    ...a, name: a.name.trim(), desc: a.desc.trim(),
    disambiguation: a.disambiguation.trim(),
  }));
}

export default function EntitiesPanel({ onJumpToRelations }: { onJumpToRelations: (id: string) => void }) {
  const [entities, setEntities] = useState<OntoEntity[]>([]);
  const [domains, setDomains] = useState<OntoDomain[]>([]);
  const [sources, setSources] = useState<string[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [addDraft, setAddDraft] = useState<(Draft & { id: string }) | null>(null);
  const [pendingDel, setPendingDel] = useState<OntoEntity | null>(null);
  const [extraDraft, setExtraDraft] = useState<OntoEntityExtra | null>(null);
  const [attrsDraft, setAttrsDraft] = useState<OntoAttribute[] | null>(null);
  const [enumFieldsByEntity, setEnumFieldsByEntity] = useState<Record<string, Set<string>>>({});
  const [enumFields, setEnumFields] = useState<Set<string>>(new Set());
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  async function reload() {
    const [es, ds, enums, src] = await Promise.all(
      [getOntoEntities(), getOntoDomains(), getOntoEnums(), getSources()]);
    setEntities(es); setDomains(ds); setSources(src.sources);
    // 建 entityId → 有码值的 field 名集合 索引 (hasEnum 断链前端预拦用)
    const idx: Record<string, Set<string>> = {};
    for (const e of enums) {
      (idx[e.entity] ??= new Set()).add(e.field);
    }
    setEnumFieldsByEntity(idx);
  }
  useEffect(() => { reload(); }, []);

  const kw = q.trim().toLowerCase();
  const filtered = entities.filter((e) =>
    !kw || e.id.toLowerCase().includes(kw) || (e.displayName ?? "").toLowerCase().includes(kw));
  const { slice, page, setPage, pageCount, total, start } = usePaged(filtered, pageSize);

  function startEdit(e: OntoEntity) {
    setOpenId(e.id);
    setDraft({
      displayName: e.displayName ?? "", domain: e.domain ?? "",
      summary: (e.summary as string) ?? "", maps_to: (e.maps_to as string) ?? "",
      tables: ((e.tables ?? []) as string[]).join(", "),
      data_source: (e.data_source as string) ?? "",
    });
    setExtraDraft(extraFromEntity(e));
    setAttrsDraft(attrsFromEntity(e));
    setEnumFields(enumFieldsByEntity[e.id] ?? new Set());
  }
  function close() { setOpenId(null); setDraft(null); setExtraDraft(null); setAttrsDraft(null); setEnumFields(new Set()); }

  async function save(id: string) {
    if (!draft) return;
    try {
      await putOntoEntity(id, {
        displayName: draft.displayName, domain: draft.domain, summary: draft.summary,
        maps_to: draft.maps_to, data_source: draft.data_source,
        tables: draft.tables.split(",").map((t) => t.trim()).filter(Boolean),
        ...(extraDraft ? { extra: normalizeExtra(extraDraft) } : {}),
        ...(attrsDraft ? { attributes: normalizeAttrs(attrsDraft) } : {}),
      });
      close(); reload();
    } catch (e) { setErrMsg((e as Error).message); }
  }

  async function saveAdd() {
    if (!addDraft) return;
    const id = addDraft.id.trim();
    if (!id) { setErrMsg("id 不能为空"); return; }
    if (entities.some((e) => e.id === id)) { setErrMsg(`实体 id 已存在: ${id}`); return; }
    try {
      await postOntoEntity({
        id, displayName: addDraft.displayName, domain: addDraft.domain,
        summary: addDraft.summary, maps_to: addDraft.maps_to,
        data_source: addDraft.data_source,
        tables: addDraft.tables.split(",").map((t) => t.trim()).filter(Boolean),
      });
      setAddDraft(null); reload();
    } catch (e) { setErrMsg((e as Error).message); }
  }

  async function confirmDelete() {
    const e = pendingDel; if (!e) return;
    setPendingDel(null);
    try { await deleteOntoEntity(e.id); reload(); }
    catch (err) { setErrMsg((err as Error).message); }
  }

  return (
    <div>
      <div className="onto-toolbar">
        <input className="onto-search" placeholder="搜索 id / 显示名" value={q}
               onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <button className="onto-btn" onClick={() => setAddDraft(
          { id: "", displayName: "", domain: "", summary: "", maps_to: "", tables: "", data_source: "" })}>
          + 新增实体
        </button>
      </div>

      <table className="onto-table">
        <thead><tr><th>id</th><th>显示名</th><th>域</th><th>data_source</th><th>表数</th><th>属性数</th><th></th></tr></thead>
        <tbody>
          {slice.map((e) => (
            <tr key={e.id}>
              <td>{e.id}</td>
              <td>{e.displayName}</td>
              <td>{e.domain || <span style={{ color: "#c0392b" }}>未分类</span>}</td>
              <td>{(e.data_source as string) || <span style={{ color: "#c0392b" }}>未指定</span>}</td>
              <td>{((e.tables ?? []) as string[]).length}</td>
              <td>{((e.attributes ?? []) as unknown[]).length}</td>
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

      {openId && draft && (() => {
        return (
          <OntoModal title={`${openId} · 编辑`} onClose={close}>
            <div className="onto-field"><label>显示名</label>
              <input className="onto-input" value={draft.displayName}
                     onChange={(ev) => setDraft({ ...draft, displayName: ev.target.value })} /></div>
            <div className="onto-field"><label>域</label>
              <select className="onto-input" value={draft.domain}
                      onChange={(ev) => setDraft({ ...draft, domain: ev.target.value })}>
                <option value="">未分类</option>
                {domains.map((d) => <option key={d.code} value={d.code}>{d.code}</option>)}
              </select></div>
            <div className="onto-field"><label>摘要</label>
              <input className="onto-input" value={draft.summary}
                     onChange={(ev) => setDraft({ ...draft, summary: ev.target.value })} /></div>
            <div className="onto-field"><label>maps_to</label>
              <input className="onto-input" value={draft.maps_to}
                     onChange={(ev) => setDraft({ ...draft, maps_to: ev.target.value })} /></div>
            <div className="onto-field"><label>data_source</label>
              <select className="onto-input" value={draft.data_source}
                      onChange={(ev) => setDraft({ ...draft, data_source: ev.target.value })}>
                <option value="">— 未指定</option>
                {sources.map((s) => <option key={s} value={s}>{s}</option>)}
              </select></div>
            <div className="onto-field"><label>tables (逗号分隔)</label>
              <input className="onto-input" value={draft.tables}
                     onChange={(ev) => setDraft({ ...draft, tables: ev.target.value })} /></div>
            <button className="onto-btn" disabled={!!(attrsDraft && attrsInvalid(attrsDraft, enumFields))}
                    title={(attrsDraft && attrsInvalid(attrsDraft, enumFields)) || ""}
                    onClick={() => save(openId)}>保存</button>
            <button className="onto-btn" onClick={close}>取消</button>

            {attrsDraft && (
              <div style={{ marginTop: 14 }}>
                <label style={{ fontSize: 12, color: "#888" }}>
                  属性 · attributes ({attrsDraft.length})
                </label>
                <AttributesEditor items={attrsDraft} onChange={setAttrsDraft} />
              </div>
            )}
            {extraDraft && (
              <div style={{ marginTop: 14 }}>
                <label style={{ fontSize: 12, color: "#888" }}>血肉 (extra_json)</label>
                <EntityExtraEditor value={extraDraft} onChange={setExtraDraft} />
              </div>
            )}
            <div style={{ marginTop: 10 }}>
              <button className="onto-btn" onClick={() => onJumpToRelations(openId)}>
                查看该实体的关系 →
              </button>
            </div>
          </OntoModal>
        );
      })()}

      {addDraft && (
        <OntoModal title="新增实体" onClose={() => setAddDraft(null)}>
          <div className="onto-field"><label>id (必填、唯一)</label>
            <input className="onto-input" value={addDraft.id}
                   onChange={(ev) => setAddDraft({ ...addDraft, id: ev.target.value })} /></div>
          <div className="onto-field"><label>显示名</label>
            <input className="onto-input" value={addDraft.displayName}
                   onChange={(ev) => setAddDraft({ ...addDraft, displayName: ev.target.value })} /></div>
          <div className="onto-field"><label>域</label>
            <select className="onto-input" value={addDraft.domain}
                    onChange={(ev) => setAddDraft({ ...addDraft, domain: ev.target.value })}>
              <option value="">未分类</option>
              {domains.map((d) => <option key={d.code} value={d.code}>{d.code}</option>)}
            </select></div>
          <div className="onto-field"><label>摘要</label>
            <input className="onto-input" value={addDraft.summary}
                   onChange={(ev) => setAddDraft({ ...addDraft, summary: ev.target.value })} /></div>
          <div className="onto-field"><label>maps_to</label>
            <input className="onto-input" value={addDraft.maps_to}
                   onChange={(ev) => setAddDraft({ ...addDraft, maps_to: ev.target.value })} /></div>
          <div className="onto-field"><label>data_source</label>
            <select className="onto-input" value={addDraft.data_source}
                    onChange={(ev) => setAddDraft({ ...addDraft, data_source: ev.target.value })}>
              <option value="">— 未指定</option>
              {sources.map((s) => <option key={s} value={s}>{s}</option>)}
            </select></div>
          <div className="onto-field"><label>tables (逗号分隔)</label>
            <input className="onto-input" value={addDraft.tables}
                   onChange={(ev) => setAddDraft({ ...addDraft, tables: ev.target.value })} /></div>
          <button className="onto-btn" onClick={saveAdd}>保存</button>
          <button className="onto-btn" onClick={() => setAddDraft(null)}>取消</button>
          <div style={{ marginTop: 8, fontSize: 12, color: "#888" }}>
            属性与血肉留空, 建完可用「编辑」补。
          </div>
        </OntoModal>
      )}

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除实体</div>
            <div className="cfg-confirm-msg">
              确定删除实体 <b>{pendingDel.id}</b>？
              <br />该实体的所有关系边也会一并删除。
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
