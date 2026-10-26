"""tests/selfevolve/test_signals.py — 热路径信号采集单测 (TDD).

覆盖: log_failure_signal 三种 kind / 异常不外抛 / rebuild_circuit_breaker_state。
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capabilities.ontology.ontology_store import OntologyStore
from capabilities.ontology.selfevolve.signals import (
    log_failure_signal,
    rebuild_circuit_breaker_state,
)


@pytest.fixture
def db_path(tmp_path):
    """创建一个有 schema 的空库，返回路径字符串。"""
    path = str(tmp_path / "test.db")
    store = OntologyStore(path)
    store.close()
    return path


class TestLogFailureSignal:
    def test_obqc_circuit_break写入(self, db_path):
        log_failure_signal("obqc_circuit_break",
                           {"sql_fingerprint": "abc123", "hit_count": 2},
                           source="lib", db_path=db_path)
        store = OntologyStore(db_path)
        signals = store.list_unprocessed_signals()
        store.close()
        assert len(signals) == 1
        assert signals[0]["kind"] == "obqc_circuit_break"
        payload = json.loads(signals[0]["payload_json"])
        assert payload["sql_fingerprint"] == "abc123"

    def test_max_iters写入(self, db_path):
        log_failure_signal("max_iters",
                           {"query": "test query", "iters": 10},
                           db_path=db_path)
        store = OntologyStore(db_path)
        signals = store.list_unprocessed_signals()
        store.close()
        assert len(signals) == 1
        assert signals[0]["kind"] == "max_iters"

    def test_cq_fail写入(self, db_path):
        log_failure_signal("cq_fail",
                           {"cq_id": "cq-1", "error": "boom"},
                           db_path=db_path)
        store = OntologyStore(db_path)
        signals = store.list_unprocessed_signals()
        store.close()
        assert len(signals) == 1
        assert signals[0]["kind"] == "cq_fail"

    def test_异常不外抛(self, tmp_path):
        """db_path 指向不存在的表 → 静默不抛。"""
        bad_path = str(tmp_path / "no_schema.db")
        # 创建一个空库但没建表
        import sqlite3
        conn = sqlite3.connect(bad_path)
        conn.close()
        # 不应抛异常
        log_failure_signal("cq_fail", {"x": 1}, db_path=bad_path)

    def test_无db_path不抛(self):
        """既没传 db_path 也没设默认 → 静默跳过。"""
        log_failure_signal("cq_fail", {"x": 1}, db_path=None)


class TestRebuildCircuitBreakerState:
    def test_从failure_signal重建(self, db_path):
        # 插两条 obqc_circuit_break + 一条 cq_fail（不该算进去）
        log_failure_signal("obqc_circuit_break",
                           {"sql_fingerprint": "fp1"}, db_path=db_path)
        log_failure_signal("obqc_circuit_break",
                           {"sql_fingerprint": "fp1"}, db_path=db_path)
        log_failure_signal("obqc_circuit_break",
                           {"sql_fingerprint": "fp2"}, db_path=db_path)
        log_failure_signal("cq_fail",
                           {"cq_id": "cq-1"}, db_path=db_path)
        state = rebuild_circuit_breaker_state(db_path)
        assert state == {"fp1": 2, "fp2": 1}

    def test_processed的不算(self, db_path):
        log_failure_signal("obqc_circuit_break",
                           {"sql_fingerprint": "fp1"}, db_path=db_path)
        # 标记为已处理
        store = OntologyStore(db_path)
        signals = store.list_unprocessed_signals()
        store.mark_signal_processed(signals[0]["id"])
        store.close()
        state = rebuild_circuit_breaker_state(db_path)
        assert state == {}

    def test_空库返空dict(self, db_path):
        assert rebuild_circuit_breaker_state(db_path) == {}


# ============ used_edges 三段链路 + no_edge 补列对 ============

class TestUsedEdgesChain:
    """验证 obqc_check 返回值含 used_edges (段①)。"""

    def test_obqc_check返回值含used_edges(self):
        """匹配的边应出现在 used_edges 里。"""
        from core.ontology_view import Ontology, build_indexes
        raw = {
            "entities": [
                {"id": "order", "entityCode": "ORDER", "displayName": "订单",
                 "maps_to": "ORDER_TBL", "data_source": "lib",
                 "tables": ["ORDER_TBL"],
                 "attributes": [{"name": "ORDER_ID", "kind": "pk"}]},
                {"id": "item", "entityCode": "ITEM", "displayName": "订单项",
                 "maps_to": "ITEM_TBL", "data_source": "lib",
                 "tables": ["ITEM_TBL"],
                 "attributes": [{"name": "ORDER_ID"}]},
            ],
            "relationships": [
                {"source": "order", "target": "item", "type": "ONE_TO_MANY",
                 "via": "order.ORDER_ID=item.ORDER_ID",
                 "source_col": "ORDER_ID", "target_col": "ORDER_ID"},
            ],
            "valueSets": {},
            "domains": [],
            "filterRules": [],
            "concepts": [],
        }
        onto = build_indexes(raw)
        from capabilities.obqc.check import obqc_check
        result = obqc_check(
            "SELECT o.ORDER_ID FROM ORDER_TBL o JOIN ITEM_TBL i ON o.ORDER_ID = i.ORDER_ID",
            "lib", onto)
        assert result["verdict"] != "hard_block"
        assert "used_edges" in result
        assert len(result["used_edges"]) == 1
        edge = result["used_edges"][0]
        assert edge["source"] == "order"
        assert edge["target"] == "item"

    def test_no_edge_hint含cols(self):
        """no_edge 分支的 hint 应含 cols 字段 (spec §3.5 R-7)。"""
        from core.ontology_view import build_indexes
        raw = {
            "entities": [
                {"id": "order", "entityCode": "ORDER", "displayName": "订单",
                 "maps_to": "ORDER_TBL", "data_source": "lib",
                 "tables": ["ORDER_TBL"], "attributes": []},
                {"id": "payment", "entityCode": "PAYMENT", "displayName": "付款",
                 "maps_to": "PAYMENT_TBL", "data_source": "lib",
                 "tables": ["PAYMENT_TBL"], "attributes": []},
            ],
            "relationships": [],  # 无边
            "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
        }
        onto = build_indexes(raw)
        from capabilities.obqc.check import obqc_check
        result = obqc_check(
            "SELECT * FROM ORDER_TBL o JOIN PAYMENT_TBL p ON o.ORDER_ID = p.ORDER_ID",
            "lib", onto)
        assert result["verdict"] == "hard_block"
        no_edge_hints = [h for h in result["hints"] if h["kind"] == "no_edge"]
        assert len(no_edge_hints) == 1
        assert "cols" in no_edge_hints[0]
        assert no_edge_hints[0]["cols"] == ["ORDER_ID", "ORDER_ID"]

    def test_obqc行为零变化_violation数量不变(self):
        """★ OBQC 行为零变化回归: violation 类型/数量/hard_block 判定全不变。"""
        from core.ontology_view import build_indexes
        raw = {
            "entities": [
                {"id": "order", "entityCode": "ORDER", "displayName": "订单",
                 "maps_to": "ORDER_TBL", "data_source": "lib",
                 "tables": ["ORDER_TBL"], "attributes": []},
                {"id": "payment", "entityCode": "PAYMENT", "displayName": "付款",
                 "maps_to": "PAYMENT_TBL", "data_source": "lib",
                 "tables": ["PAYMENT_TBL"], "attributes": []},
            ],
            "relationships": [],
            "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
        }
        onto = build_indexes(raw)
        from capabilities.obqc.check import obqc_check
        result = obqc_check(
            "SELECT * FROM ORDER_TBL o JOIN PAYMENT_TBL p ON o.ORDER_ID = p.ORDER_ID",
            "lib", onto)
        # 一条 no_edge violation
        assert result["verdict"] == "hard_block"
        assert len(result["violations"]) == 1
        assert result["violations"][0]["type"] == "no_edge"


class TestCollectOnEventUsedEdges:
    """验证 _collect_on_event 收集 used_edges (段③)。"""

    def test_tool_end带used_edges收进记录(self):
        from core.runtime.cq_runner import _collect_on_event
        events: list[dict] = []
        cb = _collect_on_event(events, {"truncated": False})
        cb({"type": "tool_start", "name": "execute_sql"})
        cb({"type": "tool_end", "name": "execute_sql",
            "result": "ok", "latency_ms": 10, "success": True,
            "used_edges": [{"source": "a", "target": "b"}]})
        assert len(events) == 1
        assert events[0]["used_edges"] == [{"source": "a", "target": "b"}]

    def test_tool_end无used_edges则None(self):
        from core.runtime.cq_runner import _collect_on_event
        events: list[dict] = []
        cb = _collect_on_event(events, {"truncated": False})
        cb({"type": "tool_start", "name": "foo"})
        cb({"type": "tool_end", "name": "foo",
            "result": "ok", "latency_ms": 5, "success": True})
        assert events[0]["used_edges"] is None
