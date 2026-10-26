// graphModel — 本体关联网络图的纯展示逻辑 (拆出来供 vitest 单测, 不含 React/DOM)。
// 职责: 算。配色/图例/连通分量/度数/边分类/边几何。所有判断都在这里,
// GraphCanvas 只负责铺元素不做判断 —— 这样组件的圈复杂度天然可控。
import type { GraphNode, GraphEdge, GraphDomain } from "./api";

/** 域名 → 稳定 HSL 色。同名恒同色 (不带随机), 用于库里没配色的域和无域实体。 */
function hashColor(code: string): string {
  let h = 0;
  for (let i = 0; i < code.length; i++) {
    h = (h * 31 + code.charCodeAt(i)) % 360;
  }
  return `hsl(${h}, 62%, 52%)`;
}

/** 域配色: 库里的 domain.color 是真相源 (与 DomainsPanel 色块一致);
 *  NULL / 查无此域 / 无域(空串) 一律走域名哈希兜底。
 *  ★ 真库里确实有域的 color 存着 NULL, 所以兜底是必需的不是防御性编程。 */
export function domainColor(code: string, domains: GraphDomain[]): string {
  const hit = domains.find((d) => d.code === code);
  return hit?.color ?? hashColor(code);
}

export type LegendItem = { key: string; code: string; color: string; count: number; note: string };

/** 图例: 域名+色+实体数。
 *  ★ key 是过滤键 (真实 domain 串; 无域桶是 ""), code 是显示名 ("(无域)")。
 *    两者必须分离: Canvas 拿节点真 domain 与过滤值比对, 无域节点 domain 是
 *    "" — 拿显示名当过滤值则 "" !== "(无域)" 恒真, 全图节点全暗 (Bug 2)。
 *  ★ 排除零实体域 (没有节点可上色, 列出来是噪音; 真库里确实存在零实体的域)。
 *  ★ note 标注数据空洞 ("未配色"/"未归域"), 让缺失可见而不是被哈希色静音。 */
export function buildLegend(nodes: GraphNode[], domains: GraphDomain[]): LegendItem[] {
  const count: Record<string, number> = {};
  for (const n of nodes) count[n.domain] = (count[n.domain] ?? 0) + 1;

  const items: LegendItem[] = [];
  for (const d of domains) {
    const c = count[d.code] ?? 0;
    if (c === 0) continue;   // 零实体域不渲染也不列图例
    items.push({
      key: d.code, code: d.code, color: domainColor(d.code, domains), count: c,
      note: d.color ? "" : "未配色",
    });
  }
  if (count[""]) {
    items.push({
      key: "", code: "(无域)", color: domainColor("", domains),
      count: count[""], note: "未归域",
    });
  }
  return items;
}

/** 度数 (两端各记一次; 自环同一点记 2)。驱动节点半径。 */
export function nodeDegree(nodes: GraphNode[], edges: GraphEdge[]): Record<string, number> {
  const deg: Record<string, number> = {};
  for (const n of nodes) deg[n.id] = 0;   // 零边节点也要有键, 不留 undefined
  for (const e of edges) {
    if (e.source in deg) deg[e.source] += 1;
    if (e.target in deg) deg[e.target] += 1;
  }
  return deg;
}

/** 零边实体 (真库里有少量这种一条边都没有的实体)。 */
export function isolatedNodes(nodes: GraphNode[], edges: GraphEdge[]): string[] {
  const deg = nodeDegree(nodes, edges);
  return nodes.filter((n) => deg[n.id] === 0).map((n) => n.id);
}

/** 连通分量: id → 分量序号, 按分量大小降序编号 (0 = 最大分量)。
 *  ★ 真库是"一个大主干 + 几个零散小分量" —— 表格翻页永远看不出的东西,
 *  Task 5 靠 `序号 !== 0` 给"游离在主干之外"的实体标红环
 *  (小的双实体分量和孤立实体都会被标出来)。 */
export function connectedComponents(
  nodes: GraphNode[], edges: GraphEdge[],
): Record<string, number> {
  const adj: Record<string, string[]> = {};
  for (const n of nodes) adj[n.id] = [];
  for (const e of edges) {
    if (e.source in adj && e.target in adj) {
      adj[e.source].push(e.target);
      adj[e.target].push(e.source);
    }
  }
  const seen = new Set<string>();
  const groups: string[][] = [];
  for (const n of nodes) {
    if (seen.has(n.id)) continue;
    const stack = [n.id];
    const group: string[] = [];
    while (stack.length) {
      const x = stack.pop() as string;
      if (seen.has(x)) continue;
      seen.add(x);
      group.push(x);
      stack.push(...adj[x]);
    }
    groups.push(group);
  }
  groups.sort((a, b) => b.length - a.length);   // 降序: 0 号 = 最大分量
  const out: Record<string, number> = {};
  groups.forEach((g, i) => g.forEach((id) => { out[id] = i; }));
  return out;
}

export type EdgeKind = "inner" | "inter-domain" | "cross-source";

/** 边分类 → 驱动线型。判定优先级: 跨库 > 跨域 > 域内。
 *  ★ 跨库靠 dataSource 判, 不能靠 domain —— 某个域恰好与某个库一一对应
 *  是巧合不是判据。跨库边不能拼进一条 SQL, 图上画虚线区分。
 *  端点查不到的边按 inter-domain 处理 (不炸)。 */
export function edgeKind(edge: GraphEdge, nodes: GraphNode[]): EdgeKind {
  const s = nodes.find((n) => n.id === edge.source);
  const t = nodes.find((n) => n.id === edge.target);
  if (!s || !t) return "inter-domain";
  if (s.dataSource !== t.dataSource) return "cross-source";
  return s.domain === t.domain ? "inner" : "inter-domain";
}

export type EdgeGeom = { selfLoop: boolean; index: number; total: number };

/** 边几何: 标出自环 + 给同一实体对内的重边编号 (驱动弧度偏移)。
 *  返回数组与入参等长同序。
 *
 *  ★ 为什么必须做: 真库里既有自环 (直线渲染起终点重合画成一个点, 完全
 *  不可见), 也有成对的重边 (两条直线完全重叠看起来只有一条)。
 *  不处理就会**静默掩盖**"这两个实体之间
 *  有两种不同关联"—— 而这恰是图应当揭示、表格难以察觉的信息。
 *  方向相反的两条边也算同一对 (a→b 与 b→a 在无向布局里同样重叠)。 */
export function edgeGeometry(edges: GraphEdge[]): EdgeGeom[] {
  const total: Record<string, number> = {};
  const pairKey = (e: GraphEdge) => [e.source, e.target].sort().join(" ");
  for (const e of edges) {
    const k = pairKey(e);
    total[k] = (total[k] ?? 0) + 1;
  }
  const used: Record<string, number> = {};
  return edges.map((e) => {
    const k = pairKey(e);
    const index = used[k] ?? 0;
    used[k] = index + 1;
    return { selfLoop: e.source === e.target, index, total: total[k] };
  });
}

/** 一条边的 SVG path。自环画成节点上方的小环; 重边按序号做弧度偏移
 *  (不偏移的话同一对实体的多条边完全重叠, 看起来只有一条)。
 *  ★ 从 GraphCanvas 移入: path 形状是"判断" (哪条边画哪条曲线),
 *  留在 .tsx 里就永远没有单测能碰它 —— 对向重边抵消 bug 正是这么漏掉的。 */
export function edgePath(
  x1: number, y1: number, x2: number, y2: number, g: EdgeGeom,
): string {
  if (g.selfLoop) {
    // 节点上方的小环 (起终点同一点, 直线会退化成一个点)
    return `M ${x1} ${y1} C ${x1 - 34} ${y1 - 46}, ${x1 + 34} ${y1 - 46}, ${x1} ${y1}`;
  }
  if (g.total <= 1) return `M ${x1} ${y1} L ${x2} ${y2}`;
  // 同对多条边: 以中点法线方向拉开, 序号决定偏移量与方向
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  // ★ 方向归一: 对向存储的重边 (a→b 与 b→a, 真库里有这种写法) 各按自己的
  //   (dx,dy) 算法线 → 法线方向相反, 而 index 的正负号也相反 → 负负得正,
  //   两条二次贝塞尔的控制点恰好重合 (起终点本来就是同一对, 曲线几何上
  //   完全同一条), 弧度偏移自我抵消。先给端点对定规范朝向 (坐标小的一端
  //   当 "首"), 同一对的所有边共用同一条法线, 序号才真正把它们分开。
  const flip = x1 > x2 || (x1 === x2 && y1 > y2);
  const [ax, ay, bx, by] = flip ? [x2, y2, x1, y1] : [x1, y1, x2, y2];
  const dx = bx - ax;
  const dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  const spread = (g.index - (g.total - 1) / 2) * 26;
  return `M ${x1} ${y1} Q ${mx - (dy / len) * spread} ${my + (dx / len) * spread}, ${x2} ${y2}`;
}

/** 度数 → 半径 (平方根缩放: 度数最高的枢纽节点不会大到盖住别人)。 */
export function radius(deg: number): number {
  return 6 + Math.sqrt(deg) * 2.6;
}

/** label 截断 (超长带省略号; 等长不截)。 */
export function truncateLabel(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n)}…`;
}

// ── 扇区径向布局 ──────────────────────────────────────────────────────
// 自由力导向的问题是"没规律": 同域实体散落各处, 上百条边看不出结构。
// 这里给每个域分一段圆周角, 域内按度数定半径 (枢纽靠内/叶子靠外),
// 算出的点交给 d3 的 forceX/forceY 当**目标**而非硬坐标 —— 仍是力学布局
// (边的拉力照常参与微调), 只是整体收成同域抱团的圆盘。

export type Sector = { start: number; mid: number; end: number };

/** 域 → 圆周角区间 (弧度, 从 0 起顺时针铺满 2π)。
 *  角度按该域实体数成比例 —— 大域占宽扇区, 免得实体最多的那个域被挤在一小段圆周里。
 *
 *  ★ 键是**真 domain 值**, 无域桶的键是 `""` 不是显示名 —— 与 buildLegend
 *    的 key/code 分离同一个理由 (下游拿 n.domain 查表, 显示名查不到)。
 *  ★ 零实体域不分扇区 (真库里确有零实体的域, 分了纯粹白占圆周角)。
 *  ★ 排序: domains 表的 displayOrder 优先 (与 DomainsPanel 一致), 表里
 *    没有的域垫在后面, 无域桶永远最后 —— 保证同一份数据每次刷新图形一致。 */
export function domainSectors(
  nodes: GraphNode[], domains: GraphDomain[],
): Record<string, Sector> {
  const count: Record<string, number> = {};
  for (const n of nodes) count[n.domain] = (count[n.domain] ?? 0) + 1;
  if (nodes.length === 0) return {};

  const order = new Map(domains.map((d, i) => [d.code, d.displayOrder ?? i]));
  const codes = Object.keys(count).sort((a, b) => {
    if (a === "") return 1;               // 无域桶垫最后
    if (b === "") return -1;
    const oa = order.get(a);
    const ob = order.get(b);
    if (oa === undefined && ob === undefined) return a.localeCompare(b);
    if (oa === undefined) return 1;       // 表里没登记的域排在已登记之后
    if (ob === undefined) return -1;
    return oa - ob;
  });

  const out: Record<string, Sector> = {};
  let acc = 0;
  for (const code of codes) {
    const span = (count[code] / nodes.length) * Math.PI * 2;
    out[code] = { start: acc, mid: acc + span / 2, end: acc + span };
    acc += span;
  }
  return out;
}

/** 节点 → 布局目标点 (画布坐标)。
 *  角度: 落在本域扇区内, 域内均匀错开 (等分且取格心, 故两点必不重合)。
 *  半径: 度数越高越靠圆心 —— 枢纽居中符合"重要的在中间"的读图直觉,
 *        叶子和孤立实体停在外环, 一眼看出谁在边缘。
 *  ★ 半径用 √ 缩放: 度数最高的枢纽与一个低度数实体的差别不该是整数倍线性放大。 */
export function radialTargets(
  nodes: GraphNode[],
  degree: Record<string, number>,
  sectors: Record<string, Sector>,
  size: { w: number; h: number },
): Record<string, { x: number; y: number }> {
  const cx = size.w / 2;
  const cy = size.h / 2;
  const rOuter = (Math.min(size.w, size.h) / 2) * 0.86;   // 留边给节点标签
  const rInner = rOuter * 0.22;
  const maxDeg = Math.max(0, ...nodes.map((n) => degree[n.id] ?? 0));

  // 按域分组; 组内定序 (度数降序 + id) 保证同一份数据每次结果一致
  const groups: Record<string, GraphNode[]> = {};
  for (const n of nodes) (groups[n.domain] ??= []).push(n);
  const FULL: Sector = { start: 0, mid: Math.PI, end: Math.PI * 2 };

  const out: Record<string, { x: number; y: number }> = {};
  for (const [code, members] of Object.entries(groups)) {
    const sec = sectors[code] ?? FULL;   // 扇区表不匹配时退整圈, 不炸
    const sorted = [...members].sort(
      (a, b) => (degree[b.id] ?? 0) - (degree[a.id] ?? 0) || a.id.localeCompare(b.id),
    );
    sorted.forEach((n, i) => {
      // 取格心 (i+0.5)/m: 不贴扇区边界, 且同扇区内必不同角
      const ang = sec.start + ((i + 0.5) / sorted.length) * (sec.end - sec.start);
      const t = maxDeg > 0 ? Math.sqrt(degree[n.id] ?? 0) / Math.sqrt(maxDeg) : 0;
      const r = rOuter - (rOuter - rInner) * t;
      out[n.id] = { x: cx + Math.cos(ang) * r, y: cy + Math.sin(ang) * r };
    });
  }
  return out;
}

// ── 视图变换 (缩放 / 平移) ─────────────────────────────────────────────
// SVG 上套一层 translate(tx,ty) scale(k)。变换与反变换是**纯函数**, 所以
// 住在这里而不是 GraphCanvas.tsx —— 上一轮 edgePath 的教训: 判断留在 .tsx
// 里就永远没有单测能碰它 (项目无 jsdom)。

export type View = { tx: number; ty: number; k: number };

export const ZOOM_MIN = 0.2;
export const ZOOM_MAX = 4;

export function clampZoom(k: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, k));
}

/** 屏幕坐标 → 图坐标。拖节点必须过这一层: 直接用鼠标坐标当图坐标,
 *  缩放后节点会以 k 倍速度跟手飞走。 */
export function screenToGraph(sx: number, sy: number, v: View): { x: number; y: number } {
  return { x: (sx - v.tx) / v.k, y: (sy - v.ty) / v.k };
}

/** 以屏幕点 (sx,sy) 为锚点缩放 factor 倍。锚点下的图坐标保持不变,
 *  否则滚轮时整张图会往一边漂。
 *  ★ tx/ty 必须用**夹过的** k 反算 —— 拿未夹的 k 算位移是这里的经典 bug:
 *    缩放触顶后 k 不再变而 tx 继续变, 图会在原地滑动。 */
export function zoomAt(v: View, sx: number, sy: number, factor: number): View {
  const k = clampZoom(v.k * factor);
  return {
    k,
    tx: sx - ((sx - v.tx) / v.k) * k,
    ty: sy - ((sy - v.ty) / v.k) * k,
  };
}

/** 适应画布: 把所有点的外接框居中缩放到画布内 (留 pad 边距)。
 *  节点被拖出视野后靠它一键找回。
 *  ★ 已知取舍: 点云太散以致理想 k 低于 ZOOM_MIN 时以下限为准, 此时边缘
 *    节点仍会在画布外 (当前真库的规模远达不到这个尺度)。 */
export function fitView(
  pts: { x: number; y: number }[], size: { w: number; h: number }, pad: number,
): View {
  if (pts.length === 0) return { tx: 0, ty: 0, k: 1 };
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  // 单点/一条水平线时宽高为 0 → 垫 1 防除零 (k 会被 clamp 到上限)
  const k = clampZoom(Math.min(
    (size.w - pad * 2) / (maxX - minX || 1),
    (size.h - pad * 2) / (maxY - minY || 1),
  ));
  return {
    k,
    tx: size.w / 2 - ((minX + maxX) / 2) * k,
    ty: size.h / 2 - ((minY + maxY) / 2) * k,
  };
}

/** 保持当前缩放, 把图坐标点 p 挪到画布正中。
 *  ★ 搜索定位必须配这一步: 只 setSelected 的话, 节点若在视野外 (或被拖远)
 *    用户看到的是"搜了没反应" —— 高亮发生在看不见的地方。
 *  ★ 不改 k: 搜索是"找到它", 不是"重新构图"。改了缩放用户会失去方位感,
 *    想看全貌有 ⤢。 */
export function centerOn(
  p: { x: number; y: number }, size: { w: number; h: number }, k: number,
): View {
  return { k, tx: size.w / 2 - p.x * k, ty: size.h / 2 - p.y * k };
}

// ── 实体搜索 ──────────────────────────────────────────────────────────

/** 按 id / 显示名搜实体, 返回最多 limit 条候选。
 *  ★ 匹配 displayName 而不只是 id: 图上节点标签显示的是 displayName,
 *    用户照着标签搜却搜不到会很困惑。
 *  ★ 排序把"前缀命中"提到"中间命中"之前 —— 搜 `order` 时 order_lib 该排在
 *    lib_reader_order 前面。同档内按 id 字典序, 保证同一查询结果稳定。
 *  ★ 空/纯空白查询返回空数组 (而不是全部): 调用方据此不显示下拉, 免得
 *    输入框一聚焦就糊上全部实体。 */
export function searchNodes(
  nodes: GraphNode[], query: string, limit = 8,
): GraphNode[] {
  const q = query.trim().toLowerCase();
  if (q === "") return [];
  const scored: { n: GraphNode; rank: number }[] = [];
  for (const n of nodes) {
    const id = n.id.toLowerCase();
    const name = n.displayName.toLowerCase();
    const iId = id.indexOf(q);
    const iName = name.indexOf(q);
    if (iId < 0 && iName < 0) continue;
    // 0 = id 前缀, 1 = 名前缀, 2 = 其它位置命中
    const rank = iId === 0 ? 0 : iName === 0 ? 1 : 2;
    scored.push({ n, rank });
  }
  scored.sort((a, b) => a.rank - b.rank || a.n.id.localeCompare(b.n.id));
  return scored.slice(0, limit).map((s) => s.n);
}

// ── 铺开 / 保存布局 ───────────────────────────────────────────────────

/** 布局空间相对画布的放大倍数。
 *  ★ 关键在于"铺开"和"缩小"是两件事: 节点密集的解药是把**布局空间**放大
 *    (节点间距按图坐标变大, 标签不再互相压盖), 再靠初始 fitView 把整张图
 *    收进画布 —— 于是首屏 k<1, 视觉上既铺开了又全看得见。
 *    若只调 zoom 不动布局, 节点和标签会一起变小, 压盖照旧。 */
export const SPREAD = 1.4;

/** 画布尺寸 → 布局空间尺寸 (等比放大, 不改长宽比: 拉比例会把圆盘压成椭圆)。 */
export function layoutSize(size: { w: number; h: number }): { w: number; h: number } {
  return { w: size.w * SPREAD, h: size.h * SPREAD };
}

/** 手工布局存档的形状。★ 必须带 size: 节点坐标是**画布绝对值**, 换了画布
 *  尺寸就不能再套用 (见 restoreLayout)。老版存档只有裸 pos, 会被判过期。 */
export type SavedLayout = {
  size: { w: number; h: number };
  pos: Record<string, { x: number; y: number }>;
};

/** 把当前坐标 + 当前画布尺寸打成一份存档。
 *  ★ 与 restoreLayout 配对存在 —— 读写两头各自硬编码这个结构, 将来改格式时
 *    只改一头就会静默分叉 (存进去的读不回来, 而表现只是"手工布局莫名失效")。 */
export function packLayout(
  pos: Record<string, { x: number; y: number }>,
  size: { w: number; h: number },
): SavedLayout {
  return { size: { w: size.w, h: size.h }, pos };
}

function sameSize(a: unknown, cur: { w: number; h: number }): boolean {
  if (a === null || typeof a !== "object") return false;
  const { w, h } = a as { w?: unknown; h?: unknown };
  if (typeof w !== "number" || typeof h !== "number") return false;
  return w === cur.w && h === cur.h;
}

/** 恢复已保存的布局: 只留下当前本体里仍存在、且坐标是有限数的条目。
 *
 *  ★★ 画布尺寸不一致就**整份丢弃**并回报 stale。存的是绝对画布坐标, 而起
 *    模拟那个 effect 依赖 size —— 全屏 (900×620 → 1900×900) 或改窗口宽度都会
 *    重跑, 届时按旧画布摆的存档坐标会和按新画布重算的 radialTargets 混在一起,
 *    图就歪了。等比缩放是另一条路, 但长宽比一变只能取一个比例 (压扁或留白),
 *    且缩放后的点与新目标点仍不在同一坐标系 —— 用户拍板走"不匹配就不套用":
 *    诚实, 代价只是换画布后手工布局失效一次 (由 stale 提示告知, 不静默)。
 *
 *  ★ 两道值过滤都不是防御性编程:
 *    - 存档是旧快照, 实体删掉后照喂会把幽灵坐标钉进模拟;
 *    - 一个 NaN 进 d3 会顺着力学传播, 让**整张图**的坐标全变 NaN (节点全
 *      消失), 而 localStorage 的内容是用户可改的任意 JSON。
 *
 *  ★ stale 只在"确实有一份存档但这次没套用"时为 true。从没保存过的人
 *    (raw 为 null) 不该看到"手工布局未套用"的提示。 */
export function restoreLayout(
  raw: unknown, liveIds: string[], size: { w: number; h: number },
): { pos: Record<string, { x: number; y: number }>; stale: boolean } {
  const pos: Record<string, { x: number; y: number }> = {};
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { pos, stale: false };
  }
  const saved = raw as { size?: unknown; pos?: unknown };
  if (!sameSize(saved.size, size)) return { pos, stale: true };
  if (saved.pos === null || typeof saved.pos !== "object") {
    return { pos, stale: false };
  }
  const live = new Set(liveIds);
  for (const [id, v] of Object.entries(saved.pos as Record<string, unknown>)) {
    if (!live.has(id)) continue;
    if (v === null || typeof v !== "object") continue;
    const { x, y } = v as { x?: unknown; y?: unknown };
    if (typeof x !== "number" || typeof y !== "number") continue;
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    pos[id] = { x, y };
  }
  return { pos, stale: false };
}
