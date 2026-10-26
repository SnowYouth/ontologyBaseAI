"""smoke: get_concept_join 端到端真库验证 + 反证。

跑法: uv run python tests/manual/smoke_concept_join.py
需 .env 配好数据源 lib (DB_SOURCES 含 lib + DB_LIB_* 六项)。种概念改测试库副本, 不污染 capabilities/ontology/ontology.db。

验五件事:
1. ★债的实证: 老 find_join_paths 直连一跳答不出, find_join_paths_multi 多跳能
   (本体若日后补了 lib_loan→lib_book 直连边, ①和②的 hops>=2 会响亮失败 ——
   届时此 smoke 的债证使命已完成, 可退役或换靶子实体对)
2. 一次调用就返出完整多跳路径 (不用 LLM 自己拼两次 get_join_path)
3. 拿 path.edges 的字段拼出的 SQL 真能跑出数
4. ★反证: JOIN 命中的 anchor 行必须是裸表全量的**真子集** (防"语法通过业务空转")
5. 响亮失败: 目标实体不存在要报错而非返空

★三处刻意偏离计划书 (都有真库探针实证, 见 notes):
- 计划假设权威库里有 valid=1 概念 → 实测 concepts 为空, 改为照 smoke_concept_compose
  的做法种到临时副本再校验回写 valid。
- 计划用 `entity_id.upper()` 当表名 → 改为读 entity["tables"][0] (本体已登记真表名,
  不靠"id 就是表名"的暗假设)。
- 计划反证用 `n_join != n_bare` → 实测 JOIN 后 COUNT(*) **大于**裸表行数
  (一对多扇出), 该断言能过但过得没道理。改为 DISTINCT anchor 主键比较,
  真子集严格小于裸表才是"JOIN 真在筛"的证据。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from agent.tools import db_adapters

from core.config import settings
from core.ontology_view import (find_join_paths, get_ontology, set_ontology)
from capabilities.ontology.ontology_store import OntologyStore
from capabilities.ontology.validate_concepts import validate_concept
import agent.tools as tool_pkg
from agent.tools.ontology_join import get_concept_join, get_join_path
from agent.tools.sql_exec import execute_sql, list_table_columns

CONCEPT_ID = "high_value_reader"
ANCHOR = "lib_loan"
TARGET = "lib_book"
# "在用"状态码: 必须是目标库里真实存在的码值 (先探 SELECT col, COUNT(*) GROUP BY col),
# 计划书示例值不可照抄 —— 照抄过一次, 库里根本没有那个值, 结果恒为空。
STATUS_IN_USE = os.environ.get("SMOKE_STATUS_IN_USE", "300001")


def _seed_concept(db_path: str) -> None:
    """高价值客户聚合概念落进测试库副本 (kind=agg 四件套, 照 compose smoke)。"""
    store = OntologyStore(db_path)
    try:
        store.upsert_concept({
            "id": CONCEPT_ID, "display_name": "高价值客户",
            "anchor_entity": ANCHOR,
            "summary": f"名下在用(REC_STATUS={STATUS_IN_USE})借阅实例数 >= 5 的客户",
            "origin": "manual",
        })
        store.upsert_concept_condition({
            "concept_id": CONCEPT_ID, "entity": ANCHOR, "kind": "agg",
            "col": "REC_STATUS", "op": "=", "value": STATUS_IN_USE,
            "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
            "having_op": ">=", "having_value": "5", "seq": 0,
        })
    finally:
        store.close()


def _table_of(onto, entity_id: str) -> str:
    """entity → 真实表名 (读本体登记的 tables, 不猜 id.upper())。"""
    tables = (onto.entities.get(entity_id) or {}).get("tables") or []
    assert tables, f"实体 {entity_id} 未登记 tables, 无法拼 SQL"
    return tables[0]


def _pk_of(onto, entity_id: str) -> str:
    """entity → 主键列名 (反证要按 anchor 主键去重, 不能猜)。"""
    for a in (onto.entities.get(entity_id) or {}).get("attributes", []):
        if a.get("kind") == "pk":
            return a["name"]
    raise AssertionError(f"实体 {entity_id} 无 kind=pk 属性, 反证无法去重")


def _count(res) -> int:
    """execute_sql 返回 {"rows": [...], "n": N}; 取第一行第一个值。"""
    assert "rows" in res, f"SQL 未返回 rows (error/blocked?): {res}"
    rows = res["rows"]
    assert rows, f"无行返回: {res}"
    return int(list(rows[0].values())[0])


def _prove_debt(onto) -> None:
    print("=" * 64)
    print(f"① 债的实证: {ANCHOR} → {TARGET}")
    one_hop = find_join_paths(onto, ANCHOR, TARGET)
    print(f"   老 find_join_paths (直连一跳): {len(one_hop)} 条")
    assert not one_hop, "前提变了: 老函数居然直连答出来了, 债的实证不成立"
    print("   → 老函数答不出, LLM 只能自己猜中转实体 (这就是要补的债)")


def _build_join_sql(onto, edges, select_expr: str) -> str:
    """按 path.edges 有序拼 JOIN (每条边自带 source_col/target_col)。

    起点从 edges[0]["from"] 推, 不焊死 ANCHOR: output_key.entity 不一定等于
    anchor (agg 概念的 group_by 实体才是把手所在), 焊死会拼错 FROM 且不响亮。
    """
    joins = [f"JOIN {_table_of(onto, e['to'])} ON "
             f"{_table_of(onto, e['from'])}.{e['source_col']} = "
             f"{_table_of(onto, e['to'])}.{e['target_col']}" for e in edges]
    return (f"SELECT {select_expr} FROM {_table_of(onto, edges[0]['from'])} "
            + " ".join(joins))


def _prove_subset(onto, edges, n_join_rows: int) -> tuple[int, int]:
    """④ JOIN 生效反证: JOIN 命中的 anchor 行必须是裸表真子集。

    JOIN 后 COUNT(*) 因一对多扇出会大于裸表 (已实测), 计划书的
    n_join != n_bare 断言空转; 按 anchor 主键 DISTINCT 比才证明 JOIN 真在筛。
    """
    anchor_tbl, anchor_pk = _table_of(onto, ANCHOR), _pk_of(onto, ANCHOR)
    n_bare = _count(execute_sql(
        f"SELECT COUNT(DISTINCT {anchor_pk}) AS c FROM {anchor_tbl}", source="lib"))
    n_hit = _count(execute_sql(
        _build_join_sql(onto, edges, f"COUNT(DISTINCT {anchor_tbl}.{anchor_pk}) AS c"),
        source="lib"))
    print(f"   裸表 distinct {anchor_pk}: {n_bare}")
    print(f"   JOIN 命中 distinct  : {n_hit}")
    print(f"   (JOIN 后 COUNT(*)={n_join_rows} 大于裸表, 是一对多扇出, 故按主键去重比)")
    assert 0 < n_hit < n_bare, (
        f"反证失败: JOIN 命中 {n_hit} 不是裸表 {n_bare} 的真子集, "
        f"说明这条路径没真起筛选作用 (业务空转) —— 不准放宽断言, 回查 path.edges 列对")
    return n_hit, n_bare


def _prove_one_call(onto) -> dict:
    """② 一次调用拿完整多跳路径 (不用 LLM 自己拼两次 get_join_path)。"""
    print("=" * 64)
    print("② 一次调用拿完整路径")
    r = get_concept_join(CONCEPT_ID, TARGET)
    assert "error" not in r, f"缝合失败: {r.get('error')}"
    print(f"   output_key: {r['output_key']}")
    print(f"   hops: {r['path']['hops']}")
    for e in r["path"]["edges"]:
        print(f"     {e['from']}.{e['source_col']} = {e['to']}.{e['target_col']}"
              f"   [{e.get('label') or '裸边'}]")
    print(f"   segments: {[s['source'] for s in r['path']['segments']]}")
    print(f"   alternatives: {r.get('alternatives')}")
    print(f"   alternatives_truncated: {r.get('alternatives_truncated', 0)}")
    assert r["path"]["hops"] >= 2, "该链路真实是 2 跳, 少于 2 说明路径不对"
    assert r["path"]["segments"], "同库也须给 segments (单段)"
    return r


def main() -> int:
    src_db = settings.obda_ontology_path
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = None
    try:
        shutil.copy(src_db, tmp)
        _seed_concept(tmp)

        conn = db_adapters.connect_source(settings.db_sources["lib"])
        tool_pkg.set_connections({"lib": conn})

        set_ontology(tmp)
        onto = get_ontology()
        _prove_debt(onto)

        # 校验概念 → 回写 valid=1 (断腿概念 get_concept_join 会响亮失败, 先让它可用)
        cols_map = {ANCHOR: list_table_columns(_table_of(onto, ANCHOR), "lib")}
        vr = validate_concept({**onto.concepts[CONCEPT_ID]}, onto, cols_map,
                              join_path=lambda a, b: get_join_path(a, b).get("paths", []))
        assert vr["ok"], f"概念校验未过: {vr['checks']}"
        store = OntologyStore(tmp)
        store.set_concept_valid(CONCEPT_ID, 1)
        store.close()
        set_ontology(tmp)
        onto = get_ontology()

        r = _prove_one_call(onto)
        edges = r["path"]["edges"]

        print("=" * 64)
        print("③ 拿 path.edges 拼 SQL 真跑")
        join_sql = _build_join_sql(onto, edges, "COUNT(*) AS c")
        n_join_rows = _count(execute_sql(join_sql, source="lib"))
        print(f"   SQL: {join_sql}")
        print(f"   JOIN 后行数: {n_join_rows}")
        assert n_join_rows > 0, "拼出的 SQL 跑通但零行, 路径业务空转"

        print("=" * 64)
        print("④ ★反证: JOIN 命中的 anchor 行必须是裸表真子集")
        n_hit, n_bare = _prove_subset(onto, edges, n_join_rows)

        print("=" * 64)
        print("⑤ 响亮失败反证: 不存在的目标实体")
        bad = get_concept_join(CONCEPT_ID, "no_such_entity")
        assert "error" in bad and "目标实体不存在" in bad["error"], bad
        print(f"   {bad['error']}")

        print()
        print(f"✅ smoke 全绿: 老函数 0 条 → 一步拿 {r['path']['hops']} 跳完整路径; "
              f"SQL 真跑出 {n_join_rows} 行; JOIN 命中 {n_hit} ⊂ 裸表 {n_bare}; 响亮失败 OK")
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass  # 连接清理失败不能挡住临时库删除
        os.remove(tmp)


if __name__ == "__main__":
    raise SystemExit(main())
