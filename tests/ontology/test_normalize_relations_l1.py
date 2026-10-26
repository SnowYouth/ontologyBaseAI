"""normalize_relations_l1 迁移脚本单测 — 造小库跑迁移, 断言 type/origin 正确 + 语义零损伤 + 幂等。"""
import json
import sqlite3

import pytest

from capabilities.ontology.normalize_relations_l1 import normalize_db, REL_TYPES, REL_ORIGINS
from capabilities.ontology.ontology_store import OntologyStore


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    store = OntologyStore(str(p))
    # 桥边: store 已锁 type 为 4 值拒 BRIDGE, 故先按合法 UNKNOWN 写入建全行,
    # 再直接 SQL 掰回 type='BRIDGE' —— 模拟"type 校验上线前遗留的脏桥边"
    # (真库里待归一的 BRIDGE 边本就是校验前写的, 这才是归一脚本的处理对象)
    store.upsert_relation({
        "source": "a", "target": "b", "via": "a.x=b.y", "type": "UNKNOWN",
        "source_col": "x", "target_col": "y", "bridge_count": 7, "note": "血肉备注",
    })
    # 手工边 (MANY_TO_ONE, 有 label)
    store.upsert_relation({
        "source": "c", "target": "d", "via": "c.CID", "type": "MANY_TO_ONE",
        "source_col": "CID", "target_col": "DID", "label": "手工关系",
    })
    store.close()
    con = sqlite3.connect(str(p))
    con.execute("UPDATE relation SET type='BRIDGE' WHERE source='a'")
    con.commit()
    con.close()
    return str(p)


def _rows(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM relation ORDER BY id").fetchall()
    con.close()
    return rows


def test_bridge边归一为unknown且origin_bridge(db):
    normalize_db(db)
    r = [x for x in _rows(db) if x["source"] == "a"][0]
    assert r["type"] == "UNKNOWN"
    assert json.loads(r["extra_json"])["origin"] == "bridge"


def test_manual边type不变且origin_manual(db):
    normalize_db(db)
    r = [x for x in _rows(db) if x["source"] == "c"][0]
    assert r["type"] == "MANY_TO_ONE"
    assert json.loads(r["extra_json"])["origin"] == "manual"


def test_语义血肉零损伤(db):
    normalize_db(db)
    a = [x for x in _rows(db) if x["source"] == "a"][0]
    assert json.loads(a["extra_json"])["note"] == "血肉备注"   # 原 extra 键不丢
    assert a["bridge_count"] == 7                              # 固定列不动
    c = [x for x in _rows(db) if x["source"] == "c"][0]
    assert c["label"] == "手工关系"


def test_幂等重跑结果不变(db):
    normalize_db(db)
    first = [(x["type"], x["extra_json"]) for x in _rows(db)]
    stats = normalize_db(db)
    second = [(x["type"], x["extra_json"]) for x in _rows(db)]
    assert first == second
    assert stats["bridge_to_unknown"] == 0   # 第二次没有 BRIDGE 可转
    assert stats["already_normalized"] == 2


def test_返回统计计数(db):
    stats = normalize_db(db)
    assert stats["bridge_to_unknown"] == 1
    assert stats["manual_tagged"] == 1
