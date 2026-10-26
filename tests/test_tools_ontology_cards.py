"""tools/ontology_cards.py 单元测试 — mock core.ontology_view 单例, 不依赖真实文件."""
import pytest


@pytest.fixture(autouse=True)
def _reset_ontology():
    """每个测试前后清掉模块级 _ontology, 避免污染."""
    import core.ontology_view as mod
    mod._ontology = None
    yield
    mod._ontology = None


def _inject_mini_ontology():
    """建一个含 2 实体的迷你本体并注入单例, 返回其 Ontology 对象."""
    from core.ontology_view import build_indexes
    raw = {
        "domains": [],
        "entities": [
            {
                "id": "e1", "entityCode": "E1", "displayName": "实体一",
                "domain": "d", "maps_to": "m", "tables": ["t1"],
                "attributes": [{"name": "ID", "kind": "pk", "type": "bigint"}],
                "summary": "测试实体一",
            },
        ],
        "relationships": [],
        "valueSets": {},
    }
    onto = build_indexes(raw)
    import core.ontology_view as mod
    mod._ontology = onto
    return onto


class TestGetEntityCards:
    def test_ontology未初始化_返回error(self):
        from agent.tools.ontology_cards import get_entity_cards
        result = get_entity_cards(ids=["e1"])
        assert "error" in result
        assert "ontology" in result["error"].lower()

    def test_命中已知id_正常返回卡片(self):
        _inject_mini_ontology()
        from agent.tools.ontology_cards import get_entity_cards
        result = get_entity_cards(ids=["e1"])
        assert len(result["cards"]) == 1
        assert result["cards"][0]["id"] == "e1"
        assert "unknown_ids" not in result

    def test_命中不存在的id_附加unknown_ids不炸(self):
        _inject_mini_ontology()
        from agent.tools.ontology_cards import get_entity_cards
        result = get_entity_cards(ids=["e1", "not_exist"])
        assert result["unknown_ids"] == ["not_exist"]
        assert len(result["cards"]) == 1  # e1 正常处理

    def test_全部id都不存在_cards为空但不炸(self):
        _inject_mini_ontology()
        from agent.tools.ontology_cards import get_entity_cards
        result = get_entity_cards(ids=["ghost"])
        assert result["cards"] == []
        assert result["unknown_ids"] == ["ghost"]


class TestGetEntityCardsLogging:
    """埋点: get_entity_cards 落 ontology.cards JSONL (照 sql.query 哲学: 落信号不落全量)."""

    def _capture(self, monkeypatch):
        """monkeypatch tools.ontology_cards.log_event, 返回捕获列表."""
        events = []
        import agent.tools.ontology_cards as mod
        monkeypatch.setattr(mod, "log_event", lambda name, payload: events.append((name, payload)))
        return events

    def test_正常命中_落ontology_cards埋点_含点名与计数(self, monkeypatch):
        _inject_mini_ontology()
        events = self._capture(monkeypatch)
        from agent.tools.ontology_cards import get_entity_cards
        get_entity_cards(ids=["e1"])

        assert len(events) == 1
        name, payload = events[0]
        assert name == "ontology.cards"
        assert payload["requested"] == ["e1"]
        assert payload["known"] == ["e1"]
        assert payload["unknown"] == []
        assert payload["n_cards"] == 1
        # 轻卡计数: related 邻居数 + notes 数 (mini 本体单实体无边, 都是 0)
        assert payload["n_related"] == 0
        assert payload["n_notes"] == 0

    def test_含unknown_id_埋点记录unknown(self, monkeypatch):
        _inject_mini_ontology()
        events = self._capture(monkeypatch)
        from agent.tools.ontology_cards import get_entity_cards
        get_entity_cards(ids=["e1", "ghost"])

        _, payload = events[0]
        assert payload["unknown"] == ["ghost"]
        assert payload["known"] == ["e1"]

    def test_ontology未初始化_不落埋点(self, monkeypatch):
        events = self._capture(monkeypatch)
        from agent.tools.ontology_cards import get_entity_cards
        get_entity_cards(ids=["e1"])
        # 未初始化直接返回 error, 没查到任何东西, 不落 cards 埋点
        assert events == []
