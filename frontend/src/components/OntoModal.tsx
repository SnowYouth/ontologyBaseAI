import type { ReactNode } from "react";

// 本体编辑器通用浅色居中弹框 (区别于设置那套暗色 cfg-*)。
// backdrop 点击关闭, 标题栏 ✕, body 内部滚动 (max-height 由 .onto-modal-body 控)。
// wide: 控件多的编辑场景 (如概念条件) 用 860px 大窗, 默认仍 560px。
export default function OntoModal(
  { title, onClose, children, wide }: {
    title: string; onClose: () => void; children: ReactNode; wide?: boolean;
  },
) {
  return (
    <div className="onto-modal-backdrop" onClick={onClose}>
      <div className={`onto-modal${wide ? " wide" : ""}`} onClick={(e) => e.stopPropagation()}>
        <div className="onto-modal-head">
          <span>{title}</span>
          <span className="onto-modal-x" onClick={onClose}>✕</span>
        </div>
        <div className="onto-modal-body">{children}</div>
      </div>
    </div>
  );
}
