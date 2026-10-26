// frontend/src/components/CqPanel.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getCqSet, getCqResults, putCqVerdict, runCq, postCqCase, putCqCase, delCqCase,
  type CqCase, type CqResult, type CqResultSet, type CqVerdict, type CqCriteria,
} from "../api";
import {
  emptyDraft, draftFromCase, draftToCase, draftError, runButtonLabel, baselineHint,
  truncatedHint, noDbEvidenceHint, CQ_KINDS, type CqDraft,
} from "../cqView";

const VERDICT_LABEL: Record<string, string> = {
  pass: "✅通过", partial: "⚠️部分", fail: "❌失败", "": "— 未判",
};

const CRITERIA_KEYS: Array<keyof CqCriteria> = ["点名", "走边", "值集", "断腿"];
const CRITERIA_PLACEHOLDER: Record<string, string> = {
  点名: "该点到哪些实体, 如 reader, loan, book",
  走边: "该走哪条边, 如 reader→loan (reader_id) → copy (copy_id)",
  值集: "码值该取哪个, 如 loan_status='0' 借出中",
  断腿: "该响亮失败的地方; 没有就写 无",
};

function resultsMap(set: CqResultSet): Record<string, CqResult> {
  const m: Record<string, CqResult> = {};
  if (set) for (const r of set.results) m[r.id] = r;
  return m;
}

export default function CqPanel() {
  const [cases, setCases] = useState<CqCase[]>([]);
  const [cur, setCur] = useState<Record<string, CqResult>>({});
  const [prev, setPrev] = useState<Record<string, CqResult>>({});
  const [ranAt, setRanAt] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [runningId, setRunningId] = useState<string>("");
  const [err, setErr] = useState<string>("");
  // 表单: draft 非 null = 展开中; editing 非 null = 改已有那条 (id 只读)
  const [draft, setDraft] = useState<CqDraft | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [formErr, setFormErr] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [addedIds, setAddedIds] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const loadResults = useCallback(async () => {
    const { current, previous } = await getCqResults();
    setCur(resultsMap(current));
    setPrev(resultsMap(previous));
    setRanAt(current?.ran_at ?? "");
  }, []);

  useEffect(() => {
    getCqSet().then(setCases).catch((e) => setErr(String(e.message ?? e)));
    loadResults();
    // 卸载时中断在跑的 CQ, 避免 onCaseDone 等回调 setState 于已卸载组件
    return () => { abortRef.current?.abort(); };
  }, [loadResults]);

  const onRun = useCallback(() => {
    setRunning(true); setErr("");
    const ac = new AbortController();
    abortRef.current = ac;
    runCq({
      onCaseStart: (id) => setRunningId(id),
      onCaseDone: (r) => setCur((m) => ({ ...m, [r.id]: r })),
      onDone: () => {
        setRunning(false); setRunningId("");
        setAddedIds([]);        // 跑完这轮, 新增的已进 baseline, 提示可以收了
        loadResults();
      },
      onError: (msg) => { setErr(msg); setRunning(false); setRunningId(""); },
    }, ac.signal);
  }, [loadResults]);

  const onVerdict = useCallback(async (id: string, verdict: Exclude<CqVerdict, null>, note: string) => {
    try {
      await putCqVerdict(id, verdict, note);
      setCur((m) => ({ ...m, [id]: { ...m[id], verdict, note } }));
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }, []);

  function openNew() {
    setDraft(emptyDraft()); setEditing(null); setFormErr("");
  }
  function openEdit(c: CqCase) {
    setDraft(draftFromCase(c)); setEditing(c.id); setFormErr("");
  }
  function closeForm() {
    setDraft(null); setEditing(null); setFormErr("");
  }

  async function save() {
    if (!draft) return;
    const msg = draftError(draft, cases, editing ?? undefined);
    if (msg) { setFormErr(msg); return; }
    const body = draftToCase(draft);
    setSaving(true); setFormErr("");
    try {
      if (editing) {
        await putCqCase(editing, body);
        setCases((rows) => rows.map((c) => (c.id === editing ? body : c)));
      } else {
        await postCqCase(body);
        setCases((rows) => [...rows, body]);
        setAddedIds((ids) => [...ids, body.id]);
        // 新建成功 = 考题集至少有一条了, 之前"读不出"的红字已过期
        setErr("");
      }
      closeForm();
    } catch (e) {
      setFormErr(String((e as Error).message ?? e));
    } finally { setSaving(false); }
  }

  async function remove(c: CqCase) {
    // 删考题不可逆 (cq_set.json 直接重写), 二次确认
    if (!window.confirm(`删除考题 ${c.id}?\n\n${c.question}\n\n此操作不可撤销。`)) return;
    try {
      await delCqCase(c.id);
      setCases((rows) => rows.filter((r) => r.id !== c.id));
      setAddedIds((ids) => ids.filter((i) => i !== c.id));
      if (editing === c.id) closeForm();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  function patch(p: Partial<CqDraft>) { setDraft((d) => (d ? { ...d, ...p } : d)); }
  function patchCriteria(k: keyof CqCriteria, v: string) {
    setDraft((d) => (d ? { ...d, criteria: { ...d.criteria, [k]: v } } : d));
  }
  function patchTurn(i: number, v: string) {
    setDraft((d) => (d ? { ...d, turns: d.turns.map((t, j) => (j === i ? v : t)) } : d));
  }

  const hint = baselineHint(addedIds);

  return (
    <div className="cq-panel">
      <div className="cq-bar">
        <button disabled={running} onClick={onRun}>
          {runButtonLabel(cases.length, running, runningId)}
        </button>
        {/* ★ 刻意不受 err / running 影响: 考题集读不出时(面板坏掉的那个态)
            新建是唯一能让它变好的途径, 一起 disable 就成了死局。 */}
        <button className="cq-btn-new" onClick={openNew} aria-label="新建考题">
          ➕ 新建考题
        </button>
        {ranAt && <span className="cq-ranat">上次跑: {ranAt}</span>}
        {err && <span className="cq-err">{err}</span>}
      </div>

      {hint && <div className="cq-hint">💡 {hint}</div>}

      {draft && (
        <div className="cq-form">
          <div className="cq-form-title">{editing ? `编辑考题 ${editing}` : "新建考题"}</div>
          <label className="cq-field">
            <span>编号</span>
            {/* 编辑时 id 只读: 改 id 等于删旧建新, 语义混乱 (后端也会 404) */}
            <input id="cq-f-id" value={draft.id} readOnly={!!editing}
                   placeholder="CQ-5"
                   onChange={(e) => patch({ id: e.target.value })} />
          </label>
          <label className="cq-field">
            <span>问题</span>
            <input id="cq-f-q" value={draft.question}
                   placeholder="真拿去问 agent 的那句话"
                   onChange={(e) => patch({ question: e.target.value })} />
          </label>
          <label className="cq-field">
            <span>类型</span>
            <select id="cq-f-kind" value={draft.kind}
                    onChange={(e) => patch({
                      kind: e.target.value,
                      // 切到多轮时给两个空行当脚手架 (后端要求 ≥2 轮)
                      turns: e.target.value === "multi_turn" && draft.turns.length < 2
                        ? ["", ""] : draft.turns,
                    })}>
              {CQ_KINDS.map((k) => (
                <option key={k} value={k}>{k === "single" ? "单轮" : "多轮"}</option>
              ))}
            </select>
          </label>

          {draft.kind === "multi_turn" && (
            <div className="cq-turns">
              <div className="cq-turns-label">
                轮次 <span className="cq-turns-note">(至少 2 轮; 后一轮考的是 agent 记不记得前一轮)</span>
              </div>
              {draft.turns.map((t, i) => (
                <div key={i} className="cq-turn-row">
                  <input value={t} placeholder={`第 ${i + 1} 轮`}
                         aria-label={`第 ${i + 1} 轮提问`}
                         onChange={(e) => patchTurn(i, e.target.value)} />
                  <button className="cq-btn-ghost" aria-label={`删除第 ${i + 1} 轮`}
                          onClick={() => patch({ turns: draft.turns.filter((_, j) => j !== i) })}>
                    ✕ 删除
                  </button>
                </div>
              ))}
              <button className="cq-btn-ghost"
                      onClick={() => patch({ turns: [...draft.turns, ""] })}>
                + 加一轮
              </button>
            </div>
          )}

          <div className="cq-form-sec">四类判据 (人审打分时的对照标准, 可留空)</div>
          {CRITERIA_KEYS.map((k) => (
            <label className="cq-field" key={k}>
              <span>{k}</span>
              <input value={draft.criteria[k] ?? ""} placeholder={CRITERIA_PLACEHOLDER[k]}
                     onChange={(e) => patchCriteria(k, e.target.value)} />
            </label>
          ))}

          {formErr && <div className="cq-form-err">{formErr}</div>}
          <div className="cq-form-actions">
            <button disabled={saving} onClick={save}>
              {saving ? "保存中…" : editing ? "保存" : "新建"}
            </button>
            <button className="cq-btn-ghost" disabled={saving} onClick={closeForm}>取消</button>
          </div>
        </div>
      )}

      {cases.length === 0 && !err && !draft && (
        <div className="cq-empty">
          还没有考题。考题集是回归门的"考卷" —— 点 ➕ 新建考题 加第一条。
        </div>
      )}

      {cases.map((cq) => {
        const r = cur[cq.id];
        const p = prev[cq.id];
        // 只在两轮都复评过且判定不同时才高亮 (避免重跑后 verdict 被重置为 null
        // 致上轮打过分的条目全亮"上轮X→本轮未判"的噪音)
        const changed = !!(p && r && p.verdict && r.verdict && p.verdict !== r.verdict);
        return (
          <div key={cq.id} className="cq-row">
            <div className="cq-head">
              <b>{cq.id}</b> {cq.question}
              {cq.kind === "multi_turn" && (
                <span className="cq-kind">多轮 {cq.turns?.length ?? 0}</span>
              )}
              <select
                className="cq-verdict"
                aria-label={`${cq.id} 判定`}
                value={r?.verdict ?? ""}
                onChange={(e) => onVerdict(cq.id, e.target.value as Exclude<CqVerdict, null>, r?.note ?? "")}
              >
                <option value="">— 未判</option>
                <option value="pass">✅通过</option>
                <option value="partial">⚠️部分</option>
                <option value="fail">❌失败</option>
              </select>
              <button className="cq-btn-ghost" aria-label={`编辑考题 ${cq.id}`}
                      onClick={() => openEdit(cq)}>✏️ 编辑</button>
              <button className="cq-btn-del" aria-label={`删除考题 ${cq.id}`}
                      onClick={() => remove(cq)}>🗑 删除</button>
            </div>
            <div className="cq-criteria">
              点名: {cq.criteria.点名} / 走边: {cq.criteria.走边} / 值集: {cq.criteria.值集} / 断腿: {cq.criteria.断腿}
            </div>
            {r?.error && <div className="cq-error">❌ {r.error}</div>}
            {/* 截断提示必须在 answer 之外单独渲染: 空答案时下面那行条件为假,
                整张卡片会什么都不显示 (CQ-14/CQ-18 的真实形态)。 */}
            {truncatedHint(r?.truncated ?? false, r?.answer ?? "") && (
              <div className="cq-truncated" role="status">
                {truncatedHint(r?.truncated ?? false, r?.answer ?? "")}
              </div>
            )}
            {noDbEvidenceHint(r?.no_db_evidence) && (
              <div className="cq-truncated" role="status">
                {noDbEvidenceHint(r?.no_db_evidence)}
              </div>
            )}
            {r?.answer && <details><summary>本轮答案</summary><pre>{r.answer}</pre></details>}
            {r?.tool_events?.length > 0 && (
              <details><summary>工具轨迹 ({r.tool_events.length})</summary>
                <pre>{r.tool_events.map((t) => `${t.name}(${t.args}) → ${t.result ?? "?"} [${t.ok ? "ok" : "fail"}]`).join("\n")}</pre>
              </details>
            )}
            {changed && <div className="cq-changed">上轮 {VERDICT_LABEL[p!.verdict ?? ""]} → 本轮 {VERDICT_LABEL[r!.verdict ?? ""]}</div>}
            <input
              className="cq-note" placeholder="备注 (哪不对)…"
              aria-label={`${cq.id} 备注`}
              defaultValue={r?.note ?? ""}
              onBlur={(e) => {
                if (!r) return;
                if (r.verdict == null) { setErr("请先给该条打分再写备注"); return; }
                onVerdict(cq.id, r.verdict, e.target.value);
              }}
            />
          </div>
        );
      })}
    </div>
  );
}
