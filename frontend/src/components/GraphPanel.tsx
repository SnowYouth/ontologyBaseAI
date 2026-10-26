import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getOntologyGraph, type GraphData } from "../api";
import {
  domainColor, buildLegend, nodeDegree, isolatedNodes, connectedComponents,
  edgeKind, edgeGeometry, domainSectors, radialTargets, zoomAt, fitView,
  layoutSize, restoreLayout, packLayout, searchNodes, centerOn,
  type View,
} from "../graphModel";
import { runLayout, type LayoutHandle } from "../graphLayout";
import GraphCanvas from "./GraphCanvas";
import GraphLegend from "./GraphLegend";

/** 容器还没量出来时**渲染**用的兜底尺寸 (首帧 ResizeObserver 未回调)。
 *  ★ 只给 GraphCanvas 当画布属性用, **不参与布局/存档判定** —— 见 measured。 */
const FALLBACK = { w: 900, h: 620 };
const IDENTITY: View = { tx: 0, ty: 0, k: 1 };
const FIT_PAD = 44;
const BTN_STEP = 1.25;
/** 手工布局存档的 localStorage 键。★ 浏览器本地, 不入库 —— 换浏览器/清
 *  缓存就没了, 这是用户拍板的取舍 (改动量小优先)。
 *  ★ 键名不随存档格式加 size 字段而升版: 老版裸 pos 数据被 restoreLayout 的
 *    尺寸闸门判过期 (来源画布不可知), 下次保存原地覆盖成新格式 —— 不需要
 *    迁移代码, 也不留一个永远没人清的 v1 垃圾键。 */
const SAVE_KEY = "ontology.graph.layout.v1";

function readSaved(): unknown {
  try {
    const s = localStorage.getItem(SAVE_KEY);
    return s ? JSON.parse(s) : null;
  } catch {
    return null;   // 隐私模式下 localStorage 可能直接抛; 坏 JSON 同理
  }
}

export default function GraphPanel({ onJumpToRelations }: {
  onJumpToRelations: (entityId: string) => void;
}) {
  const [data, setData] = useState<GraphData | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [pos, setPos] = useState<Record<string, { x: number; y: number }>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [dimDomain, setDimDomain] = useState<string | null>(null);
  const [view, setView] = useState<View>(IDENTITY);
  const [full, setFull] = useState(false);
  const [saveHint, setSaveHint] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [openList, setOpenList] = useState(false);
  // ★ 画布尺寸随容器实测, 不再写死 900×620 —— 写死时视口窄于 ~1200px 会把
  //   右侧节点裁在 SVG 外面 (看起来像"节点缺了"), 全屏按钮也会失去意义。
  // ★★ 初值是 null 而不是 FALLBACK: 存档要按画布尺寸比对, 拿一个**猜的**尺寸
  //   去比会误判。真实容器尺寸与 FALLBACK 不等时, 用 FALLBACK 跑第一轮就会报
  //   "画布尺寸变了" —— 用户只是刷新, 窗口一动没动 (浏览器实测复现过)。宁可等一帧
  //   量到真实尺寸再起模拟, 也不要拿假尺寸跑一轮还顺手弹个错提示。
  const [measured, setMeasured] = useState<{ w: number; h: number } | null>(null);
  const size = measured ?? FALLBACK;
  const stageRef = useRef<HTMLDivElement>(null);
  const layout = useRef<LayoutHandle | null>(null);
  // fitAll 要读最新坐标, 但不该因坐标每 tick 变化就重建回调 → 走 ref
  const posRef = useRef(pos);
  posRef.current = pos;

  function load() {
    setErrMsg(null);
    getOntologyGraph().then(setData).catch((e) => setErrMsg((e as Error).message));
  }
  useEffect(() => { load(); }, []);

  // 容器实测尺寸 (全屏切换、窗口缩放、侧栏折叠都会触发)
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const r = entry.contentRect;
      if (r.width > 0 && r.height > 0) {
        setMeasured({ w: Math.round(r.width), h: Math.round(r.height) });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [errMsg, data]);   // 错误态/加载态没渲染 stage, 恢复后要重新 observe

  const sectors = useMemo(
    () => (data ? domainSectors(data.nodes, data.domains) : {}),
    [data],
  );

  const model = useMemo(() => {
    if (!data) return null;
    const { nodes, edges, domains } = data;
    const comp = connectedComponents(nodes, edges);
    const iso = new Set(isolatedNodes(nodes, edges));
    const offMain = new Set(nodes.filter((n) => comp[n.id] !== 0 || iso.has(n.id))
                                 .map((n) => n.id));
    const colors: Record<string, string> = {};
    for (const n of nodes) colors[n.id] = domainColor(n.domain, domains);
    return {
      colors, offMain,
      degree: nodeDegree(nodes, edges),
      legend: buildLegend(nodes, domains),
      kinds: edges.map((e) => edgeKind(e, nodes)),
      geoms: edgeGeometry(edges),
    };
  }, [data]);

  const fitAll = useCallback(() => {
    const pts = Object.values(posRef.current);
    setView(pts.length ? fitView(pts, size, FIT_PAD) : IDENTITY);
  }, [size]);
  /** 按给定点集直接算视图。fitAll 读的是 state (起模拟那一刻还没更新),
   *  开局和 ↺ 都要拿 snapshot 现算。 */
  const fitTo = useCallback((snap: Record<string, { x: number; y: number }>) => {
    const pts = Object.values(snap);
    setView(pts.length ? fitView(pts, size, FIT_PAD) : IDENTITY);
  }, [size]);

  /** 选中并把视图挪到该节点 (搜索结果点选 / 回车)。
   *  ★ 读 posRef 而非 pos: 同 fitAll, 坐标每 tick 都变, 进依赖会让回调
   *    每帧重建。
   *  ★ 节点坐标可能还没算出来 (数据刚到、模拟未推第一帧) —— 那就只选中
   *    不挪视图, 别拿 undefined 去乘 k 把整张图推到 NaN。 */
  const locate = useCallback((id: string) => {
    setSelected(id);
    setQuery("");
    setOpenList(false);
    const p = posRef.current[id];
    if (p) setView((v) => centerOn(p, size, v.k));
  }, [size]);

  const hits = useMemo(
    () => (data ? searchNodes(data.nodes, query) : []),
    [data, query],
  );


  // ★ 依赖含 size: 全屏或改窗口宽度后扇区目标点要按新画布重算, 否则图会
  //   偏在一角 (目标点是绝对画布坐标, 不是比例)。
  // ★ 布局在**放大 SPREAD 倍的虚拟空间**里算 (layoutSize), 不是画布空间 ——
  //   节点间距因此变大, 再由随即而来的 fit 把整张图收进画布 (k<1)。
  // ★ runLayout 返回时点云**已经收敛** (它内部同步预热), 所以这里能当场
  //   fit 出最终缩放。早先这一步挂在 d3 的 "end" 事件上, 要等 ~300 帧 rAF
  //   才触发 —— 用户进 tab 先看到一张巨大的图, 几秒后才突然缩到正常。
  //   ★ 也**不能**挂 "end" 做兜底 fit: 拖完节点松手同样会收敛一次, 那样
  //     每拖一下视图就自己缩放一次。视图只在开局和 ↺ 这两个时刻动。
  useEffect(() => {
    if (!data || !model || !measured) return;   // 等真实画布尺寸, 见 measured
    const ls = layoutSize(measured);
    const targets = radialTargets(data.nodes, model.degree, sectors, ls);
    const saved = restoreLayout(readSaved(), data.nodes.map((n) => n.id), measured);
    // ★ 存档按旧画布尺寸摆的绝对坐标这次没套用 —— 响亮说出来, 别让手工布局
    //   静默消失 (用户会以为 💾 又坏了)。再存一次即按当前画布重新固定。
    if (saved.stale) {
      setSaveHint("画布尺寸变了 (全屏/改窗口), 之前保存的手工布局按旧尺寸摆放,"
        + " 这次未套用; 当前由力学重新摆放。满意的话点 💾 按新尺寸重新保存。");
    }
    const h = runLayout(
      data.nodes, data.edges, ls, targets, saved.pos,
      (ps) => {
        const m: Record<string, { x: number; y: number }> = {};
        for (const p of ps) m[p.id] = { x: p.x, y: p.y };
        setPos(m);
      },
    );
    layout.current = h;
    fitTo(h.snapshot());   // 首屏即终局
    return () => { h.stop(); layout.current = null; };
  }, [data, model, sectors, measured, fitTo]);


  /** 💾 保存当前布局。
   *  ★ 文案必须区分"有固定位置的节点"与"全是力学算出来的": 开局 settle 是
   *    确定性的 (初始坐标放在 radialTargets 目标点上, d3 无随机抖动), 所以
   *    没拖过节点就点保存, 刷新前后布局**一模一样** (浏览器实测零个节点
   *    有差异, 最大位移 0.0px)。存档确实写了, 但用户看不出任何变化 ——
   *    上一轮就是因此报"保存按钮没啥作用"。不讲清这点, 按钮再修也还是像坏的。
   *  ★ 说"已固定"而不是"手工摆放": 从存档恢复的节点也是 fx 钉住的, 刷新一次
   *    后 pinnedCount 就是全部节点 —— 写"手工摆放 N 个"是谎报 (用户只拖了
   *    一两个)。"已固定"对两种来源都成立, 也正好提示 ↺ 能把它们放开。
   *  ★ 连当前画布尺寸一起存 (packLayout): 坐标是画布绝对值, 恢复时尺寸对不上
   *    就不该套用。存的必须是**实测**尺寸 (measured) 而非 FALLBACK 兜底值 ——
   *    存了猜的尺寸, 恢复时就没有一次能对上。 */
  const saveLayout = useCallback(() => {
    const h = layout.current;
    const snap = h?.snapshot();
    if (!h || !snap || !measured) return;
    try {
      localStorage.setItem(SAVE_KEY, JSON.stringify(packLayout(snap, measured)));
      const fixed = h.pinnedCount();
      setSaveHint(fixed > 0
        ? `已保存 ${Object.keys(snap).length} 个节点位置 (其中 ${fixed} 个位置已固定,`
          + ` 不再受力学摆放; 点 ↺ 可全部放开)`
        : `已保存 ${Object.keys(snap).length} 个节点位置。当前全部由力学摆放,`
          + ` 刷新后画面不会有变化 —— 拖动节点后再保存才看得出差别。`);
    } catch (e) {
      // 配额满/隐私模式: 响亮报出, 不要假装保存成功
      setSaveHint(`保存失败: ${(e as Error).message}`);
    }
  }, [measured]);

  /** ↺ 重置: 视图 + 布局一起回到力学初始态, 并清掉存档。
   *  ★ 语义比第二轮扩大了 —— 拖动改成"拖到哪留哪"之后, 必须有一条把手工
   *    摆位交还给力学的退路, 否则拖乱了就只能清浏览器缓存。
   *  ★ 不设回 IDENTITY: 布局在 SPREAD 倍空间里, k=1 就是一张巨图。
   *    unpinAll 已同步收敛完 (见 graphLayout 的 settle), 直接按新点云 fit ——
   *    与刚进页面走同一条路, 结果也一致。 */
  const resetAll = useCallback(() => {
    try { localStorage.removeItem(SAVE_KEY); } catch { /* 隐私模式忽略 */ }
    const h = layout.current;
    if (!h) return;
    h.unpinAll();
    fitTo(h.snapshot());
    setSaveHint("已重置布局并清除存档");
  }, [fitTo]);

  // 提示语几秒后自己消失 (省一个手动关闭按钮)。
  // ★ 6s 而非 2.6s: "当前全部由力学摆放, 刷新后画面不会有变化" 那条长得多,
  //   2.6 秒读不完就没了 —— 用户会觉得按钮依旧毫无反馈。
  useEffect(() => {
    if (saveHint === null) return;
    const t = setTimeout(() => setSaveHint(null), 6000);
    return () => clearTimeout(t);
  }, [saveHint]);

  if (errMsg) {
    return (
      <div className="graph-wrap">
        <div className="onto-error">
          加载本体图失败: {errMsg}
          <button className="cfg-btn-ghost" onClick={load} style={{ marginLeft: 10 }}>
            重试
          </button>
        </div>
      </div>
    );
  }
  if (!data || !model) return <div className="graph-detail-hint">加载中…</div>;
  if (data.nodes.length === 0) {
    return <div className="graph-detail-hint">本体无实体 (0 个节点), 无图可画。</div>;
  }

  const sel = selected ? data.nodes.find((n) => n.id === selected) : null;
  const selEdges = selected
    ? data.edges.filter((e) => e.source === selected || e.target === selected)
    : [];
  const zoomBtn = (f: number) => () =>
    setView((v) => zoomAt(v, size.w / 2, size.h / 2, f));

  return (
    <div className={`graph-wrap ${full ? "full" : ""}`}>
      <div className="graph-stage" ref={stageRef}>
        <div className="graph-toolbar">
          {/* 搜索: 回车选第一条命中并把视图挪过去。 */}
          <div className="graph-search">
            <input
              className="graph-search-input"
              value={query}
              placeholder="搜实体 id / 名称"
              data-graph-search
              onChange={(e) => { setQuery(e.target.value); setOpenList(true); }}
              onFocus={() => setOpenList(true)}
              onBlur={() => setOpenList(false)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && hits.length > 0) locate(hits[0].id);
                if (e.key === "Escape") { setQuery(""); setOpenList(false); }
              }}
            />
            {openList && hits.length > 0 && (
              <div className="graph-search-list" data-graph-search-list>
                {hits.map((n) => (
                  <button key={n.id} className="graph-search-hit"
                          data-hit={n.id}
                          // ★ preventDefault 阻止输入框失焦, 这是本项的关键:
                          //   blur 在 mousedown 那一刻就触发, 若借它关下拉,
                          //   按住超过那点时间列表就已卸载, 浏览器不再派发
                          //   click (它要求 down/up 落在同一个还在的元素上) ——
                          //   于是"点了没反应"。按得快又碰巧能成, 表现为随机失灵。
                          //   延时关闭只是把窗口放宽, 治标; 不失焦才没有窗口。
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => locate(n.id)}>
                    <span className="graph-search-dot"
                          style={{ background: model.colors[n.id] }} />
                    {n.displayName}
                    {n.displayName !== n.id && (
                      <span className="graph-search-id">{n.id}</span>
                    )}
                  </button>
                ))}
              </div>
            )}
            {openList && query.trim() !== "" && hits.length === 0 && (
              <div className="graph-search-list" data-graph-search-list>
                <div className="graph-search-empty">无匹配实体</div>
              </div>
            )}
          </div>
          <button className="graph-tool" onClick={zoomBtn(BTN_STEP)}
                  title="放大 (也可滚轮)" data-act="zoom-in">＋</button>
          <button className="graph-tool" onClick={zoomBtn(1 / BTN_STEP)}
                  title="缩小 (也可滚轮)" data-act="zoom-out">－</button>
          <button className="graph-tool" onClick={fitAll}
                  title="适应画布: 把所有节点收进视野" data-act="fit">⤢</button>
          <button className="graph-tool" onClick={saveLayout}
                  title="保存当前布局到本浏览器 (刷新后仍在)" data-act="save">💾 保存</button>
          <button className="graph-tool" onClick={resetAll}
                  title="重置: 视图复位 + 交还力学摆放 + 清除已保存布局"
                  data-act="reset">↺</button>
          <button className="graph-tool" onClick={() => setFull((x) => !x)}
                  title={full ? "退出全屏" : "全屏铺满窗口"} data-act="full">
            {full ? "⤡ 退出" : "⛶ 全屏"}
          </button>
          <span className="graph-tool-hint">滚轮缩放 · 拖空白平移 · 拖节点移动</span>
          {saveHint && (
            <span className="graph-save-hint" data-save-hint>{saveHint}</span>
          )}
        </div>
        <GraphCanvas
          nodes={data.nodes} edges={data.edges} pos={pos}
          colors={model.colors} degree={model.degree} offMain={model.offMain}
          kinds={model.kinds} geoms={model.geoms}
          dimDomain={dimDomain} selected={selected}
          onSelect={setSelected}
          onOpen={onJumpToRelations}
          onDrag={(id, x, y) => layout.current?.pin(id, x, y)}
          onDragEnd={(id) => layout.current?.unpin(id)}
          size={size}
          view={view} onViewChange={setView}
        />
      </div>
      <div className="graph-side">
        <GraphLegend items={model.legend} active={dimDomain} onPick={setDimDomain} />
        <div className="graph-detail">
          {sel ? (
            <>
              <div className="graph-detail-title" data-detail-for={sel.id}>
                {sel.displayName}
              </div>
              <div className="graph-detail-row">id: {sel.id}</div>
              <div className="graph-detail-row">域: {sel.domain || "(未归域)"}</div>
              <div className="graph-detail-row">库: {sel.dataSource || "—"}</div>
              <div className="graph-detail-row" data-detail-degree={model.degree[sel.id]}>
                度数: {model.degree[sel.id]}
                {model.offMain.has(sel.id) && " · ⚠ 游离在主干之外"}
              </div>
              <div className="graph-detail-row">{sel.summary}</div>
              <div className="graph-detail-row">关联边 {selEdges.length} 条:</div>
              {selEdges.map((e, i) => (
                <div key={i} className="graph-detail-hint">
                  {e.source} → {e.target} · {e.label ?? e.via}
                </div>
              ))}
              <div className="graph-detail-hint">双击节点可跳到"关系"页筛选</div>
            </>
          ) : (
            <div className="graph-detail-hint">
              单击节点看详情, 双击跳"关系"页。红环 = 游离在主干之外的实体,
              橙色虚线 = 跨库边 (不能拼进一条 SQL)。
              <br />节点按域聚成扇区, 度数越高越靠圆心。看不到的部分用 ⤢ 收回视野。
              <br />拖动节点后它就留在原地; 💾 保存到本浏览器, ↺ 交还力学摆放。
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
