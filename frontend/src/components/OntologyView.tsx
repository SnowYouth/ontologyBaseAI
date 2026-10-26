import { useCallback, useState } from "react";
import DomainsPanel from "./DomainsPanel";
import EntitiesPanel from "./EntitiesPanel";
import RelationsPanel from "./RelationsPanel";
import EnumsPanel from "./EnumsPanel";
import CqPanel from "./CqPanel";
import FilterRulesPanel from "./FilterRulesPanel";
import ConceptsPanel from "./ConceptsPanel";
import ProposalsPanel from "./ProposalsPanel";
import GraphPanel from "./GraphPanel";

type Tab = "domain" | "entity" | "relation" | "enum" | "concept" | "cq" | "filterrule" | "proposal" | "graph";

export default function OntologyView() {
  const [tab, setTab] = useState<Tab>("domain");
  const [relationFilter, setRelationFilter] = useState<string>("");

  const jumpToRelations = useCallback((entityId: string) => {
    setRelationFilter(entityId);
    setTab("relation");
  }, []);
  // useCallback 稳定引用: RelationsPanel 预筛 useEffect 可安全放进依赖数组,
  // 不会因父重渲染拿到新闭包而误触 (修 review Minor 闭包陷阱)。
  const consumeRelationFilter = useCallback(() => setRelationFilter(""), []);

  return (
    <div className="onto-view">
      <div className="onto-tabs">
        {([["domain", "域"], ["entity", "实体"], ["relation", "关系"], ["enum", "枚举"], ["filterrule", "过滤规则"], ["concept", "概念"], ["cq", "验证"], ["proposal", "提议"], ["graph", "网络图"]] as [Tab, string][])
          .map(([k, label]) => (
            <div key={k} className={`onto-tab ${tab === k ? "on" : ""}`}
                 onClick={() => setTab(k)}>{label}</div>
          ))}
      </div>
      <div className={`onto-tab-body ${tab === "graph" ? "graph" : ""}`}>
        {tab === "domain" && <DomainsPanel />}
        {tab === "entity" && <EntitiesPanel onJumpToRelations={jumpToRelations} />}
        {tab === "relation" && (
          <RelationsPanel filterEntity={relationFilter}
                          onFilterConsumed={consumeRelationFilter} />
        )}
        {tab === "enum" && <EnumsPanel />}
        {tab === "filterrule" && <FilterRulesPanel />}
        {tab === "concept" && <ConceptsPanel />}
        {tab === "cq" && <CqPanel />}
        {tab === "proposal" && <ProposalsPanel />}
        {tab === "graph" && <GraphPanel onJumpToRelations={jumpToRelations} />}
      </div>
    </div>
  );
}
