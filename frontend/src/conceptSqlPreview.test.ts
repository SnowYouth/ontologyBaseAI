// conceptSqlPreview 纯函数单测。
//
// 重点是把 fb0d6cf 修的两个终审真雷钉成回归用例 (当时靠 node 手动实测才逮到,
// 没有测试兜底): ①IN + 含逗号函数表达式被劈开 ②非 COUNT 空目标列渲染 SUM(*)。
import { describe, it, expect } from "vitest";
import { previewSql } from "./conceptSqlPreview";
import type { Concept, ConceptCondition } from "./api";

function cond(patch: Partial<ConceptCondition>): ConceptCondition {
  return { entity: "lib_loan", col: "", op: "", value: null, seq: 0, ...patch };
}

function draft(conditions: ConceptCondition[], anchor = "lib_loan"): Concept {
  return {
    id: "c1", display_name: "测试概念", anchor_entity: anchor,
    summary: "", origin: "manual", valid: 1, conditions,
  };
}

describe("previewSql — 头部与空态", () => {
  it("零条件时只出头注释与占位, 不编造 WHERE", () => {
    const sql = previewSql(draft([]));
    expect(sql).toContain("JOIN 由 agent 运行时按 get_join_path 组装");
    expect(sql).toContain("-- 暂无条件");
    expect(sql).not.toContain("WHERE");
  });

  it("始终声明 JOIN 非本函数拼装 (零 SQL 模板铁律)", () => {
    const sql = previewSql(draft([cond({ col: "REC_STATUS", op: "=", value: "300001" })]));
    expect(sql.split("\n")[0]).toContain("示意");
    // 头注释以外不出现 JOIN 子句 (JOIN 只在那句声明里被提到)
    expect(sql.split("\n").slice(1).join("\n")).not.toContain("JOIN");
  });
});

describe("previewSql — column 条件", () => {
  it("锚实体的列不带前缀, 非锚实体带 entity. 前缀", () => {
    const sql = previewSql(draft([
      cond({ col: "REC_STATUS", op: "=", value: "300001" }),
      cond({ entity: "lib_reader", col: "MEMBER_NAME", op: "=", value: "张三", seq: 1 }),
    ]));
    expect(sql).toContain("REC_STATUS = '300001'");
    expect(sql).toContain("lib_reader.MEMBER_NAME = '张三'");
    expect(sql).not.toContain("lib_loan.REC_STATUS");
  });

  it("IS NULL / IS NOT NULL 不接 value", () => {
    const sql = previewSql(draft([cond({ col: "END_DATE", op: "IS NULL" })]));
    expect(sql).toContain("END_DATE IS NULL");
    expect(sql).not.toContain("IS NULL ''");
  });

  it("函数表达式不加引号, 普通值加引号", () => {
    const sql = previewSql(draft([
      cond({ col: "CREATE_DATE", op: "<=", value: "DATE_SUB(NOW(), INTERVAL 5 YEAR)" }),
    ]));
    expect(sql).toContain("CREATE_DATE <= DATE_SUB(NOW(), INTERVAL 5 YEAR)");
    expect(sql).not.toContain("'DATE_SUB");
  });

  it("多条件用 AND 串联", () => {
    const sql = previewSql(draft([
      cond({ col: "REC_STATUS", op: "=", value: "300001" }),
      cond({ col: "CREATE_DATE", op: "<=", value: "2020-01-01", seq: 1 }),
    ]));
    expect(sql).toContain("AND CREATE_DATE <= '2020-01-01'");
  });

  it("value 内单引号按 SQL 规矩双写转义 (O'Brien 不产生断裂引号)", () => {
    const sql = previewSql(draft([cond({ col: "MEMBER_NAME", op: "=", value: "O'Brien" })]));
    expect(sql).toContain("MEMBER_NAME = 'O''Brien'");
    expect(sql).not.toContain("'O'Brien'");
  });

  it("IN 列表里的单引号同样转义", () => {
    const sql = previewSql(draft([cond({ col: "N", op: "IN", value: "O'Brien,李'四" })]));
    expect(sql).toContain("IN ('O''Brien', '李''四')");
  });

  it("函数表达式不受转义影响 (不加引号也不改写内部)", () => {
    const sql = previewSql(draft([
      cond({ col: "D", op: "<=", value: "DATE_SUB(NOW(), INTERVAL 5 YEAR)" }),
    ]));
    expect(sql).toContain("DATE_SUB(NOW(), INTERVAL 5 YEAR)");
    expect(sql).not.toContain("''");
  });

  it("字段没填完整时响亮占位, 不静默产出半截 SQL", () => {
    const sql = previewSql(draft([cond({ col: "", op: "=", value: "x" })]));
    expect(sql).toContain("/* 条件1未填完整 */");
  });
});

describe("previewSql — ★真雷1: IN + 含逗号的函数表达式", () => {
  it("整体函数表达式不被逗号劈开 (fb0d6cf 回归)", () => {
    const sql = previewSql(draft([
      cond({ col: "CREATE_DATE", op: "IN", value: "DATE_SUB(NOW(), INTERVAL 5 YEAR)" }),
    ]));
    // 劈开的话会变成 ('DATE_SUB(NOW()', 'INTERVAL 5 YEAR)') — 语法像真的语义全错
    expect(sql).toContain("IN (DATE_SUB(NOW(), INTERVAL 5 YEAR))");
    expect(sql).not.toContain("'DATE_SUB(NOW()'");
  });

  it("顶层逗号仍正常拆成多项", () => {
    const sql = previewSql(draft([cond({ col: "REC_STATUS", op: "IN", value: "300001,300002" })]));
    expect(sql).toContain("IN ('300001', '300002')");
  });

  it("函数表达式与普通值混排, 各自按项判定", () => {
    const sql = previewSql(draft([
      cond({ col: "D", op: "IN", value: "DATE_SUB(NOW(), INTERVAL 1 DAY),2020-01-01" }),
    ]));
    expect(sql).toContain("IN (DATE_SUB(NOW(), INTERVAL 1 DAY), '2020-01-01')");
  });

  it("空项被剔除, 全空回落空串占位", () => {
    expect(previewSql(draft([cond({ col: "C", op: "IN", value: "a,,b" })])))
      .toContain("IN ('a', 'b')");
    expect(previewSql(draft([cond({ col: "C", op: "IN", value: " , " })])))
      .toContain("IN ('')");
  });
});

describe("previewSql — ★真雷2: 非 COUNT 空目标列", () => {
  const aggBase = {
    kind: "agg" as const, entity: "lib_copy", group_by_col: "MEMBER_ID",
    having_op: ">", having_value: "3",
  };

  it("SUM 缺目标列时响亮占位, 绝不渲染 SUM(*) 非法 SQL (fb0d6cf 回归)", () => {
    const sql = previewSql(draft([cond({ ...aggBase, agg_fn: "SUM", agg_target: "" })]));
    expect(sql).toContain("SUM 需目标列");
    expect(sql).not.toContain("SUM(*)");
  });

  it("COUNT 缺目标列合法回落 * (与后端写时严同口径)", () => {
    const sql = previewSql(draft([cond({ ...aggBase, agg_fn: "COUNT", agg_target: "" })]));
    expect(sql).toContain("COUNT(*)");
    expect(sql).not.toContain("需目标列");
  });

  it("SUM 带目标列正常渲染", () => {
    const sql = previewSql(draft([cond({ ...aggBase, agg_fn: "SUM", agg_target: "AMOUNT" })]));
    expect(sql).toContain("SUM(AMOUNT)");
  });
});

describe("previewSql — agg 派生表改写", () => {
  const agg = cond({
    kind: "agg", entity: "lib_copy", agg_fn: "COUNT", agg_target: "*",
    group_by_col: "MEMBER_ID", having_op: ">", having_value: "3",
  });

  it("渲染成 IN 子查询而非裸 HAVING (分库中间件坑对齐运行时形态)", () => {
    const sql = previewSql(draft([agg]));
    expect(sql).toContain("IN (");
    expect(sql).toContain("SELECT GROUP_COL FROM (");
    expect(sql).toContain("WHERE AGG > 3");
    // 裸 HAVING 是 分库中间件不接受的形态, 预览不能示范它
    expect(sql).not.toMatch(/HAVING/);
  });

  it("agg 条件自带 col/op 时渲染内层 WHERE (非锚实体带前缀)", () => {
    const sql = previewSql(draft([
      cond({ ...agg, col: "STATE", op: "=", value: "1" }),
    ]));
    expect(sql).toContain("WHERE lib_copy.STATE = '1'");
  });

  it("agg 字段缺失时响亮占位", () => {
    const sql = previewSql(draft([cond({ kind: "agg", entity: "lib_copy" })]));
    expect(sql).toContain("/* 条件1未填完整 */");
  });

  it("多条件下 agg 子查询整体缩进, 不破坏 AND 结构", () => {
    const sql = previewSql(draft([
      cond({ col: "REC_STATUS", op: "=", value: "300001" }),
      { ...agg, seq: 1 },
    ]));
    expect(sql).toContain("  AND lib_copy.MEMBER_ID IN (");
    expect(sql).toContain("      GROUP BY MEMBER_ID");
  });
});
