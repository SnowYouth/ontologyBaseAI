"""scripts/init_ontology.py — 从脱敏 seed 建本体库 (冷启动入口)。

为什么存在: capabilities/ontology/ontology.db 不进 git (含真实业务表名),
clone 下来没有库, bootstrap 的 set_ontology() 会扑空 —— 项目是个跑不起来的壳。
本脚本从 seed_ontology.json 建一个图书馆域的示例本体, 让机制能跑给人看。

用法:
    uv run python scripts/init_ontology.py                 # 建到默认路径
    uv run python scripts/init_ontology.py --db /tmp/x.db   # 建到指定路径

★ failure_signal / proposal / relation_health / filter_rule_check 四表只建表不填数据:
  seed 展示的是"本体长什么样", 不是伪造"飞轮跑过"。留空才能让 clone 的人
  自己跑出第一条信号。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capabilities.ontology.ontology_store import OntologyStore  # noqa: E402

DEFAULT_SEED = PROJECT_ROOT / "scripts" / "seed_ontology.json"
DEFAULT_DB = PROJECT_ROOT / "capabilities" / "ontology" / "ontology.db"


def load_seed(path: str | Path) -> dict:
    """读 seed json。路径不存在直接抛, 不静默兜底 —— 冷启动失败要响亮。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def init_db(seed: dict, db_path: str) -> dict:
    """按 seed 建库, 返回各表写入计数。幂等: 全走 upsert, 跑两次结果一致。"""
    store = OntologyStore(db_path)
    try:
        for d in seed["domains"]:
            store.upsert_domain(d)
        for e in seed["entities"]:
            store.upsert_entity(e)
        for r in seed["relations"]:
            store.upsert_relation(r)
        for ev in seed["enum_values"]:
            store.upsert_enum_value(ev["entity"], ev["field"], ev)
        for fr in seed["filter_rules"]:
            store.upsert_filter_rule(fr)
        for item in seed["concepts"]:
            store.save_concept_with_conditions(item["concept"], item["conditions"])
        return {
            "domains": len(seed["domains"]),
            "entities": len(seed["entities"]),
            "relations": len(seed["relations"]),
            "enum_values": len(seed["enum_values"]),
            "filter_rules": len(seed["filter_rules"]),
            "concepts": len(seed["concepts"]),
        }
    finally:
        store.close()


def is_empty_db(db: Path) -> bool:
    """这个库文件有没有值得保护的内容? 判据 = entity 表一行都没有。

    为什么要有: 跑过测试的工作区会留下一个 entity 空表的 ontology.db
    (测试走 settings.obda_ontology_path 默认值, sqlite connect 自动建库),
    见文件存在就拒绝 → 先跑测试后建库的人拿到空库, 在后续步骤莫名失败。
    空库没什么可保护, 拒绝重建纯属误伤。

    判不准的一律算"非空"→ 拒绝: 不是 sqlite 的文件, 或有表但没有 entity 表
    (别人的库)。只有"entity 为空"和"一张表都没有"(零字节裸文件) 算空。
    """
    try:
        conn = sqlite3.connect(str(db))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                return True
            if "entity" not in tables:
                return False
            return conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="从脱敏 seed 建本体库")
    ap.add_argument("--seed", default=str(DEFAULT_SEED))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    db = Path(args.db)
    rebuilt_empty = False
    if db.exists():
        if not is_empty_db(db):
            print(f"[跳过] 库已存在且有数据: {db}\n  要重建请先删除它。", file=sys.stderr)
            return 1
        rebuilt_empty = True

    counts = init_db(load_seed(args.seed), str(db))
    if rebuilt_empty:
        print("[重建] 原库是空的 (entity 0 行, 多半是跑测试留下的), 已填入 seed")
    print(f"[完成] 建库 {db}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("\n飞轮四表 (failure_signal/proposal/relation_health/filter_rule_check) "
          "已建表但留空 —— 跑起来后自己产第一条信号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
