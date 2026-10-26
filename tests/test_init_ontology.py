"""scripts/init_ontology.py — 脱敏 seed 建库 (冷启动)。

为什么要有: ontology.db 不进 git (含真实表名), clone 下来没库,
set_ontology() 必然扑空。seed 让项目能真跑起来。
"""
import json
import sqlite3
import sys
from pathlib import Path

from scripts.init_ontology import init_db, load_seed, main

SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_ontology.json"
SEED_CQ_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_cq.json"


def _run_main(monkeypatch, db: Path) -> int:
    monkeypatch.setattr(sys, "argv", ["init_ontology.py", "--db", str(db)])
    return main()


def _entity_rows(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    finally:
        conn.close()


def test_seed文件存在且是合法json():
    seed = load_seed(SEED_PATH)
    assert isinstance(seed, dict)
    assert seed["entities"], "seed 必须有实体"


def test_建库后各表行数符合seed(tmp_path):
    seed = load_seed(SEED_PATH)
    db = tmp_path / "counts.db"
    counts = init_db(seed, str(db))
    assert counts["domains"] == 3
    assert counts["entities"] == 6
    assert counts["relations"] == 8
    assert counts["enum_values"] == 4
    assert counts["filter_rules"] == 2
    assert counts["concepts"] == 2


def test_建库可被load_raw读回(tmp_path):
    from capabilities.ontology.ontology_store import OntologyStore

    seed = load_seed(SEED_PATH)
    db = tmp_path / "seed.db"
    init_db(seed, str(db))
    store = OntologyStore(str(db))
    try:
        raw = store.load_raw()
        assert len(raw["entities"]) == 6
        ids = {e["id"] for e in raw["entities"]}
        assert {"reader", "loan", "copy", "book", "branch", "publisher"} == ids
    finally:
        store.close()


def test_飞轮四表建了但留空(tmp_path):
    """seed 展示"本体长什么样", 不伪造"飞轮跑过" —— 留空才能让人跑出第一条信号。"""
    import sqlite3

    seed = load_seed(SEED_PATH)
    db = tmp_path / "seed.db"
    init_db(seed, str(db))
    conn = sqlite3.connect(str(db))
    try:
        for table in ("failure_signal", "proposal", "relation_health", "filter_rule_check"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} 应为空, 实为 {n} 行"
    finally:
        conn.close()


def test_三跳join链存在(tmp_path):
    """reader → loan → copy → book 是 3 跳, 用来压 get_join_path 跳数上限 2 的边界。"""
    from capabilities.ontology.ontology_store import OntologyStore

    seed = load_seed(SEED_PATH)
    db = tmp_path / "seed.db"
    init_db(seed, str(db))
    store = OntologyStore(str(db))
    try:
        raw = store.load_raw()
        pairs = {(r["source"], r["target"]) for r in raw["relationships"]}
        assert ("reader", "loan") in pairs
        assert ("loan", "copy") in pairs
        assert ("copy", "book") in pairs
    finally:
        store.close()


def test_幂等_跑两次行数不变(tmp_path):
    """★ 数真库行数, 不比 init_db 的返回值。

    返回值是 len(seed[...]) 算出来的, 两次调用必然相等 —— 拿它比等于没测,
    真写重了 (UNIQUE 键不对) 也照样绿。只有数 DB 才证得了幂等。
    """
    import sqlite3

    seed = load_seed(SEED_PATH)
    db = tmp_path / "seed.db"

    def _rows():
        conn = sqlite3.connect(str(db))
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("domain", "entity", "relation", "enum_value",
                              "filter_rule", "concept", "concept_condition")}
        finally:
            conn.close()

    first_counts = init_db(seed, str(db))
    first_rows = _rows()
    second_counts = init_db(seed, str(db))
    second_rows = _rows()

    assert first_counts == second_counts
    assert first_rows == second_rows, f"跑两次行数变了: {first_rows} → {second_rows}"
    assert first_rows == {"domain": 3, "entity": 6, "relation": 8, "enum_value": 4,
                          "filter_rule": 2, "concept": 2, "concept_condition": 3}


def test_seed不含真实业务表名():
    """脱敏闸: seed 里的表名必须是 lib_ 前缀的示例表。"""
    raw = SEED_PATH.read_text(encoding="utf-8")
    seed = json.loads(raw)
    for e in seed["entities"]:
        assert e["maps_to"].startswith("lib_"), f"{e['id']} 的 maps_to 不是示例表名"


def test_每个实体都有属性():
    """空壳实体 (无属性) 的 SELECT* 率是有属性的 23 倍 —— seed 不该示范这个。"""
    seed = load_seed(SEED_PATH)
    for e in seed["entities"]:
        assert e.get("attributes"), f"{e['id']} 没有属性"


def test_每条边都有label():
    """2026-09-19 装的非空闸: 裸边会让 get_join_path 选边时无区分度。"""
    seed = load_seed(SEED_PATH)
    for r in seed["relations"]:
        assert r.get("label"), f"{r['source']}→{r['target']} 无 label"


def test_概念条件都挂在已声明实体上():
    seed = load_seed(SEED_PATH)
    entity_ids = {e["id"] for e in seed["entities"]}
    for item in seed["concepts"]:
        for cc in item["conditions"]:
            assert cc["entity"] in entity_ids


# ── 空库陷阱 (2026-10-14): 测试跑完会留下 entity 表为空的 ontology.db,
#    原来见文件存在就 return 1 → 先跑测试后建库的人拿到空库, 在后续步骤莫名失败。
#    空库没有值得保护的内容, 拒绝重建纯属误伤; 有数据的库仍要拒绝。

def test_库不存在时正常建库(monkeypatch, tmp_path):
    db = tmp_path / "fresh.db"
    assert _run_main(monkeypatch, db) == 0
    assert _entity_rows(db) == 6


def test_entity表为空的库被覆盖重建(monkeypatch, tmp_path):
    """测试留下的空库长这样: schema 全在 (OntologyStore 建的), entity 0 行。"""
    from capabilities.ontology.ontology_store import OntologyStore

    db = tmp_path / "empty.db"
    OntologyStore(str(db)).close()
    assert _entity_rows(db) == 0

    assert _run_main(monkeypatch, db) == 0
    assert _entity_rows(db) == 6


def test_连entity表都没有的裸sqlite文件也被重建(monkeypatch, tmp_path):
    """裸 sqlite3.connect 建出的文件: 零张表。也算空, 该重建。"""
    db = tmp_path / "bare.db"
    sqlite3.connect(str(db)).close()

    assert _run_main(monkeypatch, db) == 0
    assert _entity_rows(db) == 6


def test_有数据的库仍然拒绝覆盖(monkeypatch, tmp_path):
    """★ 断言物理后果: 不只看返回码, 要证明库一字节没动。"""
    import hashlib

    db = tmp_path / "real.db"
    init_db(load_seed(SEED_PATH), str(db))
    before = hashlib.md5(db.read_bytes()).hexdigest()

    assert _run_main(monkeypatch, db) == 1
    assert hashlib.md5(db.read_bytes()).hexdigest() == before


def test_不是sqlite的文件拒绝覆盖(monkeypatch, tmp_path):
    """读不出表就当它有内容 —— 宁可拒绝, 不拿不认识的文件冒险。"""
    db = tmp_path / "garbage.db"
    db.write_bytes(b"this is not a database")

    assert _run_main(monkeypatch, db) == 1
    assert db.read_bytes() == b"this is not a database"


# ── 示例考题集 (scripts/seed_cq.json): 真考题集 kg_private/cq_set.json 是电信域且不进 git,
#    clone 的人照它跑出来全是"表不存在"。seed_cq 必须与 seed_ontology 同域 (图书馆)。

_CQ_CRITERIA_KEYS = {"点名", "走边", "值集", "断腿"}


def test_seed考题集与seed本体对得上():
    """★ 点名逐项必须是 seed 实体 id —— "至少命中一个"会放过写表名/写错实体的考题。"""
    import re

    seed = load_seed(SEED_PATH)
    entity_ids = {e["id"] for e in seed["entities"]}
    cases = json.loads(SEED_CQ_PATH.read_text(encoding="utf-8"))["cases"]

    assert len(cases) >= 3, f"示例考题至少 3 条, 实为 {len(cases)}"
    assert len({c["id"] for c in cases}) == len(cases), "考题 id 有重复"

    for c in cases:
        cid = c["id"]
        assert cid.strip(), "考题 id 不许为空"
        assert c["question"].strip(), f"{cid} question 为空"
        assert c["kind"] in ("single", "multi_turn"), f"{cid} kind={c['kind']} 非法"
        assert set(c["criteria"]) == _CQ_CRITERIA_KEYS, \
            f"{cid} 判据键集合是 {set(c['criteria'])}, 应恰为四类"

        for name in re.split(r"[,，]", c["criteria"]["点名"]):
            name = name.strip()
            assert name in entity_ids, \
                f"{cid} 点名的 {name!r} 不是 seed 实体 id (应在 {sorted(entity_ids)} 内)"

        if c["kind"] == "multi_turn":
            turns = c["turns"]
            assert isinstance(turns, list) and len(turns) >= 2, \
                f"{cid} 是 multi_turn, turns 应是 ≥2 轮的列表, 实为 {turns!r}"
            assert all(isinstance(t, str) and t.strip() for t in turns), \
                f"{cid} turns 里有空轮次"
        else:
            assert c["turns"] is None, f"{cid} 是 single, turns 应为 null"


def test_seed考题集不含真实业务标识符():
    """脱敏闸 (与 tests/test_repo_denylist.py 共用 _scan_text 判据, 黑名单只有哈希)。"""
    from tests.test_repo_denylist import _load_denylist, _scan_text

    raw = SEED_CQ_PATH.read_text(encoding="utf-8")
    hits = _scan_text(raw, _load_denylist())
    assert not hits, f"seed_cq.json 含 {len(hits)} 个黑名单标识符"
