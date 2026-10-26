"""tools/ontology_concept.py 测试: get_concept_detail 薄封装。"""
import core.ontology_view as onto_mod
from core.ontology_view import build_indexes
from agent.tools.ontology_concept import get_concept_detail


def _set_onto(monkeypatch, valid=1):
    onto = build_indexes({
        "entities": [
            {"id": "lib_loan", "displayName": "借阅实例", "maps_to": "读者",
             "tables": ["lib_loan"], "attributes": [], "summary": ""},
            {"id": "lib_loan_rel", "displayName": "主从号关系", "maps_to": "关系",
             "tables": ["lib_loan_rel"], "attributes": [], "summary": ""},
        ],
        "relationships": [], "valueSets": {}, "domains": [],
        "concepts": [{
            "id": "active_member", "display_name": "主号", "anchor_entity": "lib_loan",
            "summary": "A 端主号", "origin": "manual", "valid": valid,
            "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                            "op": "=", "value": "1000", "seq": 0}],
        }],
    })
    monkeypatch.setattr(onto_mod, "_ontology", onto)


def test_命中返回detail(monkeypatch):
    _set_onto(monkeypatch)
    d = get_concept_detail("active_member")
    assert d["anchor_entity"] == "lib_loan"
    assert d["involved_entities"] == ["lib_loan", "lib_loan_rel"]


def test_不存在概念报错(monkeypatch):
    _set_onto(monkeypatch)
    assert "error" in get_concept_detail("no_such")


def test_valid0概念报错(monkeypatch):
    _set_onto(monkeypatch, valid=0)
    assert "error" in get_concept_detail("active_member")


def test_单例未加载报错(monkeypatch):
    monkeypatch.setattr(onto_mod, "_ontology", None)
    assert get_concept_detail("active_member") == {"error": "ontology not initialized"}
