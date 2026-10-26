import StringListEditor from "./StringListEditor";
import type { OntoEntityExtra } from "../api";

// 2 槽位固定 extra_json 血肉编辑器 (recallKeywords/disambiguateFrom)。受控: value + onChange。
// cocVerified/inSpec 已于 2026-08-05 废除。
export default function EntityExtraEditor({
  value, onChange,
}: {
  value: OntoEntityExtra;
  onChange: (next: OntoEntityExtra) => void;
}) {
  function set<K extends keyof OntoEntityExtra>(k: K, v: OntoEntityExtra[K]) {
    onChange({ ...value, [k]: v });
  }

  return (
    <div className="onto-extra">
      <div className="onto-field"><label>正向召回词 (recallKeywords)</label>
        <StringListEditor items={value.recallKeywords}
          onChange={(v) => set("recallKeywords", v)} placeholder="添加召回关键词…" />
      </div>
      <div className="onto-field"><label>负向消歧 (disambiguateFrom)</label>
        <StringListEditor items={value.disambiguateFrom}
          onChange={(v) => set("disambiguateFrom", v)} placeholder="添加消歧边界 (命中别选它)…" />
      </div>
    </div>
  );
}
