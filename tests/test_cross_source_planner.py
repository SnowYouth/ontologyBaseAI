# ★ 2026-10-15 变异反证过: 把固件里的 lib2 全改成 lib (制造"两源同名") 后,
# 本文件两条跨源用例 + tests/obqc/test_index.py 的按source分组用例 + 前端
# graphModel.test.ts 的两条 edgeKind 跨库用例 全部变红 (后端 3 红 / 前端 2 红) ——
# 证明它们真在守"两源不等"语义, 不是碰巧绿。
# 改源名映射 (lib/lib2) 时请重跑此反证: sed 's/"lib2"/"lib"/g' 三个文件后必须见红。
from agent.tools.cross_source_planner import plan_cross_source


def test_单库序列不切段():
    edges = [{"from": "lib_loan", "to": "lib_loan_acct_rel",
              "source_col": "LIB_LOAN_ID", "target_col": "LIB_LOAN_ID",
              "type": "ONE_TO_MANY", "label": "x"}]
    src = {"lib_loan": "lib", "lib_loan_acct_rel": "lib"}
    out = plan_cross_source(edges, src)
    assert len(out["segments"]) == 1
    assert out["segments"][0]["source"] == "lib"
    assert out["segments"][0]["needs"] == []
    assert out["segments"][0]["exports"] == []


def test_跨库切两段且接力分片键():
    # lib_loan(lib) --READER_ID→MEMBER_ID--> lib_reader(lib2)
    edges = [{"from": "lib_loan", "to": "lib_reader",
              "source_col": "READER_ID", "target_col": "MEMBER_ID",
              "type": "MANY_TO_ONE", "label": "跨库出口"}]
    src = {"lib_loan": "lib", "lib_reader": "lib2"}
    out = plan_cross_source(edges, src)
    segs = out["segments"]
    assert len(segs) == 2
    assert segs[0]["source"] == "lib"
    assert segs[1]["source"] == "lib2"
    # 接力: 前段导出连接列, 后段需要它 (分片键 member_id 值接力)
    assert segs[0]["exports"] == ["MEMBER_ID"]
    assert segs[1]["needs"] == ["MEMBER_ID"]


def test_entity_source缺键按未知源切段():
    """★ 缺键兜底空串 = 未知源, 与任何真实源都不等 -> 必须切段。

    改之前兜底是写死的源名: 缺键实体会被误判成与该源同源, 不切段。
    本用例是该默认分支的唯一覆盖 —— 三个既有用例的 src dict 都显式给齐了所有
    实体, 默认分支从不触发, 所以改对改错都绿。
    """
    edges = [{"from": "lib_loan", "to": "未登记实体",
              "source_col": "READER_ID", "target_col": "READER_ID",
              "type": "MANY_TO_ONE", "label": "出口"}]
    src = {"lib_loan": "lib"}        # 故意不给 "未登记实体"
    segs = plan_cross_source(edges, src)["segments"]
    assert len(segs) == 2, "缺键实体应被当未知源切段"
    assert segs[0]["source"] == "lib"
    assert segs[1]["source"] == "", "未知源应是空串"
    assert segs[0]["exports"] == ["READER_ID"]
    assert segs[1]["needs"] == ["READER_ID"]


def test_三跳产品到客户到账户():
    # lib_loan(lib) → lib_reader(lib2) → account(lib2): lib 1段 + lib2 1段(含2边)
    edges = [
        {"from": "lib_loan", "to": "lib_reader",
         "source_col": "READER_ID", "target_col": "MEMBER_ID",
         "type": "MANY_TO_ONE", "label": "跨库出口"},
        {"from": "lib_reader", "to": "account",
         "source_col": "MEMBER_ID", "target_col": "MEMBER_ID",
         "type": "ONE_TO_MANY", "label": "库内辐射"},
    ]
    src = {"lib_loan": "lib", "lib_reader": "lib2", "account": "lib2"}
    segs = plan_cross_source(edges, src)["segments"]
    assert len(segs) == 2
    assert segs[0]["source"] == "lib" and len(segs[0]["edges"]) == 1
    assert segs[1]["source"] == "lib2" and len(segs[1]["edges"]) == 2
    assert segs[0]["exports"] == ["MEMBER_ID"]
    assert segs[1]["needs"] == ["MEMBER_ID"]
