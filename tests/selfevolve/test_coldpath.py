"""tests/selfevolve/test_coldpath.py — 冷路径单测 (TDD, Task 5).

覆盖:
- compute_suspect_edges 集合差纯函数 (含"高频主干边不被冤枉"用例)
- process_cq_regression (mismatch 累计 / 不同 CQ 才定罪 / N=3 触发 retire 提议)
- 墓碑查询 (软删边命中 / 被拒提议命中 / 无命中)
- process_pending_signals (孤儿列候选 / 去重 / skip / signal 标记 processed /
  max_iters+cq_fail 不进提议队列只写 relation_health)
- no_edge hint cols 进 failure_signal payload (埋点补强, 冷路径燃料)
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
    compute_suspect_edges,
    process_cq_regression,
    process_pending_signals,
)


# ---------- 脚手架 ----------

def _edge_tuple(e: dict) -> tuple:
    return (e["source"], e["target"], e["via"])


class TestComputeSuspectEdges:
    """集合差归因纯函数 (spec §5.2)。"""

    def test_差集_新增边是嫌疑(self):
        cur = {("a", "b", "a.X=b.X"), ("a", "c", "a.Y=c.Y")}
        prev = {("a", "b", "a.X=b.X")}
        suspects = compute_suspect_edges(cur, prev)
        assert suspects == {("a", "c", "a.Y=c.Y")}

    def test_高频主干边不被冤枉(self):
        """★ spec §5.2: 出现在几乎每条 SQL 里的主干边, 上轮也在 → 不是嫌疑。"""
        trunk = ("lib_loan", "product", "lib_loan.HOLDING_ID=product.HOLDING_ID")
        cur = {trunk, ("x", "y", "x.Z=y.Z")}
        prev = {trunk}
        suspects = compute_suspect_edges(cur, prev)
        assert trunk not in suspects
        assert suspects == {("x", "y", "x.Z=y.Z")}

    def test_上轮有这轮没有_不算嫌疑(self):
        """prev - cur 方向不算 (回归门只关心"新用的边是谁引入的")。"""
        cur = set()
        prev = {("a", "b", "a.X=b.X")}
        assert compute_suspect_edges(cur, prev) == set()

    def test_空集合(self):
        assert compute_suspect_edges(set(), set()) == set()


class TestProcessCqRegression:
    """mismatch 累计 + suspect 更新 (spec §5.2/§5.3)。

    monkeypatch kg_private 路径让 process_cq_regression 读临时 CQ 文件。
    """

    @pytest.fixture
    def store(self, tmp_path):
        s = OntologyStore(str(tmp_path / "t.db"))
        yield s
        s.close()

    def _write_cq_files(self, tmp_path, cur_results, prev_results):
        cur = {"ran_at": "2026-09-05T10:00:00", "ontology_path": "x",
               "results": cur_results}
        prev = {"ran_at": "2026-09-04T10:00:00", "ontology_path": "x",
                "results": prev_results}
        (tmp_path / "cq_result_current.json").write_text(
            json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "cq_result_previous.json").write_text(
            json.dumps(prev, ensure_ascii=False), encoding="utf-8")

    def _cq_result(self, cq_id, verdict, used_edges=None, violations=None):
        """造一条 CQ result: tool_events 里带 used_edges / violations。"""
        te = {"name": "execute_sql", "args": "", "result": "返回 dict",
              "ms": 10, "ok": None,
              "used_edges": used_edges or [],
              "violations": violations or []}
        return {"id": cq_id, "answer": "a", "tool_events": [te],
                "error": None, "verdict": verdict, "note": ""}

    # ★ 2026-10-16: 这组用例原先 prev 侧统统写 used_edges=[], 把
    # "上轮集合恒空" 这个生产 bug 写进了测试前提 —— 真差集与 `current - ∅`
    # 在这种 fixture 下结果相同, 所以 signals.py 喂空集照样全绿。
    # 现在 prev 必须带上轮真用过的边, 才能分辨两种实现。
    # (主干边用图书馆域假名 —— 仓库级脱敏闸禁止测试里出现真库标识符)
    _TRUNK = {"source": "lib_loan_fee", "target": "lib_loan",
              "via": "lib_loan_fee.LOAN_ID=lib_loan.LOAN_ID",
              "source_col": "LOAN_ID", "target_col": "LOAN_ID"}

    def test_verdict下降触发集合差归因(self, store, tmp_path, monkeypatch):
        """上轮 pass 这轮 fail → **只有差集里的新边** suspect_count++。

        上轮用了主干边, 这轮主干边 + 一条新边。嫌疑人只能是新边。
        """
        new_edge = {"source": "a", "target": "b", "via": "a.X=b.X",
                    "source_col": "X", "target_col": "X"}
        self._write_cq_files(
            tmp_path,
            [self._cq_result("CQ-1", "fail",
                             used_edges=[self._TRUNK, new_edge])],
            [self._cq_result("CQ-1", "pass", used_edges=[self._TRUNK])],
        )
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["suspects_updated"] == 1, "上轮也在用的主干边被算进了嫌疑"
        h = store.get_relation_health("a", "b", "a.X=b.X")
        assert h["suspect_count"] == 1
        trunk_h = store.get_relation_health(
            self._TRUNK["source"], self._TRUNK["target"], self._TRUNK["via"])
        assert trunk_h is None or trunk_h["suspect_count"] == 0, (
            "主干边被冤枉 —— 它 use_count 最高, 会排到人审面板最上面")

    def test_两轮用同样的边_verdict掉了也零嫌疑(self, store, tmp_path, monkeypatch):
        """★ 真库某条 CQ 的形态: 两轮 used_edges 完全相同, verdict 从 pass 掉到 fail。

        差集为空 → 掉档的原因不在边上 (prompt / 数据 / LLM 抖动), 一个嫌疑人
        都不该产生。喂空集的旧实现在这里会把两条边全部记成嫌疑。
        """
        edges = [self._TRUNK,
                 {"source": "a", "target": "b", "via": "a.X=b.X",
                  "source_col": "X", "target_col": "X"}]
        self._write_cq_files(
            tmp_path,
            [self._cq_result("CQ-12", "fail", used_edges=edges)],
            [self._cq_result("CQ-12", "pass", used_edges=edges)],
        )
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["suspects_updated"] == 0, "两轮同样的边全被当成嫌疑人"
        assert store.list_suspect_relations() == []

    def test_上轮缺used_edges字段_退化成全量嫌疑(self, store, tmp_path, monkeypatch):
        """向后兼容: 老结果文件没有 used_edges → 上轮集合为空, 这轮用过的全算嫌疑。

        这是**已知的保守退化**, 不是正确归因 —— 靠上面两条用例把它和
        "恒空集"区分开。
        """
        edge = {"source": "a", "target": "b", "via": "a.X=b.X",
                "source_col": "X", "target_col": "X"}
        prev = self._cq_result("CQ-1", "pass")
        prev["tool_events"][0].pop("used_edges")
        self._write_cq_files(
            tmp_path, [self._cq_result("CQ-1", "fail", used_edges=[edge])], [prev])
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        assert process_cq_regression(store)["suspects_updated"] == 1

    def test_上轮集合按CQ取不是全局并集(self, store, tmp_path, monkeypatch):
        """★ 上轮集合必须取**同一条 CQ** 的, 不能把上轮所有 CQ 的边并起来。

        别的 CQ 上轮用过 a→b, 不代表这条掉档的 CQ 上轮也用过它 —— 拿全局并集
        会把真嫌疑人洗白 (差集恒偏小, 归因静默失效)。
        """
        edge = {"source": "a", "target": "b", "via": "a.X=b.X",
                "source_col": "X", "target_col": "X"}
        self._write_cq_files(
            tmp_path,
            [self._cq_result("CQ-1", "fail", used_edges=[edge])],
            [self._cq_result("CQ-1", "pass", used_edges=[self._TRUNK]),
             self._cq_result("CQ-7", "pass", used_edges=[edge])],
        )
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["suspects_updated"] == 1, "上轮集合取成了全局并集, 嫌疑人被洗白"

    def test_上轮没跑过这条CQ_这轮用过的边全算嫌疑(self, store, tmp_path, monkeypatch):
        """新加的考题: prev 里根本没这条 id → 上轮集合为空 (而非 KeyError)。"""
        edge = {"source": "a", "target": "b", "via": "a.X=b.X",
                "source_col": "X", "target_col": "X"}
        self._write_cq_files(
            tmp_path,
            [self._cq_result("CQ-99", "fail", used_edges=[edge])],
            [self._cq_result("CQ-1", "pass", used_edges=[self._TRUNK])],
        )
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        # prev_verdict 为 None → 不满足 "上轮 pass" 前提, 本就不归因
        assert process_cq_regression(store)["suspects_updated"] == 0

    def test_verdict不降不归因(self, store, tmp_path, monkeypatch):
        edge = {"source": "a", "target": "b", "via": "a.X=b.X",
                "source_col": "X", "target_col": "X"}
        self._write_cq_files(
            tmp_path,
            [self._cq_result("CQ-1", "pass", used_edges=[edge])],
            [self._cq_result("CQ-1", "pass", used_edges=[self._TRUNK])],
        )
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["suspects_updated"] == 0

    # mismatch 的嫌疑人 = 本体现有的候选边 (obqc 经 legal_edges 带出),
    # 不是 LLM 错写的列对派生出的虚构边。这些用例统一给一条现有真边。
    _REAL_EDGE = {"source": "lib_loan", "target": "lib_reader",
                  "via": "lib_loan.READER_ID=lib_reader.MEMBER_ID",
                  "source_col": "READER_ID", "target_col": "MEMBER_ID"}

    def _seed_real_edge(self, store):
        store.upsert_relation({
            "source": "lib_loan", "target": "lib_reader",
            "source_col": "READER_ID", "target_col": "MEMBER_ID",
            "type": "MANY_TO_ONE"})

    def test_mismatch累计不同CQ到N3触发retire提议(self, store, tmp_path, monkeypatch):
        """★ spec §5.3/R-2: 同一错误列对在不同 CQ 出现 3 次 → relation_col retire 提议。"""
        self._seed_real_edge(store)
        v = {"type": "join_col_mismatch", "hit": "pi.MEMBER_ID = c.MEMBER_ID",
             "reason": "x", "evidence": "legal cols: [...]",
             "entities": ["lib_loan", "lib_reader"],
             "legal_edges": [self._REAL_EDGE]}
        results = []
        for i, cq_id in enumerate(["CQ-1", "CQ-5", "CQ-9"]):
            # 每条 CQ 都带同一个 mismatch violation, 边是 lib_loan→lib_reader
            te = [{"name": "execute_sql", "args": "", "result": "返回 dict",
                   "ms": 5, "ok": None, "used_edges": [],
                   "violations": [v]}]
            results.append({"id": cq_id, "answer": "a", "tool_events": te,
                            "error": None, "verdict": "partial", "note": ""})
        self._write_cq_files(tmp_path, results, [])
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["mismatch_thresholds_hit"] >= 1
        proposals = store.list_proposals()
        retires = [p for p in proposals
                   if p["kind"] == "relation_col" and p["direction"] == "retire"]
        assert len(retires) == 1

    def test_同一CQ重复mismatch不定罪(self, store, tmp_path, monkeypatch):
        """★ R-2: 一条 CQ 重跑 3 次不能定罪, 必须不同 CQ。"""
        v = {"type": "join_col_mismatch", "hit": "pi.MEMBER_ID = c.MEMBER_ID",
             "reason": "x", "evidence": "legal cols: [...]",
             "entities": ["lib_loan", "lib_reader"],
             "legal_edges": [self._REAL_EDGE]}
        te = [{"name": "execute_sql", "args": "", "result": "返回 dict",
               "ms": 5, "ok": None, "used_edges": [], "violations": [v, v, v]}]
        results = [{"id": "CQ-1", "answer": "a", "tool_events": te,
                    "error": None, "verdict": "partial", "note": ""}]
        self._write_cq_files(tmp_path, results, [])
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["retire_proposals_created"] == 0

    def test_mismatch列对归一不受字典序影响(self, store, tmp_path, monkeypatch):
        """★ col_pair 归一: 'A=B' 与 'B=A' 是同一对, 展示形态按字典序。

        原为"派生 via 保留原始列序"的回归用例。2026-09-18 二次取证推翻了
        "由错写列对派生 via"这条路本身 (mismatch 的列对按定义不是任何现有边的
        列对 → via 必然指向虚构边), 改为从 legal_edges 取真边。列对只留展示/去重
        职责, 这里守住归一口径。
        """
        self._seed_real_edge(store)
        v = {"type": "join_col_mismatch", "hit": "pi.ZZZ_ID = c.MEMBER_ID",
             "reason": "x", "evidence": "legal cols: [...]",
             "entities": ["lib_loan", "lib_reader"],
             "legal_edges": [self._REAL_EDGE]}
        results = []
        for cq_id in ["CQ-1", "CQ-5", "CQ-9"]:
            te = [{"name": "execute_sql", "args": "", "result": "返回 dict",
                   "ms": 5, "ok": None, "used_edges": [], "violations": [v]}]
            results.append({"id": cq_id, "answer": "a", "tool_events": te,
                            "error": None, "verdict": "partial", "note": ""})
        self._write_cq_files(tmp_path, results, [])
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        process_cq_regression(store)

        # 证据挂在真边上, col_pair 是字典序归一形态
        h = store.get_relation_health(
            "lib_loan", "lib_reader", self._REAL_EDGE["via"])
        assert h is not None
        assert "MEMBER_ID=ZZZ_ID" in json.loads(h["mismatch_details"])
        retires = [p for p in store.list_proposals()
                   if p["kind"] == "relation_col" and p["direction"] == "retire"]
        assert len(retires) == 1
        assert json.loads(retires[0]["payload_json"])["col_pair"] == "MEMBER_ID=ZZZ_ID"

    def test_mismatch列对无序_AB与BA仍聚合为同一条(self, store, tmp_path, monkeypatch):
        """★ 无序聚合: 同一条边被两个方向写出, 仍算同一个列对。

        CQ-1/CQ-5 写 'pi.ZZZ_ID = c.MEMBER_ID', CQ-9 里 JOIN 两侧写反
        (obqc 的 entities 与 hit 列是位置对应的, 所以两者一起反) —— 三条 CQ
        必须合并计数到 3 才定罪, 否则退化成 2+1 两组, 都够不着阈值。

        ★ 这条在 _mismatch_key 只排列名 (不绑 entity) 时是红的。
        """
        self._seed_real_edge(store)
        rev_edge = {**self._REAL_EDGE, "source": "lib_reader", "target": "lib_loan",
                    "source_col": "MEMBER_ID", "target_col": "READER_ID"}
        fwd = {"type": "join_col_mismatch", "hit": "pi.ZZZ_ID = c.MEMBER_ID",
               "reason": "x", "evidence": "legal cols: [...]",
               "entities": ["lib_loan", "lib_reader"],
               "legal_edges": [self._REAL_EDGE]}
        rev = {"type": "join_col_mismatch", "hit": "c.MEMBER_ID = pi.ZZZ_ID",
               "reason": "x", "evidence": "legal cols: [...]",
               "entities": ["lib_reader", "lib_loan"],
               "legal_edges": [rev_edge]}
        results = []
        for cq_id, viol in [("CQ-1", fwd), ("CQ-5", fwd), ("CQ-9", rev)]:
            te = [{"name": "execute_sql", "args": "", "result": "返回 dict",
                   "ms": 5, "ok": None, "used_edges": [], "violations": [viol]}]
            results.append({"id": cq_id, "answer": "a", "tool_events": te,
                            "error": None, "verdict": "partial", "note": ""})
        self._write_cq_files(tmp_path, results, [])
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        out = process_cq_regression(store)
        assert out["retire_proposals_created"] == 1, "无序聚合被破坏, 3 条 CQ 裂成两组"


class TestMismatchTargetsRealEdge:
    """★ mismatch 归因必须指认**本体里现有的那条真边** (2026-09-18 拍板)。

    背景: obqc 报 join_col_mismatch 的充要条件 = SQL 写的列对 ≠ 该 entity 对上
    任何一条边的列对。所以由 SQL 列对派生的 via 在 relation 表里**必然查无此行** ——
    老代码据此生成 retire 提议, 等于要淘汰一条自己虚构的边, 而真正把 LLM 带偏的
    现有边从头到尾没被指认过。

    新口径: 从 violation.legal_edges (obqc 结构化带出的候选边) 里取嫌疑人。
    """

    @pytest.fixture
    def store(self, tmp_path):
        s = OntologyStore(str(tmp_path / "t.db"))
        yield s
        s.close()

    def _run(self, store, tmp_path, monkeypatch, violation, n_cq=3):
        results = []
        for cq_id in ["CQ-1", "CQ-5", "CQ-9"][:n_cq]:
            te = [{"name": "execute_sql", "args": "", "result": "返回 dict",
                   "ms": 5, "ok": None, "used_edges": [], "violations": [violation]}]
            results.append({"id": cq_id, "answer": "a", "tool_events": te,
                            "error": None, "verdict": "partial", "note": ""})
        for name, data in [("cq_result_current.json", results),
                           ("cq_result_previous.json", [])]:
            (tmp_path / name).write_text(
                json.dumps({"ran_at": "x", "ontology_path": "x", "results": data},
                           ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                            tmp_path / "cq_result_current.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH",
                            tmp_path / "cq_result_previous.json")
        return process_cq_regression(store)

    def _mismatch(self, legal_edges):
        """LLM 错写 pi.MEMBER_ID = c.MEMBER_ID, 本体真边在 legal_edges 里。"""
        return {"type": "join_col_mismatch", "hit": "pi.MEMBER_ID = c.MEMBER_ID",
                "reason": "lib_loan↔lib_reader 有边但连接列对不上",
                "evidence": "legal cols: [...]",
                "entities": ["lib_loan", "lib_reader"],
                "legal_edges": legal_edges}

    def _real_edge(self, store):
        store.upsert_relation({
            "source": "lib_loan", "target": "lib_reader",
            "source_col": "READER_ID", "target_col": "MEMBER_ID",
            "type": "MANY_TO_ONE"})
        return "lib_loan.READER_ID=lib_reader.MEMBER_ID"

    def test_retire提议指向现有真边而非虚构边(self, store, tmp_path, monkeypatch):
        """★ 核心: 提议的 via 必须在 relation 表里查得到。"""
        real_via = self._real_edge(store)
        v = self._mismatch([{"source": "lib_loan", "target": "lib_reader",
                             "via": real_via, "source_col": "READER_ID",
                             "target_col": "MEMBER_ID"}])
        out = self._run(store, tmp_path, monkeypatch, v)
        assert out["retire_proposals_created"] == 1
        retires = [p for p in store.list_proposals() if p["kind"] == "relation_col"]
        via = json.loads(retires[0]["payload_json"])["via"]
        assert via == real_via, f"提议指向虚构边 {via}"
        assert store._conn.execute(
            "SELECT 1 FROM relation WHERE via=?", (via,)).fetchone() is not None

    def test_证据攒在真边的relation_health上(self, store, tmp_path, monkeypatch):
        """幽灵行回归: relation_health 的 via 也要是真边。"""
        real_via = self._real_edge(store)
        v = self._mismatch([{"source": "lib_loan", "target": "lib_reader",
                             "via": real_via, "source_col": "READER_ID",
                             "target_col": "MEMBER_ID"}])
        self._run(store, tmp_path, monkeypatch, v)
        rows = store._conn.execute(
            "SELECT via FROM relation_health").fetchall()
        assert [r["via"] for r in rows] == [real_via]

    def test_多候选边时不定罪只攒证据(self, store, tmp_path, monkeypatch):
        """★ 宽进严出: 一对 entity 有多条边时, 指认哪条有歧义 → 不生成 retire 提议。

        真库里有相当比例的 entity 对挂着多条边, 这不是罕见情况。
        证据照攒 (两条都攒), 但定罪留给人。
        """
        via1 = self._real_edge(store)
        store.upsert_relation({
            "source": "lib_loan", "target": "lib_reader",
            "source_col": "BORROWER_ID", "target_col": "MEMBER_ID",
            "type": "MANY_TO_ONE"})
        via2 = "lib_loan.BORROWER_ID=lib_reader.MEMBER_ID"
        v = self._mismatch([
            {"source": "lib_loan", "target": "lib_reader", "via": via1,
             "source_col": "READER_ID", "target_col": "MEMBER_ID"},
            {"source": "lib_loan", "target": "lib_reader", "via": via2,
             "source_col": "BORROWER_ID", "target_col": "MEMBER_ID"},
        ])
        out = self._run(store, tmp_path, monkeypatch, v)
        assert out["retire_proposals_created"] == 0, "多候选边不该自动定罪"
        vias = {r["via"] for r in
                store._conn.execute("SELECT via FROM relation_health").fetchall()}
        assert vias == {via1, via2}, "证据仍要攒在两条候选边上"

    def test_无legal_edges的老数据不崩(self, store, tmp_path, monkeypatch):
        """向后兼容: 老 CQ 结果文件没有 legal_edges 字段 → 跳过, 不炸。"""
        v = {"type": "join_col_mismatch", "hit": "pi.MEMBER_ID = c.MEMBER_ID",
             "reason": "x", "evidence": "legal cols: []",
             "entities": ["lib_loan", "lib_reader"]}
        out = self._run(store, tmp_path, monkeypatch, v)
        assert out["retire_proposals_created"] == 0


class TestTombstone:
    """墓碑查询 (spec §5.7 R-11)。"""

    @pytest.fixture
    def store(self, tmp_path):
        self._db_path = str(tmp_path / "t.db")
        s = OntologyStore(self._db_path)
        yield s
        s.close()

    def test_命中软删边_tombstone_hit_1(self, store):
        store.upsert_relation({
            "source": "a", "target": "b", "via": "a.X=b.X",
            "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE",
        })
        store.soft_delete_relation("a", "b", "a.X=b.X")
        # process_pending_signals 走探针 pass 后 INSERT proposal 时应带墓碑
        # 直接构造: 走 process_pending_signals (mock probe pass)
        out = self._run_process(store, probe_verdict="pass")
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tombstone_hit"] == 1
        assert "a.X=b.X" in (proposals[0]["tombstone_note"] or "")

    def test_命中被拒提议_tombstone_hit_1(self, store):
        """构造被拒提议 (同列对) → 新提议 tombstone_hit=1。

        注意: 被拒提议是第 1 条, 新提议是第 2 条 (rejected 不在去重范围,
        墓碑不静默跳过 — spec §5.7)。
        """
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "source_col": "X",
                         "target_col": "X", "via": "a.X=b.X"}, "test")
        store.patch_proposal_review(pid, "rejected", reviewed_by="human",
                                    review_note="不要这条")
        out = self._run_process(store, probe_verdict="pass")
        proposals = store.list_proposals()
        assert len(proposals) == 2  # 1 被拒旧提议 + 1 新提议 (带墓碑)
        new_proposals = [p for p in proposals if p["id"] != pid]
        assert len(new_proposals) == 1
        assert new_proposals[0]["tombstone_hit"] == 1
        assert "不要这条" in (new_proposals[0]["tombstone_note"] or "")

    def test_无命中_tombstone_hit_0(self, store):
        out = self._run_process(store, probe_verdict="pass")
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tombstone_hit"] == 0

    def test_命中反向软删边_tombstone_hit_1(self, store):
        """★ 反向软删边也要命中 (spec §4.2 探针无向 → 墓碑必须同口径)。

        真库实例: 有一条 lib_book.LIB_LOAN_ID=lib_loan.LIB_LOAN_ID 的边
        (已软删), 探针从 lib_loan→lib_book 方向重提同一条边。有向匹配漏掉 →
        人审看到"无历史处置", 而这条边其实曾被人删过, 正是 §5.7 要防的抖动。
        """
        store.upsert_relation({
            "source": "b", "target": "a", "via": "b.X=a.X",
            "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE",
        })
        store.soft_delete_relation("b", "a", "b.X=a.X")
        # 冷路径按 a→b 方向提议 (与软删边端点顺序相反)
        out = self._run_process(store, probe_verdict="pass")
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tombstone_hit"] == 1
        assert "b.X=a.X" in (proposals[0]["tombstone_note"] or "")

    def test_命中反向被拒提议_tombstone_hit_1(self, store):
        """★ 反向的历史被拒提议也要命中 (同上, 无向口径)。"""
        pid = store.insert_proposal(
            "relation", {"source": "b", "target": "a", "source_col": "X",
                         "target_col": "X", "via": "b.X=a.X"}, "test")
        store.patch_proposal_review(pid, "rejected", reviewed_by="human",
                                    review_note="反向拒过")
        out = self._run_process(store, probe_verdict="pass")
        new_proposals = [p for p in store.list_proposals() if p["id"] != pid]
        assert len(new_proposals) == 1
        assert new_proposals[0]["tombstone_hit"] == 1
        assert "反向拒过" in (new_proposals[0]["tombstone_note"] or "")

    def test_软删边via列名小写_探针大写_仍要命中(self, store):
        """★★ 真库上可比对的软删边几乎全部漏匹配的那个口径
        (2026-10-19 在真库副本上实测)。

        数据两头反着长的:
        - `relation.via` 里的软删边绝大多数是**小写**列名
        - `entity.attributes` 的列名是**大写**
        探针 `_derive_edge_via` 从 attributes 取列名拼 via, 墓碑却是
        `WHERE via=?` 精确匹配, 而 SQLite `SELECT 'A'='a'` 是 0 →
        人审看到"无历史处置", 对一条自己判过死的边重新裸判, 正是 §5.7 要防的抖动。

        `probe.py:164 _norm_key_cols` 2026-09-09 已为同一个坑修过上一层
        (注释还在), 墓碑这层没跟上 —— 同一个口径分裂的第二次发作。
        """
        store.upsert_relation({
            "source": "a", "target": "b", "via": "a.x=b.x",
            "source_col": "x", "target_col": "x", "type": "MANY_TO_ONE",
        })
        store.soft_delete_relation("a", "b", "a.x=b.x")
        # 冷路径的 entities 声明的是大写 X → 探针拼出 "a.X=b.X"
        out = self._run_process(store, probe_verdict="pass")
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tombstone_hit"] == 1, (
            "小写 via 的软删边没被大写口径的探针命中 —— 人审会看到'无历史处置'")
        assert "a.x=b.x" in (proposals[0]["tombstone_note"] or "").lower()

    def test_被拒提议via列名小写_探针大写_仍要命中(self, store):
        """★ 同一个大小写口径, 墓碑的另一半 (被拒提议走 payload 比对非 SQL)。

        两半必须同口径: 只修软删边那半, 历史被拒提议仍会漏 —— 人审同样
        看不到"这条边我上个月拒过", 面板的历史处置摘要照样是空的。
        """
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "source_col": "x",
                         "target_col": "x", "via": "a.x=b.x"}, "test")
        store.patch_proposal_review(pid, "rejected", reviewed_by="human",
                                    review_note="小写拒过")
        out = self._run_process(store, probe_verdict="pass")
        new_proposals = [p for p in store.list_proposals() if p["id"] != pid]
        assert len(new_proposals) == 1
        assert new_proposals[0]["tombstone_hit"] == 1
        assert "小写拒过" in (new_proposals[0]["tombstone_note"] or "")

    def test_大小写归一不许放过真不同的边(self, store):
        """★ 反证: 归一只拉平大小写, **不许**把不同列名的边也算同一条。

        没有这条, 把 `_tombstone_lookup` 改成"永远命中"也能让上面两条绿 ——
        而那样每条新提议都挂一个假墓碑, 人审看到的历史处置全是噪音,
        比漏匹配更糟 (漏匹配是少给信息, 假命中是给错信息)。
        """
        store.upsert_relation({
            "source": "a", "target": "b", "via": "a.OTHER=b.OTHER",
            "source_col": "OTHER", "target_col": "OTHER", "type": "MANY_TO_ONE",
        })
        store.soft_delete_relation("a", "b", "a.OTHER=b.OTHER")
        out = self._run_process(store, probe_verdict="pass")
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tombstone_hit"] == 0, (
            "列名真不一样 (X vs OTHER) 却报墓碑命中 —— 归一过头成了假命中")

    # ---- helper: 注入 no_edge 信号 + mock 探针 pass, 跑冷路径 ----
    # 场景最小化: a/b 各只有列 X (a 的 X 是 PK), hint (X,X) 是唯一候选
    # (无孤儿列 → 候选只有 hint 一对, 断言"恰好 1 个提议"才成立)。

    def _run_process(self, store, probe_verdict="pass"):
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{
                "entities": ["a", "b"], "cols": ["X", "X"],
            }],
        }, db_path=self._db_path)
        entities = {
            "a": {"attributes": [{"name": "X", "kind": "pk"}]},
            "b": {"attributes": [{"name": "X", "kind": "pk"}]},
        }
        relations = []
        return process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda table, source: {
                "pk_cols": ["X"], "unique_cols": [], "row_count": 100},
            entities=entities, relations=relations,
            probe_fn=_fake_probe(probe_verdict),
        )


def _fake_probe(verdict: str):
    """构造注入的探针函数 (跳过真库, 直接给 verdict)。"""
    from capabilities.ontology.selfevolve.probe import ProbeResult

    def probe(source_entity, target_entity, source_col, target_col,
              run_sql, get_index_info):
        return ProbeResult(verdict, checks=[{"name": "mock", "ok": True,
                                             "detail": ""}],
                           evidence={})
    return probe


class TestProcessPendingSignals:
    """冷路径主流程 (mock store + 注入 probe)。"""

    @pytest.fixture
    def store(self, tmp_path):
        s = OntologyStore(str(tmp_path / "t.db"))
        yield s
        s.close()

    def _add_signal(self, store, kind="obqc_circuit_break",
                    no_edge=None, payload=None):
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        p = payload if payload is not None else {
            "sql_fingerprint": "fp1",
            "no_edge_signals": no_edge or [{
                "entities": ["a", "b"], "cols": ["X", "X"]}],
        }
        log_failure_signal(kind, p,
                           db_path=self._db_path_of(store))

    @staticmethod
    def _db_path_of(store) -> str:
        # OntologyStore 持有 _conn; 路径无法直接取, 用 store 内部连接查
        # 简化: fixture 传 path。见各测试。
        raise NotImplementedError

    def _entities(self):
        return {
            "a": {"attributes": [{"name": "X", "kind": "pk"},
                                  {"name": "Y"}, {"name": "Z"}]},
            "b": {"attributes": [{"name": "X", "kind": "pk"}]},
        }

    def test_孤儿列候选进探针(self, store, tmp_path):
        """hint.cols 之外, orphan(a)×PK(b) 也要过探针。"""
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{"entities": ["a", "b"], "cols": ["Y", "X"]}],
        }, db_path=str(tmp_path / "t.db"))
        probed = []

        def probe(se, te, sc, tc, run_sql, get_index_info):
            from capabilities.ontology.selfevolve.probe import ProbeResult
            probed.append((se, te, sc, tc))
            return ProbeResult("fail", checks=[], evidence={})

        out = process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["X"], "unique_cols": [],
                                         "row_count": 100},
            entities=self._entities(), relations=[],
            probe_fn=probe)
        # 候选: hint (Y,X) + orphan(a)={X,Z}(Y已被hint用,但X/Z是孤儿) × PK(b)={X}
        # → 至少 (X,X) 和 (Z,X) 也被探过
        assert ("a", "b", "X", "X") in probed or ("a", "b", "Z", "X") in probed

    def test_探针pass生成proposal并标signal_processed(self, store, tmp_path):
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{"entities": ["a", "b"], "cols": ["X", "X"]}],
        }, db_path=str(tmp_path / "t.db"))
        out = process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["X"], "unique_cols": [],
                                         "row_count": 100},
            entities=self._entities(), relations=[],
            probe_fn=_fake_probe("pass"))
        assert out["proposals_created"] >= 1
        assert out["processed"] == 1
        proposals = store.list_proposals()
        assert len(proposals) == out["proposals_created"]
        p = proposals[0]
        payload = json.loads(p["payload_json"])
        assert payload["source"] == "a"
        assert payload["target"] == "b"
        # 信号已标 processed
        signals = store.list_unprocessed_signals()
        assert len(signals) == 0

    def test_去重_同列对pending不再生成(self, store, tmp_path):
        """两个信号同列对: 第一轮生成 N 个提议, 第二轮全部 skip (总数不变)。

        ★ 2026-09-20f: 原来用 self._entities() (列名 X/Y/Z), 加了候选预筛后
        (Y,X)/(Z,X) jaccard=0 被筛掉 → 只剩 1 个候选, 这条测试就退化成
        "单候选去重"了。**不能把期望值 3 改成 1 交差** —— 那是悄悄削弱覆盖:
        这条测的是"多候选逐个去重", 单候选下 `skipped` 累加逻辑测不出来。
        改法是给一组**共享 token 能过预筛**的列名, 保住原有的 3 候选覆盖。
        """
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        entities = {
            "a": {"attributes": [{"name": "MEMBER_ID", "kind": "pk"},
                                 {"name": "ORDER_ID"}, {"name": "ITEM_ID"}]},
            "b": {"attributes": [{"name": "MEMBER_ID", "kind": "pk"}]},
        }
        for _ in range(2):
            log_failure_signal("obqc_circuit_break", {
                "sql_fingerprint": f"fp{_}",
                "no_edge_signals": [{"entities": ["a", "b"],
                                     "cols": ["MEMBER_ID", "MEMBER_ID"]}],
            }, db_path=str(tmp_path / "t.db"))
        out = process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["MEMBER_ID"],
                                         "unique_cols": [], "row_count": 100},
            entities=entities, relations=[],
            probe_fn=_fake_probe("pass"))
        # 候选 = hint (MEMBER_ID,MEMBER_ID) + orphan(a)×PK(b): a 无边 → 三列全孤儿
        # → (MEMBER_ID,MEMBER_ID),(ITEM_ID,MEMBER_ID),(ORDER_ID,MEMBER_ID) 3 个,
        #   三者都含 token ID → 全过预筛 (jaccard 1.0 / 0.333 / 0.333)
        # → 第一轮全生成, 第二轮全去重
        assert out["proposals_created"] == 3
        assert out["skipped"] == 3
        assert len(store.list_proposals()) == 3  # 提议总数不因第二信号翻倍

    def test_探针fail不生成提议但signal仍processed(self, store, tmp_path):
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{"entities": ["a", "b"], "cols": ["X", "X"]}],
        }, db_path=str(tmp_path / "t.db"))
        out = process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["X"], "unique_cols": [],
                                         "row_count": 100},
            entities=self._entities(), relations=[],
            probe_fn=_fake_probe("fail"))
        assert out["proposals_created"] == 0
        assert store.list_unprocessed_signals() == []

    def test_max_iters不进提议队列只写health(self, store, tmp_path):
        """★ spec §8: 无法归因信号标 processed, 不送提议队列。"""
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("max_iters", {"query": "q", "iters": 10},
                           db_path=str(tmp_path / "t.db"))
        out = process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["X"], "unique_cols": [],
                                         "row_count": 100},
            entities=self._entities(), relations=[],
            probe_fn=_fake_probe("pass"))
        assert out["proposals_created"] == 0
        assert store.list_proposals() == []
        assert store.list_unprocessed_signals() == []

    def test_无no_edge的circuit_break跳过(self, store, tmp_path):
        """circuit_break 但 violations 里没 no_edge (如列归属错) → 无候选, 只标 processed。"""
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "violations": [{"type": "col_attr", "hit": "x", "reason": "y",
                            "evidence": "z"}],
        }, db_path=str(tmp_path / "t.db"))
        out = process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["X"], "unique_cols": [],
                                         "row_count": 100},
            entities=self._entities(), relations=[],
            probe_fn=_fake_probe("pass"))
        assert out["proposals_created"] == 0


class TestNoEdgeSignalPayload:
    """no_edge hint cols 进 failure_signal payload (冷路径燃料补强)。"""

    def test_sql_exec埋点含no_edge_entities和cols(self):
        """熔断埋点 payload 应含 no_edge_signals: [{entities, cols}]。"""
        # 造一个 no_edge 的 hard_block 场景, 验证 log_failure_signal 收到的 payload
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
        assert result["verdict"] == "hard_block"
        # hints 里 no_edge 带 cols (Task 2 已做); 本 Task 验证的是:
        # 从 verdict 组装 signal payload 的辅助函数能抽出 no_edge_signals
        from capabilities.ontology.selfevolve.signals import extract_no_edge_signals
        signals = extract_no_edge_signals(result)
        assert len(signals) == 1
        assert signals[0]["entities"] == ["order", "payment"]
        assert signals[0]["cols"] == ["ORDER_ID", "ORDER_ID"]


class TestSqlExecSignalPayload:
    """sql_exec 熔断埋点把 no_edge_signals 写进 failure_signal payload (端到端)。"""

    def test_熔断埋点payload带no_edge_signals(self, monkeypatch, tmp_path):
        """第二次硬拦 → log_failure_signal 收到的 payload 含 no_edge_signals。

        冷路径燃料的真实来源: 没有这个字段, process_pending_signals 拿不到
        entity 对 + AI 首选列对, 整条新增提议链路断。
        """
        import agent.tools.sql_exec as sql_exec
        from core import ontology_view as ontology
        from capabilities.ontology.selfevolve.signals import log_failure_signal

        db_path = str(tmp_path / "sig.db")
        # 建有 schema 的库
        from capabilities.ontology.ontology_store import OntologyStore
        s = OntologyStore(db_path)
        s.close()

        captured = {}

        def fake_log(kind, payload, source=None, db_path=None):
            captured["kind"] = kind
            captured["payload"] = payload

        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.signals.log_failure_signal", fake_log)
        # sql_exec 是函数内 import, patch 模块属性即可
        monkeypatch.setattr(ontology, "_ontology", object())
        sql_exec.set_connections({"lib": _FakeConn()})

        def mock_obqc(sql, source, onto):
            return {
                "verdict": "hard_block",
                "violations": [{"type": "no_edge", "hit": "a JOIN b",
                                "reason": "x", "evidence": "y"}],
                "hints": [{"kind": "no_edge", "entities": ["a", "b"],
                           "cols": ["X", "Y"]}],
                "warnings": [], "skipped": [],
            }
        monkeypatch.setattr("capabilities.obqc.obqc_check", mock_obqc)

        sql = "SELECT * FROM t1 JOIN t2 ON t1.X=t2.Y"
        sql_exec._reset_circuit_breaker()
        sql_exec.execute_sql(sql, source="lib")  # 第 1 次: 攒熔断计数
        sql_exec.execute_sql(sql, source="lib")  # 第 2 次: 触发埋点
        sql_exec._reset_circuit_breaker()

        assert captured.get("kind") == "obqc_circuit_break"
        payload = captured.get("payload") or {}
        assert payload.get("no_edge_signals") == [
            {"entities": ["a", "b"], "cols": ["X", "Y"]}]


class TestNoEdgeFuelDecoupledFromCircuitBreaker:
    """no_edge 燃料采集脱钩熔断阈值 (2026-09-17 决策 A)。

    背景 (翻过全部历史 run 日志): 真实 no_edge hard_block 有一定数量, 但
    circuit_hit **全是 1**。因为 OBQC 的 hints 本就设计来教育 agent"这条边不合法, 换路",
    它工作得很好 → agent 撞一次就换路, 永远撞不到第二次。
    我们在用一个专门防止重复的机制, 去喂一个只吃重复的闸门 → 燃料恒为零。

    脱钩后: hard_block 里只要有 no_edge violation 就采集, 不看 circuit_hit。
    熔断阈值 2 保留原职 (只管 hint 升级), 质量由冷路径三道闸把关
    (探针 verdict=pass / 同列对去重 / 墓碑)。
    """

    def _setup(self, monkeypatch, tmp_path, violations, hints):
        """公共脚手架: 建库 + mock obqc + 截获 log_failure_signal。"""
        import agent.tools.sql_exec as sql_exec
        from core import ontology_view as ontology
        from capabilities.ontology.ontology_store import OntologyStore

        s = OntologyStore(str(tmp_path / "sig.db"))
        s.close()

        calls = []
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.signals.log_failure_signal",
            lambda kind, payload, source=None, db_path=None:
                calls.append({"kind": kind, "payload": payload}))
        monkeypatch.setattr(ontology, "_ontology", object())
        sql_exec.set_connections({"lib": _FakeConn()})
        monkeypatch.setattr("capabilities.obqc.obqc_check", lambda sql, source, onto: {
            "verdict": "hard_block", "violations": violations,
            "hints": hints, "warnings": [], "skipped": [],
        })
        return sql_exec, calls

    def test_首次no_edge硬拦即采集(self, monkeypatch, tmp_path):
        """第 1 次 hard_block 带 no_edge → 立刻写 failure_signal。

        这是脱钩的核心: 真 agent 每个指纹只撞 1 次, 等第 2 次等不到。
        """
        sql_exec, calls = self._setup(
            monkeypatch, tmp_path,
            violations=[{"type": "no_edge", "hit": "a JOIN b"}],
            hints=[{"kind": "no_edge", "entities": ["a", "b"],
                    "cols": ["X", "Y"]}])

        sql_exec._reset_circuit_breaker()
        sql_exec.execute_sql("SELECT * FROM t1 JOIN t2 ON t1.X=t2.Y",
                             source="lib")
        sql_exec._reset_circuit_breaker()

        assert len(calls) == 1, (
            "首次 no_edge 硬拦没采集 → 飞轮燃料恒为零 "
            "(实测真实 no_edge 全是 circuit_hit=1, 第 2 次永不到来)")
        assert calls[0]["kind"] == "obqc_circuit_break"
        assert calls[0]["payload"]["no_edge_signals"] == [
            {"entities": ["a", "b"], "cols": ["X", "Y"]}]
        assert calls[0]["payload"]["hit_count"] == 1

    def test_不带no_edge的硬拦首次不采集(self, monkeypatch, tmp_path):
        """column_not_found / join_col_mismatch 首次硬拦不该进 failure_signal。

        脱钩只对 no_edge 开口子 —— 它是唯一能产"新增边提议"的燃料。
        其余违规类型仍按老规矩 (熔断阈值 2), 否则每次拼错列名都攒一条信号,
        队列被噪音淹掉。
        """
        sql_exec, calls = self._setup(
            monkeypatch, tmp_path,
            violations=[{"type": "join_col_mismatch",
                         "hit": "p.LIB_LOAN_ID = c.MEMBER_NAME"}],
            hints=[])

        sql_exec._reset_circuit_breaker()
        sql_exec.execute_sql("SELECT * FROM t1 JOIN t2 ON t1.X=t2.Y",
                             source="lib")

        assert calls == [], (
            "非 no_edge 违规首次硬拦不该采集 (只有 no_edge 脱钩)")

        # 第 2 次撞满熔断阈值 → 老规矩照旧采集
        sql_exec.execute_sql("SELECT * FROM t1 JOIN t2 ON t1.X=t2.Y",
                             source="lib")
        sql_exec._reset_circuit_breaker()
        assert len(calls) == 1, "熔断阈值 2 的老路径被脱钩改动破坏了"
        assert calls[0]["payload"]["hit_count"] == 2

    def test_同一指纹重复撞不重复采集(self, monkeypatch, tmp_path):
        """同一 SQL 指纹在一个进程内撞 N 次, no_edge 只采集一次。

        脱钩后首次即采, 若不去重则 agent 万一重试就攒重复信号
        (冷路径有提议级去重, 但信号表本身不该膨胀)。
        """
        sql_exec, calls = self._setup(
            monkeypatch, tmp_path,
            violations=[{"type": "no_edge", "hit": "a JOIN b"}],
            hints=[{"kind": "no_edge", "entities": ["a", "b"],
                    "cols": ["X", "Y"]}])

        sql_exec._reset_circuit_breaker()
        for _ in range(3):
            sql_exec.execute_sql("SELECT * FROM t1 JOIN t2 ON t1.X=t2.Y",
                                 source="lib")
        sql_exec._reset_circuit_breaker()

        assert len(calls) == 1, (
            f"同一指纹撞 3 次采集了 {len(calls)} 条, 应只采 1 条")


class _FakeConn:
    """mock pymysql conn: 硬拦路径不碰库, 只过初始化守门。"""

    def cursor(self):
        raise AssertionError("hard_block 不应碰库")

    def ping(self, reconnect=True):
        pass


class TestCandidatePrefilter:
    """候选预筛: 笛卡儿积按命名 Jaccard 前置过滤 (2026-09-20f 债②)。

    背景: n_paths=0 燃料的 `cols` 是空的 (没 SQL 可抄列名) → `_candidate_pairs`
    退化成 orphan×PK 纯笛卡儿积。真实燃料产出几十个候选, 每个都要连真库跑
    几条 COUNT, 而里面 `REQUEST_DATE↔LIB_BRANCH_ID` 这种垃圾对占了大半。

    ★ 零语义损失的依据 (前提已验, 见 explore_candidate_prefilter_premise.py):
    探针第三层 `_layer3_guards` 本来就拿同一个阈值判, 不过 → unknown →
    `_process_one_no_edge` 只在 pass 时建提议 → **这些对本来永远产不出提议**。
    预筛只是把已有判据前移, 省掉真库往返。

    真实数据: 候选数降到原先的三分之一; 垃圾候选的存活率远低于已登记真边。
    """

    def _entities(self):
        return {
            "a": {"attributes": [{"name": "USER_ID", "kind": "pk"},
                                 {"name": "REQUEST_DATE"},
                                 {"name": "OPERATOR_ID"}]},
            "b": {"attributes": [{"name": "LIB_BRANCH_ID", "kind": "pk"}]},
        }

    def test_笛卡儿积里零交集的垃圾对被筛掉(self):
        """OPERATOR_ID↔LIB_BRANCH_ID / REQUEST_DATE↔LIB_BRANCH_ID jaccard=0 → 不进候选。"""
        from capabilities.ontology.selfevolve.probe import find_orphan_cols
        from capabilities.ontology.selfevolve.signals import _candidate_pairs

        pairs = _candidate_pairs("a", "b", [], self._entities(), [],
                                 find_orphan_cols)
        assert ("REQUEST_DATE", "LIB_BRANCH_ID") not in pairs, (
            "jaccard=0 的垃圾对仍在候选里, 预筛没生效")

    def test_命名相近的对必须留下(self):
        """★ 预筛不许替数据做判断: USER_ID↔LIB_BRANCH_ID 有共同 token ID → 留。

        真实案例 OPERATOR_ID↔USER_ID (jaccard=0.667) 是包含率极低的真阴性,
        它该由**数据**判死, 不该被命名筛提前判死 —— 否则我们是用错误的理由
        得到了对的结论。
        """
        from capabilities.ontology.selfevolve.probe import find_orphan_cols
        from capabilities.ontology.selfevolve.signals import _candidate_pairs

        pairs = _candidate_pairs("a", "b", [], self._entities(), [],
                                 find_orphan_cols)
        assert ("OPERATOR_ID", "LIB_BRANCH_ID") in pairs, (
            "共同 token 'ID' 的对被误筛了")

    def test_hint列对不受预筛影响(self):
        """agent 真写过的 SQL 里的列对 = 有行为证据, 再难看也要探。

        hint 来自 obqc 硬拦时抓的真实 JOIN 条件, 不是我们构造的笛卡儿积 ——
        预筛只针对后者。
        """
        from capabilities.ontology.selfevolve.probe import find_orphan_cols
        from capabilities.ontology.selfevolve.signals import _candidate_pairs

        pairs = _candidate_pairs("a", "b", ["REQUEST_DATE", "LIB_BRANCH_ID"],
                                 self._entities(), [], find_orphan_cols)
        assert pairs[0] == ("REQUEST_DATE", "LIB_BRANCH_ID"), (
            "hint 列对被预筛掉了 — agent 的真实行为证据不该被我们的启发式否决")

    def test_预筛阈值与探针第三层同源(self):
        """★ 防阈值分叉: 两处必须读同一个常量, 不许各写一份字面量。

        分叉的后果是静默的: 预筛严于探针 → 探针永远看不到那些对, 而探针的
        校准注释 (probe.py:21) 还写着"3 条真边降 unknown 可接受" —— 实际
        变成了"连探都不探", 两份文档谁都不对。
        """
        from capabilities.ontology.selfevolve import signals as sig
        from capabilities.ontology.selfevolve.probe import NAME_SIM_THRESHOLD

        assert sig._PREFILTER_SIM_THRESHOLD is NAME_SIM_THRESHOLD


class TestColdPathObservability:
    """冷路径埋点: 探针判定必须可观测 (2026-09-20f 债①)。

    背景: `signals.py` 一条 log_event 都没有, 探针的 fail/unknown **跑完即丢**
    (只有 pass 留痕在 proposal.probe_result_json)。于是
    `proposals_created=0` 在两种情况下返回值**完全相同**:
      (a) 探针逐对判过, 都不合格 → 正确的零产出
      (b) 候选构造为空 / 探针没被调到 → 静默失败
    上轮只能靠"包一层录制连接数真库语句"才分得开 —— 那是取证脚本的临时手段,
    日常没人会那么干。
    """

    def _entities(self):
        return {
            "a": {"attributes": [{"name": "X", "kind": "pk"}, {"name": "Y"}]},
            "b": {"attributes": [{"name": "X", "kind": "pk"}]},
        }

    @pytest.fixture
    def store(self, tmp_path):
        s = OntologyStore(str(tmp_path / "t.db"))
        yield s
        s.close()

    def _events(self, monkeypatch) -> list:
        """截获 log_event 调用 (不落盘)。"""
        seen = []
        monkeypatch.setattr(
            "core.observability.log_event",
            lambda event, payload=None: seen.append((event, payload or {})))
        return seen

    def _run(self, store, tmp_path, verdict: str, hint=None):
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{"entities": ["a", "b"],
                                 "cols": hint if hint is not None else ["X", "X"]}],
        }, db_path=str(tmp_path / "t.db"))
        return process_pending_signals(
            store,
            run_sql=lambda sql, source: [{"n": 100}],
            get_index_info=lambda t, s: {"pk_cols": ["X"], "unique_cols": [],
                                         "row_count": 100},
            entities=self._entities(), relations=[],
            probe_fn=_fake_probe(verdict))

    def test_探针fail也落埋点(self, store, tmp_path, monkeypatch):
        """★ 核心: fail 是最常见的结局, 恰恰是唯一不留痕的 —— 反了。"""
        seen = self._events(monkeypatch)
        out = self._run(store, tmp_path, "fail")
        assert out["proposals_created"] == 0
        probe_events = [p for e, p in seen if e == "selfevolve.probe_verdict"]
        assert probe_events, (
            "探针判了 fail 却零埋点 — 与'压根没跑探针'在日志上无从分辨")
        assert all(p["verdict"] == "fail" for p in probe_events)

    def test_埋点带得上实体对和列对(self, store, tmp_path, monkeypatch):
        """光记"判了 fail"不够, 要知道判的是**哪一对**才能复查。"""
        seen = self._events(monkeypatch)
        self._run(store, tmp_path, "fail")
        probe_events = [p for e, p in seen if e == "selfevolve.probe_verdict"]
        assert any(
            p.get("source") == "a" and p.get("target") == "b"
            and p.get("source_col") == "X" and p.get("target_col") == "X"
            for p in probe_events), f"埋点缺实体/列对: {probe_events}"

    def test_每对候选一条埋点(self, store, tmp_path, monkeypatch):
        """候选 = hint(X,X) + orphan(a)={X,Y}×PK(b)={X} → 去重后 (X,X),(Y,X)。

        (Y,X) jaccard=0 会被预筛掉 → 埋点里应能看出它被筛了, 而不是无声消失。
        """
        seen = self._events(monkeypatch)
        self._run(store, tmp_path, "fail")
        verdicts = [p for e, p in seen if e == "selfevolve.probe_verdict"]
        filtered = [p for e, p in seen if e == "selfevolve.candidates_filtered"]
        assert len(verdicts) == 1, f"期望只探存活的 (X,X), 实际 {verdicts}"
        assert filtered, "被预筛掉的对无任何痕迹 — 债②把债①的坑挖大了"
        assert filtered[0]["dropped"] == 1
        assert filtered[0]["kept"] == 1

    def test_信号级汇总埋点(self, store, tmp_path, monkeypatch):
        """一条信号处理完要有汇总: 探了几对/几个 pass —— "零产出"的正向痕迹。"""
        seen = self._events(monkeypatch)
        self._run(store, tmp_path, "fail")
        done = [p for e, p in seen if e == "selfevolve.no_edge_processed"]
        assert done, "信号处理完零汇总埋点"
        assert done[0]["probed"] == 1
        assert done[0]["passed"] == 0
        assert done[0]["entities"] == ["a", "b"]

    def test_unknown也落埋点(self, store, tmp_path, monkeypatch):
        """unknown = 证据缺失 (查询失败/NULL 太多), 与 fail 同样跑完即丢。"""
        seen = self._events(monkeypatch)
        self._run(store, tmp_path, "unknown")
        verdicts = [p for e, p in seen if e == "selfevolve.probe_verdict"]
        assert verdicts and all(p["verdict"] == "unknown" for p in verdicts)
