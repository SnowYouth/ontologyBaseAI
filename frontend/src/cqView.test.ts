import { describe, it, expect } from "vitest";
import {
  emptyDraft, draftFromCase, draftToCase, draftError, runButtonLabel,
  baselineHint, truncatedHint, noDbEvidenceHint, type CqDraft,
} from "./cqView";
import type { CqCase } from "./api";

function mk(o: Partial<CqCase> = {}): CqCase {
  return {
    id: "CQ-1", question: "读者 R001 借着哪些书?", kind: "single", turns: null,
    criteria: { 点名: "reader", 走边: "reader→loan", 值集: "0 借出中", 断腿: "无" },
    ...o,
  };
}

function draft(o: Partial<CqDraft> = {}): CqDraft {
  return { ...emptyDraft(), id: "CQ-9", question: "问题", ...o };
}

describe("emptyDraft", () => {
  it("新建默认单轮, 四类判据都在 (后端 extra=forbid, 键必须齐)", () => {
    const d = emptyDraft();
    expect(d.kind).toBe("single");
    expect(d.turns).toEqual([]);
    expect(Object.keys(d.criteria).sort()).toEqual(["值集", "断腿", "点名", "走边"].sort());
  });
});

describe("draftFromCase", () => {
  it("单轮考题的 turns: null → 表单里是空数组 (null 没法 .map)", () => {
    expect(draftFromCase(mk({ turns: null })).turns).toEqual([]);
  });

  it("多轮考题原样带出轮次", () => {
    const d = draftFromCase(mk({ kind: "multi_turn", turns: ["第一问", "第二问"] }));
    expect(d.kind).toBe("multi_turn");
    expect(d.turns).toEqual(["第一问", "第二问"]);
  });

  it("后端缺 criteria 某类时补空串 (老考题集可能少键)", () => {
    const c = mk({ criteria: { 点名: "a" } as CqCase["criteria"] });
    expect(draftToCase(draftFromCase(c)).criteria).toEqual(
      { 点名: "a", 走边: "", 值集: "", 断腿: "" });
  });
});

describe("draftToCase", () => {
  it("★ single → turns 必须是 null, 不是 []", () => {
    // 后端 CqCaseIn 的 kind=single 分支判 `elif self.turns:` ——
    // 传 [] 恰好假值放行, 但真跑过的就是这条: 空数组会被 upsert 原样写进
    // cq_set.json, 与既有考题集 (turns: null) 口径不一致。更要紧的是
    // 一旦 draftToCase 对 multi_turn 也走同一分支漏了归一, 后端直接 422。
    // 判据落在 null 本身, 不接受 [] 蒙过去。
    const c = draftToCase(draft({ kind: "single", turns: ["残留的一行"] }));
    expect(c.turns).toBeNull();
  });

  it("multi_turn 过滤掉空白行", () => {
    const c = draftToCase(draft({
      kind: "multi_turn", turns: ["第一问", "   ", "第二问", ""],
    }));
    expect(c.turns).toEqual(["第一问", "第二问"]);
  });

  it("id / question 去空格 (后端用 id 当文件键, 带空格会撞不上)", () => {
    const c = draftToCase(draft({ id: "  CQ-9  ", question: "  问题  " }));
    expect(c.id).toBe("CQ-9");
    expect(c.question).toBe("问题");
  });
});

describe("draftError", () => {
  it("id 空 → 报错", () => {
    expect(draftError(draft({ id: "" }), [])).toMatch(/编号|id/i);
  });

  it("question 空 → 报错", () => {
    expect(draftError(draft({ question: "  " }), [])).toMatch(/问题/);
  });

  it("新建时 id 与现有重复 → 前端先拦, 不等后端 409", () => {
    expect(draftError(draft({ id: "CQ-1" }), [mk({ id: "CQ-1" })])).toMatch(/已存在|重复/);
  });

  it("编辑自己时 id 不算重复", () => {
    // 同 modelView.saveError 的 editing 先例: 只改问题文案时 id 没变,
    // 不该被自己卡成"重复"
    expect(draftError(draft({ id: "CQ-1" }), [mk({ id: "CQ-1" })], "CQ-1")).toBeNull();
  });

  it("★ multi_turn 只给 1 轮 → 报错且说清修法", () => {
    // 后端 422 的那条: len(turns) < 2 直接判死。前端不拦就只能看后端报错。
    const e = draftError(draft({ kind: "multi_turn", turns: ["只有一问"] }), []);
    expect(e).toMatch(/2 轮|两轮/);
    expect(e).toMatch(/单轮/);   // 文案要给出路, 不是只报错
  });

  it("★ multi_turn 两行但其中一行空白 → 报错 (空白行会被过滤成 1 轮)", () => {
    expect(draftError(draft({ kind: "multi_turn", turns: ["第一问", "   "] }), []))
      .toMatch(/2 轮|两轮/);
  });

  it("multi_turn 给足 2 轮 → 放行", () => {
    expect(draftError(draft({ kind: "multi_turn", turns: ["一", "二"] }), [])).toBeNull();
  });

  it("single 带残留 turns → 放行 (draftToCase 会归一成 null)", () => {
    // 用户先选了多轮填了两行、又切回单轮: 不该拦, 提交时丢掉即可
    expect(draftError(draft({ kind: "single", turns: ["一", "二"] }), [])).toBeNull();
  });

  it("合法单轮 → null", () => {
    expect(draftError(draft(), [mk({ id: "CQ-1" })])).toBeNull();
  });

  it("判据可以全空 (后端 CqCriteriaIn 四类都有默认空串)", () => {
    expect(draftError(
      draft({ criteria: { 点名: "", 走边: "", 值集: "", 断腿: "" } }), [])).toBeNull();
  });
});

describe("runButtonLabel", () => {
  it("★ 零条时把 0 显示出来 (阻断项 #10 的判据)", () => {
    // 坏掉的面板就是这个样子: "▶ 重跑全部 0 条"。数字必须在文案里,
    // 否则用户看不出是"没考题"还是"按钮没反应"。
    expect(runButtonLabel(0, false, "")).toMatch(/0 条/);
  });

  it("有考题时显示条数", () => {
    expect(runButtonLabel(4, false, "")).toMatch(/4 条/);
  });

  it("跑中显示当前 id", () => {
    expect(runButtonLabel(4, true, "CQ-2")).toMatch(/CQ-2/);
  });

  it("跑中但还没收到 case_start → 不留悬空空格", () => {
    expect(runButtonLabel(4, true, "")).toBe("跑中…");
  });
});

describe("baselineHint", () => {
  it("没新增考题 → 不提示", () => {
    expect(baselineHint([])).toBeNull();
  });

  it("★ 新增后提示要跑一轮才进回归门守护范围", () => {
    // D3 定稿: uncovered 保持 warning 不升级为阻断 (升级会把"首次使用建了
    // 考题还没跑"变成硬墙), 代价是没跑过的考题静默不设防 —— 靠这句提示补。
    const h = baselineHint(["CQ-9"]);
    expect(h).toMatch(/重跑全部/);
    expect(h).toMatch(/回归门/);
    expect(h).toContain("CQ-9");
  });

  it("多条新增全列出来", () => {
    expect(baselineHint(["CQ-9", "CQ-10"])).toContain("CQ-10");
  });
});

describe("truncatedHint", () => {
  it("没截断 → 不提示", () => {
    expect(truncatedHint(false, "完整答案")).toBeNull();
  });

  it("★ 截断且答案为空 → 必须提示 (CQ-14/CQ-18 的真实形态)", () => {
    // 2026-09-19 跑批里这两条 answer="" 且 error=null, 面板上
    // `r?.answer &&` 条件为假 → 整张卡片什么都不显示, 判分的人
    // 看不出发生过截断。
    const h = truncatedHint(true, "");
    expect(h).not.toBeNull();
    expect(h).toMatch(/截断/);
    expect(h).toMatch(/max_tokens|token/i);
  });

  it("截断但有半截答案 → 也要提示 (答案不可信但非空)", () => {
    expect(truncatedHint(true, "半截答案")).toMatch(/截断/);
  });

  it("空答案但没截断 → 不冒充截断", () => {
    // 反证: 模型真的无话可说也是空答案, 别一律归因到截断。
    expect(truncatedHint(false, "")).toBeNull();
  });
});

describe("noDbEvidenceHint", () => {
  it("有真库证据 / 老结果缺键 → 不提示", () => {
    expect(noDbEvidenceHint(false)).toBeNull();
    expect(noDbEvidenceHint(undefined)).toBeNull();
  });

  it("★ 一条 SQL 都没成功 → 必须提示 (2026-10-20 CQ-5 抄旧结果的真实形态)", () => {
    // 那条答案 904 字带表格, error=null, 卡片看着完全正常。
    const h = noDbEvidenceHint(true);
    expect(h).not.toBeNull();
    expect(h).toMatch(/execute_sql/);
  });
});
