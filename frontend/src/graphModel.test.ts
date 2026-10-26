// graphModel 纯函数单测 (网络图展示逻辑, 不含 React)。
// 固件形态照着真库的实际数据形状搭 (2026-09-21 实查), 见 spec 第二节。
import { describe, it, expect } from "vitest";
import { domainColor, buildLegend, nodeDegree, isolatedNodes, connectedComponents, edgeKind, edgeGeometry, edgePath, radius, truncateLabel, domainSectors, radialTargets, clampZoom, screenToGraph, zoomAt, fitView, layoutSize, restoreLayout, packLayout, searchNodes, centerOn, SPREAD, ZOOM_MIN, ZOOM_MAX } from "./graphModel";
import type { GraphDomain, GraphNode, GraphEdge } from "./api";

const DOMAINS: GraphDomain[] = [
  { code: "借阅实例", color: "#2196F3", displayOrder: 1, description: "" },
  { code: "读者账户", color: null, displayOrder: 10, description: "lib2 库" },
  { code: "ACQUISITION", color: "#00f", displayOrder: 3, description: "" },
];

describe("domainColor — 库为真相源 + 域名哈希兜底", () => {
  it("库里有色: 原样返回", () => {
    expect(domainColor("借阅实例", DOMAINS)).toBe("#2196F3");
  });

  it("库里 color 为 NULL: 走哈希兜底, 返回合法 HSL", () => {
    const c = domainColor("读者账户", DOMAINS);
    expect(c).toMatch(/^hsl\(\d+(\.\d+)?, \d+%, \d+%\)$/);
  });

  it("哈希稳定: 同名两次调用结果相同 (不能带随机)", () => {
    expect(domainColor("读者账户", DOMAINS)).toBe(domainColor("读者账户", DOMAINS));
  });

  it("无域 (空串): 也有色, 且与已配色域的色不同", () => {
    const c = domainColor("", DOMAINS);
    expect(c).toBeTruthy();
    expect(c).not.toBe("#2196F3");
  });

  it("域名不在 domains 表里: 不炸, 走哈希", () => {
    expect(domainColor("查无此域", DOMAINS)).toMatch(/^hsl\(/);
  });
});

const NODES: GraphNode[] = [
  { id: "lib_loan", displayName: "借阅实例", domain: "借阅实例", summary: "", dataSource: "lib" },
  { id: "lib_loan_rel", displayName: "借阅实例关系", domain: "借阅实例", summary: "", dataSource: "lib" },
  { id: "lib_reader", displayName: "客户", domain: "读者账户", summary: "", dataSource: "lib" },
  { id: "lib_cart", displayName: "购物车", domain: "", summary: "", dataSource: "lib" },
];

describe("buildLegend — 图例", () => {
  it("排除零实体域: ACQUISITION 不出现 (固件里它有色但 0 实体, 真库也有这种域)", () => {
    const codes = buildLegend(NODES, DOMAINS).map((l) => l.code);
    expect(codes).not.toContain("ACQUISITION");
  });

  it("统计每域实体数", () => {
    const hit = buildLegend(NODES, DOMAINS).find((l) => l.code === "借阅实例");
    expect(hit?.count).toBe(2);
  });

  it("库里没配色的域标注 '未配色'", () => {
    const hit = buildLegend(NODES, DOMAINS).find((l) => l.code === "读者账户");
    expect(hit?.note).toBe("未配色");
  });

  it("无域实体单列一项并标注 '未归域'", () => {
    const hit = buildLegend(NODES, DOMAINS).find((l) => l.note === "未归域");
    expect(hit?.count).toBe(1);
  });

  it("有色有域的正常项 note 为空串", () => {
    const hit = buildLegend(NODES, DOMAINS).find((l) => l.code === "借阅实例");
    expect(hit?.note).toBe("");
  });

  it("每项都带可渲染的颜色", () => {
    expect(buildLegend(NODES, DOMAINS).every((l) => Boolean(l.color))).toBe(true);
  });

  it("★ 过滤键与显示名分离: 无域桶 key 是空串 (真 domain 值), code 仍是显示名 '(无域)'", () => {
    // 无域节点的 n.domain 是 "", 不是 "(无域)" — 拿显示名当过滤值会让
    // Canvas 的 n.domain !== dimDomain 恒真, 全图节点全暗 (Bug 2)
    const hit = buildLegend(NODES, DOMAINS).find((l) => l.note === "未归域");
    expect(hit?.key).toBe("");
    expect(hit?.code).toBe("(无域)");
  });

  it("正常域的 key 等于其 code (过滤值就是域名本身)", () => {
    const hit = buildLegend(NODES, DOMAINS).find((l) => l.code === "借阅实例");
    expect(hit?.key).toBe("借阅实例");
  });
});

// 小图固件: a-b-c 一条链, d-e 一对, f 孤立 → 3 个分量 [3,2,1]
// ★ 刻意把小分量排在大分量前面: 若实现漏了按大小降序, DFS 发现序会把
//   [f]/[d,e] 编成 0/1 号, "0 = 最大分量" 的断言当场翻红 (变异验证过:
//   删掉 groups.sort 行, 下方测试必红)。
const G_NODES: GraphNode[] = ["f", "d", "e", "a", "b", "c"].map((id) => ({
  id, displayName: id, domain: "X", summary: "", dataSource: "lib",
}));
const G_EDGES: GraphEdge[] = [
  { source: "a", target: "b", via: "1", label: null, type: null },
  { source: "b", target: "c", via: "2", label: null, type: null },
  { source: "d", target: "e", via: "3", label: null, type: null },
];

describe("nodeDegree — 度数驱动节点半径", () => {
  it("两端各记一次", () => {
    const d = nodeDegree(G_NODES, G_EDGES);
    expect(d["b"]).toBe(2);
    expect(d["a"]).toBe(1);
  });

  it("零边节点度数为 0 而非 undefined", () => {
    expect(nodeDegree(G_NODES, G_EDGES)["f"]).toBe(0);
  });

  it("自环两端同一个点: 计 2 (真库有 lib_copy→lib_copy 一条)", () => {
    const loop: GraphEdge[] = [{ source: "a", target: "a", via: "L", label: null, type: null }];
    expect(nodeDegree(G_NODES, loop)["a"]).toBe(2);
  });
});

describe("isolatedNodes — 零边实体", () => {
  it("只挑出零边的", () => {
    expect(isolatedNodes(G_NODES, G_EDGES)).toEqual(["f"]);
  });

  it("空边集: 全部都孤立", () => {
    expect(isolatedNodes(G_NODES, [])).toHaveLength(6);
  });
});

describe("connectedComponents — 分量编号 (0 = 最大分量)", () => {
  it("最大分量编号为 0", () => {
    const c = connectedComponents(G_NODES, G_EDGES);
    expect(c["a"]).toBe(0);
    expect(c["b"]).toBe(0);
    expect(c["c"]).toBe(0);
  });

  it("小分量编号非 0 (Task 5 靠这个标红环)", () => {
    const c = connectedComponents(G_NODES, G_EDGES);
    expect(c["d"]).not.toBe(0);
    expect(c["d"]).toBe(c["e"]);      // 同一分量同号
    expect(c["f"]).not.toBe(c["d"]);  // 不同分量不同号
  });

  it("分量总数正确", () => {
    const c = connectedComponents(G_NODES, G_EDGES);
    expect(new Set(Object.values(c)).size).toBe(3);
  });
});

const K_NODES: GraphNode[] = [
  { id: "a", displayName: "a", domain: "借阅实例", summary: "", dataSource: "lib" },
  { id: "b", displayName: "b", domain: "借阅实例", summary: "", dataSource: "lib" },
  { id: "c", displayName: "c", domain: "工单", summary: "", dataSource: "lib" },
];

describe("edgeKind — 边分类", () => {
  it("同域: inner", () => {
    const e: GraphEdge = { source: "a", target: "b", via: "1", label: null, type: null };
    expect(edgeKind(e, K_NODES)).toBe("inner");
  });

  it("跨域: inter-domain", () => {
    const e: GraphEdge = { source: "a", target: "c", via: "2", label: null, type: null };
    expect(edgeKind(e, K_NODES)).toBe("inter-domain");
  });

  it("端点查不到: 不炸, 按 inter-domain 处理", () => {
    const e: GraphEdge = { source: "a", target: "无此点", via: "3", label: null, type: null };
    expect(edgeKind(e, K_NODES)).toBe("inter-domain");
  });
});

describe("edgeKind — 跨库判据 (dataSource 不同)", () => {
  const X_NODES: GraphNode[] = [
    { id: "lib_loan", displayName: "借阅实例", domain: "借阅实例",
      summary: "", dataSource: "lib" },
    { id: "lib_reader", displayName: "客户", domain: "读者账户",
      summary: "", dataSource: "lib2" },
    { id: "lib_copy", displayName: "订单项", domain: "预约受理",
      summary: "", dataSource: "lib" },
  ];

  it("dataSource 不同: cross-source (真库那条 lib_loan→lib_reader)", () => {
    const e: GraphEdge = { source: "lib_loan", target: "lib_reader",
                           via: "v", label: null, type: null };
    expect(edgeKind(e, X_NODES)).toBe("cross-source");
  });

  it("跨库优先于跨域: 不退化成 inter-domain", () => {
    const e: GraphEdge = { source: "lib_loan", target: "lib_reader",
                           via: "v", label: null, type: null };
    expect(edgeKind(e, X_NODES)).not.toBe("inter-domain");
  });

  it("同库跨域: 仍是 inter-domain", () => {
    const e: GraphEdge = { source: "lib_loan", target: "lib_copy",
                           via: "v", label: null, type: null };
    expect(edgeKind(e, X_NODES)).toBe("inter-domain");
  });
});

describe("edgeKind — 跨库判据 (空 dataSource 的边界)", () => {
  it("两端都为空: 不算跨库 (回归钉子, 防误改成 !s.dataSource || ... 之类)", () => {
    const nodes: GraphNode[] = [
      { id: "a", displayName: "a", domain: "借阅实例", summary: "", dataSource: "" },
      { id: "b", displayName: "b", domain: "借阅实例", summary: "", dataSource: "" },
    ];
    const e: GraphEdge = { source: "a", target: "b", via: "v", label: null, type: null };
    expect(edgeKind(e, nodes)).not.toBe("cross-source");
  });

  it("一端空一端有源: 算跨库 (故意行为, 与后端 _src_of 的'宁可多切段'口径一致)", () => {
    const nodes: GraphNode[] = [
      { id: "a", displayName: "a", domain: "借阅实例", summary: "", dataSource: "lib" },
      { id: "b", displayName: "b", domain: "借阅实例", summary: "", dataSource: "" },
    ];
    const e: GraphEdge = { source: "a", target: "b", via: "v", label: null, type: null };
    expect(edgeKind(e, nodes)).toBe("cross-source");
  });
});

describe("edgeGeometry — 自环与重边 (不处理会静默掩盖信息)", () => {
  it("自环被标出 (真库有 lib_copy→lib_copy; 直线会画成一个点看不见)", () => {
    const es: GraphEdge[] = [
      { source: "x", target: "x", via: "L", label: null, type: null },
    ];
    expect(edgeGeometry(es)[0].selfLoop).toBe(true);
  });

  it("重边拿到不同序号 (真库里有成对重边; 直线渲染会完全重叠)", () => {
    const es: GraphEdge[] = [
      { source: "p", target: "q", via: "v1", label: null, type: null },
      { source: "p", target: "q", via: "v2", label: null, type: null },
    ];
    const g = edgeGeometry(es);
    expect(g[0].index).toBe(0);
    expect(g[1].index).toBe(1);
    expect(g[0].total).toBe(2);
    expect(g[1].total).toBe(2);
  });

  it("方向相反也算同一对 (a→b 与 b→a 会重叠)", () => {
    const es: GraphEdge[] = [
      { source: "p", target: "q", via: "v1", label: null, type: null },
      { source: "q", target: "p", via: "v2", label: null, type: null },
    ];
    expect(edgeGeometry(es)[1].index).toBe(1);
  });

  it("单条边: index 0 / total 1 (画直线)", () => {
    const es: GraphEdge[] = [
      { source: "p", target: "q", via: "v", label: null, type: null },
    ];
    expect(edgeGeometry(es)[0]).toEqual({ selfLoop: false, index: 0, total: 1 });
  });

  it("返回数组与入参等长同序 (Task 5 按下标取)", () => {
    const es: GraphEdge[] = [
      { source: "p", target: "q", via: "v1", label: null, type: null },
      { source: "x", target: "x", via: "L", label: null, type: null },
      { source: "p", target: "q", via: "v2", label: null, type: null },
    ];
    const g = edgeGeometry(es);
    expect(g).toHaveLength(3);
    expect(g[1].selfLoop).toBe(true);
    expect(g[2].index).toBe(1);
  });
});

describe("edgePath — 重边曲线 (从 GraphCanvas 移入后才有单测)", () => {
  // a(100,100) b(300,200): 真库对向存储重边对的典型坐标形态
  const AX = 100, AY = 100, BX = 300, BY = 200;

  // 取二次贝塞尔控制点 —— 抵消 bug 的本质就是两条曲线控制点重合
  // (端点本来就是同一对; 控制点再一样 = 两条曲线几何上完全重叠)。
  // 字符串直接 toBe 会假阴性: M 起点/终点顺序不同, 字符串必不同。
  function ctrlOf(p: string): string {
    const m = p.match(/^M -?[\d.]+ -?[\d.]+ Q (-?[\d.]+ -?[\d.]+)/);
    if (!m) throw new Error(`不是带控制点的曲线: ${p}`);
    return m[1];
  }

  it("对向重边不抵消: a→b(index 0) 与 b→a(index 1) 的控制点必须不同 (核心 bug)", () => {
    const p0 = edgePath(AX, AY, BX, BY, { selfLoop: false, index: 0, total: 2 });
    const p1 = edgePath(BX, BY, AX, AY, { selfLoop: false, index: 1, total: 2 });
    expect(ctrlOf(p1)).not.toBe(ctrlOf(p0));
  });

  it("同向重边仍然分开: a→b index 0/1 的 path 不同 (9 对本来就没坏的不许回归)", () => {
    const p0 = edgePath(AX, AY, BX, BY, { selfLoop: false, index: 0, total: 2 });
    const p1 = edgePath(AX, AY, BX, BY, { selfLoop: false, index: 1, total: 2 });
    expect(p1).not.toBe(p0);
  });

  it("对向重边两条都弯曲而非一弯一直 (直线那条会从 b 拖到 a, 弯的从 a 拖到 b, 端到端也会不同)", () => {
    const p0 = edgePath(AX, AY, BX, BY, { selfLoop: false, index: 0, total: 2 });
    const p1 = edgePath(BX, BY, AX, AY, { selfLoop: false, index: 1, total: 2 });
    expect(p0).toMatch(/^M 100 100 Q/);   // 从 a 出发的仍带控制点
    expect(p1).toMatch(/^M 300 200 Q/);   // 从 b 出发的也是曲线, 不退化成 L 直线
  });

  it("自环: 仍画成小环曲线 (真库 lib_copy→lib_copy)", () => {
    const p = edgePath(AX, AY, AX, AY, { selfLoop: true, index: 0, total: 1 });
    expect(p).toContain(" C ");
    expect(p).not.toContain(" Q ");
  });

  it("单条边 (total 1): 仍是直线", () => {
    const p = edgePath(AX, AY, BX, BY, { selfLoop: false, index: 0, total: 1 });
    expect(p).toBe(`M ${AX} ${AY} L ${BX} ${BY}`);
  });

  it("垂直边 (dx=0) 也不炸不抵消: 归一化走 y 比较, 控制点仍要分开", () => {
    const p0 = edgePath(50, 50, 50, 250, { selfLoop: false, index: 0, total: 2 });
    const p1 = edgePath(50, 250, 50, 50, { selfLoop: false, index: 1, total: 2 });
    expect(ctrlOf(p1)).not.toBe(ctrlOf(p0));
  });
});

describe("radius — 度数→半径 (平方根缩放)", () => {
  it("单调增; 等输入步长的增量递减 (度数最高的枢纽不大到盖住别人)", () => {
    expect(radius(0)).toBeLessThan(radius(4));
    expect(radius(4)).toBeLessThan(radius(16));
    // 等步长 (0→4→8) 下的增量: √ 缩放保证后者更小
    expect(radius(8) - radius(4)).toBeLessThan(radius(4) - radius(0));
  });
});

describe("truncateLabel", () => {
  it("短于上限: 原样", () => {
    expect(truncateLabel("abc", 5)).toBe("abc");
  });

  it("等于上限: 不截", () => {
    expect(truncateLabel("abcde", 5)).toBe("abcde");
  });

  it("超长: 截断带省略号", () => {
    expect(truncateLabel("abcdefgh", 5)).toBe("abcde…");
  });
});

// ── 以下是"缩放/拖动 + 扇区径向布局"一轮新增 ────────────────────────────

// 三域固件: 借阅实例 2 个 / 工单 1 个 / 无域 1 个 (故意让 domains 表里
// 多出一个零实体域 ACQUISITION, 它不该分到扇区 — 分了就是白占圆周角)
const S_NODES: GraphNode[] = [
  { id: "a", displayName: "a", domain: "借阅实例", summary: "", dataSource: "lib" },
  { id: "b", displayName: "b", domain: "借阅实例", summary: "", dataSource: "lib" },
  { id: "c", displayName: "c", domain: "工单", summary: "", dataSource: "lib" },
  { id: "z", displayName: "z", domain: "", summary: "", dataSource: "lib" },
];
const S_DOMAINS: GraphDomain[] = [
  { code: "借阅实例", color: "#2196F3", displayOrder: 1, description: "" },
  { code: "工单", color: "#888", displayOrder: 2, description: "" },
  { code: "ACQUISITION", color: "#00f", displayOrder: 3, description: "" },
];

describe("domainSectors — 每个域占一段圆周角 (同域抱团的依据)", () => {
  it("扇区无缝无重叠地铺满整个圆周 (总跨度 2π)", () => {
    const s = domainSectors(S_NODES, S_DOMAINS);
    const span = Object.values(s).reduce((acc, x) => acc + (x.end - x.start), 0);
    expect(span).toBeCloseTo(Math.PI * 2, 6);
  });

  it("角度按实体数成比例: 2 个实体的域占 1 个实体的域的两倍", () => {
    const s = domainSectors(S_NODES, S_DOMAINS);
    const w = (k: string) => s[k].end - s[k].start;
    expect(w("借阅实例")).toBeCloseTo(w("工单") * 2, 6);
  });

  it("零实体域不分扇区 (ACQUISITION 有色但 0 实体, 分了就白占圆周)", () => {
    expect(domainSectors(S_NODES, S_DOMAINS)["ACQUISITION"]).toBeUndefined();
  });

  it("★ 键是真 domain 值: 无域桶的键是空串不是显示名 (同 buildLegend 的 Bug 2 教训)", () => {
    const s = domainSectors(S_NODES, S_DOMAINS);
    expect(s[""]).toBeDefined();
    expect(s["(无域)"]).toBeUndefined();
  });

  it("mid 落在 start/end 中间 (节点围绕它铺开)", () => {
    const s = domainSectors(S_NODES, S_DOMAINS)["借阅实例"];
    expect(s.mid).toBeCloseTo((s.start + s.end) / 2, 6);
  });

  it("相邻扇区首尾相接 (按 displayOrder 排, 不留缝)", () => {
    const s = domainSectors(S_NODES, S_DOMAINS);
    expect(s["工单"].start).toBeCloseTo(s["借阅实例"].end, 6);
  });

  it("空节点集: 返回空表, 不炸也不除零", () => {
    expect(domainSectors([], S_DOMAINS)).toEqual({});
  });

  it("域不在 domains 表里的节点仍分到扇区 (不能凭空消失)", () => {
    const orphan: GraphNode[] = [
      { id: "o", displayName: "o", domain: "查无此域", summary: "", dataSource: "lib" },
    ];
    expect(domainSectors(orphan, S_DOMAINS)["查无此域"]).toBeDefined();
  });
});

describe("radialTargets — 扇区内定位 (枢纽居中/叶子靠外)", () => {
  const SIZE = { w: 800, h: 600 };
  const sectors = domainSectors(S_NODES, S_DOMAINS);
  const deg: Record<string, number> = { a: 10, b: 1, c: 1, z: 0 };

  it("每个节点都拿到目标点 (漏一个 d3 就少一份拉力)", () => {
    const t = radialTargets(S_NODES, deg, sectors, SIZE);
    expect(Object.keys(t).sort()).toEqual(["a", "b", "c", "z"]);
  });

  it("★ 高度数更靠圆心: 度数 10 的 a 到中心距离 < 度数 1 的 b", () => {
    const t = radialTargets(S_NODES, deg, sectors, SIZE);
    const d = (id: string) => Math.hypot(t[id].x - SIZE.w / 2, t[id].y - SIZE.h / 2);
    expect(d("a")).toBeLessThan(d("b"));
  });

  it("★ 同域节点的角度落在本域扇区内 (这就是'抱团'的充要条件)", () => {
    const t = radialTargets(S_NODES, deg, sectors, SIZE);
    const sec = sectors["借阅实例"];
    for (const id of ["a", "b"]) {
      const ang = Math.atan2(t[id].y - SIZE.h / 2, t[id].x - SIZE.w / 2);
      // atan2 值域 (-π,π], 扇区从 0 起算 → 归一到 [0,2π) 再比
      const norm = (ang + Math.PI * 2) % (Math.PI * 2);
      expect(norm).toBeGreaterThanOrEqual(sec.start - 1e-6);
      expect(norm).toBeLessThanOrEqual(sec.end + 1e-6);
    }
  });

  it("不同域的节点落在不同方位 (工单的 c 不该跑进借阅实例扇区)", () => {
    const t = radialTargets(S_NODES, deg, sectors, SIZE);
    const ang = (id: string) =>
      (Math.atan2(t[id].y - SIZE.h / 2, t[id].x - SIZE.w / 2) + Math.PI * 2) % (Math.PI * 2);
    const sec = sectors["借阅实例"];
    expect(ang("c") > sec.end - 1e-6 || ang("c") < sec.start + 1e-6).toBe(true);
  });

  it("目标点都在画布内 (跑到画布外 d3 会把整张图拽偏)", () => {
    const t = radialTargets(S_NODES, deg, sectors, SIZE);
    for (const p of Object.values(t)) {
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.x).toBeLessThanOrEqual(SIZE.w);
      expect(p.y).toBeGreaterThanOrEqual(0);
      expect(p.y).toBeLessThanOrEqual(SIZE.h);
    }
  });

  it("度数全相同也不重叠: 同扇区两点错开 (否则同域节点全挤一个坐标)", () => {
    const flat: Record<string, number> = { a: 3, b: 3, c: 3, z: 3 };
    const t = radialTargets(S_NODES, flat, sectors, SIZE);
    expect(t["a"]).not.toEqual(t["b"]);
  });

  it("节点的域没有扇区 (sectors 不匹配): 不炸, 仍给一个画布内的点", () => {
    const t = radialTargets(S_NODES, deg, {}, SIZE);
    expect(Number.isFinite(t["a"].x)).toBe(true);
    expect(Number.isFinite(t["a"].y)).toBe(true);
  });
});

describe("clampZoom — 缩放上下限", () => {
  it("区间内原样", () => {
    expect(clampZoom(1.5)).toBe(1.5);
  });

  it("越下限夹到 ZOOM_MIN (缩太小整张图变一个点, 找不回来)", () => {
    expect(clampZoom(0.001)).toBe(ZOOM_MIN);
  });

  it("越上限夹到 ZOOM_MAX", () => {
    expect(clampZoom(999)).toBe(ZOOM_MAX);
  });

  it("上下限本身是合法值 (边界不许被夹掉)", () => {
    expect(clampZoom(ZOOM_MIN)).toBe(ZOOM_MIN);
    expect(clampZoom(ZOOM_MAX)).toBe(ZOOM_MAX);
  });
});

describe("screenToGraph — 屏幕坐标反变换 (拖节点必须用它)", () => {
  it("恒等视图: 原样返回", () => {
    expect(screenToGraph(120, 80, { tx: 0, ty: 0, k: 1 })).toEqual({ x: 120, y: 80 });
  });

  it("★ 是 translate+scale 的严格逆: 正变换回来必须落回原点", () => {
    const v = { tx: -137, ty: 62, k: 2.4 };
    const g = screenToGraph(311, 205, v);
    expect(g.x * v.k + v.tx).toBeCloseTo(311, 9);
    expect(g.y * v.k + v.ty).toBeCloseTo(205, 9);
  });

  it("缩放后拖拽位移不放大: k=2 时屏幕移 100px 只等于图上 50", () => {
    const v = { tx: 0, ty: 0, k: 2 };
    const a = screenToGraph(100, 0, v);
    const b = screenToGraph(200, 0, v);
    expect(b.x - a.x).toBe(50);
  });
});

describe("zoomAt — 以鼠标为锚点缩放", () => {
  it("★ 锚点下的图坐标缩放前后不变 (锚点漂移 = 滚轮时图往一边跑)", () => {
    const v0 = { tx: 30, ty: -20, k: 1 };
    const [sx, sy] = [400, 300];
    const before = screenToGraph(sx, sy, v0);
    const v1 = zoomAt(v0, sx, sy, 1.3);
    const after = screenToGraph(sx, sy, v1);
    expect(after.x).toBeCloseTo(before.x, 9);
    expect(after.y).toBeCloseTo(before.y, 9);
  });

  it("放大系数生效", () => {
    expect(zoomAt({ tx: 0, ty: 0, k: 1 }, 0, 0, 2).k).toBe(2);
  });

  it("★ 触顶后锚点仍不漂移 (夹 k 却用未夹的 k 算 tx 是这里的经典 bug)", () => {
    const v0 = { tx: 15, ty: 9, k: ZOOM_MAX };
    const [sx, sy] = [250, 140];
    const before = screenToGraph(sx, sy, v0);
    const v1 = zoomAt(v0, sx, sy, 4);      // 已到顶, 再放大应无位移
    expect(v1.k).toBe(ZOOM_MAX);
    const after = screenToGraph(sx, sy, v1);
    expect(after.x).toBeCloseTo(before.x, 9);
    expect(after.y).toBeCloseTo(before.y, 9);
  });

  it("触底同样夹住且不漂移", () => {
    const v0 = { tx: 5, ty: 5, k: ZOOM_MIN };
    const before = screenToGraph(100, 100, v0);
    const v1 = zoomAt(v0, 100, 100, 0.1);
    expect(v1.k).toBe(ZOOM_MIN);
    expect(screenToGraph(100, 100, v1).x).toBeCloseTo(before.x, 9);
  });
});

describe("fitView — 适应画布 (节点跑出视野时的救命按钮)", () => {
  const SIZE = { w: 800, h: 600 };

  it("★ 变换后所有点都落进画布 (这就是这个按钮的全部意义)", () => {
    const pts = [
      { x: -500, y: -400 }, { x: 1800, y: 90 }, { x: 300, y: 2200 },
    ];
    const v = fitView(pts, SIZE, 40);
    for (const p of pts) {
      const sx = p.x * v.k + v.tx;
      const sy = p.y * v.k + v.ty;
      expect(sx).toBeGreaterThanOrEqual(-1e-6);
      expect(sx).toBeLessThanOrEqual(SIZE.w + 1e-6);
      expect(sy).toBeGreaterThanOrEqual(-1e-6);
      expect(sy).toBeLessThanOrEqual(SIZE.h + 1e-6);
    }
  });

  it("点云外接框中心被移到画布中心", () => {
    const pts = [{ x: 0, y: 0 }, { x: 200, y: 100 }];
    const v = fitView(pts, SIZE, 40);
    expect(100 * v.k + v.tx).toBeCloseTo(SIZE.w / 2, 6);
    expect(50 * v.k + v.ty).toBeCloseTo(SIZE.h / 2, 6);
  });

  it("单点 (外接框宽高为 0): 不除零不返回 NaN/Infinity", () => {
    const v = fitView([{ x: 7, y: 9 }], SIZE, 40);
    expect(Number.isFinite(v.k)).toBe(true);
    expect(Number.isFinite(v.tx)).toBe(true);
    expect(v.k).toBeGreaterThan(0);
  });

  it("空点集: 返回恒等视图, 不炸", () => {
    expect(fitView([], SIZE, 40)).toEqual({ tx: 0, ty: 0, k: 1 });
  });

  it("算出的 k 也受上下限约束", () => {
    const v = fitView([{ x: 0, y: 0 }, { x: 1e7, y: 1e7 }], SIZE, 40);
    expect(v.k).toBeGreaterThanOrEqual(ZOOM_MIN);
    expect(v.k).toBeLessThanOrEqual(ZOOM_MAX);
  });
});

// ── 以下是"铺开 + 拖动固定 + 保存布局"一轮新增 ──────────────────────────

describe("layoutSize — 布局空间与画布解耦 (铺开的物理落点)", () => {
  it("★ 布局空间比画布大 SPREAD 倍: 节点相对间距变大而不是节点变小", () => {
    const s = layoutSize({ w: 1000, h: 800 });
    expect(s.w).toBeCloseTo(1000 * SPREAD, 6);
    expect(s.h).toBeCloseTo(800 * SPREAD, 6);
  });

  it("★ SPREAD > 1 (等于 1 就是没铺开, 小于 1 是反着来)", () => {
    expect(SPREAD).toBeGreaterThan(1);
  });

  it("★ 长宽比保持不变: 铺开不该把圆盘拉成椭圆", () => {
    const s = layoutSize({ w: 1200, h: 600 });
    expect(s.w / s.h).toBeCloseTo(1200 / 600, 6);
  });

  it("★ 铺开后 fit 出来的 k 正好被 SPREAD 除小一倍 — 用户要的'初始缩放小一点'", () => {
    const canvas = { w: 1000, h: 800 };
    const ls = layoutSize(canvas);
    const fit = (s: { w: number; h: number }) =>
      fitView([{ x: 0, y: 0 }, { x: s.w, y: s.h }], canvas, 44).k;
    // ★ 判据不能写成 `k < 1`: pad 本身就让 k=0.89 < 1, SPREAD=1 时照样成立
    //   (我第一版就是这个假阴性, 变异测试逮出来的)。要钉的是**因果比例**:
    //   布局空间放大 SPREAD 倍 → fit 出来的 k 必须小 SPREAD 倍。
    expect(fit(ls)).toBeCloseTo(fit(canvas) / SPREAD, 6);
    expect(fit(ls)).toBeLessThan(fit(canvas));
  });
});

describe("restoreLayout — 恢复已保存的布局 (坏数据不许进模拟)", () => {
  const LIVE = ["a", "b", "c"];
  const SZ = { w: 900, h: 620 };
  /** 按当前画布尺寸包一份存档 (下面多数用例只关心 pos 过滤, 尺寸走匹配路径)。 */
  const at = (pos: unknown, size = SZ) => ({ size, pos });

  it("正常数据原样恢复", () => {
    const got = restoreLayout(at({ a: { x: 1, y: 2 }, b: { x: 3, y: 4 } }), LIVE, SZ);
    expect(got.pos).toEqual({ a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
    expect(got.stale).toBe(false);
  });

  it("★ 本体里已删掉的实体丢弃 (存的是旧快照, 照喂会把幽灵坐标钉进模拟)", () => {
    const got = restoreLayout(at({ a: { x: 1, y: 2 }, ghost: { x: 9, y: 9 } }), LIVE, SZ);
    expect(got.pos).toEqual({ a: { x: 1, y: 2 } });
  });

  it("★ NaN / Infinity 丢弃: 一个 NaN 喂进 d3 会让整张图坐标全变 NaN (全部消失)", () => {
    const got = restoreLayout(
      at({ a: { x: NaN, y: 2 }, b: { x: 1, y: Infinity }, c: { x: 5, y: 6 } }),
      LIVE, SZ,
    );
    expect(got.pos).toEqual({ c: { x: 5, y: 6 } });
  });

  it("结构不对的值丢弃 (localStorage 里可能是任何东西)", () => {
    const raw = at({ a: { x: 1 }, b: "nope", c: null, d: { x: 1, y: 2 } });
    expect(restoreLayout(raw, LIVE, SZ).pos).toEqual({});
  });

  it("null / 非对象整体输入: 返回空表不炸 (JSON.parse('null') 就是 null)", () => {
    for (const raw of [null, "x", [1, 2]]) {
      expect(restoreLayout(raw, LIVE, SZ)).toEqual({ pos: {}, stale: false });
    }
  });

  it("空存档 / 空实体表: 返回空表", () => {
    expect(restoreLayout(at({}), LIVE, SZ).pos).toEqual({});
    expect(restoreLayout(at({ a: { x: 1, y: 2 } }), [], SZ).pos).toEqual({});
  });
});

describe("★ restoreLayout 的画布尺寸闸门 (存档坐标是绝对值, 换尺寸不能套用)", () => {
  const LIVE = ["a"];
  const SZ = { w: 900, h: 620 };
  const POS = { a: { x: 100, y: 200 } };

  it("尺寸一致: 套用, 不算过期", () => {
    expect(restoreLayout({ size: SZ, pos: POS }, LIVE, SZ))
      .toEqual({ pos: POS, stale: false });
  });

  it("★★ 尺寸不一致: 整份丢弃并报 stale —— 全屏后画布 1900×900, 存档里那些按 900×620 摆的绝对坐标会和按新画布重算的 radialTargets 混在一起, 图歪掉", () => {
    const got = restoreLayout({ size: SZ, pos: POS }, LIVE, { w: 1900, h: 900 });
    expect(got).toEqual({ pos: {}, stale: true });
  });

  it("只有一边变了也算不一致 (宽变高不变: 侧栏折叠)", () => {
    expect(restoreLayout({ size: SZ, pos: POS }, LIVE, { w: 1200, h: 620 }).stale).toBe(true);
  });

  it("★ 老版裸 pos 存档 (没有 size 字段) 判过期而不是照套 —— 它正是本次要修的那种坐标, 来源尺寸不可知, 照套等于把 bug 留着", () => {
    expect(restoreLayout(POS, LIVE, SZ)).toEqual({ pos: {}, stale: true });
  });

  it("size 字段结构坏了 (非数字 / 缺项 / 非有限数) 同样判过期", () => {
    for (const bad of [
      { w: "900", h: 620 }, { w: 900 }, { w: NaN, h: 620 }, null, 7,
    ]) {
      expect(restoreLayout({ size: bad, pos: POS }, LIVE, SZ))
        .toEqual({ pos: {}, stale: true });
    }
  });

  it("★ 没有存档 (null) 不算 stale —— stale 是给用户的\"你的手工布局这次没套用\"提示, 从没存过的人不该看到它", () => {
    expect(restoreLayout(null, LIVE, SZ).stale).toBe(false);
  });

  it("pos 字段坏了但 size 对得上: 不算 stale (存档尺寸是当前的, 没有\"换了画布\"这件事)", () => {
    expect(restoreLayout({ size: SZ, pos: "nope" }, LIVE, SZ))
      .toEqual({ pos: {}, stale: false });
  });
});

describe("packLayout — 存档写入口 (与 restoreLayout 是同一份格式)", () => {
  const SZ = { w: 900, h: 620 };

  it("把当前画布尺寸一起写进去", () => {
    expect(packLayout({ a: { x: 1, y: 2 } }, SZ))
      .toEqual({ size: SZ, pos: { a: { x: 1, y: 2 } } });
  });

  it("★ 往返: packLayout 的产物必须能被 restoreLayout 原样读回 —— 两头分别硬编码格式就会静默分叉", () => {
    const pos = { a: { x: 1, y: 2 }, b: { x: -3.5, y: 4.25 } };
    const got = restoreLayout(packLayout(pos, SZ), ["a", "b"], SZ);
    expect(got).toEqual({ pos, stale: false });
  });

  it("★ 往返 + 换尺寸: 同一份产物在别的画布下判过期 (闸门真的拦在往返路径上)", () => {
    const got = restoreLayout(packLayout({ a: { x: 1, y: 2 } }, SZ), ["a"], { w: 400, h: 300 });
    expect(got).toEqual({ pos: {}, stale: true });
  });
});

// ── 实体搜索 + 定位 ───────────────────────────────────────────────────
// 固件刻意让 id 与 displayName 分家 (真库就是这样: id 是表名, 显示名是中文),
// 否则"按名搜也能命中"这条根本测不出来。
const Q_NODES: GraphNode[] = [
  { id: "order_lib", displayName: "订单条目", domain: "订单", summary: "", dataSource: "lib" },
  { id: "lib_reader_order", displayName: "客户订单", domain: "订单", summary: "", dataSource: "lib" },
  { id: "order_lib_his", displayName: "订单条目历史", domain: "订单", summary: "", dataSource: "lib" },
  { id: "lib_loan", displayName: "借阅实例", domain: "产品", summary: "", dataSource: "lib" },
];

describe("searchNodes — 按 id / 名称搜实体", () => {
  it("id 子串命中", () => {
    expect(searchNodes(Q_NODES, "loan").map((n) => n.id)).toEqual(["lib_loan"]);
  });

  it("大小写无关 (用户不会照着表名敲大写)", () => {
    expect(searchNodes(Q_NODES, "LIB_LOAN").map((n) => n.id)).toEqual(["lib_loan"]);
  });

  it("按显示名也能搜到 (图上标签显示的是显示名)", () => {
    expect(searchNodes(Q_NODES, "借阅").map((n) => n.id)).toEqual(["lib_loan"]);
  });

  it("id 前缀命中排在中间命中之前", () => {
    // order_lib / order_lib_his 是前缀, lib_reader_order 只在中间命中
    const ids = searchNodes(Q_NODES, "order").map((n) => n.id);
    expect(ids).toEqual(["order_lib", "order_lib_his", "lib_reader_order"]);
  });

  it("空查询 / 纯空白: 返回空数组 (不是返回全部)", () => {
    expect(searchNodes(Q_NODES, "")).toEqual([]);
    expect(searchNodes(Q_NODES, "   ")).toEqual([]);
  });

  it("查询带首尾空格: 照样命中 (粘贴表名常带空格)", () => {
    expect(searchNodes(Q_NODES, "  lib_loan  ").map((n) => n.id)).toEqual(["lib_loan"]);
  });

  it("无命中: 空数组", () => {
    expect(searchNodes(Q_NODES, "查无此表")).toEqual([]);
  });

  it("limit 截断 (工具栏下拉放不下全部实体)", () => {
    expect(searchNodes(Q_NODES, "o", 2)).toHaveLength(2);
  });

  it("同一查询两次结果一致 (排序不能不稳定)", () => {
    expect(searchNodes(Q_NODES, "order")).toEqual(searchNodes(Q_NODES, "order"));
  });
});

describe("centerOn — 把某点挪到画布正中", () => {
  const SIZE = { w: 900, h: 620 };

  it("目标点变换后正好落在画布中心", () => {
    const v = centerOn({ x: 1234, y: 88 }, SIZE, 0.7);
    expect(1234 * v.k + v.tx).toBeCloseTo(450, 6);
    expect(88 * v.k + v.ty).toBeCloseTo(310, 6);
  });

  it("不改缩放 (搜索是找到它, 不是重新构图)", () => {
    expect(centerOn({ x: 5, y: 5 }, SIZE, 0.33).k).toBe(0.33);
  });

  it("与 screenToGraph 互逆: 画布中心反变换回原图坐标", () => {
    const p = { x: -700, y: 2000 };
    const v = centerOn(p, SIZE, 1.8);
    const back = screenToGraph(SIZE.w / 2, SIZE.h / 2, v);
    expect(back.x).toBeCloseTo(p.x, 6);
    expect(back.y).toBeCloseTo(p.y, 6);
  });
});
