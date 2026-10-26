import { useState } from "react";

// 受控字符串列表编辑器: 每条一行 (可删), 底部输入框 + 添加。
// multiline=true 用 textarea (query_hints 长句), 否则单行 input。
export default function StringListEditor({
  items, onChange, multiline = false, placeholder = "添加一条…",
}: {
  items: string[];
  onChange: (next: string[]) => void;
  multiline?: boolean;
  placeholder?: string;
}) {
  const [pending, setPending] = useState("");

  function add() {
    const v = pending.trim();
    if (!v) return;
    onChange([...items, v]);
    setPending("");
  }
  function removeAt(i: number) {
    onChange(items.filter((_, idx) => idx !== i));
  }
  function editAt(i: number, v: string) {
    onChange(items.map((it, idx) => (idx === i ? v : it)));
  }

  return (
    <div className="onto-strlist">
      {items.map((it, i) => (
        <div className="onto-strlist-row" key={i}>
          {multiline ? (
            <textarea className="onto-input" rows={2} value={it}
                      onChange={(e) => editAt(i, e.target.value)} />
          ) : (
            <input className="onto-input" value={it}
                   onChange={(e) => editAt(i, e.target.value)} />
          )}
          <button className="onto-btn" onClick={() => removeAt(i)}>✕</button>
        </div>
      ))}
      <div className="onto-strlist-row">
        <input className="onto-input" value={pending} placeholder={placeholder}
               onChange={(e) => setPending(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button className="onto-btn" onClick={add}>＋添加</button>
      </div>
    </div>
  );
}
