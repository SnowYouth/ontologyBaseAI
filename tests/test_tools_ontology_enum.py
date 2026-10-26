"""tools/ontology_enum.py 单元测试 — get_enum_values 按 (entity,field) 拉码表。"""
import pytest


@pytest.fixture(autouse=True)
def _reset_ontology():
    import core.ontology_view as mod
    mod._ontology = None
    yield
    mod._ontology = None


def _inject():
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
            ("lib_loan", "TYPE_CD"): {
                "name": "服务状态", "spec_ref": "LIB-ST-01",
                "codes": [{"code": "300005", "name": "双停", "desc": None}]},
        },
    }
    import core.ontology_view as mod
    mod._ontology = build_indexes(raw)


class TestGetEnumValues:
    def test_未初始化返回error(self):
        from agent.tools.ontology_enum import get_enum_values
        result = get_enum_values(entity="lib_loan", fields=["REC_STATUS"])
        assert "error" in result

    def test_单字段命中返回码表(self):
        _inject()
        from agent.tools.ontology_enum import get_enum_values
        result = get_enum_values(entity="lib_loan", fields=["REC_STATUS"])
        items = result["results"]
        assert len(items) == 1
        assert items[0]["entity"] == "lib_loan"
        assert items[0]["field"] == "REC_STATUS"
        assert items[0]["name"] == "有效性状态"
        assert items[0]["spec_ref"] == "REC_STATUS"
        assert items[0]["codes"][0]["code"] == "001"

    def test_多字段一次拉(self):
        _inject()
        from agent.tools.ontology_enum import get_enum_values
        result = get_enum_values(entity="lib_loan", fields=["REC_STATUS", "TYPE_CD"])
        assert len(result["results"]) == 2
        fields = {r["field"] for r in result["results"]}
        assert fields == {"REC_STATUS", "TYPE_CD"}

    def test_缺失字段局部报错不整体崩(self):
        _inject()
        from agent.tools.ontology_enum import get_enum_values
        result = get_enum_values(entity="lib_loan", fields=["REC_STATUS", "NOPE"])
        items = result["results"]
        assert len(items) == 2
        ok = next(r for r in items if r["field"] == "REC_STATUS")
        assert "codes" in ok
        miss = next(r for r in items if r["field"] == "NOPE")
        assert miss["error"] == "no enum registered"
