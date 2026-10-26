"""capabilities/ontology/ontology_store.py 单元测试 (TDD).

存储层是 SQLite 权威可编辑源, 核心契约:
1. load_raw() 组装出的 dict 能直接喂 core.ontology_view.build_indexes 且下游零改动
2. 节点级 CRUD (改一实体=库里一行, 不覆盖全文)
3. relation 保留完整 JOIN 字段对 (source_col/target_col/bridge_count) 供多跳拼 SQL
4. 影子实体边 (target 不在 entities) 不炸 — 沿用阶段6 断腿语义
"""
import json
import sqlite3

import pytest

from capabilities.ontology.ontology_store import OntologyStore


@pytest.fixture
def store(tmp_path):
    """空库 (建好 schema), 每个测试独立 tmp 文件。"""
    return OntologyStore(str(tmp_path / "test.db"))


def _seed_domain(store):
    store.upsert_domain({"code": "预约受理", "color": "#4CAF50", "displayOrder": 1})


def _seed_entity_a(store):
    store.upsert_entity({
        "id": "lib_reader_order", "entityCode": "LIB_READER_ORDER", "displayName": "客户订单",
        "domain": "预约受理", "maps_to": "订单", "tables": ["lib_reader_order"],
        "summary": "一张客户订单, 预约受理的主表",
        "attributes": [
            {"name": "ORDER_ID", "kind": "pk", "type": "bigint"},
            {"name": "REC_STATUS", "kind": "enum", "type": "varchar"},
        ],
    })


def _seed_entity_b(store):
    store.upsert_entity({
        "id": "lib_copy", "entityCode": "LIB_COPY", "displayName": "订单项",
        "domain": "预约受理", "maps_to": "订单", "tables": ["lib_copy"],
        "summary": "订单下的一个订单项",
        "attributes": [{"name": "ITEM_ID", "kind": "pk", "type": "bigint"}],
    })


# ============ schema / 建库 ============

class TestSchema:
    def test_新建空库建好四表(self, store):
        tables = store.table_names()
        assert {"domain", "entity", "relation", "enum_value"} <= set(tables)

    def test_新建空库建好filter_rule_check表(self, store):
        tables = store.table_names()
        assert "filter_rule_check" in set(tables)

    def test_新建空库建好concept两表(self, store):
        tables = set(store.table_names())
        assert "concept" in tables
        assert "concept_condition" in tables

    def test_concept_condition_有kind等新列(self, store):
        cols = {r[1] for r in store._conn.execute(
            "PRAGMA table_info(concept_condition)")}
        assert {"kind", "agg_fn", "agg_target",
                "group_by_col", "having_op", "having_value"} <= cols

    def test_concept_condition_kind默认column(self, store):
        store.upsert_concept({"id": "c1", "display_name": "C", "anchor_entity": "e1",
                              "origin": "manual"})
        store._conn.execute(
            "INSERT INTO concept_condition (concept_id, entity, col, op, value, seq) "
            "VALUES ('c1','e1','A','=','1',0)")
        store._conn.commit()
        row = store._conn.execute(
            "SELECT kind, agg_fn FROM concept_condition WHERE concept_id='c1'").fetchone()
        assert row["kind"] == "column"
        assert row["agg_fn"] == ""

    def test_concept_condition_聚合参数不同的断言不撞唯一键(self, store):
        store.upsert_concept({"id": "c1", "display_name": "C", "anchor_entity": "e1",
                              "origin": "manual"})
        base = ("INSERT INTO concept_condition (concept_id, entity, kind, col, op, value, "
                "agg_fn, agg_target, group_by_col, having_op, having_value, seq) VALUES ")
        store._conn.execute(base + "('c1','e1','agg','','','','COUNT','*','K','>=','5',0)")
        store._conn.execute(base + "('c1','e1','agg','','','','SUM','AMT','K','>=','100',1)")
        store._conn.commit()  # 不应 IntegrityError
        n = store._conn.execute(
            "SELECT COUNT(*) FROM concept_condition WHERE concept_id='c1'").fetchone()[0]
        assert n == 2

    def test_空库load_raw返回六键空集(self, store):
        raw = store.load_raw()
        assert raw == {"entities": [], "relationships": [], "valueSets": {},
                       "domains": [], "filterRules": [], "concepts": []}

    def test_重开同一文件不丢数据(self, tmp_path):
        p = str(tmp_path / "persist.db")
        s1 = OntologyStore(p)
        s1.upsert_domain({"code": "预约受理", "color": "#4CAF50", "displayOrder": 1})
        s1.close()
        s2 = OntologyStore(p)
        assert len(s2.load_raw()["domains"]) == 1


# ============ domain CRUD ============

class TestDomainCRUD:
    def test_upsert并读回domain(self, store):
        _seed_domain(store)
        domains = store.load_raw()["domains"]
        assert domains == [{"code": "预约受理", "color": "#4CAF50",
                            "displayOrder": 1, "description": ""}]

    def test_upsert同code是更新非插入(self, store):
        _seed_domain(store)
        store.upsert_domain({"code": "预约受理", "color": "#FF0000", "displayOrder": 1})
        domains = store.load_raw()["domains"]
        assert len(domains) == 1
        assert domains[0]["color"] == "#FF0000"

    def test_domain按displayOrder排序(self, store):
        store.upsert_domain({"code": "工单", "color": "#111", "displayOrder": 2})
        store.upsert_domain({"code": "预约受理", "color": "#222", "displayOrder": 1})
        codes = [d["code"] for d in store.load_raw()["domains"]]
        assert codes == ["预约受理", "工单"]

    def test_软删domain后load_raw不含它(self, store):
        _seed_domain(store)
        store.delete_domain("预约受理")
        assert store.load_raw()["domains"] == []


# ============ entity CRUD + attributes 柔性存 ============

class TestEntityCRUD:
    def test_upsert并读回entity保真(self, store):
        _seed_entity_a(store)
        ents = store.load_raw()["entities"]
        assert len(ents) == 1
        e = ents[0]
        assert e["id"] == "lib_reader_order"
        assert e["displayName"] == "客户订单"
        assert e["tables"] == ["lib_reader_order"]

    def test_upsert同id是更新(self, store):
        _seed_entity_a(store)
        store.upsert_entity({
            "id": "lib_reader_order", "entityCode": "LIB_READER_ORDER", "displayName": "客户订单改",
            "domain": "预约受理", "maps_to": "订单", "tables": ["lib_reader_order", "lib_order_ext"],
            "attributes": [],
        })
        ents = store.load_raw()["entities"]
        assert len(ents) == 1
        assert ents[0]["displayName"] == "客户订单改"
        assert ents[0]["tables"] == ["lib_reader_order", "lib_order_ext"]

    def test_软删entity后load_raw不含它(self, store):
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.delete_entity("lib_copy")
        ids = [e["id"] for e in store.load_raw()["entities"]]
        assert ids == ["lib_reader_order"]

    def test_entity_data_source字段存取(self, store):
        """data_source 字段: 指明实体归属哪个物理数据源 (lib/lib2/...)"""
        store.upsert_entity({
            "id": "lib2_reader", "entityCode": "LIB_READER", "displayName": "客户中心客户",
            "domain": "客户", "maps_to": "客户", "tables": ["lib_reader"],
            "data_source": "lib2",  # 新字段
            "attributes": [],
        })
        ents = store.load_raw()["entities"]
        assert len(ents) == 1
        assert ents[0]["data_source"] == "lib2"

    def test_entity_data_source缺省空串(self, store):
        """★ 2026-10-15: data_source 不传时落空串, 不再兜底写死源名。

        改之前: 默认兜底成写死的真库名 'lib' —— 对新使用者, 他的 DB_SOURCES 里
        可能根本没有这个源, 缺值却被悄悄伪装成"有值"。缺值必须对程序可见,
        由上层 core.config.default_source() 决定怎么兜底, 存储层不该替它决定。
        """
        store.upsert_entity({
            "id": "lib_order", "entityCode": "ORDER", "displayName": "订单",
            "domain": "订单", "maps_to": "订单", "tables": ["order"],
            # 不传 data_source
            "attributes": [],
        })
        ents = store.load_raw()["entities"]
        assert ents[0]["data_source"] == ""

    def test_空data_source原样读出不被兜底(self, store):
        """★ 读路径兜底 (_row_to_entity) 清零后: 库里是空的, 读出来也必须是空的。

        改之前: 库里 '' -> load_entity 读出写死的 'lib', 导致"缺值"对程序不可见,
        且 PUT 一次会把它洗成 'lib'。
        """
        store.upsert_entity({
            "id": "e1", "entityCode": "E1", "displayName": "测试",
            "domain": "d", "maps_to": "lib_x", "data_source": "",
            "tables": [], "attributes": [], "summary": "",
        })
        e = store.load_entity("e1")
        assert e["data_source"] == "", f"读路径仍在兜底: 得到 {e['data_source']!r}"

    def test_upsert不给data_source则落空值(self, store, tmp_path):
        """写路径兜底清零: 不给这个键, 落库是空串。"""
        store.upsert_entity({
            "id": "e2", "entityCode": "E2", "displayName": "测试2",
            "domain": "d", "maps_to": "lib_y",
            "tables": [], "attributes": [], "summary": "",
        })
        row = store._conn.execute(
            "select data_source from entity where id=:i", {"i": "e2"}
        ).fetchone()
        assert row["data_source"] == "", f"写路径仍在兜底: 落库值 {row['data_source']!r}"


# ============ relation CRUD + JOIN 字段对 ============

class TestRelationCRUD:
    def test_relation保留完整JOIN字段对(self, store):
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "source_col": "ORDER_ID", "target_col": "ORDER_ID",
            "via": "被忽略的手编via", "label": "订单含订单项", "bridge_count": 88,
        })
        rels = store.load_raw()["relationships"]
        assert len(rels) == 1
        r = rels[0]
        assert r["source"] == "lib_reader_order"
        assert r["target"] == "lib_copy"
        assert r["via"] == "lib_reader_order.ORDER_ID=lib_copy.ORDER_ID"  # 派生, 忽略入参
        assert r["source_col"] == "ORDER_ID"
        assert r["target_col"] == "ORDER_ID"
        assert r["bridge_count"] == 88

    def test_crossDomain边带note透传(self, store):
        _seed_entity_a(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "account", "type": "MANY_TO_ONE",
            "source_col": "LIBCARD_ID", "target_col": "LIBCARD_ID", "label": "订单归账户",
            "crossDomain": True, "note": "账户主表在账务域·仅ID",
        })
        r = store.load_raw()["relationships"][0]
        assert r["crossDomain"] is True
        assert r["note"] == "账户主表在账务域·仅ID"

    def test_影子实体边load_raw不炸(self, store):
        """target=account 不建实体 (影子), 边仍应存在且 load_raw 正常。"""
        _seed_entity_a(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "account", "type": "MANY_TO_ONE",
            "source_col": "LIBCARD_ID", "target_col": "LIBCARD_ID", "label": "订单归账户",
        })
        raw = store.load_raw()
        assert len(raw["relationships"]) == 1
        assert len(raw["entities"]) == 1  # account 不在 entities

    def test_upsert_col空抛错(self, store):
        """col 必填, 空 col 无法派生 via, 抛 ValueError。"""
        _seed_entity_a(store)
        _seed_entity_b(store)
        with pytest.raises(ValueError, match="source_col/target_col 必填"):
            store.upsert_relation({
                "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
                "source_col": "", "target_col": "ORDER_ID", "label": "x",
            })

    def test_delete_relation按col定位删除(self, store):
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "source_col": "ORDER_ID", "target_col": "ORDER_ID", "label": "x",
        })
        store.delete_relation("lib_reader_order", "lib_copy", "ORDER_ID", "ORDER_ID")
        assert store.load_raw()["relationships"] == []

    def test_delete_relation_col不匹配不删(self, store):
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "source_col": "ORDER_ID", "target_col": "ORDER_ID", "label": "x",
        })
        # col 对不上 → 派生 via 对不上 → 不删
        store.delete_relation("lib_reader_order", "lib_copy", "OTHER", "ORDER_ID")
        assert len(store.load_raw()["relationships"]) == 1


def test__derive_via双侧格式():
    from capabilities.ontology.ontology_store import _derive_via
    assert _derive_via("a", "x", "b", "y") == "a.x=b.y"


def test__derive_via_col空抛错():
    from capabilities.ontology.ontology_store import _derive_via
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _derive_via("a", "", "b", "y")
    with _pytest.raises(ValueError):
        _derive_via("a", "x", "b", "")


# ============ 端到端: load_raw 能喂 build_indexes ============

class TestRoundTripToBuildIndexes:
    def test_load_raw产物直接喂build_indexes(self, store):
        from core.ontology_view import build_indexes

        _seed_domain(store)
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_enum_value("lib_reader_order", "REC_STATUS", {
            "name": "订单状态", "spec_ref": "LIB-ORD-A",
            "codes": [{"code": "1", "name": "已受理", "desc": None}],
        })
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "via": "lib_copy.ORDER_ID", "label": "订单含订单项",
            "source_col": "lib_reader_order.ORDER_ID", "target_col": "lib_copy.ORDER_ID",
        })

        onto = build_indexes(store.load_raw())
        assert set(onto.entities) == {"lib_reader_order", "lib_copy"}
        # 邻接表双向记: 两个实体各能查到这条边
        assert len(onto.adjacency["lib_reader_order"]) == 1
        assert len(onto.adjacency["lib_copy"]) == 1
        # valueSets 经 store 往返按 (entity,field) 键索引 (卡片渐进披露重构)
        assert ("lib_reader_order", "REC_STATUS") in onto.value_sets


# ============ enum_value CRUD (卡片渐进披露重构: 主键改 (entity,field)) ============

class TestEnumValueCRUD:
    def test_新建空库建好enum_value表(self, store):
        assert "enum_value" in store.table_names()

    def test_upsert并读回enum_value按entity_field(self, store):
        store.upsert_enum_value("lib_loan", "REC_STATUS", {
            "name": "借阅实例有效性状态", "spec_ref": "REC_STATUS",
            "codes": [{"code": "001", "name": "在用", "desc": "生效使用中"}],
        })
        vs = store.load_raw()["valueSets"]
        assert ("lib_loan", "REC_STATUS") in vs
        entry = vs[("lib_loan", "REC_STATUS")]
        assert entry["name"] == "借阅实例有效性状态"
        assert entry["spec_ref"] == "REC_STATUS"
        assert entry["codes"][0]["code"] == "001"

    def test_同名字段跨实体独立不覆盖(self, store):
        store.upsert_enum_value("lib_loan", "REC_STATUS", {
            "name": "借阅实例状态", "spec_ref": "LIB-ST-01",
            "codes": [{"code": "300005", "name": "双停", "desc": None}],
        })
        store.upsert_enum_value("lib_copy", "REC_STATUS", {
            "name": "订单项状态", "spec_ref": "LIB-ST-02",
            "codes": [{"code": "1", "name": "已受理", "desc": None}],
        })
        vs = store.load_raw()["valueSets"]
        assert vs[("lib_loan", "REC_STATUS")]["name"] == "借阅实例状态"
        assert vs[("lib_copy", "REC_STATUS")]["name"] == "订单项状态"

    def test_upsert同entity_field是更新非插入(self, store):
        store.upsert_enum_value("lib_loan", "REC_STATUS", {
            "name": "旧名", "codes": []})
        store.upsert_enum_value("lib_loan", "REC_STATUS", {
            "name": "新名", "codes": [{"code": "001", "name": "在用", "desc": None}]})
        vs = store.load_raw()["valueSets"]
        assert len([k for k in vs if k == ("lib_loan", "REC_STATUS")]) == 1
        assert vs[("lib_loan", "REC_STATUS")]["name"] == "新名"

    def test_软删enum_value后load_raw不含它(self, store):
        store.upsert_enum_value("lib_loan", "REC_STATUS", {"name": "x", "codes": []})
        store.delete_enum_value("lib_loan", "REC_STATUS")
        assert ("lib_loan", "REC_STATUS") not in store.load_raw()["valueSets"]

    def test_spec_ref可空(self, store):
        store.upsert_enum_value("lib_loan", "FOO_CD", {"name": "无溯源", "codes": []})
        entry = store.load_raw()["valueSets"][("lib_loan", "FOO_CD")]
        assert entry["spec_ref"] is None


def test_load_domains_带全字段(store):
    store.upsert_domain({"code": "预约受理", "color": "#4CAF50",
                         "displayOrder": 2, "description": "订单相关"})
    d = store.load_raw()["domains"][0]
    assert d["code"] == "预约受理"
    assert d["color"] == "#4CAF50"
    assert d["displayOrder"] == 2
    assert d["description"] == "订单相关"


def test_load_entity_取单行且血肉平铺(store):
    store.upsert_entity({
        "id": "lib_order", "displayName": "客户订单", "domain": "预约受理",
        "maps_to": "订单", "tables": ["t_order"], "summary": "s",
        "attributes": [{"name": "REC_STATUS", "kind": "enum"}],
        "recallKeywords": ["订单号"],
    })
    e = store.load_entity("lib_order")
    assert e["displayName"] == "客户订单"
    assert e["attributes"] == [{"name": "REC_STATUS", "kind": "enum"}]
    assert e["recallKeywords"] == ["订单号"]  # extra_json 平铺


def test_load_entity_不存在抛KeyError(store):
    with pytest.raises(KeyError):
        store.load_entity("nope")


def test_unassign_entities_把挂靠实体domain置空(store):
    store.upsert_entity({"id": "e1", "displayName": "E1", "domain": "预约受理",
                         "maps_to": "t1", "tables": [], "attributes": [], "summary": ""})
    store.upsert_entity({"id": "e2", "displayName": "E2", "domain": "借阅实例",
                         "maps_to": "t2", "tables": [], "attributes": [], "summary": ""})
    store.unassign_entities("预约受理")
    assert store.load_entity("e1")["domain"] == ""
    assert store.load_entity("e2")["domain"] == "借阅实例"  # 别的域不动


# ============ 复合写原子性 (单事务, all-or-nothing) ============
#
# 背景: flip / 删域级联 原来在 web 层 = 两次独立 store 调用, 各自 commit。
# 中途失败 (进程崩/异常) 会留半成品: 边删了没重建 (数据丢失), 或域删了实体没归位。
# ontology.db 不进 git 无备份 → 半成品即永久损坏。
# 下沉为 store 单方法单事务后, 任一步抛错 → 整体回滚, 库停在操作前状态。


class TestCompositeAtomicity:
    def test_flip_relation_删旧建新在一个事务内(self, store):
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "label": "订单含订单项",
            "source_col": "ORDER_ID", "target_col": "ORDER_ID",
            "bridge_count": 88, "crossDomain": True,
        })
        old = store.load_raw()["relationships"][0]
        new_edge = {**old, "source": old["target"], "target": old["source"],
                    "source_col": old.get("target_col"),
                    "target_col": old.get("source_col")}
        store.flip_relation(body_source=old["source"], body_target=old["target"],
                            body_source_col="ORDER_ID", body_target_col="ORDER_ID",
                            new_edge=new_edge)
        rels = store.load_raw()["relationships"]
        assert len(rels) == 1                          # 旧边删, 新边建, 净剩 1 条
        r = rels[0]
        assert r["source"] == "lib_copy"             # 方向翻转
        assert r["target"] == "lib_reader_order"
        assert r["via"] == "lib_copy.ORDER_ID=lib_reader_order.ORDER_ID"  # 翻转后派生
        assert r["source_col"] == "ORDER_ID"
        assert r["crossDomain"] is True                # extra 血肉透传不抹

    def test_flip_relation_中途失败整体回滚(self, store, monkeypatch):
        """建新边这步抛错 → 旧边不能被删掉 (回滚)。"""
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "source_col": "ORDER_ID", "target_col": "ORDER_ID", "label": "订单含订单项",
        })
        old = store.load_raw()["relationships"][0]
        new_edge = {**old, "source": old["target"], "target": old["source"]}

        # 让"建新边"这步炸 (模拟落盘中途失败), 删旧边已先执行
        def boom(_edge):
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(store, "_upsert_relation_stmt", boom)
        with pytest.raises(sqlite3.OperationalError):
            store.flip_relation(body_source=old["source"], body_target=old["target"],
                                body_source_col="ORDER_ID", body_target_col="ORDER_ID",
                                new_edge=new_edge)
        monkeypatch.undo()
        # 旧边必须还在 (整体回滚, 没留"删了旧的没建新的"半成品)
        rels = store.load_raw()["relationships"]
        assert len(rels) == 1
        assert rels[0]["source"] == "lib_reader_order"

    def test_delete_domain_cascade_删域并归位实体在一个事务内(self, store):
        store.upsert_domain({"code": "预约受理", "color": "#4CAF50", "displayOrder": 1})
        store.upsert_entity({"id": "e1", "displayName": "E1", "domain": "预约受理",
                             "maps_to": "t1", "tables": [], "attributes": [],
                             "summary": ""})
        store.delete_domain_cascade("预约受理")
        raw = store.load_raw()
        assert raw["domains"] == []                    # 域软删
        assert store.load_entity("e1")["domain"] == ""  # 挂靠实体归未分类

    def test_delete_domain_cascade_中途失败整体回滚(self, store, monkeypatch):
        """删域这步抛错 → 实体 domain 不能被清空 (回滚)。"""
        store.upsert_domain({"code": "预约受理", "color": "#4CAF50", "displayOrder": 1})
        store.upsert_entity({"id": "e1", "displayName": "E1", "domain": "预约受理",
                             "maps_to": "t1", "tables": [], "attributes": [],
                             "summary": ""})

        # 让"删域"这步炸 (归位实体已先执行)
        def boom(*a, **k):
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(store, "_soft_delete_stmt", boom)
        with pytest.raises(sqlite3.OperationalError):
            store.delete_domain_cascade("预约受理")
        monkeypatch.undo()
        # 实体 domain 必须还在 (回滚, 没留"实体归了位但域没删"半成品)
        assert store.load_entity("e1")["domain"] == "预约受理"
        assert store.load_raw()["domains"][0]["code"] == "预约受理"

    def test_delete_entity_cascade_删实体连带删相关边而无关边存活(self, store):
        for eid in ("A", "B", "C"):
            store.upsert_entity({"id": eid, "displayName": eid})
        store.upsert_relation({"source": "A", "target": "B", "source_col": "ID1", "target_col": "ID2", "type": "UNKNOWN"})
        store.upsert_relation({"source": "B", "target": "A", "source_col": "ID2", "target_col": "ID3", "type": "UNKNOWN"})
        store.upsert_relation({"source": "A", "target": "C", "source_col": "ID4", "target_col": "ID5", "type": "UNKNOWN"})
        store.upsert_relation({"source": "B", "target": "C", "source_col": "ID6", "target_col": "ID7", "type": "UNKNOWN"})

        store.delete_entity_cascade("A")

        raw = store.load_raw()
        live_entity_ids = {e["id"] for e in raw["entities"]}
        assert "A" not in live_entity_ids
        assert {"B", "C"} <= live_entity_ids
        live_edges = {(r["source"], r["target"], r["via"]) for r in raw["relationships"]}
        assert ("A", "B", "A.ID1=B.ID2") not in live_edges   # A 为 source
        assert ("B", "A", "B.ID2=A.ID3") not in live_edges   # A 为 target
        assert ("A", "C", "A.ID4=C.ID5") not in live_edges   # A 为 source
        assert ("B", "C", "B.ID6=C.ID7") in live_edges       # 无关边存活


def test_ENTITY_FIXED_KEYS_导出供web层复用():
    """web 层 import 这个 public 名, 不再内联复制 store 私有 _ENTITY_FIXED。"""
    from capabilities.ontology.ontology_store import ENTITY_FIXED_KEYS
    assert ENTITY_FIXED_KEYS == {"id", "entityCode", "displayName", "domain",
                                 "maps_to", "data_source", "tables", "attributes", "summary"}


def test_enum_fields_只算该实体存活行(tmp_path):
    store = OntologyStore(str(tmp_path / "t.db"))
    store.upsert_enum_value("order", "A_CD", {"name": "a", "codes": [{"code": "1", "name": "x"}]})
    store.upsert_enum_value("order", "B_CD", {"name": "b", "codes": [{"code": "1", "name": "x"}]})
    store.upsert_enum_value("other", "C_CD", {"name": "c", "codes": [{"code": "1", "name": "x"}]})
    store.delete_enum_value("order", "B_CD")   # 软删 → 不算
    assert store.enum_fields("order") == {"A_CD"}
    assert store.enum_fields("nobody") == set()
    store.close()


def test_upsert_relation_非法type抛错(store):
    with pytest.raises(ValueError):
        store.upsert_relation({
            "source": "a", "target": "b", "source_col": "x", "target_col": "y",
            "type": "BRIDGE",
        })


def test_upsert_relation_合法type通过(store):
    for t in ("ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "UNKNOWN"):
        store.upsert_relation({
            "source": "a", "target": "b", "source_col": f"x{t}", "target_col": "y",
            "type": t,
        })  # 不抛即通过


# ============ filter_rule CRUD (新表: 默认过滤规则结构化) ============

class TestFilterRuleCRUD:
    def test_新建空库建好filter_rule表(self, store):
        assert "filter_rule" in store.table_names()

    def test_新建空库filter_rule的value列是NOT_NULL(self, store):
        info = store._conn.execute("PRAGMA table_info(filter_rule)").fetchall()
        value_col = [r for r in info if r[1] == "value"][0]
        assert value_col[3] == 1  # notnull=1

    def test_upsert并读回filter_rule(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code",
        })
        rules = store.load_raw()["filterRules"]
        assert len(rules) == 1
        r = rules[0]
        assert r["entity"] == "lib_loan"
        assert r["col"] == "REC_STATUS"
        assert r["op"] == "="
        assert r["value"] == "001"
        assert r["mode"] == "soft"
        assert r["origin"] == "source_code"

    def test_upsert同entity_col_op_value是更新非插入(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"})
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "hard", "origin": "manual"})   # 同幂等键, 改 mode/origin
        rules = store.load_raw()["filterRules"]
        assert len(rules) == 1
        assert rules[0]["mode"] == "hard"
        assert rules[0]["origin"] == "manual"

    def test_不同value是两条独立规则(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"})
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "000",
            "mode": "soft", "origin": "source_code"})
        assert len(store.load_raw()["filterRules"]) == 2

    def test_软删filter_rule后load_raw不含它(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
            "mode": "soft", "origin": "source_code"})
        store.delete_filter_rule("lib_loan", "REC_STATUS", "=", "001")
        assert store.load_raw()["filterRules"] == []

    def test_IN列表value逗号分隔透传(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "REC_STATUS", "op": "IN", "value": "000,001",
            "mode": "soft", "origin": "source_code"})
        assert store.load_raw()["filterRules"][0]["value"] == "000,001"

    def test_非法mode拒绝(self, store):
        with pytest.raises(ValueError, match="非法 filter_rule mode"):
            store.upsert_filter_rule({
                "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
                "mode": "conditional", "origin": "source_code"})   # 只两档 hard/soft

    def test_非法origin拒绝(self, store):
        with pytest.raises(ValueError, match="非法 filter_rule origin"):
            store.upsert_filter_rule({
                "entity": "lib_loan", "col": "REC_STATUS", "op": "=", "value": "001",
                "mode": "soft", "origin": "guessed"})   # 只三档 source_code/manual/ai

    def test_upsert_value为None归一成空串(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "READER_ID", "op": "IS NOT NULL",
            "value": None, "mode": "hard", "origin": "source_code"})
        rules = store.load_raw()["filterRules"]
        assert len(rules) == 1
        assert rules[0]["value"] == ""

    def test_upsert无值op同键二次是更新非累积(self, store):
        # 债的核心: 旧库两条 value=NULL 会累积成 2 行, 焊 NOT NULL 后归'' 真幂等
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "READER_ID", "op": "IS NOT NULL",
            "value": None, "mode": "hard", "origin": "source_code"})
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "READER_ID", "op": "IS NOT NULL",
            "value": None, "mode": "soft", "origin": "manual"})
        rules = store.load_raw()["filterRules"]
        assert len(rules) == 1          # 幂等: 未累积
        assert rules[0]["mode"] == "soft"

    def test_upsert普通op空值写时拦截(self, store):
        with pytest.raises(ValueError, match="需要值"):
            store.upsert_filter_rule({
                "entity": "lib_loan", "col": "REC_STATUS", "op": "=",
                "value": "", "mode": "soft", "origin": "manual"})

    def test_upsert普通op_None也拦截(self, store):
        with pytest.raises(ValueError, match="需要值"):
            store.upsert_filter_rule({
                "entity": "lib_loan", "col": "REC_STATUS", "op": "=",
                "value": None, "mode": "soft", "origin": "manual"})

    def test_软删无值op规则value传None按空串匹配(self, store):
        store.upsert_filter_rule({
            "entity": "lib_loan", "col": "READER_ID", "op": "IS NOT NULL",
            "value": None, "mode": "hard", "origin": "source_code"})
        store.delete_filter_rule("lib_loan", "READER_ID", "IS NOT NULL", None)
        assert store.load_raw()["filterRules"] == []


class TestFilterRuleCheck:
    def _rows(self):
        return [
            {"entity": "lib_reader_order", "col": "REC_STATUS", "op": "=", "value": "1000",
             "column_exists": 1, "value_in_enum": 1, "sql_runs": 1, "detail": ""},
            {"entity": "lib_reader_order", "col": "DEL_FLAG", "op": "=", "value": "0",
             "column_exists": 1, "value_in_enum": None, "sql_runs": 1, "detail": ""},
            {"entity": "lib_copy", "col": "GONE_COL", "op": "=", "value": "x",
             "column_exists": 0, "value_in_enum": None, "sql_runs": 0,
             "detail": "列 GONE_COL 不在物理表列集合内"},
        ]

    def test_落盘后取回一致且ok现算(self, store):
        store.insert_check_run("run-1", "2026-08-05T10:00:00", self._rows())
        runs = store.load_check_runs(limit=2)
        assert len(runs) == 1
        run = runs[0]
        assert run["run_id"] == "run-1"
        assert run["checked_at"] == "2026-08-05T10:00:00"
        results = {(r["col"]): r for r in run["results"]}
        # 全绿 + value_in_enum=1 → ok True
        assert results["REC_STATUS"]["ok"] is True
        # value_in_enum=None (skipped) 视为通过 → ok True
        assert results["DEL_FLAG"]["ok"] is True
        assert results["DEL_FLAG"]["value_in_enum"] is None
        # column_exists=0 → ok False
        assert results["GONE_COL"]["ok"] is False
        assert results["GONE_COL"]["detail"] == "列 GONE_COL 不在物理表列集合内"

    def test_value_in_enum为0时ok为False(self, store):
        rows = [{"entity": "e", "col": "c", "op": "=", "value": "bad",
                 "column_exists": 1, "value_in_enum": 0, "sql_runs": 1,
                 "detail": "值 ['bad'] 不在枚举码值内"}]
        store.insert_check_run("run-x", "2026-08-05T11:00:00", rows)
        run = store.load_check_runs()[0]
        assert run["results"][0]["ok"] is False

    def test_多run按时间倒序且limit截断(self, store):
        store.insert_check_run("run-1", "2026-08-05T10:00:00", self._rows())
        store.insert_check_run("run-2", "2026-08-05T12:00:00", self._rows())
        store.insert_check_run("run-3", "2026-08-05T14:00:00", self._rows())
        runs = store.load_check_runs(limit=2)
        assert [r["run_id"] for r in runs] == ["run-3", "run-2"]


class TestConceptCRUD:
    def _seed(self, store):
        store.upsert_concept({
            "id": "active_member", "display_name": "主号",
            "anchor_entity": "lib_loan",
            "summary": "作为主从号关系里 A 端主号、且该关系当前生效的号码",
            "origin": "manual",
        })

    def test_upsert并读回concept(self, store):
        self._seed(store)
        cs = store._load_concepts()
        assert len(cs) == 1
        c = cs[0]
        assert c["id"] == "active_member"
        assert c["display_name"] == "主号"
        assert c["anchor_entity"] == "lib_loan"
        assert c["origin"] == "manual"
        assert c["valid"] == 0          # 新建默认未校验
        assert c["conditions"] == []    # 尚无条件

    def test_origin非法抛ValueError(self, store):
        with pytest.raises(ValueError):
            store.upsert_concept({
                "id": "x", "display_name": "X", "anchor_entity": "lib_loan",
                "origin": "source_code",   # 概念无此来源
            })

    def test_set_concept_valid只改valid(self, store):
        self._seed(store)
        store.set_concept_valid("active_member", 1)
        assert store._load_concepts()[0]["valid"] == 1

    def test_upsert不覆盖已有valid(self, store):
        self._seed(store)
        store.set_concept_valid("active_member", 1)
        # 再 upsert 同 id (改 summary), valid 应保留 1 (不被默认 0 覆盖)
        store.upsert_concept({
            "id": "active_member", "display_name": "主号",
            "anchor_entity": "lib_loan", "summary": "改了描述", "origin": "manual",
        })
        cs = store._load_concepts()[0]
        assert cs["summary"] == "改了描述"
        assert cs["valid"] == 1

    def test_软删concept不再load(self, store):
        self._seed(store)
        store.delete_concept("active_member")
        assert store._load_concepts() == []

    def test_live_condition_ids_返回幂等键到id映射(self, store):
        store.upsert_concept({"id": "c_x", "display_name": "X",
                              "anchor_entity": "e1", "origin": "manual"})
        store.upsert_concept_condition({"concept_id": "c_x", "entity": "e1",
                                        "col": "A", "op": "=", "value": "1", "seq": 0})
        store.upsert_concept_condition({"concept_id": "c_x", "entity": "e2",
                                        "col": "B", "op": "=", "value": "2", "seq": 1})
        m = store._live_condition_ids("c_x")
        # 11 字段幂等键 (kind 缺省 column, 聚合列缺省空串), 见 _cc_key
        assert set(m.keys()) == {"e1|column|A|=|1|||||", "e2|column|B|=|2|||||"}
        assert all(isinstance(v, int) for v in m.values())

    def test_加条件并内嵌读回(self, store):
        self._seed(store)
        store.upsert_concept_condition({
            "concept_id": "active_member", "entity": "lib_loan_rel",
            "col": "REC_STATUS", "op": "=", "value": "1000",
        })
        c = store._load_concepts()[0]
        assert len(c["conditions"]) == 1
        cond = c["conditions"][0]
        assert cond["entity"] == "lib_loan_rel"
        assert cond["col"] == "REC_STATUS"
        assert cond["op"] == "="
        assert cond["value"] == "1000"

    def test_条件UNIQUE幂等(self, store):
        self._seed(store)
        cc = {"concept_id": "active_member", "entity": "lib_loan_rel",
              "col": "REC_STATUS", "op": "=", "value": "1000"}
        store.upsert_concept_condition(cc)
        store.upsert_concept_condition(cc)   # 同键再插 = update, 不新增行
        assert len(store._load_concepts()[0]["conditions"]) == 1

    def test_load_raw含concepts键(self, store):
        self._seed(store)
        store.upsert_concept_condition({
            "concept_id": "active_member", "entity": "lib_loan_rel",
            "col": "REC_STATUS", "op": "=", "value": "1000",
        })
        raw = store.load_raw()
        assert "concepts" in raw
        assert raw["concepts"][0]["id"] == "active_member"
        assert raw["concepts"][0]["conditions"][0]["col"] == "REC_STATUS"

    def test_save_concept_with_conditions_全量存(self, store):
        store.save_concept_with_conditions(
            {"id": "c_m", "display_name": "主号", "anchor_entity": "lib_loan",
             "summary": "s", "origin": "manual"},
            [{"entity": "lib_loan_rel", "col": "REC_STATUS", "op": "=", "value": "1000", "seq": 0}])
        raw = store.load_raw()
        c = next(x for x in raw["concepts"] if x["id"] == "c_m")
        assert c["display_name"] == "主号"
        assert len(c["conditions"]) == 1
        assert c["conditions"][0]["col"] == "REC_STATUS"

    def test_save_concept_with_conditions_删掉被移除的旧条件(self, store):
        store.save_concept_with_conditions(
            {"id": "c_m", "display_name": "主号", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0},
             {"entity": "e2", "col": "B", "op": "=", "value": "2", "seq": 1}])
        store.save_concept_with_conditions(
            {"id": "c_m", "display_name": "主号", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0}])
        raw = store.load_raw()
        c = next(x for x in raw["concepts"] if x["id"] == "c_m")
        keys = {f"{cc['entity']}|{cc['col']}" for cc in c["conditions"]}
        assert keys == {"e1|A"}

    def test_save_concept_条件value为0不被误删(self, store):
        # value="0" 与 NULL 的归一必须区分(防 `or ''` 碰撞)
        store.save_concept_with_conditions(
            {"id": "c_z", "display_name": "Z", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "FLAG", "op": "=", "value": "0", "seq": 0}])
        # 再存一次同样内容, 该条件应保留(不因归一 bug 被 diff 当"被移除"删掉)
        store.save_concept_with_conditions(
            {"id": "c_z", "display_name": "Z", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "FLAG", "op": "=", "value": "0", "seq": 0}])
        raw = store.load_raw()
        c = next(x for x in raw["concepts"] if x["id"] == "c_z")
        assert len(c["conditions"]) == 1 and c["conditions"][0]["value"] == "0"

    def test_save_concept_中途异常全回滚(self, store):
        import sqlite3
        # 先存一个已有概念作对照(应不受影响)
        store.save_concept_with_conditions(
            {"id": "c_keep", "display_name": "K", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0}])
        # 第2条件 col=None 触发 NOT NULL 约束错误: 此时概念身份+第1条件已写入,
        # 异常落在"已写了一部分之后", 正好验证 try/rollback/raise 把半拉数据全撤。
        with pytest.raises(sqlite3.IntegrityError):
            store.save_concept_with_conditions(
                {"id": "c_new", "display_name": "N", "anchor_entity": "e1", "origin": "manual"},
                [{"entity": "e1", "col": "X", "op": "=", "value": "9", "seq": 0},
                 {"entity": "e2", "col": None, "op": "=", "value": "8", "seq": 1}])
        raw = store.load_raw()
        # 新概念完全没落库(回滚干净)
        assert all(x["id"] != "c_new" for x in raw["concepts"])
        # 对照概念不受影响
        assert any(x["id"] == "c_keep" for x in raw["concepts"])

    def test_save_concept_IS_NULL条件重复保存不累积(self, store):
        # value=None 的 IS NULL 条件, 存两次, 活行应仍为 1(根治 NULL 不去重累积)
        cond = {"entity": "e1", "col": "END_DT", "op": "IS NULL", "value": None, "seq": 0}
        store.save_concept_with_conditions(
            {"id": "c_n", "display_name": "N", "anchor_entity": "e1", "origin": "manual"}, [cond])
        store.save_concept_with_conditions(
            {"id": "c_n", "display_name": "N", "anchor_entity": "e1", "origin": "manual"}, [cond])
        raw = store.load_raw()
        c = next(x for x in raw["concepts"] if x["id"] == "c_n")
        assert len(c["conditions"]) == 1
        # 落库 value 归一为空串, 非 None
        assert c["conditions"][0]["value"] == ""

    def test_delete_concept_级联软删条件(self, store):
        store.save_concept_with_conditions(
            {"id": "c_del", "display_name": "D", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0}])
        store.delete_concept("c_del")
        raw = store.load_raw()
        assert all(x["id"] != "c_del" for x in raw["concepts"])
        # 条件也不该以任何存活形态残留
        assert store._live_condition_ids("c_del") == {}

    # ---- 聚合表达式 (kind=agg) 透传 ----

    def test_upsert_agg条件读回带聚合字段(self, store):
        store.upsert_concept({"id": "hv", "display_name": "高价值", "anchor_entity": "lib_loan",
                              "origin": "manual"})
        store.upsert_concept_condition({
            "concept_id": "hv", "entity": "lib_loan", "kind": "agg",
            "col": "REC_STATUS", "op": "=", "value": "10000",
            "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
            "having_op": ">=", "having_value": "5", "seq": 0})
        cond = store._load_concepts()[0]["conditions"][0]
        assert cond["kind"] == "agg"
        assert cond["agg_fn"] == "COUNT"
        assert cond["group_by_col"] == "READER_ID"
        assert cond["having_op"] == ">="
        assert cond["having_value"] == "5"
        assert cond["col"] == "REC_STATUS"     # 前置过滤复用老字段

    def test_老column条件读回新字段为空串(self, store):
        store.upsert_concept({"id": "c1", "display_name": "C", "anchor_entity": "e1",
                              "origin": "manual"})
        store.upsert_concept_condition({"concept_id": "c1", "entity": "e1",
                                        "col": "A", "op": "=", "value": "1", "seq": 0})
        cond = store._load_concepts()[0]["conditions"][0]
        assert cond["kind"] == "column"
        assert cond["agg_fn"] == "" and cond["group_by_col"] == ""

    def test_save_两个agg断言共存不撞键(self, store):
        store.save_concept_with_conditions(
            {"id": "hv", "display_name": "高价值", "anchor_entity": "lib_loan",
             "origin": "manual"},
            [{"entity": "lib_loan", "kind": "agg", "col": "", "op": "", "value": "",
              "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
              "having_op": ">=", "having_value": "5", "seq": 0},
             {"entity": "lib_loan", "kind": "agg", "col": "", "op": "", "value": "",
              "agg_fn": "SUM", "agg_target": "AMT", "group_by_col": "READER_ID",
              "having_op": ">=", "having_value": "100", "seq": 1}])
        c = next(x for x in store.load_raw()["concepts"] if x["id"] == "hv")
        assert len(c["conditions"]) == 2

    def test_save_agg断言参数变更不残留旧行(self, store):
        # 同 seq 但 having_value 变 → 幂等键不同 → 旧行应被 diff 出并软删
        store.save_concept_with_conditions(
            {"id": "hv", "display_name": "H", "anchor_entity": "lib_loan", "origin": "manual"},
            [{"entity": "lib_loan", "kind": "agg", "col": "", "op": "", "value": "",
              "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
              "having_op": ">=", "having_value": "5", "seq": 0}])
        store.save_concept_with_conditions(
            {"id": "hv", "display_name": "H", "anchor_entity": "lib_loan", "origin": "manual"},
            [{"entity": "lib_loan", "kind": "agg", "col": "", "op": "", "value": "",
              "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
              "having_op": ">=", "having_value": "8", "seq": 0}])
        c = next(x for x in store.load_raw()["concepts"] if x["id"] == "hv")
        assert len(c["conditions"]) == 1
        assert c["conditions"][0]["having_value"] == "8"

    def test_save_falsy非字符串value不误删刚写入行(self, store):
        # 回归: INSERT 与 _cc_key 若对 value=0(int) 归一不一致 → keep 键与读回键错位 → 误删。
        # _cc_norm 统一两处归一后, 该行应存活。
        store.save_concept_with_conditions(
            {"id": "z", "display_name": "Z", "anchor_entity": "e1", "origin": "manual"},
            [{"entity": "e1", "col": "A", "op": "=", "value": 0, "seq": 0}])
        c = next(x for x in store.load_raw()["concepts"] if x["id"] == "z")
        assert len(c["conditions"]) == 1
        assert c["conditions"][0]["value"] == "0"


# ============ 自演化三新表 (failure_signal / proposal / relation_health) ============


class TestSelfEvolveSchema:
    def test_三新表建表成功(self, store):
        tables = set(store.table_names())
        assert {"failure_signal", "proposal", "relation_health"} <= tables

    def test_proposal表check约束拒绝非法kind(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO proposal (kind, payload_json, proposed_at) "
                "VALUES ('bogus','{}','2026-09-05')")

    def test_proposal表check约束拒绝非法direction(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO proposal (kind, direction, payload_json, proposed_at) "
                "VALUES ('relation','bogus','{}','2026-09-05')")


class TestFailureSignalCRUD:
    def test_list_unprocessed和mark_processed(self, store):
        store._conn.execute(
            "INSERT INTO failure_signal (kind, payload_json, source, created_at) "
            "VALUES (?,?,?,?)",
            ("obqc_circuit_break", '{"fp":"abc"}', "lib", "2026-09-05T10:00:00"))
        store._conn.commit()
        signals = store.list_unprocessed_signals()
        assert len(signals) == 1
        assert signals[0]["kind"] == "obqc_circuit_break"
        store.mark_signal_processed(signals[0]["id"], proposal_id=42)
        assert store.list_unprocessed_signals() == []

    def test_list_unprocessed_limit(self, store):
        for i in range(5):
            store._conn.execute(
                "INSERT INTO failure_signal (kind, payload_json, created_at) "
                "VALUES (?,?,?)", ("cq_fail", f'{{"i":{i}}}', "2026-09-05"))
        store._conn.commit()
        assert len(store.list_unprocessed_signals(limit=3)) == 3


class TestProposalCRUD:
    def test_insert并list_proposals(self, store):
        pid = store.insert_proposal(
            kind="relation", payload={"source": "a", "target": "b"},
            reason="探针通过", probe_result={"verdict": "pass"})
        assert isinstance(pid, int)
        proposals = store.list_proposals()
        assert len(proposals) == 1
        p = proposals[0]
        assert p["kind"] == "relation"
        assert p["direction"] == "add"
        assert p["review_status"] == "pending"

    def test_insert_proposal含tombstone字段(self, store):
        pid = store.insert_proposal(
            kind="relation", payload={"source": "a", "target": "b"},
            reason="墓碑命中", tombstone_hit=True, tombstone_note="2026-09-01 被拒")
        p = store.list_proposals()[0]
        assert p["tombstone_hit"] == 1
        assert p["tombstone_note"] == "2026-09-01 被拒"

    def test_insert_proposal_direction_retire(self, store):
        pid = store.insert_proposal(
            kind="relation", payload={"source": "a", "target": "b", "via": "x"},
            reason="mismatch N=3", direction="retire")
        p = store.list_proposals()[0]
        assert p["direction"] == "retire"

    def test_list_proposals按review_status过滤(self, store):
        store.insert_proposal("relation", {"s": "a"}, "r1")
        pid2 = store.insert_proposal("relation", {"s": "b"}, "r2")
        store.patch_proposal_review(pid2, "approved", reviewed_by="human")
        pending = store.list_proposals(review_status="pending")
        approved = store.list_proposals(review_status="approved")
        assert len(pending) == 1
        assert len(approved) == 1
        assert approved[0]["reviewed_by"] == "human"

    def test_patch_proposal_patch_status(self, store):
        pid = store.insert_proposal("relation", {"s": "a"}, "r")
        store.patch_proposal_patch_status(pid, "applied",
                                           regression_run_id="run-123",
                                           regression_ok=1)
        p = store.list_proposals()[0]
        assert p["patch_status"] == "applied"
        assert p["regression_run_id"] == "run-123"
        assert p["regression_ok"] == 1

    def test_软删proposal不出现在list(self, store):
        pid = store.insert_proposal("relation", {"s": "a"}, "r")
        store._conn.execute("UPDATE proposal SET status=0 WHERE id=?", (pid,))
        store._conn.commit()
        assert store.list_proposals() == []

    def test_patch_proposal_payload整体替换(self, store):
        """人审补 label 用 (2026-09-19): 调用方读出来改完整体写回。"""
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "via": "a.X=b.X"}, "r")
        store.patch_proposal_payload(
            pid, {"source": "a", "target": "b", "via": "a.X=b.X",
                  "label": "甲的乙"})
        p = store.list_proposals()[0]
        assert json.loads(p["payload_json"])["label"] == "甲的乙"
        assert json.loads(p["payload_json"])["source"] == "a"

    def test_patch_proposal_payload中文不转义(self, store):
        """与 insert_proposal 同口径 (ensure_ascii=False), 否则库里两种编码混着。"""
        pid = store.insert_proposal("relation", {"s": "a"}, "r")
        store.patch_proposal_payload(pid, {"label": "订单的变更记录"})
        raw = store.list_proposals()[0]["payload_json"]
        assert "订单的变更记录" in raw, f"中文被转义了: {raw}"


class TestRelationHealthCRUD:
    def test_upsert_relation_health累加(self, store):
        store.upsert_relation_health("a", "b", "a.x=b.y",
                                      use_count_delta=1, batch_id="batch-1")
        store.upsert_relation_health("a", "b", "a.x=b.y",
                                      use_count_delta=2, suspect_count_delta=1)
        h = store.get_relation_health("a", "b", "a.x=b.y")
        assert h["use_count"] == 3
        assert h["suspect_count"] == 1
        assert h["last_batch_id"] == "batch-1"  # 第二次没传 batch_id, 保留旧值

    def test_get_relation_health不存在返None(self, store):
        assert store.get_relation_health("x", "y", "z") is None

    def test_list_suspect_relations(self, store):
        store.upsert_relation_health("a", "b", "v1", suspect_count_delta=2)
        store.upsert_relation_health("c", "d", "v2", suspect_count_delta=0)
        store.upsert_relation_health("e", "f", "v3", suspect_count_delta=3)
        suspects = store.list_suspect_relations(min_suspect=1)
        assert len(suspects) == 2
        assert suspects[0]["suspect_count"] >= suspects[1]["suspect_count"]

    def test_update_mismatch_details累计(self, store):
        store.update_mismatch_details("a", "b", "a.x=b.y",
                                       col_pair="x=y", cq_id="cq-1")
        store.update_mismatch_details("a", "b", "a.x=b.y",
                                       col_pair="x=y", cq_id="cq-2")
        store.update_mismatch_details("a", "b", "a.x=b.y",
                                       col_pair="x=y", cq_id="cq-1")  # 重复 cq_id 不累加
        import json
        h = store.get_relation_health("a", "b", "a.x=b.y")
        details = json.loads(h["mismatch_details"])
        assert details["x=y"]["count"] == 3
        assert set(details["x=y"]["cq_ids"]) == {"cq-1", "cq-2"}


class TestSoftDeleteRelation:
    def test_软删边后load_raw不含(self, store):
        _seed_entity_a(store)
        _seed_entity_b(store)
        store.upsert_relation({
            "source": "lib_reader_order", "target": "lib_copy", "type": "ONE_TO_MANY",
            "source_col": "ORDER_ID", "target_col": "ORDER_ID", "label": "x"})
        via = "lib_reader_order.ORDER_ID=lib_copy.ORDER_ID"
        store.soft_delete_relation("lib_reader_order", "lib_copy", via)
        assert store.load_raw()["relationships"] == []

    def test_软删不存在的边不报错(self, store):
        store.soft_delete_relation("x", "y", "z")  # 不抛即通过
