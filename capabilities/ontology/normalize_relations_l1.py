"""capabilities/ontology/normalize_relations_l1.py — 关系层 L1 结构归一 (原地 UPDATE, 幂等).

把 relation.type 从"来源+基数"双维度重载归一:
- type='BRIDGE'  → type='UNKNOWN', extra_json 合并 {"origin":"bridge"}
- type ∈ 3 基数值 → type 不变,   extra_json 合并 {"origin":"manual"}

原地改真库, 不重跑 migrate_to_store (那会从 JSON 源重建, 冲掉语义血肉)。
extra_json 用 load-merge-write 不覆盖已有键。幂等: 已 UNKNOWN 且已有 origin 的不再动。

命令行: uv run python -m capabilities.ontology.normalize_relations_l1 <db_path>
"""
from __future__ import annotations

import json
import sqlite3
import sys

REL_TYPES = ("ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "UNKNOWN")
REL_ORIGINS = ("manual", "bridge")

_CARDINALITY = {"ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE"}


def _merge_origin(extra_json: str, origin: str) -> str:
    """load-merge-write: 已有 origin 不覆盖, 其它键保留。"""
    d = json.loads(extra_json or "{}")
    d.setdefault("origin", origin)
    return json.dumps(d, ensure_ascii=False)


def normalize_db(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    stats = {"bridge_to_unknown": 0, "manual_tagged": 0, "already_normalized": 0}
    try:
        rows = con.execute(
            "SELECT id, type, extra_json FROM relation WHERE status=1"
        ).fetchall()
        for r in rows:
            typ = r["type"]
            has_origin = "origin" in json.loads(r["extra_json"] or "{}")
            if typ == "BRIDGE":
                new_extra = _merge_origin(r["extra_json"], "bridge")
                con.execute(
                    "UPDATE relation SET type='UNKNOWN', extra_json=? WHERE id=?",
                    (new_extra, r["id"]),
                )
                stats["bridge_to_unknown"] += 1
            elif typ in _CARDINALITY:
                if has_origin:
                    stats["already_normalized"] += 1
                    continue
                new_extra = _merge_origin(r["extra_json"], "manual")
                con.execute(
                    "UPDATE relation SET extra_json=? WHERE id=?",
                    (new_extra, r["id"]),
                )
                stats["manual_tagged"] += 1
            else:  # 已是 UNKNOWN 或其它
                stats["already_normalized"] += 1
        con.commit()
    finally:
        con.close()
    return stats


def main() -> None:
    if len(sys.argv) != 2:
        print("用法: uv run python -m capabilities.ontology.normalize_relations_l1 <db_path>")
        raise SystemExit(2)
    stats = normalize_db(sys.argv[1])
    print("归一完成:", json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
