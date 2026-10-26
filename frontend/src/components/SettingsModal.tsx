import { useState } from "react";
import ToolsPanel from "./ToolsPanel";
import McpPanel from "./McpPanel";
import SkillsPanel from "./SkillsPanel";
import ModelPanel from "./ModelPanel";

type Tab = "model" | "tools" | "mcp" | "skills";

export default function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("model");
  if (!open) return null;
  return (
    <div className="cfg-backdrop" onClick={onClose}>
      <div className="cfg-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cfg-head">
          <b>⚙ 设置</b>
          <span className="cfg-x" onClick={onClose}>✕</span>
        </div>
        <div className="cfg-body">
          <div className="cfg-nav">
            <div className={`cfg-navitem ${tab === "model" ? "on" : ""}`}
                 onClick={() => setTab("model")}>🧠 Model</div>
            <div className={`cfg-navitem ${tab === "tools" ? "on" : ""}`}
                 onClick={() => setTab("tools")}>🔧 本地 Tool</div>
            <div className={`cfg-navitem ${tab === "mcp" ? "on" : ""}`}
                 onClick={() => setTab("mcp")}>🔌 MCP</div>
            <div className={`cfg-navitem ${tab === "skills" ? "on" : ""}`}
                 onClick={() => setTab("skills")}>📦 Skill</div>
          </div>
          {tab === "model" ? <ModelPanel />
            : tab === "tools" ? <ToolsPanel />
            : tab === "mcp" ? <McpPanel />
            : <SkillsPanel />}
        </div>
      </div>
    </div>
  );
}
