// 三态 radio: 是/否/未设置。null=未设置 (后端不写此键)。
// 从 EntityExtraEditor 抽出共享 (extra 血肉 + attributes 编辑两处用)。
export default function TriBool({ label, value, onChange }: {
  label: string; value: boolean | null; onChange: (v: boolean | null) => void;
}) {
  const opts: [string, boolean | null][] = [["是", true], ["否", false], ["未设置", null]];
  return (
    <div className="onto-field onto-tribool"><label>{label}</label>
      <div>
        {opts.map(([txt, v]) => (
          <label key={txt} className="onto-tribool-opt">
            <input type="radio" checked={value === v} onChange={() => onChange(v)} /> {txt}
          </label>
        ))}
      </div>
    </div>
  );
}
