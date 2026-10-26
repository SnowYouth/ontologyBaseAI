import type { LegendItem } from "../graphModel";

/** 网络图图例: 域名 + 色块 + 实体数。点击某域高亮它 (再点取消)。
 *  note 字段标注数据空洞 ("未配色"/"未归域") —— 让缺失可见, 不被哈希色静音。
 *  ★ 过滤/高亮一律用 it.key (真实 domain 串, 无域桶是 "") —— code 是显示名,
 *    拿它当过滤值会让无域节点永远比不中, 全图变暗。dimDomain===null 才是
 *    "无过滤", "" 是合法过滤值, 两者靠 !== null 区分 (别用 falsy 判断)。 */
export default function GraphLegend({ items, active, onPick }: {
  items: LegendItem[];
  active: string | null;
  onPick: (domain: string | null) => void;
}) {
  return (
    <div className="graph-legend">
      {items.map((it) => (
        <div
          key={it.code}
          className={`graph-legend-item ${active === it.key ? "on" : ""}`}
          data-domain={it.code}
          onClick={() => onPick(active === it.key ? null : it.key)}
          title={it.note ? `${it.code} — ${it.note}` : it.code}
        >
          <span className="graph-legend-swatch" style={{ background: it.color }} />
          <span className="graph-legend-name">{it.code}</span>
          <span className="graph-legend-count">{it.count}</span>
          {it.note && <span className="graph-legend-note">{it.note}</span>}
        </div>
      ))}
    </div>
  );
}
