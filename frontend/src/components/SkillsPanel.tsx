import { useEffect, useState } from "react";
import {
  getSkills, getSkillBody, getSkillFull, setSkillEnabled,
  createSkill, updateSkillBody, deleteSkill, type SkillStatus,
} from "../api";

const NEW_TEMPLATE = `---
name: my-skill
description: 一句话说明这个技能是干嘛的
---

在这里写技能正文 (agent 调 load_skill 时看到的内容)。`;

// 镜像后端 core/toolconfig/skills.py 的 _NAME_RE, 前端先拦一道 (后端仍是权威防线)。
const NAME_RE = /^[a-z0-9][a-z0-9_-]*$/;

// 返回 name 的校验错误文案 (null = 合法)。空串视为"还没填"不报错, 由保存按钮 disabled 兜。
function nameError(name: string): string | null {
  if (name === "") return null;
  if (!NAME_RE.test(name)) return "只能用小写字母/数字打头,后续含 - 或 _(不能有空格/大写/中文)";
  return null;
}

export default function SkillsPanel() {
  const [skills, setSkills] = useState<SkillStatus[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [body, setBody] = useState<string>("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newContent, setNewContent] = useState(NEW_TEMPLATE);
  const [pendingDel, setPendingDel] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  function reload() { getSkills().then(setSkills); }
  useEffect(() => { reload(); }, []);

  async function toggle(name: string, enabled: boolean) {
    try {
      await setSkillEnabled(name, enabled);
      setSkills((ss) => ss.map((s) => (s.name === name ? { ...s, enabled } : s)));
    } catch (e) { setErrMsg((e as Error).message); reload(); }
  }

  async function view(name: string) {
    if (open === name) { setOpen(null); setEditing(false); return; }
    try {
      const text = await getSkillBody(name);
      setBody(text); setOpen(name); setEditing(false);
    } catch (e) { setErrMsg((e as Error).message); }
  }

  async function startEdit(name: string) {
    try {
      // 编辑必须基于完整 SKILL.md (含 frontmatter), 否则回写挂 update_body 校验。
      // 查看用的 body 是剥了 frontmatter 的纯正文, 不能直接编辑回写。
      const full = await getSkillFull(name);
      setDraft(full); setEditing(true);
    } catch (e) { setErrMsg((e as Error).message); }
  }

  async function saveEdit(name: string) {
    try {
      await updateSkillBody(name, draft);
      // draft 是含 frontmatter 全文; 预览 body 要纯正文, 重新拉一次 (别直接 setBody(draft))
      const text = await getSkillBody(name);
      setBody(text); setEditing(false); reload();
    } catch (e) { setErrMsg((e as Error).message); }
  }

  async function submitNew() {
    const name = newName.trim();
    // 前端已实时拦非法/空 name (保存按钮 disabled), 这里再兜一道防手滑。
    if (!name || nameError(name)) return;
    try {
      await createSkill(name, newContent);
      setAdding(false); setNewName(""); setNewContent(NEW_TEMPLATE); reload();
    } catch (e) { setErrMsg((e as Error).message); }
  }

  async function confirmDelete() {
    const n = pendingDel;
    if (!n) return;
    setPendingDel(null);
    try {
      await deleteSkill(n);
      setSkills((ss) => ss.filter((s) => s.name !== n));
      if (open === n) { setOpen(null); setEditing(false); }
    } catch (e) { setErrMsg((e as Error).message); reload(); }
  }

  return (
    <div className="cfg-pane">
      <div className="cfg-sec">
        技能 · {skills.length} 个已发现
        <button className="cfg-view" style={{ marginLeft: 8 }}
                onClick={() => setAdding((a) => !a)}>
          {adding ? "取消" : "➕ 新建"}
        </button>
      </div>

      {adding && (() => {
        const nameErr = nameError(newName.trim());
        const canSave = newName.trim() !== "" && !nameErr;
        return (
          <div className="cfg-add" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <input placeholder="skill name (小写字母/数字/-/_)"
                   value={newName} onChange={(e) => setNewName(e.target.value)}
                   style={nameErr ? { borderColor: "#c96442" } : undefined} />
            {nameErr && (
              <div style={{ color: "#c96442", fontSize: 11, marginTop: -2 }}>{nameErr}</div>
            )}
            <textarea rows={8} value={newContent}
                      style={{ fontFamily: "monospace", fontSize: 12 }}
                      onChange={(e) => setNewContent(e.target.value)} />
            <button onClick={submitNew} disabled={!canSave}
                    style={!canSave ? { opacity: 0.5, cursor: "not-allowed" } : undefined}>
              保存新建
            </button>
          </div>
        );
      })()}

      {skills.map((s) => (
        <div key={s.name}>
          <div className="cfg-row">
            <span className="cfg-nm">{s.name}</span>
            <span style={{ flex: 1, color: "#8a827c", fontSize: 12, margin: "0 8px" }}>
              {s.description}
            </span>
            <button className="cfg-view" onClick={() => view(s.name)}>
              {open === s.name ? "收起" : "查看"}
            </button>
            <button className="cfg-del" onClick={() => setPendingDel(s.name)}>删除</button>
            <label className="cfg-switch">
              <input type="checkbox" checked={s.enabled}
                     onChange={(e) => toggle(s.name, e.target.checked)} />
              <span className="cfg-slider" />
            </label>
          </div>
          {open === s.name && (
            <div style={{ margin: "0 0 8px" }}>
              {editing ? (
                <>
                  <div style={{ color: "#8a827c", fontSize: 11, marginBottom: 4 }}>
                    编辑完整 SKILL.md (含 frontmatter)。可改 description;改 name 不会重命名目录。
                  </div>
                  <textarea rows={12} value={draft}
                            style={{ width: "100%", fontFamily: "monospace", fontSize: 12,
                                     background: "#211e1b", color: "#d8d2cc", padding: 12,
                                     borderRadius: 6, boxSizing: "border-box" }}
                            onChange={(e) => setDraft(e.target.value)} />
                  <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
                    <button className="cfg-btn-ghost" onClick={() => setEditing(false)}>取消</button>
                    <button className="cfg-view" onClick={() => saveEdit(s.name)}>保存</button>
                  </div>
                </>
              ) : (
                <>
                  <pre style={{ background: "#211e1b", color: "#d8d2cc", padding: 12,
                               borderRadius: 6, whiteSpace: "pre-wrap", fontSize: 12, margin: 0 }}>
                    {body}
                  </pre>
                  <button className="cfg-view" style={{ marginTop: 4 }}
                          onClick={() => startEdit(s.name)}>编辑</button>
                </>
              )}
            </div>
          )}
        </div>
      ))}

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除技能</div>
            <div className="cfg-confirm-msg">
              确定删除 <b>{pendingDel}</b>?此操作不可撤销 (整个目录会被移除)。
            </div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-ghost" onClick={() => setPendingDel(null)}>取消</button>
              <button className="cfg-btn-danger" onClick={confirmDelete}>删除</button>
            </div>
          </div>
        </div>
      )}

      {errMsg && (
        <div className="cfg-confirm-backdrop" onClick={() => setErrMsg(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">操作失败</div>
            <div className="cfg-confirm-msg">{errMsg}</div>
            <div className="cfg-confirm-actions">
              <button className="cfg-btn-ghost" onClick={() => setErrMsg(null)}>知道了</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
