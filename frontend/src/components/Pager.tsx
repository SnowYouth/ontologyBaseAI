import { useState } from "react";

export const PAGE_SIZES = [20, 50, 100];

// 客户端分页 hook: 传入全量 items + 当前每页条数, 返回当前页切片 + 页码状态。
// 页码越界钳制在 render 期算 (items/pageSize 变小时旧 page 可能超界)。
export function usePaged<T>(items: T[], pageSize: number) {
  const [page, setPage] = useState(1);
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const cur = Math.min(page, pageCount);        // 钳制, 不直接改 state (避免 render 里 set)
  const start = (cur - 1) * pageSize;
  const slice = items.slice(start, start + pageSize);
  return { slice, page: cur, setPage, pageCount, total, start };
}

// 分页控件: 每页条数下拉 + 上/下一页 + 页码。onSizeChange 由父在切换时重置 page。
export function Pager(
  { page, pageCount, total, start, shown, pageSize, onPage, onSize }: {
    page: number; pageCount: number; total: number; start: number; shown: number;
    pageSize: number; onPage: (p: number) => void; onSize: (s: number) => void;
  },
) {
  return (
    <div className="onto-pager">
      <span className="onto-pager-info">
        {total === 0 ? "共 0 条" : `${start + 1}–${start + shown} / 共 ${total} 条`}
      </span>
      <span className="onto-pager-spacer" />
      <label className="onto-pager-size">
        每页
        <select className="onto-input" value={pageSize}
                onChange={(e) => onSize(Number(e.target.value))}>
          {PAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
      <button className="onto-btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
      <span className="onto-pager-num">{page} / {pageCount}</span>
      <button className="onto-btn" disabled={page >= pageCount} onClick={() => onPage(page + 1)}>下一页</button>
    </div>
  );
}
