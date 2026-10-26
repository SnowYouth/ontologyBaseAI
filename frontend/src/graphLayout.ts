// graphLayout — d3-force 力学模拟封装。
// ★ 本项目唯一 import d3 的文件。将来若换成 canvas 渲染器 (react-force-graph
// 等), 改动面就是这个文件 + GraphCanvas.tsx 两个, graphModel.ts 及其单测
// 一行不动 —— 这是 spec 里 "换渲染器成本局部" 的物理落点。
//
// 不写单测: d3 模拟是有状态迭代 (mutate node 对象写 x/y/vx/vy), 包成纯函数
// 会失真。布局正确性由 gstack 浏览器取证覆盖。
import {
  forceSimulation, forceLink, forceManyBody, forceCollide, forceX, forceY,
  type Simulation, type SimulationNodeDatum,
} from "d3-force";
import type { GraphNode, GraphEdge } from "./api";
import { SPREAD } from "./graphModel";

export type Positioned = { id: string; x: number; y: number };

export type LayoutHandle = {
  /** 停模拟 (组件卸载时必调, 否则 tick 回调会往已卸载组件里 setState)。 */
  stop: () => void;
  /** 钉住某节点 (拖拽中)。 */
  pin: (id: string, x: number, y: number) => void;
  /** 松开 (拖拽结束)。
   *  ★ 实现里**仍保持钉住** —— 详见 runLayout 文档注释的"拖到哪留哪"。 */
  unpin: (id: string) => void;
  /** 解钉全部 (重置按钮): 让所有节点重新交给力学摆。 */
  unpinAll: () => void;
  /** 当前所有节点坐标 (保存布局用)。 */
  snapshot: () => Record<string, { x: number; y: number }>;
  /** 有几个节点**位置已固定** (fx/fy 非空: 拖过的, 或从存档恢复的)。
   *  ★ 保存提示语要靠它区分两种情况: 开局 settle 是确定性的, 没拖过节点就
   *    点 💾, 存档虽然真写了但刷新前后布局一模一样 (浏览器实测零差异) ——
   *    提示语必须讲清"看不出变化是正常的", 否则用户会以为按钮坏了。
   *  ★ 语义是"已固定"而非"手工拖过": 存档恢复的节点也钉着 fx, 刷新一次后
   *    这个数就是全部节点。调用方文案别写成"手工摆放 N 个" (那是谎报)。 */
  pinnedCount: () => number;
  /** 手动推进 n 步。★ 仅供取证脚本用 —— 浏览器里 d3 自己按 rAF 跑,
   *  生产代码不该调它。暴露出来是因为"松手不回弹"只能靠**跑够多 tick 后
   *  坐标仍没变**来证明, 而 d3 的 sim 对象本身不该漏给调用方。 */
  tick: (n?: number) => void;
};

type SimNode = SimulationNodeDatum & { id: string };

/** 起模拟时同步跑掉的帧数。300 = d3 默认 alpha 衰减到 alphaMin 所需步数,
 *  即"跑完一整轮"。几十节点的规模下耗时几十毫秒, 而前 100 步就已基本收敛到
 *  终局缩放 —— 取 300 是为了跑满而不是压时间。 */
const WARMUP = 300;

/** 起一轮力导向布局。每 tick 回调一次当前坐标。
 *
 * ★ 传给 d3 的是 nodes/edges 的**副本** —— forceLink 会把 link.source/target
 * 从字符串就地替换成对象引用, 传原数组会污染调用方的数据 (下游
 * edgeGeometry 的 pairKey 排序会拿到对象而炸)。
 *
 * ★ `targets` = graphModel.radialTargets 算出的扇区目标点。用 forceX/forceY
 * 往目标点**拉**而不是直接赋坐标 —— 仍是力学布局 (边的拉力/斥力照常参与
 * 微调, 强连通的实体会互相靠近), 只是整体收成同域抱团的圆盘, 不是把节点
 * 硬钉在算好的格子上 (那样就退化成静态圆环布局, 看不出连接紧密程度)。
 * 目标点同时**顶掉了 forceCenter**: 径向力已经定住了全局位置, 再叠一个
 * 向心力会把扇区往中间压扁。
 *
 * ★ `pinned` = 初始就钉住的节点 (从已保存布局恢复)。
 *
 * ★ 力参数按 SPREAD 同步放松 (distance/charge/collide 都是**图坐标**下的
 * 绝对值): 只放大 targets 不放大这三项, 节点会被原样的斥力/碰撞半径重新
 * 挤回原来的密度, 铺开就白做了。
 */
export function runLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  size: { w: number; h: number },
  targets: Record<string, { x: number; y: number }>,
  pinned: Record<string, { x: number; y: number }>,
  onTick: (p: Positioned[]) => void,
): LayoutHandle {
  // 初始坐标直接放到目标点: 模拟从"已经成形"的状态开始收敛, 省掉开局
  // 那几十帧乱飞 (也让首屏截图就是最终形态)。已保存的节点用存档坐标并钉住。
  const simNodes: SimNode[] = nodes.map((n) => {
    const p = pinned[n.id];
    const x = p?.x ?? targets[n.id]?.x ?? size.w / 2;
    const y = p?.y ?? targets[n.id]?.y ?? size.h / 2;
    return p ? { id: n.id, x, y, fx: x, fy: y } : { id: n.id, x, y };
  });
  const byId = new Map(simNodes.map((n) => [n.id, n]));
  // 自环喂给 forceLink 无意义 (两端同点), 过滤掉; 它的渲染由 edgeGeometry 管
  const simLinks = edges
    .filter((e) => e.source !== e.target && byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const tx = (n: SimNode) => targets[n.id]?.x ?? size.w / 2;
  const ty = (n: SimNode) => targets[n.id]?.y ?? size.h / 2;

  const sim: Simulation<SimNode, undefined> = forceSimulation(simNodes)
    .force("link", forceLink(simLinks).id((d) => (d as SimNode).id)
      .distance(70 * SPREAD).strength(0.25))
    .force("charge", forceManyBody().strength(-170 * SPREAD * SPREAD))
    .force("sector-x", forceX<SimNode>(tx).strength(0.42))
    .force("sector-y", forceY<SimNode>(ty).strength(0.42))
    .force("collide", forceCollide(24 * SPREAD));

  const push = () =>
    onTick(simNodes.map((n) => ({ id: n.id, x: n.x ?? 0, y: n.y ?? 0 })));

  /** 同步跑完一整轮收敛, 然后把坐标推给调用方。
   *
   *  ★ 开局和 ↺ 重置**共用**这一条路, 因为它们是同一个病:
   *    点云形状要变 (开局从目标点散开 / 重置从手工摆位散回), 而调用方的视图
   *    还停在旧缩放上。若交给 d3 自己按 rAF 慢慢跑 (~300 帧, 肉眼几秒), 用户
   *    就会看到"先一张巨图, 停几秒突然缩到正常"—— 开局和点 ↺ 各被报过一次。
   *    同步 settle 完再交坐标, 调用方当场就能 fit 出最终缩放。
   *  ★ sim.tick() 不派发 "tick"/"end" 事件, 所以这 300 步对外完全静默,
   *    没有中间态闪动; 跑完 alpha 已到 alphaMin, d3 的定时器下一拍即停。
   *    代价几十毫秒 (几十节点规模), 只在这两个时刻付。 */
  const settle = () => { sim.tick(WARMUP); push(); };

  // 开局: 先 settle 再挂监听 —— 上面 300 步不该触发 onTick 洪水,
  // 一次 push 就够 (首帧即终局)。
  sim.tick(WARMUP);

  sim.on("tick", push);
  // ★ 刻意**不挂** sim.on("end"): 视图只在开局和 ↺ 这两个时刻动, 而 "end"
  //   每次收敛都触发 —— 拖完节点松手也算一次, 挂了就变成"每拖一下视图自己
  //   缩放一次"。这两个时刻现在都由 settle() 同步处理, 不需要事件。

  push();   // 预热结果先推一次 (上面的 sim.tick 是静默的)

  return {
    stop: () => { sim.stop(); },
    pin: (id, x, y) => {
      const n = byId.get(id);
      if (!n) return;
      n.fx = x; n.fy = y;
      sim.alphaTarget(0.3).restart();
    },
    // ★ 松手后**不清 fx/fy** —— 节点拖到哪留哪, 这是用户的明确要求。
    //   清掉的话径向力 (strength 0.42) 会立刻把它拽回扇区目标点, 等于白拖。
    //   不重合由 forceCollide 负责: 被挤开的是**周围没钉住的**节点, 让路。
    //   想恢复力学摆放走 unpinAll (工具栏 ↺)。
    unpin: (id) => {
      if (!byId.has(id)) return;
      sim.alphaTarget(0);
    },
    unpinAll: () => {
      for (const n of simNodes) { n.fx = null; n.fy = null; }
      // ★ 必须 alpha(1) 重新加热: 此时模拟已收敛 (alpha 衰减到 alphaMin),
      //   只 restart() 而不抬 alpha 的话节点根本不会动 —— 解钉了但画面不变,
      //   看起来像"↺ 没反应"。alphaTarget(0) 让它这轮跑完自己停住。
      sim.alpha(1).alphaTarget(0);
      // ★ 同步收敛完再交坐标 (同开局): 否则解钉后点云还在散开的半路上,
      //   调用方这时 fit 出来的缩放偏大, 用户看到"点重置又变成很大一张图"。
      settle();
    },
    snapshot: () => {
      const out: Record<string, { x: number; y: number }> = {};
      for (const n of simNodes) out[n.id] = { x: n.x ?? 0, y: n.y ?? 0 };
      return out;
    },
    // fx/fy 非空 ⟺ 该节点被钉住 (拖过或从存档恢复), 见 unpin 的注释
    pinnedCount: () =>
      simNodes.filter((n) => n.fx !== null && n.fx !== undefined).length,
    tick: (n = 1) => { sim.tick(n); onTick(simNodes.map((s) => ({ id: s.id, x: s.x ?? 0, y: s.y ?? 0 }))); },
  };
}
