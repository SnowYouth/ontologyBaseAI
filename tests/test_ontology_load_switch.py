"""tests/test_ontology_load_switch.py — set_ontology 按后缀分流 (阶段C).

.json → 旧 json.load 逻辑 (向后兼容, 零回归);
.db/.sqlite → capabilities.ontology.OntologyStore.load_raw()。
下游 build_indexes / 单例结构不变。
"""
import json

import pytest


@pytest.fixture(autouse=True)
def _reset_ontology():
    import core.ontology_view as mod
    mod._ontology = None
    mod._ontology_path = None
    yield
    mod._ontology = None
    mod._ontology_path = None


_MINI_RAW = {
    "domains": [{"code": "域一", "color": "#111"}],
    "entities": [
        {
            "id": "e1", "entityCode": "E1", "displayName": "实体一",
            "domain": "域一", "maps_to": "m", "tables": ["t1"],
            "attributes": [{"name": "ID", "kind": "pk", "type": "bigint"}],
            "summary": "测试实体一",
        },
    ],
    "relationships": [
        {"source": "e1", "target": "e2", "type": "UNKNOWN",
         "via": "t1.a=t2.b", "source_col": "a", "target_col": "b",
         "bridge_count": 7},
    ],
    "valueSets": {"VS-1": {"name": "码值", "sheet": "s", "codes": []}},
}


def test_json后缀_走旧逻辑_零回归(tmp_path):
    from core.ontology_view import get_ontology, set_ontology
    p = tmp_path / "onto.json"
    p.write_text(json.dumps(_MINI_RAW, ensure_ascii=False), encoding="utf-8")
    set_ontology(str(p))
    onto = get_ontology()
    assert "e1" in onto.entities
    assert onto.domains[0]["code"] == "域一"


def test_db后缀_走OntologyStore(tmp_path):
    from core.ontology_view import get_ontology, set_ontology
    from capabilities.ontology.ontology_store import OntologyStore
    db = tmp_path / "onto.db"
    store = OntologyStore(str(db))
    store.upsert_domain({"code": "域一", "color": "#111"})
    store.upsert_entity(_MINI_RAW["entities"][0])
    store.upsert_relation(_MINI_RAW["relationships"][0])
    store.close()

    set_ontology(str(db))
    onto = get_ontology()
    assert "e1" in onto.entities
    # 桥边 JOIN 字段对经 store 往返仍在
    edge = onto.adjacency["e1"][0]
    assert edge["source_col"] == "a"
    assert edge["bridge_count"] == 7


def test_sqlite后缀也走store(tmp_path):
    from core.ontology_view import get_ontology, set_ontology
    from capabilities.ontology.ontology_store import OntologyStore
    db = tmp_path / "onto.sqlite"
    store = OntologyStore(str(db))
    store.upsert_entity(_MINI_RAW["entities"][0])
    store.close()
    set_ontology(str(db))
    assert "e1" in get_ontology().entities


def test_set_ontology_记录运行时加载路径(tmp_path):
    """set_ontology 后 get_ontology_path() 返回真实加载路径 (供审计标签用, 非 settings 声明值)。"""
    from core.ontology_view import get_ontology, get_ontology_path, set_ontology
    p = tmp_path / "onto.json"
    p.write_text(json.dumps(_MINI_RAW, ensure_ascii=False), encoding="utf-8")
    set_ontology(str(p))
    assert get_ontology_path() == str(p)


def test_get_ontology_path_未加载返回None():
    from core.ontology_view import get_ontology_path
    assert get_ontology_path() is None
