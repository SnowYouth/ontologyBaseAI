import { useEffect, useState } from "react";
import {
  getMcpServers, setMcpEnabled, addMcpServer, deleteMcpServer, type McpServer,
} from "../api";
import { isGatedOff, statusText } from "../mcpView";

export default function McpPanel() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [name, setName] = useState("");
  const [command, setCommand] = useState("uv");
  const [argsText, setArgsText] = useState("");
  const [category, setCategory] = useState<"local" | "serving">("local");
  const [pendingDel, setPendingDel] = useState<string | null>(null);

  function reload() { getMcpServers().then(setServers); }
  useEffect(() => { reload(); }, []);

  async function toggle(n: string, enabled: boolean) {
    try {
      await setMcpEnabled(n, enabled);     // 不传 category = 不动它
      setServers((ss) => ss.map((s) => (s.name === n ? { ...s, enabled } : s)));
    } catch (e) { alert(String(e)); reload(); }
  }

  async function changeCategory(s: McpServer, category: "local" | "serving") {
    try {
      await setMcpEnabled(s.name, s.enabled, category);
      reload();      // active 由后端算 (取决于当前 profile), 不本地推断
    } catch (e) { alert(String(e)); reload(); }
  }

  async function confirmRemove() {
    const n = pendingDel;
    if (!n) return;
    setPendingDel(null);
    try {
      await deleteMcpServer(n);
      setServers((ss) => ss.filter((s) => s.name !== n));
    } catch (e) { alert(String(e)); reload(); }
  }

  async function add() {
    if (!name.trim()) return;
    try {
      await addMcpServer({
        name: name.trim(), command: command.trim(),
        args: argsText.split(/\s+/).filter(Boolean), enabled: true, category,
      });
    } catch (e) { alert(String(e)); return; }
    setName(""); setArgsText(""); setCategory("local"); reload();
  }

  return (
    <div className="cfg-pane">
      <div className="cfg-sec">MCP 服务器 · {servers.length} 个</div>
      {servers.map((s) => (
        <div key={s.name} className="cfg-row">
          <div>
            <div className="cfg-nm">{s.name}</div>
            <div className="cfg-ds">{s.command} {s.args.join(" ")}</div>
            {/* 开关是"配置启用", active 是"本轮真加载" —— serving 部署下
                local server 两者会不一致, 不说明白就是在误导人。 */}
            {isGatedOff(s) && (
              <div className="cfg-warn" title="当前 DEPLOY_PROFILE=serving, 只加载 category=serving 的 server">
                {statusText(s)}
              </div>
            )}
          </div>
          <div className="cfg-actions">
            <select className="cfg-sel" value={s.category} aria-label={`${s.name} 可见性层级`}
                    onChange={(e) => changeCategory(s, e.target.value as "local" | "serving")}>
              <option value="local">local</option>
              <option value="serving">serving</option>
            </select>
            <label className="cfg-switch">
              <input type="checkbox" checked={s.enabled} aria-label={`${s.name} 启用`}
                     onChange={(e) => toggle(s.name, e.target.checked)} />
              <span className="cfg-slider" />
            </label>
            <button className="cfg-del" onClick={() => setPendingDel(s.name)}>删除</button>
          </div>
        </div>
      ))}
      <div className="cfg-add">
        <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} />
        <input placeholder="command" value={command} onChange={(e) => setCommand(e.target.value)} />
        <input placeholder="args (空格分隔)" value={argsText} onChange={(e) => setArgsText(e.target.value)} />
        <select className="cfg-sel" value={category} aria-label="新增 server 的可见性层级"
                onChange={(e) => setCategory(e.target.value as "local" | "serving")}>
          <option value="local">local</option>
          <option value="serving">serving</option>
        </select>
        <button onClick={add}>+ 新增</button>
      </div>

      {pendingDel && (
        <div className="cfg-confirm-backdrop" onClick={() => setPendingDel(null)}>
          <div className="cfg-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="cfg-confirm-title">删除 MCP 服务器</div>
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
