from core.ontology_view import build_indexes
from capabilities.obqc.index import build_obqc_index


def _mini_raw():
    """两 entity 一条边的最小本体 dict (build_indexes 契约)。"""
    return {
        "entities": [
            {"id": "lib_loan", "tables": ["LIB_LOAN"], "data_source": "lib",
             "attributes": [{"name": "LIB_LOAN_ID"}, {"name": "READER_ID"}]},
            {"id": "lib_reader", "tables": ["LIB_READER"], "data_source": "lib2",
             "attributes": [{"name": "MEMBER_ID"}, {"name": "MEMBER_NAME"}]},
        ],
        "relationships": [
            {"source": "lib_loan", "target": "lib_reader",
             "source_col": "READER_ID", "target_col": "MEMBER_ID",
             "type": "MANY_TO_ONE", "status": 1},
        ],
        "valueSets": {}, "domains": [], "filterRules": [], "concepts": [],
    }


def test_物理表名到entity_按source分组():
    onto = build_indexes(_mini_raw())
    idx = build_obqc_index(onto)
    # lib 库的 LIB_LOAN → lib_loan; lib2 库的 LIB_READER → lib_reader
    assert idx.table_to_entity["lib"]["LIB_LOAN"] == "lib_loan"
    assert idx.table_to_entity["lib2"]["LIB_READER"] == "lib_reader"
    # 跨 source 不串: lib 库里没有 LIB_READER
    assert "LIB_READER" not in idx.table_to_entity.get("lib", {})


def test_边按无序端点对索引():
    onto = build_indexes(_mini_raw())
    idx = build_obqc_index(onto)
    key = frozenset({"lib_loan", "lib_reader"})
    assert key in idx.edges
    # 双向 adjacency 存了同一条边两次, 必须被 id(rel) 去重成一条
    assert len(idx.edges[key]) == 1
    edge = idx.edges[key][0]
    assert edge["source_col"] == "READER_ID"
    assert edge["target_col"] == "MEMBER_ID"


def test_表名大小写归一():
    """SQL 里表名可能大小写混写, 索引键统一大写便于比对。"""
    onto = build_indexes(_mini_raw())
    idx = build_obqc_index(onto)
    assert "LIB_LOAN" in idx.table_to_entity["lib"]


def test_无data_source的entity被跳过():
    """无 data_source 的 entity 无法定位库, 不进反向索引。"""
    raw = _mini_raw()
    raw["entities"].append(
        {"id": "orphan", "tables": ["ORPHAN_TBL"],
         "attributes": [{"name": "X"}]},  # 无 data_source 键
    )
    onto = build_indexes(raw)
    idx = build_obqc_index(onto)
    # orphan 的表不进任何 source 桶
    for source, bucket in idx.table_to_entity.items():
        assert "ORPHAN_TBL" not in bucket


def _raw_with_via():
    """带 via 的最小本体 (真库活边的 via 全非空, 索引必须带上)。"""
    raw = _mini_raw()
    raw["relationships"][0]["via"] = "lib_loan.READER_ID=lib_reader.MEMBER_ID"
    return raw


def test_边带via字段():
    """spec §3.3: 边身份是 (source, target, via) 三元组。

    索引丢了 via → used_edges 的 via 恒 None → relation_health 主键不完整
    → _bump_use_count 全量跳过 → use_count 恒 0 (2026-09-17 端到端取证挖出)。
    """
    onto = build_indexes(_raw_with_via())
    idx = build_obqc_index(onto)
    edge = idx.edges[frozenset({"lib_loan", "lib_reader"})][0]
    assert edge["via"] == "lib_loan.READER_ID=lib_reader.MEMBER_ID"


def test_边无via时为None不炸():
    """本体里 via 缺失 (老数据/手工插入) → 字段在但为 None, 不抛异常。"""
    onto = build_indexes(_mini_raw())
    idx = build_obqc_index(onto)
    edge = idx.edges[frozenset({"lib_loan", "lib_reader"})][0]
    assert "via" in edge
    assert edge["via"] is None
