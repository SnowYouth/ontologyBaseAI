import { useState, useRef, useEffect } from "react";
import {
  postAgentStream, listSessions, createSession, getSession, deleteSession,
  type StepEvent, type SessionMeta, type Step,
} from "./api";
import { applyStepEvent } from "./agentStream";
import SettingsModal from "./components/SettingsModal";
import OntologyView from "./components/OntologyView";
import "./App.css";

type Msg = { role: "user" | "agent" | "error"; text: string; steps?: Step[] };
const LS_KEY = "currentSessionId";

export default function App() {
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [currentId, setCurrentId] = useState<string>("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [view, setView] = useState<"chat" | "ontology">("chat");

  useEffect(() => {
    const el = messagesRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, loading]);

  // 挂载: 拉会话列表, 选中上次的 (或第一个), 空则新建
  useEffect(() => {
    (async () => {
      const list = await listSessions();
      if (list.length === 0) {
        const s = await createSession();
        setSessions([{ id: s.id, title: s.title, updated_at: s.updated_at }]);
        selectSession(s.id);
      } else {
        setSessions(list);
        const saved = localStorage.getItem(LS_KEY);
        const pick = list.find((s) => s.id === saved) ?? list[0];
        selectSession(pick.id);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function selectSession(sid: string) {
    // 切走: abort 上一条流 (防迟到事件串台)
    abortRef.current?.abort();
    setCurrentId(sid);
    localStorage.setItem(LS_KEY, sid);
    const full = await getSession(sid);
    setMessages(full.messages.map((m) => ({
      role: m.role === "user" ? "user" : "agent",
      text: m.content,
      steps: m.steps,
    })));
  }

  async function newSession() {
    const s = await createSession();
    const list = await listSessions();
    setSessions(list);
    selectSession(s.id);
  }

  async function removeSession(sid: string, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await deleteSession(sid);
    } catch (err) {
      alert(String(err));  // 409 会话正在生成中
      return;
    }
    const list = await listSessions();
    setSessions(list);
    if (sid === currentId) {
      if (list.length > 0) selectSession(list[0].id);
      else newSession();
    }
  }

  function patchLast(fn: (m: Msg) => Msg) {
    setMessages((ms) => {
      const copy = ms.slice();
      copy[copy.length - 1] = fn(copy[copy.length - 1]);
      return copy;
    });
  }

  function onEvent(ev: StepEvent) {
    patchLast((m) => ({ ...m, ...applyStepEvent({ text: m.text, steps: m.steps ?? [] }, ev) }));
  }

  async function send() {
    const text = input.trim();
    if (!text || loading || !currentId) return;
    const sid = currentId;
    setMessages((m) => [...m, { role: "user", text }, { role: "agent", text: "", steps: [] }]);
    setInput("");
    setLoading(true);
    const ac = new AbortController();
    abortRef.current = ac;
    await postAgentStream(text, sid, {
      onEvent: (ev) => { if (sid === currentId) onEvent(ev); },  // guard: 只 patch 当前会话
      onError: (msg) => { if (sid === currentId) patchLast(() => ({ role: "error", text: msg })); },
      onDone: () => {},
    }, ac.signal);
    setLoading(false);
    // 刷新列表 (标题/排序可能变)
    listSessions().then(setSessions);
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <button className="newbtn" onClick={newSession}>+ 新建会话</button>
        <button className="cfgbtn" onClick={() => setSettingsOpen(true)}>⚙ 设置</button>
        <div className="viewswitch">
          <button className={view === "chat" ? "on" : ""} onClick={() => setView("chat")}>对话</button>
          <button className={view === "ontology" ? "on" : ""} onClick={() => setView("ontology")}>本体</button>
        </div>
        <div className="sesslist">
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`sessitem ${s.id === currentId ? "active" : ""}`}
              onClick={() => selectSession(s.id)}
            >
              <span className="sesstitle">{s.title || "新会话"}</span>
              <span className="delbtn" onClick={(e) => removeSession(s.id, e)}>🗑</span>
            </div>
          ))}
        </div>
      </aside>
      {view === "chat" && (
        <main className="main">
          <div className="messages" ref={messagesRef}>
            {messages.map((m, i) => (
              <div key={i} className="msgrow">
                {m.role === "agent" && m.steps && m.steps.length > 0 && (
                  <div className="trace">
                    <div className="hd">过程</div>
                    {m.steps.map((s, j) => (
                      <div key={j} className={`step ${s.ok === false ? "bad" : ""}`}>
                        <span className="ico">🔧</span>
                        <span className="nm">{s.name}</span>
                        <span className="rs">{s.result ? `${s.result}${s.ms != null ? ` · ${s.ms}ms` : ""}` : "…"}</span>
                      </div>
                    ))}
                  </div>
                )}
                {(m.role !== "agent" || m.text) && (
                  <div className={`bubble ${m.role}`}>{m.text}</div>
                )}
              </div>
            ))}
            {loading && <div className="bubble agent thinking">思考中…</div>}
          </div>
          <div className="composer">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder="和 agent 说点什么…"
              disabled={loading}
            />
            <button onClick={send} disabled={loading}>发送</button>
          </div>
        </main>
      )}
      {view === "ontology" && (
        <main className="main onto-main"><OntologyView /></main>
      )}
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
