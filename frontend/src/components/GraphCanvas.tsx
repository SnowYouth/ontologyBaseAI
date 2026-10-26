import { useEffect, useRef } from "react";
import type { GraphNode, GraphEdge } from "../api";
import {
  edgePath, radius, truncateLabel, screenToGraph, zoomAt,
  type EdgeKind, type EdgeGeom, type View,
} from "../graphModel";

/** 滚轮一格的缩放系数 (向上放大/向下缩小)。 */
const WHEEL_STEP = 1.12;

export default function GraphCanvas({
  nodes, edges, pos, colors, degree, offMain, kinds, geoms,
  dimDomain, selected, onSelect, onOpen, onDrag, onDragEnd, size,
  view, onViewChange,
}: {
  nodes: GraphNode[]; edges: GraphEdge[];
  pos: Record<string, { x: number; y: number }>;
  colors: Record<string, string>;
  degree: Record<string, number>;
  offMain: Set<string>;
  kinds: EdgeKind[];
  geoms: EdgeGeom[];
  dimDomain: string | null;
  selected: string | null;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  onDrag: (id: string, x: number, y: number) => void;
  onDragEnd: (id: string) => void;
  size: { w: number; h: number };
  /** 视图变换 (translate+scale)。算法在 graphModel, 这里只负责套上去。 */
  view: View;
  onViewChange: (v: View) => void;
}) {
  const dragging = useRef<string | null>(null);
  /** 平移中: 记按下时的鼠标屏幕坐标与当时的 tx/ty。 */
  const panning = useRef<{ sx: number; sy: number; tx: number; ty: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  // 事件回调里要读最新 view, 但 wheel 监听只挂一次 → 用 ref 取代闭包捕获
  // (把 view 放进 effect 依赖会让每次滚动都重挂原生监听)
  const viewRef = useRef(view);
  useEffect(() => { viewRef.current = view; }, [view]);

  function toLocal(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const r = svgRef.current?.getBoundingClientRect();
    return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) };
  }

  // 滚轮缩放。★ 必须用原生监听 + { passive: false }: React 的 onWheel 是
  // passive 的, 里面 preventDefault 无效 → 缩放的同时整页会跟着滚。
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const p = toLocal(e);
      const factor = e.deltaY < 0 ? WHEEL_STEP : 1 / WHEEL_STEP;
      onViewChange(zoomAt(viewRef.current, p.x, p.y, factor));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [onViewChange]);

  function onBgDown(e: React.MouseEvent) {
    // 只有按在空白处才平移 (节点自己的 onMouseDown 会先把 dragging 设上)
    if (dragging.current) return;
    const p = toLocal(e);
    panning.current = { sx: p.x, sy: p.y, tx: view.tx, ty: view.ty };
  }

  function onMove(e: React.MouseEvent) {
    const p = toLocal(e);
    const id = dragging.current;
    if (id) {
      // ★ 拖节点必须反变换到图坐标: 直接喂屏幕坐标, 缩放后节点会以 k 倍
      //   速度跟手飞走 (pin 写的是模拟里的 fx/fy, 那是图坐标系)
      const g = screenToGraph(p.x, p.y, view);
      onDrag(id, g.x, g.y);
      return;
    }
    const pan = panning.current;
    if (!pan) return;
    onViewChange({ ...view, tx: pan.tx + (p.x - pan.sx), ty: pan.ty + (p.y - pan.sy) });
  }

  function onUp() {
    panning.current = null;
    const id = dragging.current;
    if (!id) return;
    dragging.current = null;
    onDragEnd(id);
  }

  return (
    <svg ref={svgRef} className="graph-svg"
         width={size.w} height={size.h}
         data-node-count={nodes.length} data-edge-count={edges.length}
         data-zoom={view.k.toFixed(2)}
         onMouseDown={onBgDown}
         onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}>
      <g className="graph-viewport"
         transform={`translate(${view.tx} ${view.ty}) scale(${view.k})`}>
        <g className="graph-edges">
          {edges.map((e, i) => {
            const a = pos[e.source];
            const b = pos[e.target];
            if (!a || !b) return null;
            const dim = dimDomain !== null;
            return (
              <path
                key={`${e.source}-${e.target}-${e.via ?? i}`}
                className={`graph-edge ${kinds[i]} ${dim ? "graph-dim" : ""}`}
                data-kind={kinds[i]}
                d={edgePath(a.x, a.y, b.x, b.y, geoms[i])}
              >
                <title>{e.label ?? e.via ?? ""}</title>
              </path>
            );
          })}
        </g>
        <g className="graph-nodes">
          {nodes.map((n) => {
            const p = pos[n.id];
            if (!p) return null;
            const dim = dimDomain !== null && n.domain !== dimDomain;
            const r = radius(degree[n.id] ?? 0);
            return (
              <g key={n.id} className={`graph-node ${dim ? "graph-dim" : ""}`}
                 data-id={n.id} data-domain={n.domain}
                 data-off-main={offMain.has(n.id) ? "1" : "0"}
                 onMouseDown={() => { dragging.current = n.id; }}
                 onClick={() => onSelect(n.id)}
                 onDoubleClick={() => onOpen(n.id)}>
                <circle
                  cx={p.x} cy={p.y} r={r}
                  fill={colors[n.id]}
                  stroke={offMain.has(n.id) ? "#dc2626" : (selected === n.id ? "#222" : "#fff")}
                  strokeWidth={offMain.has(n.id) ? 2.5 : (selected === n.id ? 2.5 : 1.2)}
                >
                  <title>{n.displayName} · 度数 {degree[n.id] ?? 0}</title>
                </circle>
                <text className="graph-node-label" x={p.x} y={p.y + r + 10}
                      textAnchor="middle">
                  {truncateLabel(n.displayName, 8)}
                </text>
              </g>
            );
          })}
        </g>
      </g>
    </svg>
  );
}
