"""build_concept_join 单测 (纯函数, 不碰真库)。"""
from core.ontology_view import Ontology, build_concept_join


def _mk_onto(concepts=None, entities=None, edges=None):
    """A(anchor)→B→C 两跳 + A→D→C 两跳备选(D 路无 label)。"""
    edges = edges if edges is not None else [
        {"source": "A", "target": "B", "via": "A.AID=B.AID",
         "source_col": "AID", "target_col": "AID", "type": "ONE_TO_MANY",
         "label": "A到B", "note": "避坑口径", "bridge_count": 100},
        {"source": "B", "target": "C", "via": "B.CID=C.CID",
         "source_col": "CID", "target_col": "CID", "type": "ONE_TO_ONE",
         "label": "B到C", "note": None, "bridge_count": 50},
        {"source": "A", "target": "D", "via": "A.DID=D.DID",
         "source_col": "DID", "target_col": "DID", "type": "ONE_TO_MANY",
         "label": None, "note": None, "bridge_count": 9},
        {"source": "D", "target": "C", "via": "D.CID=C.CID",
         "source_col": "CID", "target_col": "CID", "type": "ONE_TO_MANY",
         "label": None, "note": None, "bridge_count": 9},
    ]
    adjacency: dict = {}
    for e in edges:
        adjacency.setdefault(e["source"], []).append(e)
        adjacency.setdefault(e["target"], []).append(e)
    ents = entities if entities is not None else {
        "A": {"id": "A", "data_source": "lib",
              "attributes": [{"name": "AID", "kind": "pk"}]},
        "B": {"id": "B", "data_source": "lib", "attributes": []},
        "C": {"id": "C", "data_source": "lib", "attributes": []},
        "D": {"id": "D", "data_source": "lib", "attributes": []},
    }
    cs = concepts if concepts is not None else {
        "vip": {"id": "vip", "anchor_entity": "A", "valid": 1,
                "conditions": [{"entity": "A", "col": "LVL", "op": "=",
                                "value": "9", "kind": "column"}]},
    }
    # 真实 Ontology dataclass 无 relations 字段且 domains 必填 (见 core/ontology.py:16-24)。
    # 照 Task 1 做法适配: 构造补 domains=[], relations 构造后挂实例属性。
    onto = Ontology(entities=ents, adjacency=adjacency, value_sets={},
                    domains=[], filter_rules={}, concepts=cs)
    onto.relations = edges
    return onto


def test_正例_返回把手路径与备选():
    r = build_concept_join(_mk_onto(), "vip", "C")
    assert "error" not in r
    assert r["concept_id"] == "vip"
    assert r["target_entity"] == "C"
    assert r["output_key"] == {"entity": "A", "col": "AID"}
    assert r["path"]["hops"] == 2
    assert [e["to"] for e in r["path"]["edges"]] == ["B", "C"]
    assert r["path"]["segments"], "同库也须给 segments (单段)"
    assert r["alternatives"] == [{"hops": 2, "through": ["D"]}]


def test_排序_语义边多的当path():
    """A→B→C 两边都有 label, A→D→C 都没有; 前者必须当 path。"""
    r = build_concept_join(_mk_onto(), "vip", "C")
    assert [e["to"] for e in r["path"]["edges"]] == ["B", "C"]
    assert r["alternatives"][0]["through"] == ["D"]


def test_备选截断到5条并告知():
    """造 7 条 A→中转→C 的路, 断言只返 5 条备选 + truncated=1 (共6备选)。"""
    edges = [
        {"source": "A", "target": "B", "via": "A.AID=B.AID",
         "source_col": "AID", "target_col": "AID", "type": "ONE_TO_MANY",
         "label": "A到B", "note": "正路", "bridge_count": 100},
        {"source": "B", "target": "C", "via": "B.CID=C.CID",
         "source_col": "CID", "target_col": "CID", "type": "ONE_TO_ONE",
         "label": "B到C", "note": None, "bridge_count": 50},
    ]
    ents = {"A": {"id": "A", "data_source": "lib",
                  "attributes": [{"name": "AID", "kind": "pk"}]},
            "B": {"id": "B", "data_source": "lib", "attributes": []},
            "C": {"id": "C", "data_source": "lib", "attributes": []}}
    for i in range(6):
        mid = f"M{i}"
        ents[mid] = {"id": mid, "data_source": "lib", "attributes": []}
        edges.append({"source": "A", "target": mid, "via": f"A.X{i}=M{i}.X{i}",
                      "source_col": f"X{i}", "target_col": f"X{i}",
                      "type": "ONE_TO_MANY", "label": None, "note": None,
                      "bridge_count": 5})
        edges.append({"source": mid, "target": "C", "via": f"M{i}.CID=C.CID",
                      "source_col": "CID", "target_col": "CID",
                      "type": "ONE_TO_MANY", "label": None, "note": None,
                      "bridge_count": 5})
    r = build_concept_join(_mk_onto(entities=ents, edges=edges), "vip", "C")
    assert [e["to"] for e in r["path"]["edges"]] == ["B", "C"]
    assert len(r["alternatives"]) == 5
    assert r["alternatives_truncated"] == 1


def test_零跳_把手已在目标实体():
    r = build_concept_join(_mk_onto(), "vip", "A")
    assert "error" not in r
    assert r["path"] == {"hops": 0, "edges": [], "segments": []}
    assert "无需 JOIN" in r["path_note"]


def test_失败_概念不存在():
    r = build_concept_join(_mk_onto(), "nope", "C")
    assert r["error"] == "概念不存在: nope"


def test_失败_概念断腿valid非1():
    cs = {"broken": {"id": "broken", "anchor_entity": "A", "valid": 0,
                     "conditions": [{"entity": "A", "col": "LVL", "op": "=",
                                     "value": "9", "kind": "column"}]}}
    r = build_concept_join(_mk_onto(concepts=cs), "broken", "C")
    assert "断腿" in r["error"] and "broken" in r["error"]


def test_失败_目标实体不存在():
    r = build_concept_join(_mk_onto(), "vip", "ZZZ")
    assert r["error"] == "目标实体不存在: ZZZ"


def test_失败_anchor无主键无把手():
    """anchor 的 attributes 无 kind=pk → output_key.col 空 → 无法缝合。"""
    ents = {"A": {"id": "A", "data_source": "lib", "attributes": []},
            "B": {"id": "B", "data_source": "lib", "attributes": []},
            "C": {"id": "C", "data_source": "lib", "attributes": []},
            "D": {"id": "D", "data_source": "lib", "attributes": []}}
    r = build_concept_join(_mk_onto(entities=ents), "vip", "C")
    assert "无主键标记" in r["error"]
    assert "get_concept_detail" in r["error"]


def test_失败_跳数内无路径且提示可调大():
    r = build_concept_join(_mk_onto(), "vip", "C", max_hops=1)
    assert "在 1 跳内无 JOIN 路径" in r["error"]
    assert "max_hops" in r["error"]


def test_跨库_segments切两段带接力列():
    """A(lib)→B(lib)→C(lib2): 跨库边处切段, exports/needs 带连接列。"""
    ents = {"A": {"id": "A", "data_source": "lib",
                  "attributes": [{"name": "AID", "kind": "pk"}]},
            "B": {"id": "B", "data_source": "lib", "attributes": []},
            "C": {"id": "C", "data_source": "lib2", "attributes": []},
            "D": {"id": "D", "data_source": "lib2", "attributes": []}}
    r = build_concept_join(_mk_onto(entities=ents), "vip", "C")
    segs = r["path"]["segments"]
    assert [s["source"] for s in segs] == ["lib", "lib2"]
    assert segs[0]["exports"] == ["CID"]
    assert segs[1]["needs"] == ["CID"]
