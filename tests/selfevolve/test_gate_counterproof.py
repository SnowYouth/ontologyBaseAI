"""tests/selfevolve/test_gate_counterproof.py — 两道闸的**反证**测试 (验收清单剩余两项).

区别于已有单测: 这里不 mock 被验证的闸本身, 只 mock 最外层的 LLM/真库依赖
(run_one_cq), 走 /selfevolve/proposals/{id}/apply 端点整条链, 断言坏边**真的进不了**
relation 表。

背景 (memory selfevolve-wiring-gaps-2026-09-17): 上轮端到端取证挖出飞轮燃料入口
四处"定义写好、调用点漏了"。这两项是出口侧的同类审计:
- CQ 回归门: 唯一拦坏边落库的闸 (spec §5.6 闸2)
- 墓碑机制: 被拒/软删过的三元组重新提议时必须带 tombstone_hit=1 标记

★ 回归门反证发现的真缺陷: apply_proposal 先落库 + 热重载 + 标 applied, 回归门
在其后跑, 失败时只改 proposal 状态 —— 坏边留在 relation 表里。闸门装在水闸后面。
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capabilities.ontology.ontology_store import OntologyStore


# ---------- 脚手架 ----------

def _te(blocked: bool) -> dict:
    """一条 tool_event; blocked=True 模拟 obqc 硬拦 (hard_block 判据是 violations 非空)。"""
    return {"name": "execute_sql", "args": "", "result": "返回 dict",
            "ms": 5, "ok": None, "used_edges": [],
            "violations": [{"type": "no_edge", "hit": "a JOIN b"}] if blocked else []}


def _cq_result(cq_id: str, blocked: bool) -> dict:
    return {"id": cq_id, "answer": "a", "tool_events": [_te(blocked)],
            "error": None, "verdict": "fail" if blocked else "pass", "note": ""}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离环境: tmp 本体库 + tmp kg_private (cq_set / baselines) + TestClient。"""
    import web.app as webapp
    from core.config import settings
    from capabilities.ontology.selfevolve import apply as apply_mod

    db_path = str(tmp_path / "onto.db")
    OntologyStore(db_path).close()
    monkeypatch.setattr(settings, "obda_ontology_path", db_path)

    kg = tmp_path / "kg_private"
    baselines = kg / "cq_baselines"
    baselines.mkdir(parents=True)
    cq_set = kg / "cq_set.json"
    cq_set.write_text(json.dumps({"cases": [
        {"id": "CQ-1", "question": "q", "kind": "single",
         "turns": None, "criteria": {}}]}), encoding="utf-8")
    monkeypatch.setattr("core.runtime.cq_runner.CQ_SET_PATH", cq_set)
    # current: apply 前的现状 (snapshot_baseline 会拷它当 baseline)
    cur_path = kg / "cq_result_current.json"
    cur_path.write_text(json.dumps({
        "ran_at": "t0", "ontology_path": db_path,
        "results": [_cq_result("CQ-1", blocked=False)]}), encoding="utf-8")
    monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH", cur_path)
    monkeypatch.setattr(apply_mod, "_BASELINES_DIR", baselines)

    return {"client": TestClient(webapp.app), "db_path": db_path,
            "baselines": baselines, "cq_set": cq_set}


def _mock_cq_rerun(monkeypatch, blocked: bool) -> None:
    """mock run_one_cq: 回归门重跑时的结果 (blocked=True → 新增 hard_block)。"""
    monkeypatch.setattr("core.runtime.cq_runner.run_one_cq",
                        lambda case: _cq_result(case["id"], blocked))


def _approved_proposal(db_path: str, source="a", target="b",
                       sc="X", tc="X") -> int:
    """造一条已批准待落地的 relation add 提议 (模拟"假边"经人审通过)。"""
    store = OntologyStore(db_path)
    try:
        pid = store.insert_proposal(
            "relation",
            {"source": source, "target": target, "source_col": sc,
             "target_col": tc, "via": f"{source}.{sc}={target}.{tc}",
             "type": "MANY_TO_ONE",
             # apply 拒绝无 label 的新增边 (2026-09-19); 真链路由人审填
             "label": f"{source}的{target}"},
            reason="探针 pass (假边反证用)")
        store.patch_proposal_review(pid, "approved", reviewed_by="human")
        return pid
    finally:
        store.close()


def _active_edges(db_path: str, source: str, target: str) -> list:
    store = OntologyStore(db_path)
    try:
        return [r for r in store.load_raw()["relationships"]
                if r["source"] == source and r["target"] == target]
    finally:
        store.close()


def _proposal(db_path: str, pid: int) -> dict:
    store = OntologyStore(db_path)
    try:
        return [p for p in store.list_proposals() if p["id"] == pid][0]
    finally:
        store.close()


# ---------- 验收 1: CQ 回归门反证 ----------

class TestRegressionGateCounterproof:
    """★ 假边 → approve → apply → 必须被拒且**不落库** (spec §5.6 闸2)。"""

    def test_回归fail_提议标failed(self, env, monkeypatch):
        pid = _approved_proposal(env["db_path"])
        _mock_cq_rerun(monkeypatch, blocked=True)

        resp = env["client"].post(f"/selfevolve/proposals/{pid}/apply")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

        p = _proposal(env["db_path"], pid)
        assert p["patch_status"] == "failed"
        assert p["regression_ok"] == 0

    def test_回归fail_坏边不得留在relation表(self, env, monkeypatch):
        """★ 核心反证: 闸拒绝了, 边就不能是 active 的。

        原实现 apply_proposal 先 upsert 后跑闸, 拒绝时不回滚 → 此断言见红。
        """
        pid = _approved_proposal(env["db_path"])
        _mock_cq_rerun(monkeypatch, blocked=True)

        env["client"].post(f"/selfevolve/proposals/{pid}/apply")

        assert _active_edges(env["db_path"], "a", "b") == [], \
            "回归门拒绝后, 假边仍留在 relation 表 (status=1) — 闸门形同虚设"

    def test_回归ok_边正常落库并标applied(self, env, monkeypatch):
        """对照组: 回归通过时边必须真落库 (防"回滚修复"过度拦截)。"""
        pid = _approved_proposal(env["db_path"])
        _mock_cq_rerun(monkeypatch, blocked=False)

        resp = env["client"].post(f"/selfevolve/proposals/{pid}/apply")
        assert resp.json()["ok"] is True

        edges = _active_edges(env["db_path"], "a", "b")
        assert len(edges) == 1
        assert edges[0]["source_col"] == "X"
        p = _proposal(env["db_path"], pid)
        assert p["patch_status"] == "applied"
        assert p["regression_ok"] == 1

    def test_回归门自身抛异常_改动必回滚(self, env, monkeypatch):
        """★ 闸"判都没判"也必须回滚 (2026-09-17 那条的第二个版本)。

        坏 JSON / LLM 异常 / 真库断连 / cq_set 运行中被改 → run_regression 抛异常。
        原实现只有 try/finally 没有 except → 异常直穿 500, 而 apply_proposal
        已把边落库 + patch_status 置成 applied + 热重载过 → 坏边留在表里。
        """
        pid = _approved_proposal(env["db_path"])

        def boom(store, proposal, **kw):
            raise RuntimeError("cq_set 在运行中被改")

        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression", boom)

        resp = env["client"].post(f"/selfevolve/proposals/{pid}/apply")

        assert resp.status_code != 500, "回归门异常直穿成 500 = 闸没判且没回滚"
        body = resp.json()
        assert body["ok"] is False
        assert "回归门异常" in body["detail"] or "cq_set 在运行中被改" in body["detail"]

        # ★ 核心物理后果: 边真的回滚了
        assert _active_edges(env["db_path"], "a", "b") == [], \
            "回归门抛异常后, 假边仍留在 relation 表 — 判都没判等于没有闸"

        p = _proposal(env["db_path"], pid)
        assert p["patch_status"] == "failed"
        assert p["regression_ok"] == 0

    def test_回归门中途进程死_重启服务后坏边被回滚(self, env, monkeypatch):
        """★ 端到端: 回归门跑到一半进程被杀 → 重启 (lifespan) 必须回滚。

        BaseException 模拟进程被杀: `except Exception` 接不住, 端点那层 rollback
        一行都不跑 —— 与 Ctrl+C / 被系统杀时库里的状态一致。
        """
        pid = _approved_proposal(env["db_path"])

        class Killed(BaseException):
            pass

        def killed(store, proposal, **kw):
            raise Killed()
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression", killed)
        with pytest.raises(Killed):
            env["client"].post(f"/selfevolve/proposals/{pid}/apply")
        assert len(_active_edges(env["db_path"], "a", "b")) == 1, "前提: 崩时边已落库"
        assert _proposal(env["db_path"], pid)["patch_status"] == "applied"

        import web.app as webapp
        monkeypatch.setattr(webapp, "init_agent_runtime", lambda: None)
        with TestClient(webapp.app):       # 进入 lifespan = 重启服务
            pass

        assert _active_edges(env["db_path"], "a", "b") == [], \
            "重启后没过闸的边仍在 relation 表"
        p = _proposal(env["db_path"], pid)
        assert p["patch_status"] == "failed" and p["regression_ok"] == 0

    def test_提议不存在仍返回404(self, env):
        """守 `except HTTPException: raise`: 裸 except Exception 会把 404 吞成 200。"""
        resp = env["client"].post("/selfevolve/proposals/99999/apply")
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_retire提议回归fail_软删要还原(self, env, monkeypatch):
        """retire 方向同理: 闸拒绝 → 被软删的边要恢复 active。"""
        store = OntologyStore(env["db_path"])
        try:
            store.upsert_relation({
                "source": "a", "target": "b", "via": "a.X=b.X",
                "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE"})
            pid = store.insert_proposal(
                "relation",
                {"source": "a", "target": "b", "via": "a.X=b.X",
                 "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE"},
                "退役反证", direction="retire")
            store.patch_proposal_review(pid, "approved", reviewed_by="human")
        finally:
            store.close()
        _mock_cq_rerun(monkeypatch, blocked=True)

        env["client"].post(f"/selfevolve/proposals/{pid}/apply")

        assert len(_active_edges(env["db_path"], "a", "b")) == 1, \
            "回归门拒绝 retire 后, 边应恢复 active"


# ---------- 验收 2: 墓碑机制反证 ----------

class TestTombstoneCounterproof:
    """★ rejected 提议 → 同三元组再 process → tombstone_hit=1 且不静默重复入队。

    已有 test_coldpath.py::TestTombstone 覆盖 signals 层; 这里补端到端:
    走 /selfevolve/process 端点 (真 process_pending_signals, 只 mock 探针)。
    """

    def _seed_entities(self, db_path: str) -> None:
        store = OntologyStore(db_path)
        try:
            for eid in ("a", "b"):
                store.upsert_entity({
                    "id": eid, "entityCode": eid.upper(),
                    "displayName": eid.upper(), "domain": "",
                    "maps_to": eid, "tables": [eid],
                    "attributes": [{"name": "X", "kind": "pk"}]})
        finally:
            store.close()

    def _signal(self, db_path: str) -> None:
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{"entities": ["a", "b"], "cols": ["X", "X"]}],
        }, db_path=db_path)

    def _mock_probe_pass(self, monkeypatch) -> None:
        from capabilities.ontology.selfevolve.probe import ProbeResult
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.probe.probe_relation",
            lambda se, te, sc, tc, run_sql, gii: ProbeResult(
                "pass", checks=[], evidence={}))

    def test_被拒提议_同三元组再process_带墓碑标记(self, env, monkeypatch):
        db_path = env["db_path"]
        self._seed_entities(db_path)
        self._mock_probe_pass(monkeypatch)

        # 第一轮: 产生提议, 人工 reject
        self._signal(db_path)
        env["client"].post("/selfevolve/process?limit=10")
        store = OntologyStore(db_path)
        try:
            first = store.list_proposals()
            assert len(first) == 1, f"第一轮应产 1 条提议, 实际 {len(first)}"
            first_id = first[0]["id"]
            assert first[0]["tombstone_hit"] == 0
        finally:
            store.close()
        env["client"].patch(f"/selfevolve/proposals/{first_id}/review",
                            json={"review_status": "rejected",
                                  "review_note": "业务上不成立"})

        # 第二轮: 同三元组信号再来
        self._signal(db_path)
        resp = env["client"].post("/selfevolve/process?limit=10")
        assert resp.json()["tombstone_hits"] == 1

        store = OntologyStore(db_path)
        try:
            fresh = [p for p in store.list_proposals() if p["id"] != first_id]
            assert len(fresh) == 1, "同三元组应恰好再入队 1 条 (墓碑不静默跳过)"
            assert fresh[0]["tombstone_hit"] == 1
            assert "业务上不成立" in (fresh[0]["tombstone_note"] or "")
        finally:
            store.close()

    def test_已pending提议_同三元组再process_不重复入队(self, env, monkeypatch):
        """去重 (区别于墓碑): pending 的同列对提议不再入队, 计 skipped。"""
        db_path = env["db_path"]
        self._seed_entities(db_path)
        self._mock_probe_pass(monkeypatch)

        self._signal(db_path)
        env["client"].post("/selfevolve/process?limit=10")
        self._signal(db_path)
        resp = env["client"].post("/selfevolve/process?limit=10")

        assert resp.json()["skipped"] == 1
        assert resp.json()["proposals_created"] == 0
        store = OntologyStore(db_path)
        try:
            assert len(store.list_proposals()) == 1
        finally:
            store.close()
