"""标杆"主号"概念端到端 smoke (连真库)。

链路: 落库标杆概念 → 校验 → set_concept_valid → 加载单例 → 概念目录点名 →
get_concept_detail → get_join_path 选 A 端边 → 组装 SQL → run_sql → 真库结果。

跑法: uv run python tests/manual/smoke_concept.py
需 .env 配好数据源 lib (DB_SOURCES 含 lib + DB_LIB_* 六项)。本脚本改测试库副本, 不污染 capabilities/ontology/ontology.db。
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
from core.ontology_view import build_catalog_text, build_concept_detail, get_ontology, set_ontology
from capabilities.ontology.ontology_store import OntologyStore
from capabilities.ontology.validate_concepts import validate_concept
import agent.tools as tool_pkg
from agent.tools.ontology_join import get_join_path
from agent.tools.sql_exec import execute_sql, list_table_columns   # 注: 是 sql_exec 不是 execute_sql 模块


def _seed_active_member(db_path: str) -> None:
    """标杆概念落进测试库副本 (须与 spec §3.2 真库核对值一致)。"""
    store = OntologyStore(db_path)
    try:
        store.upsert_concept({
            "id": "active_member", "display_name": "主号", "anchor_entity": "lib_loan",
            "summary": "作为主从号关系里 A 端主号、且该关系当前生效的号码 (副卡/亲情号主卡)",
            "origin": "manual",
        })
        store.upsert_concept_condition({
            "concept_id": "active_member", "entity": "lib_loan_rel",
            "col": "REC_STATUS", "op": "=", "value": "1000", "seq": 0,
        })
    finally:
        store.close()


def main() -> int:
    # 0. 复制真库本体到临时文件 (不污染权威库); mkstemp 拿到 fd 立刻关, 末尾清理
    src = settings.obda_ontology_path
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copy(src, tmp)
        _seed_active_member(tmp)

        # 1. 注入 lib 库连接 (多源池: list_table_columns / execute_sql(source=) 都走 _connections)
        db_conn = db_adapters.connect_source(settings.db_sources["lib"])
        tool_pkg.set_connections({"lib": db_conn})

        # 2. 校验标杆 (真库查列复用 list_table_columns + 真调 get_join_path 连通性)
        set_ontology(tmp)
        onto = get_ontology()
        # table_cols_map: 只需标杆涉及实体的列 (lib_loan / lib_loan_rel), 复用现成
        # list_table_columns (带 TABLE_SCHEMA=DATABASE() 过滤 + 参数化, 返回真库列名集合)
        cols_map = {
            e: list_table_columns(onto.entities[e]["tables"][0], "lib")
            for e in ["lib_loan", "lib_loan_rel"]
        }
        concept = {**onto.concepts["active_member"]}
        vr = validate_concept(concept, onto, cols_map,
                              join_path=lambda a, b: get_join_path(a, b).get("paths", []))
        print("① 校验:", "✅ 全绿" if vr["ok"] else f"❌ {vr['checks']}")
        assert vr["ok"], "标杆校验未过, 检查 col/value/边方向真库核对"

        # 3. 回写 valid + 重载 → 概念进目录
        store = OntologyStore(tmp)
        store.set_concept_valid("active_member", 1)
        store.close()
        set_ontology(tmp)
        onto = get_ontology()
        catalog = build_catalog_text(onto)
        assert "active_member" in catalog, "校验过的概念应进目录"
        print("② 概念目录点名: ✅ active_member 在目录")

        # 4. get_concept_detail → involved
        detail = build_concept_detail(onto, "active_member")
        print("③ 详情:", detail)
        assert detail["involved_entities"] == ["lib_loan", "lib_loan_rel"]

        # 5. get_join_path: 先打印全部 paths 供人工确认 "靠 label/语义能选对 A 端边"
        #    (概念层不存边, 这一步压测的正是 LLM 靠边的语义信息选对方向, 不是靠脚本作者背列名)
        paths = get_join_path("lib_loan", "lib_loan_rel").get("paths", [])
        print("④ lib_loan→lib_loan_rel 全部边 (人工确认哪条是 A 端主号):")
        for p in paths:
            print("   ", p)
        assert paths, "get_join_path 未返回任何边, 核对 relation 边是否存在"
        # A 端边识别: 以真库核对的 A_LIB_LOAN_ID 为准 (paths 内字段名执行时按打印结果确认)
        a_edge = next((p for p in paths
                       if "A_LIB_LOAN_ID" in (p.get("source_col"), p.get("target_col"))), None)
        assert a_edge is not None, "未找到 A 端主号边 (A_LIB_LOAN_ID), 核对 paths 字段名与边方向"

        # 6. 组装 SQL + execute_sql(source="lib"): JOIN 列从 a_edge 拼出, 焊死第④步的选择
        #    → 第⑥步的执行 (选错端 = SQL 变 = 结果变, a_edge 真正驱动执行, 非演给人看)。
        #    get_join_path view 里 from=lib_loan 侧: source_col=lib_loan 列(LIB_LOAN_ID),
        #    target_col=lib_loan_rel 列(A_LIB_LOAN_ID)。
        sql = (f"SELECT COUNT(*) AS n FROM lib_loan p "
               f"JOIN lib_loan_rel r ON r.{a_edge['target_col']} = p.{a_edge['source_col']} "
               f"WHERE r.REC_STATUS = '1000'")
        res = execute_sql(sql, source="lib")
        print("⑤ 真库结果 (A 端):", res)
        assert "rows" in res, f"execute_sql 返回 error: {res}"
        # spec §3.2 实测 A 端有结果; 允许真库数据漂移, 只断言 > 0
        n = res["rows"][0]["n"] if res.get("rows") else 0
        assert n > 0, f"标杆 SQL 应有结果, 实得 {n}"

        # 7. 反证: 换 Z 端边应得不同行数, 证明"选对端"真的有意义。两端 JOIN 各自都 >0,
        #    光靠 n>0 无法区分走对没走对 —— 必须 A/Z 行数不同才坐实端选择真的驱动了结果。
        z_edge = next((p for p in paths
                       if "Z_LIB_LOAN_ID" in (p.get("source_col"), p.get("target_col"))), None)
        assert z_edge is not None, "未找到 Z 端从号边 (Z_LIB_LOAN_ID)"
        sql_z = (f"SELECT COUNT(*) AS n FROM lib_loan p "
                 f"JOIN lib_loan_rel r ON r.{z_edge['target_col']} = p.{z_edge['source_col']} "
                 f"WHERE r.REC_STATUS = '1000'")
        res_z = execute_sql(sql_z, source="lib")
        assert "rows" in res_z, f"Z 端 execute_sql 返回 error: {res_z}"
        n_z = res_z["rows"][0]["n"] if res_z.get("rows") else 0
        print(f"⑥ 反证 Z 端行数: {n_z} (应 != A 端 {n})")
        assert n != n_z, f"A/Z 端行数相同({n}), 无法证明选对端 —— 标杆压测失效"
        print(f"✅ smoke 全绿: 主号(A 端) {n} 行 vs 从号(Z 端) {n_z} 行, 端选择焊进 SQL")
        return 0
    finally:
        os.remove(tmp)


if __name__ == "__main__":
    raise SystemExit(main())
