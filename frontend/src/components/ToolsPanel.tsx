import { useEffect, useState } from "react";
import { getToolConfig, setToolEnabled, type ToolStatus } from "../api";

export default function ToolsPanel() {
  const [tools, setTools] = useState<ToolStatus[]>([]);

  useEffect(() => { getToolConfig().then(setTools); }, []);

  async function toggle(name: string, enabled: boolean) {
    await setToolEnabled(name, enabled);
    setTools((ts) => ts.map((t) => (t.name === name ? { ...t, enabled } : t)));
  }

  return (
    <div className="cfg-pane">
      <div className="cfg-sec">本地工具 · {tools.length} 个已注册</div>
      {tools.map((t) => (
        <div key={t.name} className="cfg-row">
          <span className="cfg-nm">{t.name}</span>
          <label className="cfg-switch">
            <input type="checkbox" checked={t.enabled}
                   onChange={(e) => toggle(t.name, e.target.checked)} />
            <span className="cfg-slider" />
          </label>
        </div>
      ))}
    </div>
  );
}
