"""组合 smoke: 概念作为积木嵌进外层查询 (spec §2 消费模型 / §9.7)。

链路: 落库"高价值客户"聚合概念 → 校验绿灯 → build_concept_detail 取 output_key →
用 output_key 把概念谓词嵌进外层"书目包订购"查询 → 真库跑通取书目包热度行数。
断言焊死 output_key: 外层 JOIN 用 output_key.col 拼装, 非硬编码 READER_ID。

跑法: uv run python tests/manual/smoke_concept_compose.py
需 .env 配好数据源 lib (DB_SOURCES 含 lib + DB_LIB_* 六项)。改测试库副本, 不污染 capabilities/ontology/ontology.db。
连接注入走多源池 set_connections({"lib": conn}) (list_table_columns/execute_sql(source=) 都读它)。
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
from core.ontology_view import build_concept_detail, get_ontology, set_ontology
from capabilities.ontology.ontology_store import OntologyStore
from capabilities.ontology.validate_concepts import validate_concept
import agent.tools as tool_pkg
from agent.tools.ontology_join import get_join_path
from agent.tools.sql_exec import execute_sql, list_table_columns

STATUS_IN_USE = os.environ.get("SMOKE_STATUS_IN_USE", "300001")


def _seed_high_value(db_path: str) -> None:
    """高价值客户聚合概念落进测试库副本 (kind=agg 四件套)。"""
    store = OntologyStore(db_path)
    try:
        # "在用"状态码须按目标库实际分布校准 (示例值照抄过一次, 库里不存在 → 恒空);
        # 用 SMOKE_STATUS_IN_USE 覆盖。
        store.upsert_concept({
            "id": "high_value_reader", "display_name": "高价值客户",
            "anchor_entity": "lib_loan",
            "summary": f"名下在用(REC_STATUS={STATUS_IN_USE})借阅实例数 >= 5 的客户",
            "origin": "manual",
        })
        store.upsert_concept_condition({
            "concept_id": "high_value_reader", "entity": "lib_loan", "kind": "agg",
            "col": "REC_STATUS", "op": "=", "value": STATUS_IN_USE,
            "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
            "having_op": ">=", "having_value": "5", "seq": 0,
        })
    finally:
        store.close()


def main() -> int:
    # 0. 复制真库本体到临时文件 (不污染权威库)
    src = settings.obda_ontology_path
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copy(src, tmp)
        _seed_high_value(tmp)

        # 1. 注入 lib 库连接 (多源池: list_table_columns / execute_sql(source=) 都走 _connections)
        conn = db_adapters.connect_source(settings.db_sources["lib"])
        tool_pkg.set_connections({"lib": conn})

        # 2. 校验高价值客户概念 (真库查列 + 真调 get_join_path 连通性)
        set_ontology(tmp)
        onto = get_ontology()
        cols_map = {"lib_loan": list_table_columns("lib_loan", "lib")}
        concept = {**onto.concepts["high_value_reader"]}
        vr = validate_concept(concept, onto, cols_map,
                              join_path=lambda a, b: get_join_path(a, b).get("paths", []))
        print("① 校验:", "✅ 全绿" if vr["ok"] else f"❌ {vr['checks']}")
        assert vr["ok"], f"高价值客户校验未过: {vr['checks']}"

        # 3. 回写 valid 让概念可用 + 重载
        store = OntologyStore(tmp)
        store.set_concept_valid("high_value_reader", 1)
        store.close()
        set_ontology(tmp)
        onto = get_ontology()

        # 4. build_concept_detail → output_key (对外 JOIN 把手)
        detail = build_concept_detail(onto, "high_value_reader")
        print("② output_key:", detail["output_key"])
        assert detail["output_key"] == {"entity": "lib_loan", "col": "READER_ID"}, \
            f"output_key 不符: {detail['output_key']}"
        key_col = detail["output_key"]["col"]   # 焊死: 外层 JOIN 用这列, 非硬编码

        # 5. 内层: 概念谓词 (从 detail 的 agg 条件拼, 非手写常量)。
        #    ★分库中间件方言限制 (真库探针实测): HAVING 不接受裸聚合表达式
        #    (报 "Expr which is not supported in Having"), 必须 SELECT 里 alias 出聚合列、
        #    HAVING 引别名, 再用派生表包裹只投影 group_by_col。这是 sqlite→真库(分库中间件/MySQL)
        #    的方言退化点, 见 notes。
        agg = next(c for c in detail["conditions"] if c["kind"] == "agg")
        inner = (f"SELECT {agg['group_by_col']} FROM ("
                 f"SELECT {agg['group_by_col']}, "
                 f"{agg['agg_fn']}({agg['agg_target']}) AS _agg_cnt FROM lib_loan "
                 f"WHERE {agg['col']} = '{agg['value']}' "
                 f"GROUP BY {agg['group_by_col']} "
                 f"HAVING _agg_cnt {agg['having_op']} {agg['having_value']}"
                 f") _hv")
        r_inner = execute_sql(inner, source="lib")
        assert "rows" in r_inner, f"内层查询 error: {r_inner}"
        n_hv = len(r_inner["rows"])
        print(f"③ 高价值客户数: {n_hv}")

        # 5b. ★反证 HAVING 真在筛 (防"安静答错": 分库中间件若静默忽略 HAVING, n_hv 会=全体客户数)。
        #     用 COUNT 取全体客户数 (避开 execute_sql 的行数 LIMIT 截断干扰), 高价值须为真子集。
        r_all = execute_sql(
            f"SELECT COUNT(DISTINCT {agg['group_by_col']}) AS n FROM lib_loan "
            f"WHERE {agg['col']} = '{agg['value']}'", source="lib")
        n_all = r_all["rows"][0]["n"]
        print(f"③b 对照无 HAVING 客户总数: {n_all} (HAVING 应筛出更小子集)")
        assert 0 < n_hv < n_all, (
            f"HAVING 未生效 (安静答错嫌疑) 或无命中: 高价值 {n_hv}, 全体 {n_all}")

        # 6. 外层: 概念作为积木, 用 output_key.col 缝进"书目包订购"维度。
        #    key_col 焊死组合把手: 用错列 = SQL 变 = 结果变, output_key 真正驱动组合。
        outer = (
            "SELECT oi.TITLE_ID, COUNT(*) AS cnt "
            "FROM lib_item_tag_rel oor "
            "JOIN lib_book oi ON oi.LIB_BOOK_ID = oor.LIB_BOOK_ID "
            "WHERE oor.HOLDING_REF IN ("
            f"  SELECT p.LIB_LOAN_ID FROM lib_loan p WHERE p.{key_col} IN ({inner})"
            ") GROUP BY oi.TITLE_ID ORDER BY cnt DESC LIMIT 10")
        r_outer = execute_sql(outer, source="lib")
        print("④ 书目包热度 (前几):", r_outer.get("rows", [])[:5])
        assert "rows" in r_outer, f"外层组合查询 error: {r_outer}"
        assert r_outer["rows"], "高价值客户 >0 但组合查询无书目包行, 核对边路径"
        print(f"✅ 组合 smoke 全绿: {n_hv} 个高价值客户 (全体 {n_all}), "
              f"最爱书目包 TOP1 = {r_outer['rows'][0]}")
        return 0
    finally:
        os.remove(tmp)


if __name__ == "__main__":
    raise SystemExit(main())
