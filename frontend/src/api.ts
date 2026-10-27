// 后端唯一端点。dev 时后端跑在 8000。
const BASE = "http://localhost:8000";

export type Step = { name: string; result?: string; ms?: number; ok?: boolean };
export type SessionMeta = { id: string; title: string; updated_at: string };
export type SessionFull = SessionMeta & {
  created_at: string;
  messages: { role: string; content: string; steps?: Step[] }[];
};

// agent SSE 事件协议 (真相源 = core/agent.py 的 _emit 调用点)。
// 答案文本走 llm_chunk 增量拼, 不是单发的整段 —— 旧的 answer 事件已废弃。
export type StepEvent =
  | { type: "llm_start" }
  | { type: "llm_chunk"; chunk: string }
  | { type: "tool_start"; name: string; args: string }
  | { type: "tool_end"; name: string; result: string; latency_ms: number; success: boolean }
  | { type: "done" }
  | { type: "error"; error: string };

export type StreamHandlers = {
  onEvent: (ev: StepEvent) => void;
  onError: (msg: string) => void;
  onDone: () => void;
};

export async function postAgentStream(
  message: string,
  sessionId: string,
  h: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  try {
    const resp = await fetch(`${BASE}/agent/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId }),
      signal,
    });
    if (!resp.ok || !resp.body) {
      h.onError(`请求失败 (HTTP ${resp.status})`);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // 按 SSE 帧分隔符 \n\n 切; 不完整的尾部留在 buf
      const frames = buf.split("\n\n");
      buf = frames.pop() ?? "";
      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5).trim()) as StepEvent;
        if (ev.type === "error") { h.onError(ev.error); return; }
        if (ev.type === "done") { h.onDone(); return; }
        h.onEvent(ev);
      }
    }
    // 流结束但没收到 done (后端中途断)
    h.onDone();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") return;  // 切走主动 abort, 静默
    h.onError(`网络错误: ${String(e)}`);
  }
}

// ---------- 会话 CRUD ----------

export async function listSessions(): Promise<SessionMeta[]> {
  const resp = await fetch(`${BASE}/sessions`);
  return resp.json();
}

export async function createSession(): Promise<SessionFull> {
  const resp = await fetch(`${BASE}/sessions`, { method: "POST" });
  return resp.json();
}

export async function getSession(sid: string): Promise<SessionFull> {
  const resp = await fetch(`${BASE}/sessions/${sid}`);
  return resp.json();
}

export async function deleteSession(sid: string): Promise<{ ok: boolean }> {
  const resp = await fetch(`${BASE}/sessions/${sid}`, { method: "DELETE" });
  if (!resp.ok) {
    // 409 = 会话正在生成中
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `删除失败 (HTTP ${resp.status})`);
  }
  return resp.json();
}

// ---------- 工具 / MCP 配置管理 ----------

export type ToolStatus = { name: string; enabled: boolean };
export type McpServer = {
  name: string;
  command: string;
  args: string[];
  enabled: boolean;
  // 可见性层级 (与本地工具同口径): local = 只在 local profile 加载。
  category: "local" | "serving";
  // 后端算好的"本轮 agent 真会不会加载它" = enabled && 过了 profile 门控。
  // 与 enabled 分开: serving 部署下 local server 显示启用但 active=false。
  active: boolean;
};

export async function getToolConfig(): Promise<ToolStatus[]> {
  const resp = await fetch(`${BASE}/config/tools`);
  return resp.json();
}

export async function setToolEnabled(name: string, enabled: boolean): Promise<void> {
  await fetch(`${BASE}/config/tools/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export async function getMcpServers(): Promise<McpServer[]> {
  const resp = await fetch(`${BASE}/config/mcp_servers`);
  return resp.json();
}

// 新增表单的入参: active 是后端算的只读字段, 不进 POST body。
export type McpServerIn = Omit<McpServer, "active">;

export async function addMcpServer(s: McpServerIn): Promise<void> {
  const resp = await fetch(`${BASE}/config/mcp_servers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(s),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `新增失败 (HTTP ${resp.status})`);
  }
}

export async function deleteMcpServer(name: string): Promise<void> {
  const resp = await fetch(`${BASE}/config/mcp_servers/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `删除失败 (HTTP ${resp.status})`);
  }
}

// ---------- 模型配置 (候选清单 + 激活项) ----------
//
// 后端真相是 config/model.json, 改完下一轮对话即生效 (不用重启后端)。
// api_key 永远只收到掩码; 明文要单独调 getModelKey, 且后端只在
// DEPLOY_PROFILE=local 放行 (serving 下 403)。

export type ModelConfig = {
  name: string;
  model: string;
  base_url: string;
  api_key_masked: string;
  api_key_set: boolean;
  active: boolean;
};

export type ModelInput = {
  name: string;
  model: string;
  api_key: string;
  base_url: string;
};

/** 省略的字段 = 不动; api_key 空串同样按"不动"处理 (表单里显示的是掩码)。 */
export type ModelPatch = {
  model?: string;
  api_key?: string;
  base_url?: string;
};

export async function getModels(): Promise<ModelConfig[]> {
  const resp = await fetch(`${BASE}/config/model`);
  return resp.json();
}

export async function addModel(m: ModelInput): Promise<void> {
  const resp = await fetch(`${BASE}/config/model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(m),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `新增失败 (HTTP ${resp.status})`);
  }
}

export async function updateModel(name: string, patch: ModelPatch): Promise<void> {
  const resp = await fetch(`${BASE}/config/model/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `保存失败 (HTTP ${resp.status})`);
  }
}

export async function deleteModel(name: string): Promise<void> {
  const resp = await fetch(`${BASE}/config/model/${encodeURIComponent(name)}`,
                           { method: "DELETE" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `删除失败 (HTTP ${resp.status})`);
  }
}

export async function activateModel(name: string): Promise<void> {
  const resp = await fetch(`${BASE}/config/model/active`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `切换失败 (HTTP ${resp.status})`);
  }
}

/** 明文 key。后端只在 DEPLOY_PROFILE=local 放行, 其它形态 403。 */
export async function getModelKey(name: string): Promise<string> {
  const resp = await fetch(`${BASE}/config/model/${encodeURIComponent(name)}/key`);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `读取失败 (HTTP ${resp.status})`);
  }
  return (await resp.json()).api_key;
}

export async function setMcpEnabled(
  name: string, enabled: boolean, category?: "local" | "serving",
): Promise<void> {
  // category 省略 → 不进 body, 后端按"不动该字段"处理 (点开关不洗掉层级)。
  const resp = await fetch(`${BASE}/config/mcp_servers/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(category === undefined ? { enabled } : { enabled, category }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `操作失败 (HTTP ${resp.status})`);
  }
}

// ---------- 技能 (Skill) 配置管理 ----------

export type SkillStatus = { name: string; description: string; enabled: boolean };

export async function getSkills(): Promise<SkillStatus[]> {
  const resp = await fetch(`${BASE}/config/skills`);
  return resp.json();
}

export async function getSkillBody(name: string): Promise<string> {
  const resp = await fetch(`${BASE}/config/skills/${encodeURIComponent(name)}`);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `读取失败 (HTTP ${resp.status})`);
  }
  const data = await resp.json();
  return data.content;
}

// 编辑用: 拿含 frontmatter 的完整 SKILL.md (getSkillBody 剥了 frontmatter, 回写会挂校验)
export async function getSkillFull(name: string): Promise<string> {
  const resp = await fetch(`${BASE}/config/skills/${encodeURIComponent(name)}/full`);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `读取失败 (HTTP ${resp.status})`);
  }
  const data = await resp.json();
  return data.content;
}

export async function setSkillEnabled(name: string, enabled: boolean): Promise<void> {
  const resp = await fetch(`${BASE}/config/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `操作失败 (HTTP ${resp.status})`);
  }
}

export async function createSkill(name: string, content: string): Promise<void> {
  const resp = await fetch(`${BASE}/config/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, content }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `新建失败 (HTTP ${resp.status})`);
  }
}

export async function updateSkillBody(name: string, content: string): Promise<void> {
  const resp = await fetch(`${BASE}/config/skills/${encodeURIComponent(name)}/body`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `保存失败 (HTTP ${resp.status})`);
  }
}

export async function deleteSkill(name: string): Promise<void> {
  const resp = await fetch(`${BASE}/config/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `删除失败 (HTTP ${resp.status})`);
  }
}

// ---------- 本体编辑器 (domain/entity/relation/enum) ----------

export type OntoDomain = { code: string; color: string; displayOrder: number; description: string };
export type OntoEntity = {
  id: string; displayName: string; domain: string; maps_to: string;
  tables: string[]; summary: string; data_source: string;
  attributes: Record<string, unknown>[];
  [k: string]: unknown;   // extra_json 血肉 (recallKeywords/disambiguateFrom/hasEnum...)
};
export type OntoEntityExtra = {
  recallKeywords: string[];
  disambiguateFrom: string[];
  // cocVerified/inSpec 已于 2026-08-05 废除 (运行时零消费的旧一代治理位)
};
// entity attributes_json 单条属性 (9 键白名单, 封闭语义)。
// valueSet/inSpec/note 已于 2026-08-05 废除。
export type OntoAttribute = {
  name: string; kind: string; type: string;
  desc: string; desc_source: string; disambiguation: string;
  hasEnum: boolean | null;
  sensitive: boolean | null;
};
// 封闭可选值 ([值, 中文注释], 与后端 AttributeIn Literal 同步)。
export const ATTR_KINDS: [string, string][] = [
  ["pk", "主键"], ["fk", "外键"], ["enum", "枚举"],
  ["attr", "普通属性"], ["time", "时间"], ["bizkey", "业务键"],
];
export const ATTR_TYPES = ["bigint", "varchar", "datetime", "int", "char"];
export const ATTR_DESC_SOURCES: [string, string][] = [
  ["code", "代码注释"], ["infer", "推断"], ["convention", "惯例"],
];
export const MAX_ATTRS = 20;   // 与后端 _MAX_ATTRS 口径一致
export const REL_TYPES: [string, string][] = [
  ["ONE_TO_ONE", "一对一"], ["ONE_TO_MANY", "一对多"],
  ["MANY_TO_ONE", "多对一"], ["UNKNOWN", "待定基数"],
];
export type OntoRelation = {
  source: string; target: string; via: string | null;
  label: string | null; type: string | null;
  source_col: string | null; target_col: string | null; bridge_count: number | null;
  origin?: string | null;
};
export type OntoEnumCode = { code: string; name: string; desc: string };
export type OntoEnum = {
  entity: string; field: string; name: string; spec_ref: string; codes: OntoEnumCode[];
};

async function okOrThrow(resp: Response, verb: string): Promise<void> {
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `${verb}失败 (HTTP ${resp.status})`);
  }
}

// ---------- 本体关联网络图 (GET /ontology/graph) ----------
// ★ 这里的 GraphDomain.color 是 `string | null` 而非复用 OntoDomain ——
// 真库里 `读者账户` 域的 color 就是 NULL (2026-09-21 实查)。OntoDomain 声明
// 的是 string, 复用会让类型撒谎, 下游写 d.color.toUpperCase() 运行时炸。
export type GraphNode = { id: string; displayName: string; domain: string; summary: string; dataSource: string };
export type GraphEdge = {
  source: string; target: string;
  via: string | null; label: string | null; type: string | null;
};
export type GraphDomain = {
  code: string; color: string | null; displayOrder: number; description: string;
};
export type GraphData = { nodes: GraphNode[]; edges: GraphEdge[]; domains: GraphDomain[] };

export async function getOntologyGraph(): Promise<GraphData> {
  const resp = await fetch(`${BASE}/ontology/graph`);
  await okOrThrow(resp, "加载本体图");
  return resp.json();
}

// domain
export async function getOntoDomains(): Promise<OntoDomain[]> {
  return (await fetch(`${BASE}/ontology/domains`)).json();
}

// 源清单 (本机 DB_SOURCES 配的源名, 供实体编辑器 data_source 下拉用)
export type SourcesOut = { sources: string[] };
export async function getSources(): Promise<SourcesOut> {
  const resp = await fetch(`${BASE}/config/sources`);
  if (!resp.ok) throw new Error(`源清单读取失败 (HTTP ${resp.status})`);
  return resp.json();
}
export async function postOntoDomain(d: OntoDomain): Promise<void> {
  const resp = await fetch(`${BASE}/ontology/domain`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(d),
  });
  await okOrThrow(resp, "保存域");
}
export async function deleteOntoDomain(code: string): Promise<void> {
  await okOrThrow(await fetch(`${BASE}/ontology/domain/${encodeURIComponent(code)}`,
    { method: "DELETE" }), "删除域");
}

// entity
export async function getOntoEntities(): Promise<OntoEntity[]> {
  return (await fetch(`${BASE}/ontology/entities`)).json();
}
export async function putOntoEntity(
  id: string,
  edits: {
    displayName: string; domain: string; summary: string; maps_to: string; tables: string[];
    data_source: string;
    extra?: OntoEntityExtra;
    attributes?: OntoAttribute[];
  },
): Promise<void> {
  const resp = await fetch(`${BASE}/ontology/entity/${encodeURIComponent(id)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(edits),
  });
  await okOrThrow(resp, "保存实体");
}
export async function postOntoEntity(e: {
  id: string; displayName: string; domain: string;
  maps_to: string; tables: string[]; summary: string; data_source: string;
}): Promise<void> {
  const resp = await fetch(`${BASE}/ontology/entity`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(e),
  });
  await okOrThrow(resp, "新增实体");
}
export async function deleteOntoEntity(id: string): Promise<void> {
  await okOrThrow(await fetch(`${BASE}/ontology/entity/${encodeURIComponent(id)}`,
    { method: "DELETE" }), "删除实体");
}

// relation
export async function getOntoRelations(): Promise<OntoRelation[]> {
  return (await fetch(`${BASE}/ontology/relations`)).json();
}
export async function postOntoRelation(r: OntoRelation): Promise<void> {
  // via 由后端 store 从 source_col/target_col 派生, 前端不传。col 必填 (空挂 422)。
  const payload = {
    source: r.source, target: r.target,
    type: r.type || "UNKNOWN", label: r.label ?? "",
    source_col: r.source_col ?? "", target_col: r.target_col ?? "",
    origin: r.origin || "manual",
  };
  const resp = await fetch(`${BASE}/ontology/relation`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  await okOrThrow(resp, "保存关系");
}
export async function deleteOntoRelation(
  source: string, target: string, source_col: string, target_col: string
): Promise<void> {
  const qs = new URLSearchParams({ source, target, source_col, target_col });
  await okOrThrow(await fetch(`${BASE}/ontology/relation?${qs}`, { method: "DELETE" }), "删除关系");
}
export async function flipOntoRelation(
  source: string, target: string, source_col: string, target_col: string
): Promise<void> {
  const resp = await fetch(`${BASE}/ontology/relation/flip`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, target, source_col, target_col }),
  });
  await okOrThrow(resp, "改方向");
}

// enum
export async function getOntoEnums(): Promise<OntoEnum[]> {
  return (await fetch(`${BASE}/ontology/enums`)).json();
}
export async function postOntoEnum(e: OntoEnum): Promise<void> {
  // 后端 EnumIn.name/spec_ref 是 str (非 Optional), 读接口对无值字段返回 null → 直接回写挂 422。
  // 归一可空标量 null→"" (与 postOntoRelation 同源坑)。codes 内 code/name/desc 同理兜底。
  const payload = {
    ...e,
    name: e.name ?? "",
    spec_ref: e.spec_ref ?? "",
    codes: (e.codes ?? []).map((c) => ({
      code: c.code ?? "", name: c.name ?? "", desc: c.desc ?? "",
    })),
  };
  const resp = await fetch(`${BASE}/ontology/enum`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  await okOrThrow(resp, "保存码表");
}
export async function deleteOntoEnum(entity: string, field: string): Promise<void> {
  const qs = new URLSearchParams({ entity, field });
  await okOrThrow(await fetch(`${BASE}/ontology/enum?${qs}`, { method: "DELETE" }), "删除码表");
}

// ---------- 验证回路 B (CQ) ----------

export type CqCriteria = { 点名: string; 走边: string; 值集: string; 断腿: string };
export type CqCase = {
  id: string; question: string; kind: string;
  turns: string[] | null; criteria: CqCriteria;
};
export type ToolEvent = {
  name: string; args: string; result: string | null;
  ms: number | null; ok: boolean | null;
};
export type CqVerdict = "pass" | "partial" | "fail" | null;
export type CqResult = {
  id: string; answer: string; tool_events: ToolEvent[];
  error: string | null; verdict: CqVerdict; note: string;
  /** agent 撞 max_tokens 被切断 (2026-10-20 新增)。老结果文件没这个键 → undefined。 */
  truncated?: boolean;
  /** 整轮没有一次 execute_sql 成功 (2026-10-21 新增)。老结果文件没这个键 → undefined。 */
  no_db_evidence?: boolean;
};
export type CqResultSet = { ran_at: string; ontology_path: string; results: CqResult[] } | null;

export async function getCqSet(): Promise<CqCase[]> {
  const resp = await fetch(`${BASE}/cq/set`);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `考题集读取失败 (HTTP ${resp.status})`);
  }
  const data = await resp.json();
  return data.cases;
}

export async function getCqResults(): Promise<{ current: CqResultSet; previous: CqResultSet }> {
  const resp = await fetch(`${BASE}/cq/results`);
  return resp.json();
}

export async function putCqVerdict(id: string, verdict: Exclude<CqVerdict, null>, note: string): Promise<void> {
  const resp = await fetch(`${BASE}/cq/verdict/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verdict, note }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `保存失败 (HTTP ${resp.status})`);
  }
}

/** 新建考题; id 撞现有 → 后端 409, detail 要显示出来。 */
export async function postCqCase(c: CqCase): Promise<void> {
  const resp = await fetch(`${BASE}/cq/case`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(c),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(detailOf(data) ?? `新建考题失败 (HTTP ${resp.status})`);
  }
}

export async function putCqCase(id: string, c: CqCase): Promise<void> {
  const resp = await fetch(`${BASE}/cq/case/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(c),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(detailOf(data) ?? `保存考题失败 (HTTP ${resp.status})`);
  }
}

export async function delCqCase(id: string): Promise<void> {
  const resp = await fetch(`${BASE}/cq/case/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(detailOf(data) ?? `删除考题失败 (HTTP ${resp.status})`);
  }
}

/**
 * 取后端错误文案。422 的 detail 是 pydantic 的数组 (每项 {loc, msg}),
 * 直接塞进 React 会渲染成 "[object Object]" —— 这里摊平成人话。
 */
function detailOf(data: unknown): string | null {
  const d = (data as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    const msgs = d.map((e) => (e as { msg?: string })?.msg).filter(Boolean);
    if (msgs.length > 0) return msgs.join("; ");
  }
  return null;
}

export type CqRunHandlers = {
  onCaseStart: (id: string) => void;
  onCaseDone: (r: CqResult) => void;
  onDone: (n: number) => void;
  onError: (msg: string) => void;
};

export async function runCq(h: CqRunHandlers, signal?: AbortSignal): Promise<void> {
  try {
    const resp = await fetch(`${BASE}/cq/run`, { method: "POST", signal });
    if (!resp.ok || !resp.body) { h.onError(`请求失败 (HTTP ${resp.status})`); return; }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop() ?? "";
      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5).trim());
        if (ev.type === "error") { h.onError(ev.error); return; }
        if (ev.type === "done") { h.onDone(ev.n); return; }
        if (ev.type === "case_start") h.onCaseStart(ev.id);
        else if (ev.type === "case_done") h.onCaseDone(ev as CqResult);
      }
    }
    h.onDone(0);
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") return;
    h.onError(`网络错误: ${String(e)}`);
  }
}

// ---------- filter_rule 管理 + 校验 ----------
export type FilterRule = {
  entity: string; col: string; op: string; value: string;
  mode: "hard" | "soft"; origin: "source_code" | "manual" | "ai";
};
export type FilterRuleCheck = { name: string; ok: boolean; detail: string; skipped?: boolean };
export type FilterRuleValidateResult = {
  entity: string; col: string; op: string; value: string;
  checks: FilterRuleCheck[]; ok: boolean;
};

export async function getFilterRules(): Promise<FilterRule[]> {
  const resp = await fetch(`${BASE}/filter-rules`);
  if (!resp.ok) throw new Error(`规则读取失败 (HTTP ${resp.status})`);
  return (await resp.json()).rules;
}

export async function postFilterRule(fr: FilterRule): Promise<void> {
  const resp = await fetch(`${BASE}/filter-rules`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fr),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ? JSON.stringify(data.detail) : `保存失败 (HTTP ${resp.status})`);
  }
}

export async function deleteFilterRule(
  key: { entity: string; col: string; op: string; value: string }
): Promise<void> {
  const resp = await fetch(`${BASE}/filter-rules`, {
    method: "DELETE", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(key),
  });
  if (!resp.ok) throw new Error(`删除失败 (HTTP ${resp.status})`);
}

export async function validateFilterRules(): Promise<FilterRuleValidateResult[]> {
  const resp = await fetch(`${BASE}/filter-rules/validate`, { method: "POST" });
  if (!resp.ok) throw new Error(`校验失败 (HTTP ${resp.status})`);
  return (await resp.json()).results;
}

// 校验历史一次 run 的单条结果 (后端 load_check_runs 组装, ok 已现算)。
export type FilterRuleCheckRow = {
  entity: string; col: string; op: string; value: string;
  column_exists: number | null;      // 1/0
  value_in_enum: number | null;      // 1/0/null (null=该字段无枚举登记, skipped)
  sql_runs: number | null;           // 1/0
  detail: string;
  ok: boolean;
};
export type FilterRuleCheckRun = {
  run_id: string; checked_at: string; results: FilterRuleCheckRow[];
};

export async function getFilterRuleCheckHistory(limit = 2): Promise<FilterRuleCheckRun[]> {
  const resp = await fetch(`${BASE}/filter-rules/check-history?limit=${limit}`);
  if (!resp.ok) throw new Error(`check-history 失败 (HTTP ${resp.status})`);
  const data = await resp.json();
  return data.runs;
}

// ── 概念层 (OBDA TBox) ──
export type ConceptCondition = {
  entity: string; col: string; op: string; value: string | null; seq: number;
  kind?: "column" | "agg";
  agg_fn?: string; agg_target?: string; group_by_col?: string;
  having_op?: string; having_value?: string;
};
export type Concept = {
  id: string; display_name: string; anchor_entity: string;
  summary: string; origin: "manual" | "ai"; valid: number;
  conditions: ConceptCondition[];
};
export type ConceptCheck = { name: string; ok: boolean; detail: string };
export type ConceptValidateResult = { id: string; checks: ConceptCheck[]; ok: boolean };

export async function getConcepts(): Promise<Concept[]> {
  const resp = await fetch(`${BASE}/concepts`);
  if (!resp.ok) throw new Error(`概念读取失败 (HTTP ${resp.status})`);
  return (await resp.json()).concepts;
}

export async function postConcept(c: Concept): Promise<void> {
  const resp = await fetch(`${BASE}/concepts`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: c.id, display_name: c.display_name, anchor_entity: c.anchor_entity,
      summary: c.summary, origin: c.origin,
      conditions: c.conditions.map((cc, i) => ({
        entity: cc.entity, col: cc.col, op: cc.op, value: cc.value, seq: i,
        kind: cc.kind ?? "column",
        agg_fn: cc.agg_fn ?? "", agg_target: cc.agg_target ?? "",
        group_by_col: cc.group_by_col ?? "",
        having_op: cc.having_op ?? "", having_value: cc.having_value ?? "",
      })),
    }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ? JSON.stringify(data.detail) : `保存失败 (HTTP ${resp.status})`);
  }
}

export async function deleteConcept(key: { id: string }): Promise<void> {
  const resp = await fetch(`${BASE}/concepts`, {
    method: "DELETE", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(key),
  });
  if (!resp.ok) throw new Error(`删除失败 (HTTP ${resp.status})`);
}

export async function validateConcepts(): Promise<ConceptValidateResult[]> {
  const resp = await fetch(`${BASE}/concepts/validate`, { method: "POST" });
  if (!resp.ok) throw new Error(`校验失败 (HTTP ${resp.status})`);
  return (await resp.json()).results;
}

// ── 自演化 (self-evolve) ──
export type Proposal = {
  id: number; kind: string; direction: "add" | "retire";
  payload_json: string; reason: string;
  review_status: "pending" | "approved" | "rejected";
  patch_status: "pending" | "applied" | "failed";
  proposed_at: string; proposed_by: string;
  probe_result_json: string | null;
  regression_ok: number | null;
  tombstone_hit: number;
  tombstone_note: string | null;
  apply_journal_json: string | null;
};

export type RelationHealth = {
  source: string; target: string; via: string;
  use_count: number; suspect_count: number;
  last_suspect_at: string | null;
};

export async function listProposals(review_status?: string): Promise<Proposal[]> {
  const qs = review_status ? `?review_status=${encodeURIComponent(review_status)}` : "";
  const resp = await fetch(`${BASE}/selfevolve/proposals${qs}`);
  if (!resp.ok) throw new Error(`提议读取失败 (HTTP ${resp.status})`);
  return (await resp.json()).proposals;
}

export async function reviewProposal(
    id: number, review_status: "approved" | "rejected", review_note: string,
    label = ""): Promise<void> {
  const resp = await fetch(`${BASE}/selfevolve/proposals/${id}/review`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review_status, review_note, label }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `审批失败 (HTTP ${resp.status})`);
  }
}

export type ProcessStats = {
  processed: number; proposals_created: number;
  skipped: number; tombstone_hits: number;
};

export async function processSignals(limit = 10): Promise<ProcessStats> {
  const resp = await fetch(`${BASE}/selfevolve/process?limit=${limit}`, { method: "POST" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `处理失败 (HTTP ${resp.status})`);
  }
  return resp.json();
}

/** 冷路径流式版: 探针要连真库逐对跑, 同步版会让人干等几十秒且不知死活。
 *  onStart 一收到就能把"正在处理 N 条信号"显示出来。 */
export async function processSignalsStream(
  limit: number,
  onStart: (pending: number) => void,
): Promise<ProcessStats> {
  const resp = await fetch(`${BASE}/selfevolve/process-stream?limit=${limit}`,
                           { method: "POST" });
  if (!resp.ok || !resp.body) throw new Error(`处理失败 (HTTP ${resp.status})`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith("data:")) continue;
      const ev = JSON.parse(line.slice(5).trim());
      if (ev.type === "error") throw new Error(ev.error);
      if (ev.type === "start") onStart(ev.pending);
      if (ev.type === "done") return ev as ProcessStats;
    }
  }
  // 流结束却没收到 done = 后端中途断了, 不能当成功 (安静的失败)
  throw new Error("处理中断: 后端未返回结果");
}


export async function applyProposal(
    id: number): Promise<{ ok: boolean; detail: string; new_hard_blocks?: string[] }> {
  const resp = await fetch(`${BASE}/selfevolve/proposals/${id}/apply`, { method: "POST" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `落地失败 (HTTP ${resp.status})`);
  }
  return resp.json();
}

export type CqRegressionOut = {
  suspects_updated: number;
  mismatch_thresholds_hit: number;
  retire_proposals_created: number;
  scored_current: number;
  scored_previous: number;
  total_current: number;
  warning: string | null;
};

// 淘汰半环: 扫两轮 CQ 结果做 verdict 下降归因 + mismatch 累计。
// ★ 必须在 CQ 面板逐条打完分之后点, 否则 verdict 归因空转 (端点会给 warning)。
export async function runCqRegression(): Promise<CqRegressionOut> {
  const resp = await fetch(`${BASE}/selfevolve/cq-regression`, { method: "POST" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `CQ 回归扫描失败 (HTTP ${resp.status})`);
  }
  return resp.json();
}

export async function listRelationHealth(): Promise<RelationHealth[]> {
  const resp = await fetch(`${BASE}/selfevolve/health`);
  if (!resp.ok) throw new Error(`边健康读取失败 (HTTP ${resp.status})`);
  return (await resp.json()).health;
}

// 可疑边一键提 retire: 只进人审队列, 落地仍走 approve → apply → 回归门。
export async function proposeRetireFromHealth(
    h: RelationHealth): Promise<{ ok: boolean; detail?: string }> {
  const resp = await fetch(`${BASE}/selfevolve/health/retire`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: h.source, target: h.target, via: h.via }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail ?? `提 retire 失败 (HTTP ${resp.status})`);
  }
  return resp.json();
}
