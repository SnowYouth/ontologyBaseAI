import { useState } from "react";
import TriBool from "./TriBool";
import { ATTR_KINDS, ATTR_TYPES, ATTR_DESC_SOURCES, MAX_ATTRS,
  type OntoAttribute } from "../api";

// attributes_json 受控编辑器: 列表摘要行 (展开编辑/删除) + 底部添加。
// 8 键封闭槽位 (valueSet/inSpec/note/_valueName 已于 2026-08-05 废除); kind/type/desc_source 封闭下拉; 每字段中文标签+英文键名。
export function emptyAttr(): OntoAttribute {
  return { name: "", kind: "attr", type: "varchar", desc: "", desc_source: "code",
           disambiguation: "", hasEnum: null, sensitive: null };
}

// 保存可行性检查: 违规返回中文原因 (用来禁保存按钮), 合法返回 null。
// enumFields = 该实体在 enum_value 表有存活码值的 field 名集合; 传则预拦 hasEnum 断链
// (标了 hasEnum=true 但 enumFields 没该 field), 省后端 400 round-trip。默认空集=不预拦。
export function attrsInvalid(items: OntoAttribute[], enumFields: Set<string> = new Set()): string | null {
  const names = items.map((a) => a.name.trim());
  if (names.some((n) => !n)) return "有属性字段名为空";
  const dup = names.find((n, i) => names.indexOf(n) !== i);
  if (dup) return `属性字段名重复: ${dup}`;
  if (items.length > MAX_ATTRS) return `字段数超上限 ${MAX_ATTRS}`;
  const broken = items.filter((a) => a.hasEnum === true && a.name.trim() && !enumFields.has(a.name.trim()));
  if (broken.length) {
    return `字段 ${broken.map((a) => a.name.trim()).join(", ")} 标了有枚举码表但码表无码值, 请先到枚举 Tab 建码表`;
  }
  return null;
}

export default function AttributesEditor({ items, onChange }: {
  items: OntoAttribute[]; onChange: (next: OntoAttribute[]) => void;
}) {
  const [openIdx, setOpenIdx] = useState<number | null>(null);
  const [delIdx, setDelIdx] = useState<number | null>(null);

  const names = items.map((a) => a.name.trim());
  const dupAt = (i: number) => names[i] !== "" && names.indexOf(names[i]) !== i;

  function set(i: number, patch: Partial<OntoAttribute>) {
    onChange(items.map((a, idx) => (idx === i ? { ...a, ...patch } : a)));
  }
  function add() {
    onChange([...items, emptyAttr()]);
    setOpenIdx(items.length);
  }
  function removeAt(i: number) {
    onChange(items.filter((_, idx) => idx !== i));
    setOpenIdx(null); setDelIdx(null);
  }

  return (
    <div className="onto-attrs">
      {items.map((a, i) => (
        <div className="onto-attr" key={i}>
          <div className="onto-attr-row">
            <span className="onto-attr-summary">
              <b>{a.name.trim() || "(未命名)"}</b> · {a.kind} · {a.type}
              {a.desc && <> · {a.desc.slice(0, 20)}</>}
            </span>
            {dupAt(i) && <span className="onto-attr-err">重名</span>}
            {!names[i] && <span className="onto-attr-err">名空</span>}
            <button className="onto-btn"
                    onClick={() => setOpenIdx(openIdx === i ? null : i)}>
              {openIdx === i ? "收起" : "编辑"}
            </button>
            <button className="onto-btn" onClick={() => setDelIdx(i)}>✕</button>
          </div>
          {openIdx === i && <AttrForm a={a} onPatch={(p) => set(i, p)} />}
        </div>
      ))}
      <button className="onto-btn" onClick={add} disabled={items.length >= MAX_ATTRS}
              title={items.length >= MAX_ATTRS ? `字段数上限 ${MAX_ATTRS}` : ""}>
        ＋添加属性{items.length >= MAX_ATTRS ? ` (已达上限 ${MAX_ATTRS})` : ""}
      </button>

      {delIdx !== null && (
        <div className="cfg-confirm-backdrop" onClick={() => setDelIdx(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除属性</div>
            <div className="cfg-confirm-msg">
              确认删除属性 {items[delIdx].name.trim() || "(未命名)"} ? 保存后生效。
            </div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-danger" onClick={() => removeAt(delIdx)}>删除</button>
              <button className="cfg-btn-ghost" onClick={() => setDelIdx(null)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// 单条属性展开表单: 9 字段, 中文标签+英文键名。(valueSet/inSpec/note 已于 2026-08-05 废除)
function AttrForm({ a, onPatch }: {
  a: OntoAttribute; onPatch: (p: Partial<OntoAttribute>) => void;
}) {
  return (
    <div className="onto-attr-form">
      <div className="onto-field"><label>字段名 · name (数据库列名)</label>
        <input className="onto-input" value={a.name}
               onChange={(e) => onPatch({ name: e.target.value })} /></div>
      <div className="onto-field"><label>字段角色 · kind</label>
        <select className="onto-input" value={a.kind}
                onChange={(e) => onPatch({ kind: e.target.value })}>
          {ATTR_KINDS.map(([v, zh]) => <option key={v} value={v}>{v} — {zh}</option>)}
        </select></div>
      <div className="onto-field"><label>数据库类型 · type</label>
        <select className="onto-input" value={a.type}
                onChange={(e) => onPatch({ type: e.target.value })}>
          {ATTR_TYPES.map((v) => <option key={v} value={v}>{v}</option>)}
        </select></div>
      <div className="onto-field"><label>字段描述 · desc (业务含义)</label>
        <textarea className="onto-input" rows={2} value={a.desc}
                  onChange={(e) => onPatch({ desc: e.target.value })} /></div>
      <div className="onto-field"><label>描述来源 · desc_source</label>
        <select className="onto-input" value={a.desc_source}
                onChange={(e) => onPatch({ desc_source: e.target.value })}>
          {ATTR_DESC_SOURCES.map(([v, zh]) => <option key={v} value={v}>{v} — {zh}</option>)}
        </select></div>
      <div className="onto-field"><label>消歧说明 · disambiguation (与相近字段的区别)</label>
        <textarea className="onto-input" rows={2} value={a.disambiguation}
                  onChange={(e) => onPatch({ disambiguation: e.target.value })} /></div>
      <TriBool label="有枚举码表 · hasEnum (需先在枚举 Tab 建码值)" value={a.hasEnum}
               onChange={(v) => onPatch({ hasEnum: v })} />
      <TriBool label="敏感字段 · sensitive" value={a.sensitive}
               onChange={(v) => onPatch({ sensitive: v })} />
    </div>
  );
}
