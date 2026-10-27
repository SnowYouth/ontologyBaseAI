"""归档实体 X_his 继承 X 的 JOIN 边 (core.ontology_view._build_archive_edges)。

CQ-13 (2026-10-27): 归档明细实体缺在用实体登记过的边, agent 写
`归档明细 JOIN 号码实例` 被 obqc no_edge 拦, 只能拆两段手工对齐, 挂错账户。
"""
from core.ontology_view import build_indexes, find_join_paths, find_join_paths_multi
from capabilities.obqc.check import obqc_check


def _raw(extra_rels=None, his_attrs=("ITEM_ID", "PHONE", "ORDER_ID")):
    def attrs(names, pk="ITEM_ID"):
        return [{"name": n, **({"kind": "pk"} if n == pk else {})} for n in names]
    return {
        "entities": [
            {"id": "item", "tables": ["ITEM"], "data_source": "s",
             "attributes": attrs(["ITEM_ID", "PHONE", "ORDER_ID", "OLD_ITEM_ID"])},
            {"id": "item_his", "tables": ["ITEM_HIS"], "data_source": "s",
             "attributes": attrs(his_attrs)},
            {"id": "inst", "tables": ["INST"], "data_source": "s",
             "attributes": attrs(["INST_KEY", "PHONE_NO"], pk="INST_KEY")},
            {"id": "order", "tables": ["ORD"], "data_source": "s",
             "attributes": attrs(["ORDER_ID"], pk="ORDER_ID")},
            {"id": "noise", "tables": ["NOISE"], "data_source": "s",
             "attributes": attrs(["ITEM_ID"], pk="ITEM_ID")},
        ],
        "relationships": [
            {"source": "item", "target": "inst", "source_col": "PHONE",
             "target_col": "PHONE_NO", "via": "item.PHONE=inst.PHONE_NO", "label": "操作的号码"},
            {"source": "item", "target": "order", "source_col": "ORDER_ID",
             "target_col": "ORDER_ID", "via": "item.ORDER_ID=order.ORDER_ID", "label": "所属订单"},
            {"source": "item", "target": "item", "source_col": "OLD_ITEM_ID",
             "target_col": "ITEM_ID", "via": "item.OLD_ITEM_ID=item.ITEM_ID", "label": "自关联"},
            {"source": "noise", "target": "item", "source_col": "ITEM_ID",
             "target_col": "ITEM_ID", "via": "noise.ITEM_ID=item.ITEM_ID", "label": None},
            *(extra_rels or []),
        ],
        "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
    }


def test_归档实体继承在用实体的带label边_get_join_path能找到():
    paths = find_join_paths(build_indexes(_raw()), "item_his", "inst")
    assert len(paths) == 1
    p = paths[0]
    assert (p["source_col"], p["target_col"]) == ("PHONE", "PHONE_NO")
    assert "归档" in p["label"]


def test_反向查询也通_列对随方向交换():
    p = find_join_paths(build_indexes(_raw()), "inst", "item_his")[0]
    assert (p["source_col"], p["target_col"]) == ("PHONE_NO", "PHONE")


def test_obqc放行继承边且不计入used_edges():
    """继承边不是 relation 表的行: 放行, 但不进 use_count / 嫌疑归因。"""
    sql = "SELECT h.ITEM_ID FROM ITEM_HIS h JOIN INST p ON p.PHONE_NO = h.PHONE"
    r = obqc_check(sql, "s", build_indexes(_raw()))
    assert r["verdict"] == "pass"
    assert not r.get("used_edges")


def test_obqc对继承边仍校验列对():
    sql = "SELECT h.ITEM_ID FROM ITEM_HIS h JOIN INST p ON p.INST_KEY = h.ITEM_ID"
    r = obqc_check(sql, "s", build_indexes(_raw()))
    assert r["verdict"] == "hard_block"
    assert any(v["type"] == "join_col_mismatch" for v in r["violations"])


def test_在用与归档两半有同名主键边():
    sql = "SELECT COUNT(*) FROM ITEM a JOIN ITEM_HIS b ON a.ITEM_ID = b.ITEM_ID"
    assert obqc_check(sql, "s", build_indexes(_raw()))["verdict"] == "pass"


def test_归档表缺连接列_不继承():
    onto = build_indexes(_raw(his_attrs=("ITEM_ID", "ORDER_ID")))
    assert find_join_paths(onto, "item_his", "inst") == []
    assert find_join_paths(onto, "item_his", "order")  # 有列的那条照常继承


def test_裸边与自关联边不继承():
    onto = build_indexes(_raw())
    assert find_join_paths(onto, "item_his", "noise") == []
    assert not any(e["source"] == e["target"] for es in onto.archive_edges.values() for e in es)


def test_已有显式登记边时以登记为准():
    explicit = {"source": "item_his", "target": "order", "source_col": "ORDER_ID",
                "target_col": "ORDER_ID", "via": "item_his.ORDER_ID=order.ORDER_ID",
                "label": "人审登记的归档边"}
    paths = find_join_paths(build_indexes(_raw([explicit])), "item_his", "order")
    assert [p["label"] for p in paths] == ["人审登记的归档边"]


def test_继承边不进adjacency_关系列表与轻卡不变():
    onto = build_indexes(_raw())
    assert all(e["target"] != "inst" for e in onto.adjacency["item_his"])


def test_多跳寻路也走继承边():
    hops = find_join_paths_multi(build_indexes(_raw()), "inst", "order", 2)
    assert any(e["to"] == "order" for p in hops for e in p["edges"])
    hops_his = [p for p in hops if any(e["to"] == "item_his" for e in p["edges"])]
    assert hops_his and hops_his[0]["hops"] == 2


def test_没登记归档实体_不造边():
    raw = _raw()
    raw["entities"] = [e for e in raw["entities"] if e["id"] != "item_his"]
    assert build_indexes(raw).archive_edges == {}
