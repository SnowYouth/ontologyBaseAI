import { useEffect, useState } from "react";
import {
  getModels, addModel, updateModel, deleteModel, activateModel, getModelKey,
  type ModelConfig,
} from "../api";
import {
  modelSubtitle, keyHint, saveError, deleteDisabled, deleteTooltip, sortModels,
  type ModelDraft,
} from "../modelView";

const EMPTY: ModelDraft = { name: "", model: "", api_key: "", base_url: "" };

export default function ModelPanel() {
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [draft, setDraft] = useState<ModelDraft>(EMPTY);
  const [editing, setEditing] = useState<string | null>(null);  // null = 新增模式
  const [err, setErr] = useState<string | null>(null);
  const [pendingDel, setPendingDel] = useState<string | null>(null);
  const [plainKey, setPlainKey] = useState<Record<string, string>>({});

  function reload() { getModels().then((rows) => setModels(sortModels(rows))); }
  useEffect(() => { reload(); }, []);

  async function activate(name: string) {
    try {
      await activateModel(name);
      reload();
    } catch (e) { setErr(String(e)); reload(); }
  }

  function startEdit(m: ModelConfig) {
    // key 框留空: 显示的是掩码, 空提交 = 不动原 key (后端同语义)
    setEditing(m.name);
    setDraft({ name: m.name, model: m.model, api_key: "", base_url: m.base_url });
    setErr(null);
  }

  function cancelEdit() { setEditing(null); setDraft(EMPTY); setErr(null); }

  async function save() {
    const msg = saveError(draft, models, editing ?? undefined);
    if (msg) { setErr(msg); return; }
    try {
      if (editing) {
        await updateModel(editing, {
          model: draft.model.trim(),
          api_key: draft.api_key,           // 空 = 不动
          base_url: draft.base_url.trim(),
        });
      } else {
        await addModel({
          name: draft.name.trim(), model: draft.model.trim(),
          api_key: draft.api_key, base_url: draft.base_url.trim(),
        });
      }
    } catch (e) { setErr(String(e)); return; }
    cancelEdit();
    reload();
  }

  async function confirmRemove() {
    const n = pendingDel;
    if (!n) return;
    setPendingDel(null);
    try {
      await deleteModel(n);
      reload();
    } catch (e) { setErr(String(e)); reload(); }
  }

  async function peekKey(name: string) {
    if (plainKey[name]) {       // 再点一次收起
      setPlainKey((p) => { const q = { ...p }; delete q[name]; return q; });
      return;
    }
    try {
      const k = await getModelKey(name);
      setPlainKey((p) => ({ ...p, [name]: k || "(空)" }));
    } catch (e) { setErr(String(e)); }
  }

  return (
    <div className="cfg-pane">
      <div className="cfg-sec">模型 · {models.length} 个 (选中的那条立即生效)</div>

      {models.map((m) => (
        <div key={m.name} className="cfg-row">
          <div>
            <div className="cfg-nm">
              <label style={{ cursor: "pointer" }}>
                <input type="radio" name="active-model" checked={m.active}
                       onChange={() => activate(m.name)}
                       aria-label={`激活 ${m.name}`}
                       style={{ marginRight: 6 }} />
                {m.name}{m.active && " ✓"}
              </label>
            </div>
            <div className="cfg-ds">{modelSubtitle(m)}</div>
            <div className="cfg-ds cfg-keyline">
              <span>🔑</span>
              <code>{plainKey[m.name] ?? keyHint(m)}</code>
              <button className="cfg-btn-ghost cfg-eye"
                      title="查看明文 (仅 DEPLOY_PROFILE=local 可用)"
                      onClick={() => peekKey(m.name)}>
                {plainKey[m.name] ? "🙈" : "👁"}
              </button>
            </div>
          </div>
          <div className="cfg-actions">
            <button className="cfg-btn-ghost" onClick={() => startEdit(m)}>编辑</button>
            <button className="cfg-del" disabled={deleteDisabled(m)}
                    title={deleteTooltip(m)}
                    onClick={() => setPendingDel(m.name)}>删除</button>
          </div>
        </div>
      ))}

      <div className="cfg-sec">{editing ? `编辑 ${editing}` : "新增模型"}</div>
      <div className="cfg-form">
        <label>
          <span>名字</span>
          <input placeholder="mid-glm" value={draft.name} disabled={!!editing}
                 onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </label>
        <label>
          <span>模型</span>
          <input placeholder="anthropic/claude-sonnet-5-5 (provider 全小写)"
                 value={draft.model}
                 onChange={(e) => setDraft({ ...draft, model: e.target.value })} />
        </label>
        <label>
          <span>api_key</span>
          <input placeholder={editing ? "留空 = 不改" : "sk-..."}
                 type="password" value={draft.api_key}
                 onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} />
        </label>
        <label>
          <span>base_url</span>
          <input placeholder="留空 = 走 provider 官方端点" value={draft.base_url}
                 onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} />
        </label>
      </div>
      <div className="cfg-form-actions">
        <button onClick={save}>{editing ? "保存" : "+ 新增"}</button>
        {editing && <button className="cfg-btn-ghost" onClick={cancelEdit}>取消</button>}
        <span className="cfg-ds">
          provider 前缀决定走哪套协议 (anthropic / openai / deepseek / azure ...),
          litellm 认 141 种, 必须全小写
        </span>
      </div>
      {err && <div className="cfg-err">{err}</div>}

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除模型配置</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel}</b>?此操作不可撤销。
            </div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-ghost" onClick={() => setPendingDel(null)}>取消</button>
              <button className="cfg-btn-danger" onClick={confirmRemove}>删除</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
