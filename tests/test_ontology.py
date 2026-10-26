"""core/ontology.py 单元测试 — 手写迷你本体 dict, 不依赖真实 kg_private/ontology.json。"""
import pytest


def _mini_ontology() -> dict:
    """4 实体 / 3 边 / 1 valueSet 的迷你本体, 覆盖:
    普通边(a-b) / crossDomain 边(带 note, b-shadow) / enum 属性。
    """
    return {
        "_meta": {"name": "测试本体"},
        "domains": [{"code": "域A", "color": "#111111"}],
        "entities": [
            {
                "id": "entity_a", "entityCode": "A", "displayName": "实体A",
                "domain": "域A", "maps_to": "户组X", "tables": ["tbl_a"],
                "attributes": [
                    {"name": "A_ID", "kind": "pk", "type": "bigint"},
                    {"name": "B_ID", "kind": "fk", "type": "bigint"},
                    {
                        "name": "REC_STATUS", "kind": "enum", "type": "varchar",
                        "hasEnum": True,
                    },
                    {
                        "name": "LEGACY_CD", "kind": "enum", "type": "varchar",
                    },
                ],
            },
            {
                "id": "entity_b", "entityCode": "B", "displayName": "实体B",
                "domain": "域A", "maps_to": "户组X", "tables": ["tbl_b"],
                "attributes": [{"name": "B_ID", "kind": "pk", "type": "bigint"}],
            },
            {
                "id": "entity_c", "entityCode": "C", "displayName": "实体C",
                "domain": "域A", "maps_to": "户组Y", "tables": ["tbl_c"],
                "attributes": [{"name": "C_ID", "kind": "pk", "type": "bigint"}],
            },
            {
                "id": "entity_bridge", "entityCode": "BR", "displayName": "桥实体",
                "domain": "域A", "maps_to": "户组X↔Y", "tables": ["tbl_bridge"],
                "attributes": [{"name": "BR_ID", "kind": "pk", "type": "bigint"}],
            },
        ],
        "relationships": [
            {
                "source": "entity_a", "target": "entity_b", "type": "MANY_TO_ONE",
                "via": "entity_a.B_ID", "label": "A属于B",
            },
            {
                "source": "entity_a", "target": "entity_bridge", "type": "ONE_TO_MANY",
                "via": "entity_bridge.A_ID", "label": "A经桥到C",
            },
            {
                "source": "entity_bridge", "target": "entity_c", "type": "MANY_TO_ONE",
                "via": "entity_bridge.C_ID", "label": "桥到C",
                "crossDomain": True, "note": "跨域断腿: entity_bridge 仅有 C_ID, 够不到 C 详情",
            },
        ],
        "valueSets": {
            ("entity_a", "REC_STATUS"): {
                "name": "测试状态", "spec_ref": "VS-1",
                "codes": [
                    {"code": "1", "name": "在用", "desc": "正常"},
                    {"code": "2", "name": "停用", "desc": None},
                ],
            }
        },
    }


class TestBuildIndexes:
    def test_主索引_by_id_覆盖全部实体(self):
        from core.ontology_view import build_indexes
        onto = build_indexes(_mini_ontology())
        assert set(onto.entities.keys()) == {"entity_a", "entity_b", "entity_c", "entity_bridge"}
        assert onto.entities["entity_a"]["displayName"] == "实体A"

    def test_valueSets_直接引用原始dict(self):
        from core.ontology_view import build_indexes
        onto = build_indexes(_mini_ontology())
        assert onto.value_sets[("entity_a", "REC_STATUS")]["name"] == "测试状态"
        assert len(onto.value_sets[("entity_a", "REC_STATUS")]["codes"]) == 2

    def test_邻接表_双向都记(self):
        from core.ontology_view import build_indexes
        onto = build_indexes(_mini_ontology())
        # entity_a 是 source: 应在 entity_a 的邻接表里
        a_edges = onto.adjacency["entity_a"]
        assert len(a_edges) == 2  # a->b, a->bridge
        # entity_b 是 target: 也应在 entity_b 的邻接表里查到同一条边
        b_edges = onto.adjacency["entity_b"]
        assert len(b_edges) == 1
        assert b_edges[0]["source"] == "entity_a"
        # entity_c 只作为 target 出现在第三条边里
        c_edges = onto.adjacency["entity_c"]
        assert len(c_edges) == 1
        assert c_edges[0]["crossDomain"] is True

    def test_domains_原样保留(self):
        from core.ontology_view import build_indexes
        onto = build_indexes(_mini_ontology())
        assert onto.domains == [{"code": "域A", "color": "#111111"}]


class TestBuildIndexesFilterRules:
    def test_filterRules按entity建索引(self):
        from core.ontology_view import build_indexes
        raw = _mini_ontology()
        raw["filterRules"] = [
            {"entity": "entity_a", "col": "REC_STATUS", "op": "=", "value": "001",
             "mode": "soft", "origin": "source_code"},
            {"entity": "entity_a", "col": "TYPE_CD", "op": "=", "value": "1000",
             "mode": "hard", "origin": "source_code"},
            {"entity": "entity_b", "col": "X", "op": "=", "value": "1",
             "mode": "soft", "origin": "manual"},
        ]
        onto = build_indexes(raw)
        assert len(onto.filter_rules["entity_a"]) == 2
        assert len(onto.filter_rules["entity_b"]) == 1
        assert onto.filter_rules["entity_a"][0]["col"] == "REC_STATUS"

    def test_无filterRules键时索引为空dict(self):
        """老本体 raw 无 filterRules 键 → build_indexes 不炸, 索引空 (向后兼容)。"""
        from core.ontology_view import build_indexes
        raw = _mini_ontology()   # 无 filterRules 键
        onto = build_indexes(raw)
        assert onto.filter_rules == {}


class TestBuildIndexesConcepts:
    def test_build_indexes加载concepts索引(self):
        from core.ontology_view import build_indexes
        raw = {
            "entities": [{"id": "lib_loan", "displayName": "借阅实例", "maps_to": "读者",
                          "tables": ["lib_loan"], "attributes": [], "summary": ""}],
            "relationships": [], "valueSets": {}, "domains": [],
            "concepts": [{
                "id": "active_member", "display_name": "主号", "anchor_entity": "lib_loan",
                "summary": "A 端主号", "origin": "manual", "valid": 1,
                "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                                "op": "=", "value": "1000", "seq": 0}],
            }],
        }
        onto = build_indexes(raw)
        assert "active_member" in onto.concepts
        assert onto.concepts["active_member"]["conditions"][0]["col"] == "REC_STATUS"

    def test_build_indexes无concepts键向后兼容(self):
        from core.ontology_view import build_indexes
        raw = {"entities": [], "relationships": [], "valueSets": {}, "domains": []}
        onto = build_indexes(raw)     # 老本体无 concepts 键, 不炸
        assert onto.concepts == {}


class TestBuildCards:
    def test_单实体命中_卡片基本字段(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        result = build_cards(onto, ["entity_b"])
        assert len(result["cards"]) == 1
        card = result["cards"][0]
        assert card["id"] == "entity_b"
        assert card["displayName"] == "实体B"
        assert card["maps_to"] == "户组X"
        assert card["tables"] == ["tbl_b"]

    def test_轻卡_enum属性只标hasEnum不内联values(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        card = build_cards(onto, ["entity_a"])["cards"][0]
        status = next(a for a in card["attributes"] if a["name"] == "REC_STATUS")
        assert status["hasEnum"] is True
        assert "values" not in status  # 轻卡不内联码值

    def test_轻卡_非enum属性无hasEnum标记或为False(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        card = build_cards(onto, ["entity_a"])["cards"][0]
        a_id = next(a for a in card["attributes"] if a["name"] == "A_ID")
        assert a_id.get("hasEnum", False) is False

    def test_轻卡_related只给语义邻居含to和hint(self):
        """有 label 的边 → related 项 {to, hint}; 无 label 的裸桥边不进 related。"""
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        card = build_cards(onto, ["entity_a"])["cards"][0]
        related = card["related"]
        tos = {r["to"] for r in related}
        assert "entity_b" in tos  # a->b 边有 label "A属于B"
        b_rel = next(r for r in related if r["to"] == "entity_b")
        assert b_rel["hint"] == "A属于B"
        # related 项只有 to/hint, 无 via/col/count
        assert set(b_rel.keys()) == {"to", "hint"}

    def test_轻卡_无label裸桥边不进related(self):
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        raw["relationships"].append({
            "source": "entity_a", "target": "entity_c", "type": "MANY_TO_ONE",
            "via": "entity_a.RAW", "label": None,  # 裸桥边无 label
        })
        onto = build_indexes(raw)
        card = build_cards(onto, ["entity_a"])["cards"][0]
        tos = {r["to"] for r in card["related"]}
        assert "entity_c" not in tos  # 裸桥边被过滤

    def test_轻卡_related含全部有label邻居(self):
        """plan-review 发现6: 显式断言 entity_a 的 related 到底含哪几个 to, 防漏收/多收。
        entity_a 有 a->b(label"A属于B") 和 a->bridge(label"A经桥到C") 两条有 label 边。"""
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        card = build_cards(onto, ["entity_a"])["cards"][0]
        tos = {r["to"] for r in card["related"]}
        assert tos == {"entity_b", "entity_bridge"}

    def _onto_with_archive(self):
        from core.ontology_view import build_indexes
        raw = _mini_ontology()
        raw["entities"].append({
            "id": "entity_a_his", "entityCode": "AH", "displayName": "实体A(归档)",
            "domain": "域A", "maps_to": "户组X", "tables": ["tbl_a_his"], "attributes": []})
        return build_indexes(raw)

    def test_轻卡_在用实体指向登记过的归档实体(self):
        """CQ-16 根因之二: 在用订单项卡片里看不到其归档实体的存在 (两者无直连边,
        related 里也没有), agent 在在用表查到一条就断言'只有一个订单项'。"""
        from core.ontology_view import build_cards
        card = build_cards(self._onto_with_archive(), ["entity_a"])["cards"][0]
        assert card["archive"] == "entity_a_his"

    def test_轻卡_归档实体反指在用实体(self):
        from core.ontology_view import build_cards
        card = build_cards(self._onto_with_archive(), ["entity_a_his"])["cards"][0]
        assert card["live"] == "entity_a"

    def test_轻卡_无归档实体不挂archive键(self):
        """反证: 没登记 _his 实体就不挂, 不许凭命名凭空造一个实体 id。"""
        from core.ontology_view import build_indexes, build_cards
        card = build_cards(build_indexes(_mini_ontology()), ["entity_b"])["cards"][0]
        assert "archive" not in card and "live" not in card

    def test_build_cards不再返回subgraph(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        result = build_cards(onto, ["entity_a"])
        assert "subgraph" not in result
        assert "cards" in result and "notes" in result

    def test_属性带desc时透传进卡片(self):
        """字段中文描述 desc + 来源标签 desc_source (阶段E 语义血肉) 原样进卡片。"""
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        raw["entities"][0]["attributes"][0]["desc"] = "实体A主键"
        raw["entities"][0]["attributes"][0]["desc_source"] = "code"
        onto = build_indexes(raw)
        card = build_cards(onto, ["entity_a"])["cards"][0]
        a_id = next(a for a in card["attributes"] if a["name"] == "A_ID")
        assert a_id["desc"] == "实体A主键"
        assert a_id["desc_source"] == "code"

    def test_属性缺desc时卡片给默认空串(self):
        """老数据无 desc 键时, 卡片仍给统一结构 (desc='' / desc_source=''), 下游不必判 get。"""
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())  # mini 本体属性均无 desc
        card = build_cards(onto, ["entity_a"])["cards"][0]
        b_id = next(a for a in card["attributes"] if a["name"] == "B_ID")
        assert b_id["desc"] == ""
        assert b_id["desc_source"] == ""

    def test_属性带disambiguation时透传进卡片(self):
        """易混淆说明 disambiguation (步骤4) 原样进卡片, 治 TYPE_CD/REC_STATUS 混淆。"""
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        raw["entities"][0]["attributes"][0]["disambiguation"] = (
            "记录有效性, 非服务状态; 要查在网服务状态用 TYPE_CD"
        )
        onto = build_indexes(raw)
        card = build_cards(onto, ["entity_a"])["cards"][0]
        a_id = next(a for a in card["attributes"] if a["name"] == "A_ID")
        assert a_id["disambiguation"] == (
            "记录有效性, 非服务状态; 要查在网服务状态用 TYPE_CD"
        )

    def test_属性缺disambiguation时卡片给默认空串(self):
        """老数据无 disambiguation 键时, 卡片仍给统一结构 (空串), 下游不必判 get。"""
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())  # mini 本体属性均无 disambiguation
        card = build_cards(onto, ["entity_a"])["cards"][0]
        b_id = next(a for a in card["attributes"] if a["name"] == "B_ID")
        assert b_id["disambiguation"] == ""

    def test_实体带新语义键时透传进卡片(self):
        """重划后: recallKeywords/disambiguateFrom 原样进卡片。"""
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        raw["entities"][0]["recallKeywords"] = ["查号码状态", "在网读者"]
        raw["entities"][0]["disambiguateFrom"] = ["书目包资费问价 → 找 lib_book"]
        onto = build_indexes(raw)
        card = build_cards(onto, ["entity_a"])["cards"][0]
        assert card["recallKeywords"] == ["查号码状态", "在网读者"]
        assert "lib_book" in card["disambiguateFrom"][0]

    def test_实体缺新语义键时卡片给默认空列表(self):
        """老数据/无值 → 卡片给 recallKeywords:[] / disambiguateFrom:[] 统一结构。"""
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        card = build_cards(onto, ["entity_a"])["cards"][0]
        assert card["recallKeywords"] == []
        assert card["disambiguateFrom"] == []

    def test_卡片输出filterRules给agent读(self):
        """默认过滤规则以结构化 {col,op,value,mode} 进卡片, 取代旧散文 query_hints。"""
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        raw["filterRules"] = [
            {"entity": "entity_a", "col": "REC_STATUS", "op": "=", "value": "001",
             "mode": "soft", "origin": "source_code"},
        ]
        onto = build_indexes(raw)
        card = build_cards(onto, ["entity_a"])["cards"][0]
        assert card["filterRules"] == [
            {"col": "REC_STATUS", "op": "=", "value": "001", "mode": "soft"}
        ]

    def test_卡片filterRules缺省空列表(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())   # 无 filterRules
        card = build_cards(onto, ["entity_a"])["cards"][0]
        assert card["filterRules"] == []

    def test_卡片不再含query_hints键(self):
        """query_hints 已废除, 卡片不应再出现该键 (防回潮)。"""
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        raw["entities"][0]["query_hints"] = ["残留旧数据不该进卡片"]
        onto = build_indexes(raw)
        card = build_cards(onto, ["entity_a"])["cards"][0]
        assert "query_hints" not in card

    def test_crossDomain边_note汇总进顶层notes(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        result = build_cards(onto, ["entity_bridge", "entity_c"])
        assert len(result["notes"]) == 1
        assert "跨域断腿" in result["notes"][0]

    def test_无crossDomain边_notes为空(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_mini_ontology())
        result = build_cards(onto, ["entity_a", "entity_b"])
        assert result["notes"] == []

    def test_同域边带note也汇总进notes(self):
        """note 是"这条边的断腿/避坑告警", 跨域只是断腿的一种场景, 不该是硬门槛。
        同域边 (crossDomain 缺失/False) 只要带 note 也应显眼汇总给 agent。
        (真库案例: lib_copy↔lib_loan 的 LIB_LOAN_ID 同名列 JOIN 陷阱是同域边。)
        """
        from core.ontology_view import build_indexes, build_cards
        raw = _mini_ontology()
        # a→b 是同域普通边, 给它挂 note (不带 crossDomain)
        raw["relationships"][0]["note"] = "JOIN 陷阱: A.B_ID 与别表同名, 别搞混"
        onto = build_indexes(raw)
        result = build_cards(onto, ["entity_a", "entity_b"])
        assert len(result["notes"]) == 1
        assert "JOIN 陷阱" in result["notes"][0]


def _ontology_with_shadow_entity() -> dict:
    """1 实体 + 1 条边指向不在 entities 数组里的"影子实体"(如 lib_reader/account 那种
    只留 ID 指针、故意不建实体节点的设计), 覆盖 build_indexes/build_cards 对
    "边端点不在已知实体集合里"这个真实场景的处理。
    """
    return {
        "domains": [],
        "entities": [
            {
                "id": "entity_a", "entityCode": "A", "displayName": "实体A",
                "domain": "域A", "maps_to": "户组X", "tables": ["tbl_a"],
                "attributes": [
                    {"name": "A_ID", "kind": "pk", "type": "bigint"},
                    {"name": "SHADOW_ID", "kind": "fk", "type": "bigint"},
                ],
            },
        ],
        "relationships": [
            {
                "source": "entity_a", "target": "shadow_x", "type": "MANY_TO_ONE",
                "via": "entity_a.SHADOW_ID", "label": "到影子实体",
                "crossDomain": True, "note": "影子实体不在本体内, 仅有 ID 指针",
            },
        ],
        "valueSets": {},
    }


class TestShadowEntity:
    def test_build_indexes_不因边指向影子实体而崩(self):
        from core.ontology_view import build_indexes
        onto = build_indexes(_ontology_with_shadow_entity())
        assert "entity_a" in onto.entities
        assert "shadow_x" not in onto.entities
        assert len(onto.adjacency["entity_a"]) == 1

    def test_build_cards_影子邻居进related(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_ontology_with_shadow_entity())
        card = build_cards(onto, ["entity_a"])["cards"][0]
        tos = {r["to"] for r in card["related"]}
        assert "shadow_x" in tos  # 影子边有 label "到影子实体"

    def test_build_cards_影子实体边的note仍汇总(self):
        from core.ontology_view import build_indexes, build_cards
        onto = build_indexes(_ontology_with_shadow_entity())
        result = build_cards(onto, ["entity_a"])
        assert len(result["notes"]) == 1
        assert "影子实体" in result["notes"][0]


class TestBuildCatalogText:
    def test_catalog含实体名与summary(self):
        from core.ontology_view import build_indexes, build_catalog_text
        onto = build_indexes(_mini_ontology())
        text = build_catalog_text(onto)
        assert "entity_a" in text
        assert "实体A" in text

    def test_catalog带disambiguateFrom时追加边界行(self):
        """步骤5: 实体有 disambiguateFrom 时, catalog 里带出'不涉及/该找'边界,
        让 LLM 点名阶段就避开高相似实体召回错 (BSS 痛点)。"""
        from core.ontology_view import build_indexes, build_catalog_text
        raw = _mini_ontology()
        raw["entities"][0]["disambiguateFrom"] = [
            "书目包资费 → 找 lib_book", "账户余额 → 找 account",
        ]
        onto = build_indexes(raw)
        text = build_catalog_text(onto)
        assert "不涉及" in text
        assert "lib_book" in text

    def test_catalog无disambiguateFrom时不炸也不加边界行(self):
        from core.ontology_view import build_indexes, build_catalog_text
        onto = build_indexes(_mini_ontology())  # 均无 disambiguateFrom
        text = build_catalog_text(onto)
        # 老结构照常渲染, 不因缺 disambiguateFrom 报错
        assert "entity_b" in text

    def _onto_with_concepts(self):
        from core.ontology_view import build_indexes
        return build_indexes({
            "entities": [{"id": "lib_loan", "displayName": "借阅实例", "maps_to": "读者",
                          "tables": ["lib_loan"], "attributes": [], "summary": ""}],
            "relationships": [], "valueSets": {}, "domains": [],
            "concepts": [
                {"id": "active_member", "display_name": "主号", "anchor_entity": "lib_loan",
                 "summary": "A 端主号且关系生效", "origin": "manual", "valid": 1, "conditions": []},
                {"id": "broken_one", "display_name": "断腿概念", "anchor_entity": "lib_loan",
                 "summary": "校验没过", "origin": "manual", "valid": 0, "conditions": []},
            ],
        })

    def test_catalog只输出valid1概念(self):
        """断腿层次1: 概念目录只出 valid=1, valid=0 代码层不进目录 (对齐裸边纪律)。"""
        from core.ontology_view import build_catalog_text
        text = build_catalog_text(self._onto_with_concepts())
        assert "active_member" in text          # valid=1 进目录
        assert "主号" in text
        assert "broken_one" not in text         # valid=0 断腿, 代码层不进目录
        assert "断腿概念" not in text


def _onto_active_member(valid=1):
    from core.ontology_view import build_indexes
    return build_indexes({
        "entities": [
            {"id": "lib_loan", "displayName": "借阅实例", "maps_to": "读者",
             "tables": ["lib_loan"], "attributes": [], "summary": ""},
            {"id": "lib_loan_rel", "displayName": "主从号关系", "maps_to": "关系",
             "tables": ["lib_loan_rel"], "attributes": [], "summary": ""},
        ],
        "relationships": [], "valueSets": {}, "domains": [],
        "concepts": [{
            "id": "active_member", "display_name": "主号", "anchor_entity": "lib_loan",
            "summary": "A 端主号且关系生效", "origin": "manual", "valid": valid,
            "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                            "op": "=", "value": "1000", "seq": 0}],
        }],
    })


def test_concept_detail_结构与involved去重():
    from core.ontology_view import build_concept_detail
    d = build_concept_detail(_onto_active_member(), "active_member")
    assert d["anchor_entity"] == "lib_loan"
    # 条件透传原 4 字段 + Task5 起 6 个聚合字段 (纯 column 时聚合字段为空串)
    assert d["conditions"] == [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                                "op": "=", "value": "1000", "kind": "column",
                                "agg_fn": "", "agg_target": "", "group_by_col": "",
                                "having_op": "", "having_value": ""}]
    # involved = anchor + condition.entity 去重保序
    assert d["involved_entities"] == ["lib_loan", "lib_loan_rel"]
    # 零 SQL/JOIN: 不含这些键
    assert "sql" not in d and "join" not in d


def test_concept_detail_不存在报错():
    from core.ontology_view import build_concept_detail
    d = build_concept_detail(_onto_active_member(), "no_such")
    assert "error" in d


def test_concept_detail_valid0兜底报错():
    from core.ontology_view import build_concept_detail
    d = build_concept_detail(_onto_active_member(valid=0), "active_member")
    assert "error" in d      # 断腿层次2: valid=0 返回 error 而非半拉子条件


def _onto_with_concept(conditions, anchor="lib_loan", attrs=None):
    from core.ontology_view import build_indexes
    return build_indexes({
        "entities": [
            {"id": "lib_loan", "displayName": "借阅实例", "maps_to": "读者",
             "tables": ["lib_loan"],
             "attributes": attrs if attrs is not None else [
                 {"name": "LIB_LOAN_ID", "kind": "pk", "type": "bigint", "desc": ""}],
             "summary": ""},
        ],
        "relationships": [], "domains": [], "valueSets": {},
        "concepts": [{"id": "hv", "display_name": "高价值", "anchor_entity": anchor,
                      "summary": "", "origin": "manual", "valid": 1,
                      "conditions": conditions}],
    })


def test_concept_detail_agg字段透传():
    from core.ontology_view import build_concept_detail
    onto = _onto_with_concept([
        {"entity": "lib_loan", "kind": "agg", "col": "REC_STATUS", "op": "=",
         "value": "10000", "agg_fn": "COUNT", "agg_target": "*",
         "group_by_col": "READER_ID", "having_op": ">=", "having_value": "5", "seq": 0}])
    d = build_concept_detail(onto, "hv")
    cond = d["conditions"][0]
    assert cond["kind"] == "agg"
    assert cond["agg_fn"] == "COUNT"
    assert cond["group_by_col"] == "READER_ID"


def test_concept_detail_agg输出键取groupby():
    from core.ontology_view import build_concept_detail
    onto = _onto_with_concept([
        {"entity": "lib_loan", "kind": "agg", "col": "", "op": "", "value": "",
         "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
         "having_op": ">=", "having_value": "5", "seq": 0}])
    d = build_concept_detail(onto, "hv")
    assert d["output_key"] == {"entity": "lib_loan", "col": "READER_ID"}


def test_concept_detail_column输出键取anchor主键():
    from core.ontology_view import build_concept_detail
    onto = _onto_with_concept([
        {"entity": "lib_loan", "kind": "column", "col": "REC_STATUS", "op": "=",
         "value": "1000", "agg_fn": "", "agg_target": "", "group_by_col": "",
         "having_op": "", "having_value": "", "seq": 0}])
    d = build_concept_detail(onto, "hv")
    assert d["output_key"] == {"entity": "lib_loan", "col": "LIB_LOAN_ID"}


def test_concept_detail_无主键输出键降级带note():
    from core.ontology_view import build_concept_detail
    onto = _onto_with_concept(
        [{"entity": "lib_loan", "kind": "column", "col": "REC_STATUS", "op": "=",
          "value": "1000", "agg_fn": "", "agg_target": "", "group_by_col": "",
          "having_op": "", "having_value": "", "seq": 0}],
        attrs=[{"name": "REC_STATUS", "kind": "attr", "type": "char", "desc": ""}])  # 无 pk
    d = build_concept_detail(onto, "hv")
    assert d["output_key"]["col"] == ""
    assert "output_key_note" in d


class TestLookupEnum:
    def _onto_with_enum(self):
        from core.ontology_view import build_indexes
        raw = {
            "domains": [], "entities": [
                {"id": "lib_loan", "entityCode": "PI", "displayName": "借阅实例",
                 "domain": "d", "maps_to": "读者", "tables": ["lib_loan"],
                 "attributes": [{"name": "REC_STATUS", "kind": "enum", "hasEnum": True}]},
            ], "relationships": [],
            "valueSets": {
                ("lib_loan", "REC_STATUS"): {
                    "name": "有效性状态", "spec_ref": "REC_STATUS",
                    "codes": [{"code": "001", "name": "在用", "desc": "生效"}]},
            },
        }
        return build_indexes(raw)

    def test_lookup_enum命中返回码表(self):
        from core.ontology_view import lookup_enum
        onto = self._onto_with_enum()
        result = lookup_enum(onto, "lib_loan", "REC_STATUS")
        assert result["name"] == "有效性状态"
        assert result["spec_ref"] == "REC_STATUS"
        assert result["codes"][0]["code"] == "001"

    def test_lookup_enum未登记返回None(self):
        from core.ontology_view import lookup_enum
        onto = self._onto_with_enum()
        assert lookup_enum(onto, "lib_loan", "NOPE_CD") is None
        assert lookup_enum(onto, "ghost", "REC_STATUS") is None


class TestFindJoinPaths:
    def _onto(self):
        from core.ontology_view import build_indexes
        raw = {
            "domains": [],
            "entities": [
                {"id": "a", "entityCode": "A", "displayName": "A", "domain": "d",
                 "maps_to": "m", "tables": ["a"], "attributes": []},
                {"id": "b", "entityCode": "B", "displayName": "B", "domain": "d",
                 "maps_to": "m", "tables": ["b"], "attributes": []},
            ],
            "relationships": [
                {"source": "a", "target": "b", "type": "MANY_TO_ONE",
                 "via": "a.B_ID", "label": "语义边", "source_col": "a.B_ID",
                 "target_col": "b.ID", "bridge_count": 10},
                {"source": "a", "target": "b", "type": "MANY_TO_ONE",
                 "via": "a.b_his", "label": None, "source_col": "a.HID",
                 "target_col": "b_his.ID", "bridge_count": 999},
            ],
            "valueSets": {},
        }
        return build_indexes(raw)

    def test_返回ab之间直连边_his噪音边被过滤(self):
        from core.ontology_view import find_join_paths
        paths = find_join_paths(self._onto(), "a", "b")
        # 2 条边里 a.b_his 是噪音边被过滤, 只剩 1 条语义边
        assert len(paths) == 1
        p = paths[0]
        assert {p["from"], p["to"]} == {"a", "b"}
        assert "source_col" in p and "target_col" in p

    def test_语义边保留_his裸边被过滤(self):
        from core.ontology_view import find_join_paths
        paths = find_join_paths(self._onto(), "a", "b")
        assert paths[0]["label"] == "语义边"
        # _his 边不在结果里 (过滤而非沉底)
        assert all("his" not in (p.get("via") or "") for p in paths)

    def test_无直连边返回空(self):
        from core.ontology_view import find_join_paths
        paths = find_join_paths(self._onto(), "a", "ghost")
        assert paths == []

    def _onto_his_entity(self):
        """归档表本身是登记过的实体 (订单项归档表那种), 边带人审 label。"""
        from core.ontology_view import build_indexes
        ent = lambda i: {"id": i, "entityCode": i.upper(), "displayName": i,  # noqa: E731
                         "domain": "d", "maps_to": "m", "tables": [i], "attributes": []}
        raw = {
            "domains": [],
            "entities": [ent("w"), ent("item"), ent("item_his"), ent("head_his")],
            "relationships": [
                {"source": "w", "target": "item_his", "type": "MANY_TO_ONE",
                 "via": "w.ITEM_ID=item_his.ITEM_ID", "label": "工单(已归档)",
                 "source_col": "ITEM_ID", "target_col": "ITEM_ID"},
                {"source": "item_his", "target": "head_his", "type": "MANY_TO_ONE",
                 "via": "item_his.HID=head_his.HID", "label": "归档项属于归档头",
                 "source_col": "HID", "target_col": "HID"},
                {"source": "w", "target": "item_his", "type": "MANY_TO_ONE",
                 "via": "w.X=item_his.X", "label": None,
                 "source_col": "X", "target_col": "X"},
            ],
            "valueSets": {},
        }
        return build_indexes(raw)

    def test_带label的his边不算噪音_直连返回(self):
        """CQ-16 根因: 09-20 人审上线的 his 边 (带 label) 被 _his 子串规则整条滤掉,
        get_join_path 永远给不出到归档表的路径。label 是人审过的'这是正路'信号。"""
        from core.ontology_view import find_join_paths
        paths = find_join_paths(self._onto_his_entity(), "w", "item_his")
        assert [p["label"] for p in paths] == ["工单(已归档)"]

    def test_带label的his边不算噪音_多跳返回(self):
        from core.ontology_view import find_join_paths_multi
        paths = find_join_paths_multi(self._onto_his_entity(), "w", "head_his")
        assert len(paths) == 1 and paths[0]["hops"] == 2

    def test_his裸边仍是噪音(self):
        """反证: 放行只给有 label 的; 聚类脚本抽的 his 裸边 (w.X) 照旧过滤。"""
        from core.ontology_view import find_join_paths
        paths = find_join_paths(self._onto_his_entity(), "w", "item_his")
        assert all(p["via"] != "w.X=item_his.X" for p in paths)

    def test_双向记不重复_同via只返回一次(self):
        """邻接表 source/target 双向记同一边; find_join_paths 按 via 去重。"""
        from core.ontology_view import build_indexes, find_join_paths
        raw = {
            "domains": [],
            "entities": [
                {"id": "a", "entityCode": "A", "displayName": "A", "domain": "d",
                 "maps_to": "m", "tables": ["a"], "attributes": []},
                {"id": "b", "entityCode": "B", "displayName": "B", "domain": "d",
                 "maps_to": "m", "tables": ["b"], "attributes": []},
            ],
            "relationships": [
                {"source": "a", "target": "b", "type": "MANY_TO_ONE", "via": "a.B_ID",
                 "label": "唯一语义边", "source_col": "a.B_ID", "target_col": "b.ID",
                 "bridge_count": 5},
            ],
            "valueSets": {},
        }
        # 从 b 侧查 a (b 的邻接表里也有这条边) 也只返回 1 条
        paths = find_join_paths(build_indexes(raw), "b", "a")
        assert len(paths) == 1

    def test_反向查询时col交换对齐from_to(self):
        """反向查 (from=target侧) 时, _join_edge_view 应交换 source_col/target_col,
        使返回永远对齐 from.col = to.col, agent 不用自己换。
        边存 source=a target=b, source_col=a.B_ID(=a侧col) target_col=b.ID(=b侧col)。
        正向 a→b: from=a 用 source_col=B_ID, to=b 用 target_col=ID。
        反向 b→a: from=b 该用 b 侧 col=ID, to=a 该用 a 侧 col=B_ID → 需交换。
        """
        from core.ontology_view import build_indexes, find_join_paths
        raw = {
            "domains": [],
            "entities": [
                {"id": "a", "entityCode": "A", "displayName": "A", "domain": "d",
                 "maps_to": "m", "tables": ["a"], "attributes": []},
                {"id": "b", "entityCode": "B", "displayName": "B", "domain": "d",
                 "maps_to": "m", "tables": ["b"], "attributes": []},
            ],
            "relationships": [
                {"source": "a", "target": "b", "type": "MANY_TO_ONE",
                 "via": "a.B_ID=b.ID",
                 "label": "语义边", "source_col": "B_ID", "target_col": "ID",
                 "bridge_count": 5},
            ],
            "valueSets": {},
        }
        onto = build_indexes(raw)
        # 正向 a→b: from=a 用 B_ID, to=b 用 ID
        fwd = find_join_paths(onto, "a", "b")[0]
        assert fwd["from"] == "a" and fwd["to"] == "b"
        assert fwd["source_col"] == "B_ID" and fwd["target_col"] == "ID"
        # 反向 b→a: from=b 该用 ID, to=a 该用 B_ID (col 交换)
        rev = find_join_paths(onto, "b", "a")[0]
        assert rev["from"] == "b" and rev["to"] == "a"
        assert rev["source_col"] == "ID", f"反向时 source_col 应是 from(b) 侧的 ID, 实得 {rev['source_col']}"
        assert rev["target_col"] == "B_ID", f"反向时 target_col 应是 to(a) 侧的 B_ID, 实得 {rev['target_col']}"
        # via 是存储方向稳定标识, 反向查询不交换 (只换 col)
        assert rev["via"] == "a.B_ID=b.ID", "via 按存储方向固定, 反向不交换"


def _mk_onto_multi():
    """A→B→C 两跳图 + A→D 一跳 + D→C 一跳(构成 A→C 的 2 跳备选) + 噪音边。"""
    from core.ontology_view import Ontology
    edges = [
        {"source": "A", "target": "B", "via": "A.AID=B.AID",
         "source_col": "AID", "target_col": "AID", "type": "ONE_TO_MANY",
         "label": "A到B", "note": None, "bridge_count": 100},
        {"source": "B", "target": "C", "via": "B.CID=C.CID",
         "source_col": "CID", "target_col": "CID", "type": "ONE_TO_ONE",
         "label": "B到C", "note": None, "bridge_count": 50},
        {"source": "A", "target": "D", "via": "A.DID=D.DID",
         "source_col": "DID", "target_col": "DID", "type": "ONE_TO_MANY",
         "label": None, "note": None, "bridge_count": 10},
        {"source": "D", "target": "C", "via": "D.CID=C.CID",
         "source_col": "CID", "target_col": "CID", "type": "ONE_TO_MANY",
         "label": None, "note": None, "bridge_count": 10},
        {"source": "A", "target": "C_HIS", "via": "A_HIS.AID=C_HIS.AID",
         "source_col": "AID", "target_col": "AID", "type": "ONE_TO_MANY",
         "label": None, "note": None, "bridge_count": 1},
        {"source": "C_HIS", "target": "C", "via": "C_HIS.CID=C.CID",
         "source_col": "CID", "target_col": "CID", "type": "ONE_TO_MANY",
         "label": None, "note": None, "bridge_count": 1},
    ]
    adjacency: dict = {}
    for e in edges:
        adjacency.setdefault(e["source"], []).append(e)
        adjacency.setdefault(e["target"], []).append(e)
    entities = {k: {"id": k, "data_source": "lib"}
                for k in ["A", "B", "C", "D", "C_HIS"]}
    onto = Ontology(entities=entities, adjacency=adjacency, domains=[],
                    value_sets={}, filter_rules={}, concepts={})
    onto.relations = edges
    return onto


def test_multi_直连一跳与老函数一致():
    from core.ontology_view import find_join_paths, find_join_paths_multi
    onto = _mk_onto_multi()
    old = find_join_paths(onto, "A", "B")
    new = find_join_paths_multi(onto, "A", "B")
    assert [p["hops"] for p in new] == [1]
    assert new[0]["edges"] == old


def test_multi_两跳命中且边序列连通():
    from core.ontology_view import find_join_paths_multi
    paths = find_join_paths_multi(_mk_onto_multi(), "A", "C")
    assert paths, "A→C 两跳应有路径"
    for p in paths:
        es = p["edges"]
        assert p["hops"] == len(es)
        assert es[0]["from"] == "A" and es[-1]["to"] == "C"
        for i in range(len(es) - 1):
            assert es[i]["to"] == es[i + 1]["from"], "边序列必须连通"


def test_multi_最短优先():
    """A→C 既有 2 跳; 再加一条 A→C 直连, 1 跳必须排前且只返 1 跳那层。"""
    from core.ontology_view import find_join_paths_multi
    onto = _mk_onto_multi()
    direct = {"source": "A", "target": "C", "via": "A.CID=C.CID",
              "source_col": "CID", "target_col": "CID", "type": "ONE_TO_ONE",
              "label": "A直连C", "note": None, "bridge_count": 5}
    onto.relations.append(direct)
    onto.adjacency["A"].append(direct)
    onto.adjacency["C"].append(direct)
    paths = find_join_paths_multi(onto, "A", "C")
    assert paths[0]["hops"] == 1
    assert all(p["hops"] == 1 for p in paths), "命中层收完即停, 不含2跳"


def test_multi_超上限返空():
    from core.ontology_view import find_join_paths_multi
    assert find_join_paths_multi(_mk_onto_multi(), "A", "C", max_hops=1) == []


def test_multi_噪音边不进路径():
    from core.ontology_view import find_join_paths_multi
    paths = find_join_paths_multi(_mk_onto_multi(), "A", "C")
    for p in paths:
        for e in p["edges"]:
            assert "C_HIS" not in (e["from"], e["to"])


def test_multi_环路防护不重入():
    from core.ontology_view import find_join_paths_multi
    onto = _mk_onto_multi()
    back = {"source": "B", "target": "A", "via": "B.X=A.X",
            "source_col": "X", "target_col": "X", "type": "ONE_TO_MANY",
            "label": "B回A", "note": None, "bridge_count": 3}
    onto.relations.append(back)
    onto.adjacency["B"].append(back)
    onto.adjacency["A"].append(back)
    paths = find_join_paths_multi(onto, "A", "C", max_hops=3)
    for p in paths:
        visited = [p["edges"][0]["from"]] + [e["to"] for e in p["edges"]]
        assert len(visited) == len(set(visited)), "路径内实体不可重复"


def test_multi_反向查询列已交换():
    """从 C 反查 A: 每条边 source_col/target_col 须按 from 方向对齐。"""
    from core.ontology_view import find_join_paths_multi
    paths = find_join_paths_multi(_mk_onto_multi(), "C", "A")
    assert paths
    first = paths[0]["edges"][0]
    assert first["from"] == "C"
