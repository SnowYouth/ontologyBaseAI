/**
 * cqView.ts — CQ 考题面板的纯逻辑 (表单草稿归一 + 校验 + 按钮/提示文案)。
 *
 * 跟 CqPanel.tsx 分开是为了可测: 这里不碰 React/fetch, 判据全是纯函数,
 * 同 modelView.ts / proposalView.ts / conceptSqlPreview.ts 的分工。
 *
 * 这个模块扛的是两件事:
 *  1. kind ↔ turns 的归一 —— 后端 CqCaseIn 对这两者的组合判死 (422), 规则
 *     不对称 (multi_turn 必须 ≥2 条非空, single 则一条都不许带), 放在组件里
 *     写就只能靠真发请求才验得到。
 *  2. "0 条" 的文案 —— 阻断项 #10 的外在症状就是按钮上那个 0, 判据要能被单测
 *     钉住, 不能散在 JSX 模板串里。
 */
import type { CqCase, CqCriteria } from "./api";

export type CqDraft = {
  id: string;
  question: string;
  kind: string;
  /** 表单里恒为数组 (null 没法 .map); 提交时由 draftToCase 归一。 */
  turns: string[];
  criteria: CqCriteria;
};

/** 后端 CqCaseIn 的 kind 白名单镜像。 */
export const CQ_KINDS = ["single", "multi_turn"] as const;

function emptyCriteria(): CqCriteria {
  // 四类键必须齐: 后端 CqCriteriaIn 是 extra="forbid" 的封闭模型, 少键虽有
  // 默认值兜着, 但多键/错键会 422 —— 以这里为唯一出处, 别在组件里手拼。
  return { 点名: "", 走边: "", 值集: "", 断腿: "" };
}

export function emptyDraft(): CqDraft {
  return { id: "", question: "", kind: "single", turns: [], criteria: emptyCriteria() };
}

export function draftFromCase(c: CqCase): CqDraft {
  return {
    id: c.id,
    question: c.question,
    kind: c.kind || "single",
    turns: c.turns ?? [],          // 单轮考题存的是 null
    criteria: { ...emptyCriteria(), ...(c.criteria ?? {}) },
  };
}

/** 多轮的有效轮次 = 去掉纯空白行 (用户加了行没填就提交是常态)。 */
function liveTurns(d: CqDraft): string[] {
  return d.turns.map((t) => t.trim()).filter((t) => t.length > 0);
}

export function draftToCase(d: CqDraft): CqCase {
  return {
    id: d.id.trim(),               // id 是考题集里的键, 带空格会撞不上
    question: d.question.trim(),
    kind: d.kind,
    // ★ single 必须存 null 而不是 []: 既有考题集 (含 scripts/seed_cq.json)
    // 全是 turns: null, 后端 single 分支也会把空值归一成 null。前端送 []
    // 等于把一个口径不一致的空数组写进 cq_set.json。
    turns: d.kind === "multi_turn" ? liveTurns(d) : null,
    criteria: { ...d.criteria },
  };
}

/**
 * 保存前校验, 返回错误文案或 null。
 *
 * @param editing 正在编辑的考题 id (新建时留空) —— 只改问题文案时 id 没变,
 *                不该被自己卡成"重复" (同 modelView.saveError 的先例)。
 */
export function draftError(
  d: CqDraft, existing: CqCase[], editing?: string,
): string | null {
  const id = d.id.trim();
  if (!id) return "请填考题编号 (如 CQ-5, 是考题集里的唯一键)";
  if (!d.question.trim()) return "请填问题 (要真拿去问 agent 的那句话)";
  if (existing.some((c) => c.id === id && c.id !== editing)) {
    return `考题 ${id} 已存在, 换个编号或直接去编辑那条`;
  }
  // ★ 后端判死的那条: multi_turn 的 turns 不足 2 条非空就 422。不拦的话
  // 用户只能看到一句后端报错, 且不知道还有"改成单轮"这条出路。
  if (d.kind === "multi_turn" && liveTurns(d).length < 2) {
    return "多轮考题至少要 2 轮 (只有 1 轮请改成单轮)";
  }
  // single 带着残留 turns 刻意不拦: 用户先选多轮填了两行又切回单轮是正常
  // 操作, draftToCase 提交时丢掉即可。
  return null;
}

export function runButtonLabel(n: number, running: boolean, runningId: string): string {
  // 跑中带 id 让用户知道卡在哪条 (一条 CQ 要走完整 agent loop, 几十秒);
  // 还没收到 case_start 时别拼出 "跑中… " 带尾空格。
  if (running) return runningId ? `跑中… ${runningId}` : "跑中…";
  // ★ 条数必须露在文案里: 面板坏掉 (考题集读不出) 的症状就是 "重跑全部 0 条",
  // 把 0 藏起来用户会以为是按钮没反应, 而不是没考题。
  return `▶ 重跑全部 ${n} 条`;
}

/**
 * D3 提示: 新建的考题还没跑过, 回归门守不到它。
 *
 * 定稿是 uncovered 保持 warning 不升级为阻断 —— 升级会把"首次使用建了考题
 * 还没跑"变成硬墙。代价是没跑过的考题静默不设防, 靠这句提示补上。
 */
export function baselineHint(addedIds: string[]): string | null {
  if (addedIds.length === 0) return null;
  return `新增考题 ${addedIds.join("、")} 需跑一轮 ▶ 重跑全部 才会进回归门守护范围`;
}

/**
 * 截断提示: agent 撞 max_tokens 被切断时, 答案不可信 (可能是半截, 也可能是空)。
 *
 * ★ 为什么必须单独提示: 截断不抛异常, `error` 是 null, 而面板渲染答案的条件是
 * `r?.answer &&` —— 空答案时整张卡片什么都不显示。2026-09-19 跑批的
 * CQ-14/CQ-18 就是这个形态: 判分的人看到一张空卡, 无从知道发生过截断,
 * 只能当成"模型答不出来"记 fail, 而真实原因是输出长度上限。
 */
export function truncatedHint(truncated: boolean, answer: string): string | null {
  if (!truncated) return null;          // 空答案但没截断 = 模型真的无话可说, 不冒充
  const tail = answer ? "下面的答案只是被切断前的半截, 不完整" : "答案为空";
  return `⚠️ 本条撞 max_tokens 被截断 (${tail}) —— 判分前先调大 MAX_TOKENS 重跑该条`;
}

/**
 * 无真库证据提示: 整轮没有一次 execute_sql 成功, 答案没碰过真库。
 *
 * ★ 2026-10-20 CQ-5: 库没初始化, agent 读旧结果文件编出 904 字带表格的答案,
 * error=null, 卡片看着完全正常。老结果文件没这个键 → undefined → 不提示。
 */
export function noDbEvidenceHint(noDbEvidence: boolean | undefined): string | null {
  if (!noDbEvidence) return null;
  return "⚠️ 本条没有一次 execute_sql 成功 —— 答案没查过真库, 可能是编的, 判分前先确认数据源已连上";
}
