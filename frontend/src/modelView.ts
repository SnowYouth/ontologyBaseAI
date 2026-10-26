/**
 * modelView.ts — 模型配置面板的纯逻辑 (展示文案 + 保存校验 + 删除门禁)。
 *
 * 跟 ModelPanel.tsx 分开是为了可测: 这里不碰 React/fetch, 判据全是纯函数,
 * 同 proposalView.ts / conceptSqlPreview.ts 的分工。
 */
import type { ModelConfig } from "./api";

export type ModelDraft = {
  name: string;
  model: string;
  api_key: string;
  base_url: string;
};

/** 后端 _NAME_RE 的前端镜像 (name 要进 URL path)。两处必须一致。 */
const NAME_RE = /^[A-Za-z0-9_-]+$/;

export function modelSubtitle(m: ModelConfig): string {
  // base_url 空不是"没配好"而是走 provider 官方端点 —— 文案说清, 否则
  // 用户看见空白会以为漏填了。
  return `${m.model} · ${m.base_url || "官方端点"}`;
}

export function keyHint(m: ModelConfig): string {
  // api_key_set 由后端用 is_placeholder_api_key 判 (空值或 .env.example 的
  // 占位值都算未配), 前端忠实显示即可。
  return m.api_key_set ? m.api_key_masked : "⚠ 未配 key";
}

/**
 * 保存前校验, 返回错误文案或 null。
 *
 * @param editing 正在编辑的条目名 (新增时留空) —— 改 base_url 时 name 没变,
 *                不该被自己卡成"重名"。
 */
export function saveError(
  draft: ModelDraft, existing: ModelConfig[], editing?: string,
): string | null {
  const name = draft.name.trim();
  const model = draft.model.trim();
  if (!name) return "请填名字 (这条配置的标识, 如 mid-glm)";
  if (!NAME_RE.test(name)) return "名字只能用字母、数字、下划线、连字符";
  if (!model) return "请填模型 (litellm 格式 provider/model)";
  // litellm 认 "provider/model"; 只写 claude-sonnet-5-5 会 BadRequest ——
  // 在这儿拦比等一次失败的对话便宜。
  if (!model.includes("/")) return "模型要带 provider 前缀, 如 anthropic/claude-sonnet-5-5";
  // ★ provider 段大小写敏感 (实测): openai/gpt-5 可解析, OpenAI/ 和 openAi/
  // 都抛 "LLM Provider NOT provided"。只校验第一段 —— 后面的模型名/部署名
  // 允许大写 (azure/GPT-4o-Deployment 是合法的)。
  const provider = model.slice(0, model.indexOf("/"));
  if (provider !== provider.toLowerCase()) {
    return `provider 前缀要全小写: ${provider.toLowerCase()}/... (litellm 大小写敏感)`;
  }
  if (existing.some((m) => m.name === name && m.name !== editing)) {
    return `名字 "${name}" 已存在`;
  }
  // 刻意不强制填 key: 允许先存配置、拿到 key 再补 (真正的拦截在 CLI 启动和对话时)。
  return null;
}

export function deleteDisabled(m: ModelConfig): boolean {
  // 后端也拒 (409), 这里只是别让用户白点一次。
  return m.active;
}

export function deleteTooltip(m: ModelConfig): string {
  return m.active ? "正在使用中, 请先切换到其它模型" : "删除这条配置";
}

export function sortModels(rows: ModelConfig[]): ModelConfig[] {
  return [...rows].sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}
