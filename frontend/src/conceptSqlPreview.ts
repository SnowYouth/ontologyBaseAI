// 概念条件 → SQL 片段示意 (纯函数, 概念编辑弹窗实时预览用)。
//
// 只拼条件片段不拼 JOIN (概念层零 SQL 模板铁律): 运行时真正的 JOIN 由
// agent 调 get_join_path 现场组装, 头部注释如实声明这一点。
// agg 条件按派生表改写渲染 (非裸 HAVING) — 与真实运行时形态一致
// (分库中间件不接受裸聚合 HAVING, 后端已踩过的坑)。
import type { Concept, ConceptCondition } from "./api";

// value 形如函数表达式 (DATE_SUB(NOW(), INTERVAL 5 YEAR)) 时不加引号
const FN_EXPR = /^[A-Z_][A-Z0-9_]*\(.*\)$/;

function fmtOne(v: string): string {
  // 单引号双写: 纯展示无注入面 (真 SQL 由 agent 运行时组装), 但不转义会渲染出
  // 'O'Brien' 这种断裂引号, 违反"预览不假装有效"
  return FN_EXPR.test(v) ? v : `'${v.replace(/'/g, "''")}'`;
}

// 按顶层逗号拆项: 括号内的逗号属于函数实参, 不是 IN 列表分隔符
// (否则 DATE_SUB(NOW(), INTERVAL 5 YEAR) 会被劈成两项, 语法像真的语义全错)
function splitTopLevel(v: string): string[] {
  const items: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of v) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    if (ch === "," && depth === 0) {
      items.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  items.push(cur);
  return items;
}

function fmtValue(op: string, value: string | null): string {
  const v = (value ?? "").trim();
  if (v === "") return "''";
  if (op.toUpperCase() === "IN") {
    // 先剔原始空项再逐项格式化: 逗号串按项判函数表达式, 整体函数表达式不被劈开
    const items = splitTopLevel(v)
      .map((x) => x.trim())
      .filter((x) => x !== "")
      .map(fmtOne);
    return `(${items.join(", ") || "''"})`;
  }
  return fmtOne(v);
}

function condOk(c: ConceptCondition): boolean {
  return !!c.entity && !!c.col && !!c.op;
}

// 单条 column 条件 → "COL op value" / "entity.COL op value"
function columnExpr(c: ConceptCondition, anchor: string): string {
  const col = c.entity && c.entity !== anchor ? `${c.entity}.${c.col}` : c.col;
  const op = c.op.toUpperCase();
  if (op === "IS NULL" || op === "IS NOT NULL") return `${col} ${op}`;
  return `${col} ${op} ${fmtValue(op, c.value)}`;
}

// 单条 agg 条件 → 派生表改写的 IN 子查询 (缩进 2 空格进 WHERE 块)
function aggExpr(c: ConceptCondition, anchor: string, idx: number): string {
  if (!c.entity || !c.agg_fn || !c.group_by_col || !c.having_op || !c.having_value) {
    return `/* 条件${idx + 1}未填完整 */`;
  }
  const gb = c.entity !== anchor ? `${c.entity}.${c.group_by_col}` : c.group_by_col;
  // 只有 COUNT 允许无目标列 (后端写时严同口径), 其余空目标列响亮占位而非渲染 SUM(*)
  const fn = (c.agg_fn ?? "").toUpperCase();
  const target = c.agg_target || (fn === "COUNT" ? "*" : "");
  if (!target) return `/* 条件${idx + 1}未填完整 (${fn} 需目标列) */`;
  const lines = [
    `${gb} IN (`,
    `    SELECT GROUP_COL FROM (`,
    `      SELECT ${c.group_by_col} AS GROUP_COL, ${fn}(${target}) AS AGG`,
    `      FROM ${c.entity}`,
  ];
  if (c.col && c.op) lines.push(`      WHERE ${columnExpr(c, anchor)}`);
  lines.push(
    `      GROUP BY ${c.group_by_col}`,
    `    ) t`,
    `    WHERE AGG ${c.having_op} ${c.having_value}`,
    `  )`,
  );
  return lines.join("\n");
}

export function previewSql(draft: Concept): string {
  const header = "-- 示意: JOIN 由 agent 运行时按 get_join_path 组装";
  if (draft.conditions.length === 0) {
    return `${header}\n-- 暂无条件`;
  }
  const clauses: string[] = [];
  draft.conditions.forEach((c, i) => {
    const kind = c.kind ?? "column";
    if (kind === "agg") {
      clauses.push(aggExpr(c, draft.anchor_entity, i));
    } else if (condOk(c)) {
      clauses.push(columnExpr(c, draft.anchor_entity));
    } else {
      clauses.push(`/* 条件${i + 1}未填完整 */`);
    }
  });
  const where = clauses.map((x, i) => (i === 0 ? x : `  AND ${x.replace(/\n/g, "\n  ")}`)).join("\n");
  return `${header}\nSELECT *\nFROM ${draft.anchor_entity || "<anchor>"}\nWHERE ${where}`;
}
