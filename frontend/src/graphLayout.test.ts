// graphLayout 的行为测试。
//
// ★ 为什么这个文件存在: graphLayout.ts 的注释原本写着"不写单测, 布局正确性
//   由浏览器取证覆盖"。那对**布局美观**成立, 但对"拖动松手后节点留在原地"
//   不成立 —— 那是一条确定性的状态合同 (fx/fy 留不留), 在 node 里驱动真实
//   d3 模拟就能钉死, 不需要浏览器。上一轮把它划进"取证覆盖"是划错了层:
//   结果是 unpinAll 里一个真 bug (收敛后不重新加热 → 解钉了画面却不动)
//   没有任何测试能发现, 靠人眼在浏览器里看。
//
// ★ 这里**不 mock d3**: 力学是被测行为本身, mock 掉就什么都没测。
// ★ 判据都放在"跑够多 tick 之后" —— 松手瞬间读坐标当然没变, 那验的是 pin
//   不是 unpin。回弹是个需要几百帧才显形的现象。
import { describe, it, expect } from "vitest";
import type { GraphNode, GraphEdge } from "./api";
import { runLayout } from "./graphLayout";
import {
  fitView, layoutSize, radialTargets, domainSectors, nodeDegree,
} from "./graphModel";

const NODES: GraphNode[] = Array.from({ length: 12 }, (_, i) => ({
  id: `n${i}`, displayName: `n${i}`,
  domain: i < 6 ? "A" : "B", summary: "", dataSource: "lib",
}));
const EDGES: GraphEdge[] = [
  { source: "n0", target: "n1" }, { source: "n0", target: "n2" },
  { source: "n1", target: "n3" }, { source: "n6", target: "n7" },
  { source: "n6", target: "n0" }, { source: "n8", target: "n9" },
] as GraphEdge[];
const SIZE = { w: 1000, h: 800 };

/** 目标点铺成一圈 (模拟 radialTargets 的产物, 但不依赖它 —— 这里测的是
 *  graphLayout 对目标点的处理, 不是目标点算得对不对)。 */
const TARGETS: Record<string, { x: number; y: number }> = {};
NODES.forEach((n, i) => {
  const a = (i / NODES.length) * Math.PI * 2;
  TARGETS[n.id] = { x: 500 + Math.cos(a) * 300, y: 400 + Math.sin(a) * 300 };
});

function start(pinned: Record<string, { x: number; y: number }> = {}) {
  let last: Record<string, { x: number; y: number }> = {};
  const h = runLayout(NODES, EDGES, SIZE, TARGETS, pinned, (ps) => {
    last = {};
    for (const p of ps) last[p.id] = { x: p.x, y: p.y };
  });
  return { h, at: (id: string) => last[id], all: () => last };
}
const dist = (a: { x: number; y: number }, b: { x: number; y: number }) =>
  Math.hypot(a.x - b.x, a.y - b.y);

describe("拖动语义 — 松手后节点留在拖放的位置 (本轮用户的第 1 条要求)", () => {
  it("★ 松手跑 600 tick 后仍在拖放点: 不回弹", () => {
    const { h, at } = start();
    const DROP = { x: 120, y: 700 };   // 故意拖到远离该节点目标点的角落
    h.pin("n0", DROP.x, DROP.y);
    h.unpin("n0");
    h.tick(600);                       // 径向力若还生效, 这么多帧足够拽回去
    expect(dist(at("n0"), DROP)).toBeLessThan(1);
    h.stop();
  });

  it("★ 底线: 被钉的节点不与任何其他节点重合 (周围的让路)", () => {
    const { h, at, all } = start();
    h.pin("n0", 500, 400);             // 钉在正中间, 最容易被别人叠上
    h.unpin("n0");
    h.tick(800);
    const p0 = at("n0");
    const others = Object.entries(all()).filter(([id]) => id !== "n0");
    const min = Math.min(...others.map(([, p]) => dist(p, p0)));
    expect(min).toBeGreaterThan(24);   // 节点半径量级 ~10, collide 半径 33.6
    h.stop();
  });

  it("拖拽过程中 (未松手) 节点精确跟手", () => {
    const { h, at } = start();
    h.pin("n0", 333, 222);
    h.tick(5);
    expect(at("n0")).toEqual({ x: 333, y: 222 });
    h.stop();
  });
});

describe("unpinAll — ↺ 把手工摆位交还力学 (拖乱了的退路)", () => {
  it("★ 解钉后节点重新被力学摆开 (不加热的话解了也不动 — 真实 bug 的回归测试)", () => {
    const { h, at } = start();
    const DROP = { x: 80, y: 60 };
    h.pin("n0", DROP.x, DROP.y);
    h.unpin("n0");
    h.tick(300);                       // 先收敛到"钉住"的稳定态
    const pinnedPos = { ...at("n0") };
    h.unpinAll();
    h.tick(900);
    expect(dist(at("n0"), pinnedPos)).toBeGreaterThan(50);
    h.stop();
  });

  it("解钉后节点朝自己的扇区目标点靠 (不是随便乱飞)", () => {
    const { h, at } = start();
    h.pin("n0", 80, 60);
    h.unpin("n0");
    h.tick(300);
    const before = dist(at("n0"), TARGETS["n0"]);
    h.unpinAll();
    h.tick(900);
    expect(dist(at("n0"), TARGETS["n0"])).toBeLessThan(before);
    h.stop();
  });
});

describe("pinned 入参 — 已保存的布局在起模拟时就固定 (刷新后位置还在)", () => {
  it("★ 存档节点 800 tick 后仍在存档坐标", () => {
    const SAVED = { n0: { x: 150, y: 150 }, n5: { x: 900, y: 700 } };
    const { h, at } = start(SAVED);
    h.tick(800);
    expect(dist(at("n0"), SAVED.n0)).toBeLessThan(1);
    expect(dist(at("n5"), SAVED.n5)).toBeLessThan(1);
    h.stop();
  });

  it("★ 哨兵: 没存档的节点仍由力学摆放 — 区分'存档生效'与'全钉死了'", () => {
    const SAVED = { n0: { x: 150, y: 150 } };
    const { h, at } = start(SAVED);
    h.tick(800);
    // n3 没存档 → 必须离开它的初始目标点 (被边和斥力推动过)
    expect(dist(at("n3"), TARGETS["n3"])).toBeGreaterThan(1);
    h.stop();
  });

  it("存档里的坏 id 不影响其他节点 (restoreLayout 已滤, 这里是第二道)", () => {
    const { h, at } = start({ ghost: { x: 1, y: 1 } });
    h.tick(100);
    expect(Object.keys(at("n0"))).toEqual(["x", "y"]);
    expect(Number.isFinite(at("n0").x)).toBe(true);
    h.stop();
  });
});

describe("pinnedCount — 保存提示要区分'有固定位置'与'全是力学算的'", () => {
  // ★ 为什么需要这个计数: 开局 settle 是**确定性**的 (初始坐标放在
  //   radialTargets 的目标点上, d3 无随机抖动), 所以同一份数据每次刷新都
  //   收敛到同一个布局。浏览器实测: 没拖过节点就点 💾, 刷新前后没有一个
  //   节点有差异 (最大位移 0.0px) —— 存档确实写进去了, 但视觉上与不存完全
  //   一样, 用户因此报"保存按钮没作用"。
  //   提示语要能区分这两种情况, 就得知道**有几个节点位置已固定**, 而那是
  //   fx/fy 的状态, 只有 graphLayout 知道。
  it("★ 全新模拟没有任何固定位置 (提示语该说'刷新后看不出变化')", () => {
    const { h } = start();
    h.tick(100);
    expect(h.pinnedCount()).toBe(0);
    h.stop();
  });

  it("★ 拖过 2 个节点后计数为 2 (松手仍算钉住 — 拖到哪留哪)", () => {
    const { h } = start();
    h.pin("n0", 100, 100); h.unpin("n0");
    h.pin("n5", 200, 300); h.unpin("n5");
    h.tick(100);
    expect(h.pinnedCount()).toBe(2);
    h.stop();
  });

  it("★ 从存档恢复的节点也计入 (刷新后提示仍说得出固定了几个)", () => {
    const { h } = start({ n0: { x: 1, y: 2 }, n3: { x: 3, y: 4 } });
    expect(h.pinnedCount()).toBe(2);
    h.stop();
  });

  it("★★ 语义是'已固定'不是'手工拖过': 存档存的是全量, 恢复后计数=全部节点", () => {
    // ★ 这条是我写文案时踩的坑的回归测试: 保存按钮存的是 snapshot (**全部**
    //   节点), 于是刷新一次后 restoreLayout 会把它们全钉住 —— 浏览器实测
    //   pinnedCount 就等于节点总数。我第一版文案写"含 N 个手工摆放"是谎报, 用户
    //   只拖了一两个。调用方必须按"已固定"来措辞。
    const saved: Record<string, { x: number; y: number }> = {};
    NODES.forEach((n, i) => { saved[n.id] = { x: 100 + i * 10, y: 200 }; });
    const { h } = start(saved);
    expect(h.pinnedCount()).toBe(NODES.length);
    h.stop();
  });

  it("★ unpinAll 后归零 (↺ 把一切交还力学)", () => {
    const { h } = start({ n0: { x: 1, y: 2 } });
    h.pin("n7", 50, 50); h.unpin("n7");
    expect(h.pinnedCount()).toBe(2);
    h.unpinAll();
    expect(h.pinnedCount()).toBe(0);
    h.stop();
  });

  it("哨兵: 计数不是'节点总数'也不是常量 — 拖一个就该是 1", () => {
    // 若实现成 `simNodes.length` 或写死, 上面几条里有的仍会绿。
    const { h } = start();
    h.pin("n2", 10, 10); h.unpin("n2");
    h.tick(50);
    expect(h.pinnedCount()).toBe(1);
    expect(h.pinnedCount()).not.toBe(NODES.length);
    h.stop();
  });
});

describe("snapshot — 保存按钮存的就是它", () => {
  it("覆盖全部节点且坐标都是有限数 (一个 NaN 存进去下次刷新整图消失)", () => {
    const { h } = start();
    h.tick(100);
    const snap = h.snapshot();
    expect(Object.keys(snap).sort()).toEqual(NODES.map((n) => n.id).sort());
    for (const p of Object.values(snap)) {
      expect(Number.isFinite(p.x)).toBe(true);
      expect(Number.isFinite(p.y)).toBe(true);
    }
    h.stop();
  });

  it("snapshot 反映拖动后的真实位置 (存的是当前态不是初始态)", () => {
    const { h } = start();
    h.pin("n0", 42, 43);
    h.unpin("n0");
    h.tick(50);
    expect(h.snapshot()["n0"]).toEqual({ x: 42, y: 43 });
    h.stop();
  });
});

describe("自环与坏边不进力学 (喂给 forceLink 会炸或静默失真)", () => {
  it("自环被过滤, 模拟仍产出有限坐标", () => {
    let last: Record<string, { x: number; y: number }> = {};
    const h = runLayout(
      NODES,
      [{ source: "n0", target: "n0" }, { source: "n0", target: "nope" }] as GraphEdge[],
      SIZE, TARGETS, {},
      (ps) => { last = {}; for (const p of ps) last[p.id] = { x: p.x, y: p.y }; },
    );
    h.tick(50);
    expect(Number.isFinite(last["n0"].x)).toBe(true);
    h.stop();
  });
});

// ★ 大规模固件 (58 节点/91 边, 照真库的量级搭)。下面两组 describe 都用它, 且都**只在这个
//   规模下才测得出差别** —— 12 节点的小固件里扇区目标力 (strength 0.42)
//   压过一切, 我第一版在小固件上测, 变异存活还以为"断言不敏感", 实际是
//   **固件规模不对**。密度和收敛距离都只在够密/够大的时候才成为约束。
const BIG: GraphNode[] = Array.from({ length: 58 }, (_, i) => ({
  id: `b${i}`, displayName: `b${i}`,
  domain: i < 29 ? "A" : "B", summary: "", dataSource: "lib",
}));
const BIG_EDGES = Array.from({ length: 91 }, (_, i) => ({
  source: `b${i % 58}`, target: `b${(i * 7 + 3) % 58}`,
})).filter((e) => e.source !== e.target) as GraphEdge[];
const BIG_SIZE = { w: 1400, h: 1120 };
const BIG_T: Record<string, { x: number; y: number }> = {};
BIG.forEach((n, i) => {
  const a = (i / BIG.length) * Math.PI * 2;
  BIG_T[n.id] = { x: 700 + Math.cos(a) * 480, y: 560 + Math.sin(a) * 480 };
});

describe("铺开 — 力参数随 SPREAD 放大 (用户的第 2 条要求: 节点别那么挤)", () => {
  // ★ 实测: collide 跟 SPREAD 时最小间距 67.2px, 不跟时 48.0px (-29%)。
  //   link.distance / charge 在这个规模几乎不影响间距 (各 <1px 与 -0%),
  //   它们跟着放大是为了量纲一致 (都是图坐标绝对值), 本测试不声称覆盖它们。
  it("★ 58 节点下最小间距 > 60px: 碰撞半径跟着铺开 (不跟只有 48px, 标签会压盖)", () => {
    let last: Record<string, { x: number; y: number }> = {};
    const h = runLayout(
      BIG, BIG_EDGES, BIG_SIZE, BIG_T, {},
      (ps) => { last = {}; for (const p of ps) last[p.id] = { x: p.x, y: p.y }; },
    );
    h.tick(1200);
    const ids = Object.keys(last);
    const nearest = ids.map((a) =>
      Math.min(...ids.filter((b) => b !== a).map((b) => dist(last[a], last[b]))));
    expect(Math.min(...nearest)).toBeGreaterThan(60);
    h.stop();
  });
});

describe("开局同步预热 — 首屏就是最终形态 (用户第三轮: 进来缩放很大, 几秒后才变对)", () => {
  // ★ 病理: 布局跑在 画布×SPREAD 的虚拟空间里, 而视图初值是 k=1 → 首屏
  //   等于"1.4 倍空间按 1:1 看", 又大又偏。原先靠 d3 的 "end" 事件 (要跑完
  //   ~300 帧 rAF, 肉眼几秒) 才自动 fit, 于是用户看到"停几秒突然缩小"。
  // ★ 修法不能是"拿目标点算初始 fit" —— 实测目标点云 fit 出 k=1.82 而收敛后
  //   是 k=1.28, 用它当初始视图会有近三成节点落在画布外 (力学会把节点从
  //   目标点推开很远, 目标点不是形状的近似)。只能真的先把布局跑完。
  //
  // ★★ 固件**必须用真实 radialTargets 的产物**, 不能用上面那个手搓圆环:
  //   圆环把所有节点放在同一半径上, 点云外接框从第一帧起就是终局尺寸
  //   (实测目标点fit/最终fit = 0.93), 于是"预热 1 步"和"预热 300 步"测不出
  //   差别 —— 我第一版就这么写, `warmup-1-step` 变异存活。而真实目标点是
  //   **按度数径向分层 + 按域分扇区**的, 初态比终态紧凑得多 (比值 1.42)。
  //   同[[规模]]那条教训的变体: 这次错的是固件**形状**不是规模。
  //   为此本 describe 依赖 radialTargets (别处刻意不依赖), 因为"目标点云不是
  //   终局形状"正是这个 bug 的成因, 换成理想化固件就把病理抹掉了。
  const DOMS = ["A", "B", "C", "D", "E", ""];
  const RN: GraphNode[] = Array.from({ length: 58 }, (_, i) => ({
    id: `r${i}`, displayName: `r${i}`,
    domain: DOMS[i % DOMS.length], summary: "", dataSource: "lib",
  }));
  const RE = Array.from({ length: 91 }, (_, i) => ({
    source: `r${i % 58}`, target: `r${(i * 7 + 3) % 58}`,
  })).filter((e) => e.source !== e.target) as GraphEdge[];
  const CANVAS = { w: 1000, h: 800 };
  const LS = layoutSize(CANVAS);            // GraphPanel 就是这么调的
  const RT = radialTargets(
    RN, nodeDegree(RN, RE),
    domainSectors(RN, DOMS.map((c) => ({ code: c, name: c, color: null })) as never),
    LS,
  );
  const fitK = (m: Record<string, { x: number; y: number }>) =>
    fitView(Object.values(m), CANVAS, 44).k;

  function startReal() {
    let last: Record<string, { x: number; y: number }> = {};
    let calls = 0;
    const h = runLayout(RN, RE, LS, RT, {}, (ps) => {
      calls++; last = {};
      for (const p of ps) last[p.id] = { x: p.x, y: p.y };
    });
    return { h, last: () => last, calls: () => calls };
  }

  it("★ runLayout 一返回, snapshot 的 fit 缩放就已等于跑完 1200 帧的 fit", () => {
    const r = startReal();
    const kFirst = fitK(r.h.snapshot());   // 首屏能算出的缩放
    r.h.tick(1200);
    const kFinal = fitK(r.last());         // 最终稳定态的缩放
    // 判据钉的是**因果比例** (首屏 k / 最终 k ≈ 1), 不是"k 落在某区间" ——
    // 后者是上一轮踩过的假阴性 (FIT_PAD 自己就让 k<1, 断言恒真)。
    // 不预热时这个比值是 1.42 (首屏大 42%), 正是用户看到的"突然缩小"。
    expect(kFirst / kFinal).toBeGreaterThan(0.98);
    expect(kFirst / kFinal).toBeLessThan(1.02);
    r.h.stop();
  });

  it("哨兵: 目标点云本身 fit 不出终局缩放 — 证明上面那条不是恒真", () => {
    // 若"目标点 fit ≈ 最终 fit"(理想化固件的情形), 上面那条测什么都会绿。
    const r = startReal();
    r.h.tick(1200);
    const ratio = fitK(RT) / fitK(r.last());
    expect(ratio).toBeGreaterThan(1.2);   // 实测 1.42
    r.h.stop();
  });

  it("★ 返回前已回调过一次 onTick: 首帧就有坐标, 不是空图闪一下", () => {
    const r = startReal();
    expect(r.calls()).toBeGreaterThan(0);
    expect(Object.keys(r.last()).length).toBe(RN.length);
    expect(Number.isFinite(r.last()["r0"].x)).toBe(true);
    r.h.stop();
  });

  it("哨兵: 预热没把节点钉死 — 存档外的节点之后仍能被力学挪动", () => {
    // 与上面两条相反的方向: 若"预热"被实现成钉住全部节点, 上面也会绿。
    const { h, at } = start();
    const before = { ...at("n3") };
    h.pin("n0", 120, 700);
    h.tick(400);
    expect(dist(at("n3"), before)).toBeGreaterThan(1);
    h.stop();
  });

  // ★ ↺ 走的是同一条路: 用户报"点重置又变成很大一张图"。病理一模一样 ——
  //   解钉后节点要重新散开 (点云变大), 而视图还停在旧缩放上, 靠 d3 的 rAF
  //   慢慢收敛。所以 unpinAll 也必须**同步收敛完**再把坐标交出去,
  //   调用方才能当场 fit 出正确缩放。两条路复用 graphLayout 里的 settle()。
  it("★ unpinAll 一返回, snapshot 的 fit 就已是最终缩放 (重置后不该再变大)", () => {
    const r = startReal();
    r.h.pin("r0", 40, 40); r.h.unpin("r0");     // 先把图拖乱
    r.h.pin("r1", 60, 780); r.h.unpin("r1");
    r.h.tick(200);
    r.h.unpinAll();
    const kAfter = fitK(r.h.snapshot());
    r.h.tick(1200);
    const kFinal = fitK(r.last());
    expect(kAfter / kFinal).toBeGreaterThan(0.98);
    expect(kAfter / kFinal).toBeLessThan(1.02);
    r.h.stop();
  });

  it("★ unpinAll 返回前回调过 onTick: 调用方拿得到解钉后的新坐标", () => {
    const r = startReal();
    r.h.tick(100);
    const before = r.calls();
    r.h.unpinAll();
    expect(r.calls()).toBeGreaterThan(before);
    r.h.stop();
  });
});
