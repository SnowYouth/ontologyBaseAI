"""tests/selfevolve/test_web_selfevolve.py — /selfevolve/* 端点单测 (TDD, Task 7).

覆盖: proposals 列表 / review 审批 / process 冷路径触发 / apply 落地 /
health 只读观察端点 (spec §8.1 R-4) / cq-regression 淘汰半环触发。

隔离: monkeypatch settings.obda_ontology_path → tmp_path 库; process/apply
的探针和回归门全 mock (真库端到端走 manual smoke)。
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离环境: tmp 本体库 + tmp kg_private (baselines/current/cq_set)。

    ★ kg_private 也必须隔离 (2026-10-16 审计): /apply 端点在落库前调
    snapshot_baseline(), 它写 apply._BASELINES_DIR —— 默认是真
    kg_private/cq_baselines/。原先这个 fixture 只 patch 了本体库路径, 于是
    TestApplyEndpoint 每跑一次往真目录漏几个 baseline 文件, 攒到几百个文件、
    几十 MB 才被发现。conftest 的会话级污染守卫只盯 failure_signal 行数和
    config/model.json, 这个目录在它视野外。
    对照 test_gate_counterproof.py 的 env fixture —— 它三个路径都 patch 了,
    两个 fixture 的隔离面不对等就是这笔债的形状。
    """
    import web.app as webapp
    from capabilities.ontology.ontology_store import OntologyStore
    from capabilities.ontology.selfevolve import apply as apply_mod
    from core.config import settings

    db_path = str(tmp_path / "onto.db")
    OntologyStore(db_path).close()
    monkeypatch.setattr(settings, "obda_ontology_path", db_path)

    kg = tmp_path / "kg_private"
    baselines = kg / "cq_baselines"
    baselines.mkdir(parents=True)
    monkeypatch.setattr(apply_mod, "_BASELINES_DIR", baselines)
    monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH",
                        kg / "cq_result_current.json")
    monkeypatch.setattr("core.runtime.cq_runner.CQ_SET_PATH",
                        kg / "cq_set.json")
    return TestClient(webapp.app)


def _make_proposal(client, **kw) -> dict:
    """插入一条提议 (直接走 store, 端点只读回来)。"""
    from capabilities.ontology.ontology_store import OntologyStore
    from core.config import settings
    store = OntologyStore(settings.obda_ontology_path)
    try:
        pid = store.insert_proposal(
            kw.get("kind", "relation"),
            kw.get("payload", {"source": "a", "target": "b",
                               "source_col": "X", "target_col": "X",
                               "via": "a.X=b.X", "type": "MANY_TO_ONE"}),
            kw.get("reason", "探针 pass"), direction=kw.get("direction", "add"))
        return {"id": pid}
    finally:
        store.close()


class TestProposalsEndpoint:

    def test_列表默认全量(self, client):
        _make_proposal(client)
        resp = client.get("/selfevolve/proposals")
        assert resp.status_code == 200
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("proposals", [])
        assert len(data) == 1
        assert data[0]["kind"] == "relation"
        assert data[0]["review_status"] == "pending"

    def test_按review_status过滤(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.get("/selfevolve/proposals?review_status=approved")
        assert resp.status_code == 200
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("proposals", [])
        assert data == []  # 还没审批

    def test_字段齐全(self, client):
        _make_proposal(client)
        data = client.get("/selfevolve/proposals").json()
        if isinstance(data, dict):
            data = data.get("proposals", [])
        p = data[0]
        for field in ("id", "kind", "direction", "payload_json", "reason",
                      "review_status", "patch_status", "proposed_at",
                      "proposed_by", "tombstone_hit"):
            assert field in p, f"缺字段 {field}"


class TestReviewEndpoint:

    def test_审批通过(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "approved",
                                  "review_note": "ok", "label": "甲的乙"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        data = client.get("/selfevolve/proposals?review_status=approved").json()
        if isinstance(data, dict):
            data = data.get("proposals", [])
        assert len(data) == 1

    def test_非法状态422(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "nonsense"})
        assert resp.status_code == 422

    def test_不存在404(self, client):
        resp = client.patch("/selfevolve/proposals/999/review",
                            json={"review_status": "approved"})
        assert resp.status_code == 404


class TestReviewLabel:
    """★ 人审必须给新增边填 label (2026-09-19)。

    背景: apply 路径把 payload 原样 upsert, 而 probe 产的 payload 从来不带
    label → 飞轮落库的边全是裸边, 与 R10 那轮"裸边清零"的成果对冲。
    裸边的危害在 check_relation_label_distinct 的 docstring 里: 概念层/agent
    靠 get_join_path 返回的 label 选边, 没 label 就只能靠 source_col 前缀猜。

    为什么落在人审这一刻: 人审正在判这条边的业务语义 (它是否成立、走不走得通),
    顺手写一句 label 成本最低、质量最高。机器按列名拼的草稿是"有 label 但无
    信息量", 比裸边更坏 —— 从此看不出哪条是待补的。
    """

    def test_approve新增边不填label被拒(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "approved"})
        assert resp.status_code == 400
        assert "label" in resp.json()["detail"]

    def test_approve新增边填了label写进payload(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "approved",
                                  "label": "订单的变更记录"})
        assert resp.status_code == 200
        data = client.get("/selfevolve/proposals").json()
        if isinstance(data, dict):
            data = data.get("proposals", [])
        payload = json.loads(data[0]["payload_json"])
        assert payload["label"] == "订单的变更记录"
        # 原有字段不能被踩掉
        assert payload["source"] == "a" and payload["target"] == "b"
        assert payload["via"] == "a.X=b.X"

    def test_label纯空白等于没填(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "approved", "label": "   "})
        assert resp.status_code == 400

    def test_reject不要求label(self, client):
        """拒绝不落库, 没有 label 可言。"""
        pid = _make_proposal(client)["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "rejected",
                                  "review_note": "业务上走不准"})
        assert resp.status_code == 200

    def test_retire方向不要求label(self, client):
        """retire 是软删一条**现有**边, label 早就在库里, 不该逼人重填。"""
        pid = _make_proposal(client, direction="retire",
                            payload={"source": "a", "target": "b",
                                     "via": "a.X=b.X"})["id"]
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "approved"})
        assert resp.status_code == 200

    def test_坏payload_json不炸(self, client):
        """payload_json 解析不了时报 400 说清楚, 不是 500。"""
        from capabilities.ontology.ontology_store import OntologyStore
        from core.config import settings
        pid = _make_proposal(client)["id"]
        store = OntologyStore(settings.obda_ontology_path)
        try:
            store._conn.execute(
                "UPDATE proposal SET payload_json='{坏' WHERE id=?", (pid,))
            store._conn.commit()
        finally:
            store.close()
        resp = client.patch(f"/selfevolve/proposals/{pid}/review",
                            json={"review_status": "approved", "label": "x"})
        assert resp.status_code == 400
        assert "payload" in resp.json()["detail"]


class TestProcessEndpoint:

    def test_处理信号返回统计(self, client, monkeypatch, tmp_path):
        """process 端点调 process_pending_signals (注入 mock 探针)。"""
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        from core.config import settings
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp1",
            "no_edge_signals": [{"entities": ["a", "b"], "cols": ["X", "X"]}],
        }, db_path=settings.obda_ontology_path)

        # mock 探针 (不连真库): process_pending_signals 在函数体内
        # from capabilities.ontology.selfevolve.probe import probe_relation → patch 源模块
        from capabilities.ontology.selfevolve.probe import ProbeResult

        def fake_probe(se, te, sc, tc, run_sql, get_index_info):
            return ProbeResult("pass", checks=[], evidence={})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.probe.probe_relation", fake_probe)

        # entities/relations 从本体读: 造 a/b 实体进库
        from capabilities.ontology.ontology_store import OntologyStore
        store = OntologyStore(settings.obda_ontology_path)
        try:
            store.upsert_entity({
                "id": "a", "entityCode": "A", "displayName": "A",
                "domain": "", "maps_to": "a", "tables": ["a"],
                "attributes": [{"name": "X", "kind": "pk"}]})
            store.upsert_entity({
                "id": "b", "entityCode": "B", "displayName": "B",
                "domain": "", "maps_to": "b", "tables": ["b"],
                "attributes": [{"name": "X", "kind": "pk"}]})
        finally:
            store.close()

        resp = client.post("/selfevolve/process?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 1
        assert data["proposals_created"] >= 1
        # 提议出现在列表里
        plist = client.get("/selfevolve/proposals").json()
        if isinstance(plist, dict):
            plist = plist.get("proposals", [])
        assert len(plist) >= 1


class TestProcessStreamEndpoint:
    """SSE 版冷路径 (A1: 同步阻塞修复)。

    ★ 为什么新增端点而不是把 /selfevolve/process 改成 SSE:
    那个端点有 5 处调用方依赖它返回 JSON (含两个端到端取证脚本),
    直接改流式会全部破坏。老端点保留不动, 前端切到流式版。
    """

    def _setup_signal(self, monkeypatch):
        from capabilities.ontology.selfevolve.signals import log_failure_signal
        from capabilities.ontology.selfevolve.probe import ProbeResult
        from capabilities.ontology.ontology_store import OntologyStore
        from core.config import settings

        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "fp-sse",
            "no_edge_signals": [{"entities": ["a", "b"], "cols": ["X", "X"]}],
        }, db_path=settings.obda_ontology_path)

        def fake_probe(se, te, sc, tc, run_sql, get_index_info):
            return ProbeResult("pass", checks=[], evidence={})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.probe.probe_relation", fake_probe)

        store = OntologyStore(settings.obda_ontology_path)
        try:
            for eid in ("a", "b"):
                store.upsert_entity({
                    "id": eid, "entityCode": eid.upper(),
                    "displayName": eid.upper(), "domain": "",
                    "maps_to": eid, "tables": [eid],
                    "attributes": [{"name": "X", "kind": "pk"}]})
        finally:
            store.close()

    def _events(self, resp) -> list:
        out = []
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
        return out

    def test_流式返回done事件带统计(self, client, monkeypatch):
        self._setup_signal(monkeypatch)
        resp = client.post("/selfevolve/process-stream?limit=10")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = self._events(resp)
        done = [e for e in events if e["type"] == "done"]
        assert len(done) == 1, f"没收到 done 事件: {events}"
        assert done[0]["processed"] == 1
        assert done[0]["proposals_created"] >= 1

    def test_流式先推start事件(self, client, monkeypatch):
        """★ 修阻塞的意义就在这: 人要立刻看到"开跑了", 而不是干等。"""
        self._setup_signal(monkeypatch)
        events = self._events(
            client.post("/selfevolve/process-stream?limit=10"))
        assert events[0]["type"] == "start"
        assert events[0]["pending"] == 1, "start 事件要带待处理信号数"

    def test_异常走error事件不是500(self, client, monkeypatch):
        """★ 后台线程里抛异常, HTTP 头早就发出去了 —— 必须转成 error 事件,
        否则前端只看到连接断开, 不知道为什么 (安静的失败)。"""
        self._setup_signal(monkeypatch)

        def boom(*a, **kw):
            raise RuntimeError("探针炸了")
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.signals.process_pending_signals", boom)

        events = self._events(
            client.post("/selfevolve/process-stream?limit=10"))
        err = [e for e in events if e["type"] == "error"]
        assert len(err) == 1, f"异常没转成 error 事件: {events}"
        assert "探针炸了" in err[0]["error"]

    def test_老端点仍返回json(self, client, monkeypatch):
        """★ 回归保护: 5 处调用方 (含端到端取证脚本) 依赖它是 JSON。"""
        self._setup_signal(monkeypatch)
        resp = client.post("/selfevolve/process?limit=10")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["processed"] == 1


class TestApplyEndpoint:

    def test_未审批的拒绝apply(self, client):
        pid = _make_proposal(client)["id"]
        resp = client.post(f"/selfevolve/proposals/{pid}/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "审批" in data["detail"] or "review" in data["detail"]

    def test_审批后apply成功_实体先存在(self, client, monkeypatch):
        pid = _make_proposal(client)["id"]
        # 审批
        client.patch(f"/selfevolve/proposals/{pid}/review",
                     json={"review_status": "approved", "review_note": "",
                           "label": "甲的乙 (人审必填)"})
        # mock 回归门 (真 CQ 跑不了)
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression",
            lambda store, proposal, **kw: {
                "regression_ok": True, "run_id": "r1",
                "new_hard_blocks": []})
        resp = client.post(f"/selfevolve/proposals/{pid}/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # patch_status 变 applied
        plist = client.get("/selfevolve/proposals?review_status=approved").json()
        if isinstance(plist, dict):
            plist = plist.get("proposals", [])
        assert plist[0]["patch_status"] == "applied"

    def test_apply放行时未被闸守住的CQ要出现在响应里(self, client, monkeypatch):
        """★ 闸放行分两种: 真没坏 vs 根本没看, 二者必须可分辨 (2026-09-19)。

        run_regression 返回 uncovered_cqs (baseline 没跑过 → 这轮判不了),
        web 层若不透出, ok=True 就会被人读成"全套 CQ 都守住了"。
        """
        pid = _make_proposal(client)["id"]
        client.patch(f"/selfevolve/proposals/{pid}/review",
                     json={"review_status": "approved", "review_note": "",
                           "label": "甲的乙 (人审必填)"})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression",
            lambda store, proposal, **kw: {
                "regression_ok": True, "run_id": "r1",
                "new_hard_blocks": [], "uncovered_cqs": ["CQ-20", "CQ-21"]})
        data = client.post(f"/selfevolve/proposals/{pid}/apply").json()
        assert data["ok"] is True
        assert data["uncovered_cqs"] == ["CQ-20", "CQ-21"]
        assert "CQ-20" in (data.get("warning") or ""), "未守住的 CQ 要有人话警告"

    def test_apply拒绝时也带uncovered(self, client, monkeypatch):
        pid = _make_proposal(client)["id"]
        client.patch(f"/selfevolve/proposals/{pid}/review",
                     json={"review_status": "approved", "review_note": "",
                           "label": "甲的乙 (人审必填)"})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression",
            lambda store, proposal, **kw: {
                "regression_ok": False, "run_id": "r1",
                "new_hard_blocks": ["CQ-3"], "uncovered_cqs": ["CQ-20"]})
        data = client.post(f"/selfevolve/proposals/{pid}/apply").json()
        assert data["ok"] is False
        assert data["uncovered_cqs"] == ["CQ-20"]

    def test_与边无因果的噪音硬拦要透出(self, client, monkeypatch):
        """★ 真跑实测 (2026-09-19): CQ-14 因 column_not_found 噪音硬拦。

        不定罪 (regression_ok=True), 但必须透出 —— 否则"闸放行"这一个信号
        同时代表"真没坏"和"有 CQ 挂了只是判不到它头上", 人无从分辨。
        """
        pid = _make_proposal(client)["id"]
        client.patch(f"/selfevolve/proposals/{pid}/review",
                     json={"review_status": "approved", "review_note": "",
                           "label": "甲的乙 (人审必填)"})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression",
            lambda store, proposal, **kw: {
                "regression_ok": True, "run_id": "r1", "new_hard_blocks": [],
                "uncovered_cqs": [], "unrelated_blocks": ["CQ-14"]})
        data = client.post(f"/selfevolve/proposals/{pid}/apply").json()
        assert data["ok"] is True
        assert data["unrelated_blocks"] == ["CQ-14"]
        assert "CQ-14" in (data.get("warning") or ""), "噪音硬拦要有人话说明"

    def test_apply透出truncated_cqs(self, client, monkeypatch):
        """★ 截断的 CQ 没跑完 → 放行不可信, 必须透出 (2026-10-20)。

        与 unrelated_blocks 同理: 闸的定罪依据是 tool_events 里的违规, 一次
        被 max_tokens 截断的运行可能根本没走到那句会被拦的 execute_sql,
        它的"零违规"不构成"没坏"的证据。
        """
        pid = _make_proposal(client)["id"]
        client.patch(f"/selfevolve/proposals/{pid}/review",
                     json={"review_status": "approved", "review_note": "",
                           "label": "甲的乙 (人审必填)"})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression",
            lambda store, proposal, **kw: {
                "regression_ok": True, "run_id": "r1", "new_hard_blocks": [],
                "uncovered_cqs": [], "unrelated_blocks": [],
                "truncated_cqs": ["CQ-14"]})
        data = client.post(f"/selfevolve/proposals/{pid}/apply").json()
        assert data["ok"] is True
        assert data["truncated_cqs"] == ["CQ-14"]
        w = data.get("warning") or ""
        assert "CQ-14" in w, "截断的 CQ 要有人话警告"
        assert "MAX_TOKENS" in w, "要告诉人怎么修"

    def test_apply透出no_db_evidence_cqs(self, client, monkeypatch):
        """★ 没查到库的 CQ 零违规不可信, 必须透出 (2026-10-21)。"""
        pid = _make_proposal(client)["id"]
        client.patch(f"/selfevolve/proposals/{pid}/review",
                     json={"review_status": "approved", "review_note": "",
                           "label": "甲的乙 (人审必填)"})
        monkeypatch.setattr(
            "capabilities.ontology.selfevolve.apply.run_regression",
            lambda store, proposal, **kw: {
                "regression_ok": True, "run_id": "r1", "new_hard_blocks": [],
                "uncovered_cqs": [], "unrelated_blocks": [],
                "truncated_cqs": [], "no_db_evidence_cqs": ["CQ-5"]})
        data = client.post(f"/selfevolve/proposals/{pid}/apply").json()
        assert data["ok"] is True
        assert data["no_db_evidence_cqs"] == ["CQ-5"]
        assert "CQ-5" in (data.get("warning") or "")


class TestHealthEndpoint:
    """GET /selfevolve/health (spec §8.1 R-4): suspect_count>0 的行, DESC 排序。"""

    def test_只返回suspect行_降序(self, client):
        from capabilities.ontology.ontology_store import OntologyStore
        from core.config import settings
        store = OntologyStore(settings.obda_ontology_path)
        try:
            store.upsert_relation_health("a", "b", "a.X=b.X",
                                         use_count_delta=10)
            store.upsert_relation_health("c", "d", "c.Y=d.Y",
                                         suspect_count_delta=3)
            store.upsert_relation_health("e", "f", "e.Z=f.Z",
                                         suspect_count_delta=1,
                                         use_count_delta=5)
        finally:
            store.close()

        resp = client.get("/selfevolve/health")
        assert resp.status_code == 200
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("health", [])
        # 只有 suspect>0 的两行, 按 suspect DESC
        assert len(data) == 2
        assert data[0]["source"] == "c"
        assert data[0]["suspect_count"] == 3
        assert data[1]["source"] == "e"
        for row in data:
            for field in ("source", "target", "via", "use_count",
                          "suspect_count", "last_suspect_at"):
                assert field in row


class TestSafeTableIdent:
    """_safe_table_ident — 表名白名单校验 (还债: COUNT(*) 那句在拼表名)。

    表名来自本体 entity id, 不是外部直接输入, 但本体可从提议/CRUD 落新实体,
    "上游可信"这个假设不该写死在一条拼接语句里。
    """

    def test_正常表名放行(self):
        from web.app import _safe_table_ident
        assert _safe_table_ident("lib_loan") == "lib_loan"
        assert _safe_table_ident("OC_LIB_BRANCH_CHARGE") == "OC_LIB_BRANCH_CHARGE"
        assert _safe_table_ident("t1_2") == "t1_2"

    @pytest.mark.parametrize("bad", [
        "a`b",              # 反引号闭合 → 逃出标识符
        "a b",              # 空格
        "a;DROP TABLE x",   # 语句分隔
        "a-b", "a.b", "a'b", "", "a*", "a/*x*/",
    ])
    def test_非法表名拒绝(self, bad):
        from web.app import _safe_table_ident
        with pytest.raises(ValueError):
            _safe_table_ident(bad)


class TestHealthRetireEndpoint:
    """POST /selfevolve/health/retire — 可疑边一键提 retire 提议 (2026-09-18)。

    health 面板原先纯只读: 可疑边攒够了只能干看着, 要人手工去库里塞提议。
    这个端点把"观察"接到"人审队列", 但**只生成提议**, 落地仍走原来的
    approve → apply → 回归门, 不绕过任何一道闸。
    """

    def _seed_edge_and_health(self, suspect: int = 3):
        from capabilities.ontology.ontology_store import OntologyStore
        from core.config import settings
        store = OntologyStore(settings.obda_ontology_path)
        try:
            store.upsert_relation({
                "source": "lib_loan", "target": "lib_reader",
                "source_col": "READER_ID", "target_col": "MEMBER_ID",
                "type": "MANY_TO_ONE"})
            store.upsert_relation_health(
                "lib_loan", "lib_reader",
                "lib_loan.READER_ID=lib_reader.MEMBER_ID",
                suspect_count_delta=suspect)
        finally:
            store.close()
        return "lib_loan.READER_ID=lib_reader.MEMBER_ID"

    def test_一键提retire进人审队列(self, client):
        via = self._seed_edge_and_health()
        resp = client.post("/selfevolve/health/retire",
                           json={"source": "lib_loan", "target": "lib_reader",
                                 "via": via})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

        plist = client.get("/selfevolve/proposals").json()
        if isinstance(plist, dict):
            plist = plist.get("proposals", [])
        retires = [p for p in plist if p["direction"] == "retire"]
        assert len(retires) == 1
        assert retires[0]["kind"] == "relation_col"
        # ★ 必须停在人审队列, 不许自动落地
        assert retires[0]["review_status"] == "pending"
        assert retires[0]["patch_status"] == "pending"
        assert json.loads(retires[0]["payload_json"])["via"] == via
        assert retires[0]["proposed_by"] == "human_health_panel"

    def test_边不存在时拒绝(self, client):
        """★ 与 apply 同一道守卫: 查无此行不许生成提议 (否则 apply 必失败)。"""
        resp = client.post("/selfevolve/health/retire",
                           json={"source": "a", "target": "b",
                                 "via": "a.GHOST=b.GHOST"})
        assert resp.status_code == 404

    def test_重复提交不生成第二条(self, client):
        """已有 pending retire → 幂等, 不堆重复提议。"""
        via = self._seed_edge_and_health()
        body = {"source": "lib_loan", "target": "lib_reader", "via": via}
        client.post("/selfevolve/health/retire", json=body)
        resp = client.post("/selfevolve/health/retire", json=body)
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

        plist = client.get("/selfevolve/proposals").json()
        if isinstance(plist, dict):
            plist = plist.get("proposals", [])
        assert len([p for p in plist if p["direction"] == "retire"]) == 1

    def test_带墓碑命中标记(self, client):
        """★ 墓碑机制对人工提的提议同样生效 (这条边此前被拒过 → 标记出来)。"""
        via = self._seed_edge_and_health()
        from capabilities.ontology.ontology_store import OntologyStore
        from core.config import settings
        store = OntologyStore(settings.obda_ontology_path)
        try:
            pid = store.insert_proposal(
                "relation",
                {"source": "lib_loan", "target": "lib_reader", "via": via},
                "早先提过", direction="add")
            store.patch_proposal_review(pid, "rejected", reviewed_by="human",
                                        review_note="业务上走不准")
        finally:
            store.close()

        client.post("/selfevolve/health/retire",
                    json={"source": "lib_loan", "target": "lib_reader", "via": via})
        plist = client.get("/selfevolve/proposals").json()
        if isinstance(plist, dict):
            plist = plist.get("proposals", [])
        retires = [p for p in plist if p["direction"] == "retire"]
        assert retires[0]["tombstone_hit"] == 1


def _write_cq_files(tmp_path, monkeypatch, cur_results, prev_results):
    """把两轮 CQ 结果写进 tmp_path 并把 cq_runner 的路径指过去。"""
    def _dump(name, results, ran_at):
        p = tmp_path / name
        p.write_text(json.dumps(
            {"ran_at": ran_at, "ontology_path": "x", "results": results},
            ensure_ascii=False), encoding="utf-8")
        return p
    cur = _dump("cq_result_current.json", cur_results, "2026-09-18T10:00:00")
    prev = _dump("cq_result_previous.json", prev_results, "2026-09-17T10:00:00")
    monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH", cur)
    monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH", prev)


def _cq_result(cq_id, verdict, used_edges=None, violations=None):
    te = {"name": "execute_sql", "args": "", "result": "r", "ms": 1, "ok": True,
          "used_edges": used_edges or [], "violations": violations or []}
    return {"id": cq_id, "answer": "a", "tool_events": [te], "error": None,
            "verdict": verdict, "note": ""}


class TestCqRegressionEndpoint:
    """POST /selfevolve/cq-regression — 淘汰半环唯一生产入口。

    ★ 这个端点在 2026-09-18 之前不存在: process_cq_regression 写好了但零调用者,
    整个 retire 半环 (verdict 下降归因 + mismatch 累计) 在生产里从未跑过。
    """

    def test_verdict下降_suspect落库(self, client, tmp_path, monkeypatch):
        edge = {"source": "a", "target": "b", "via": "a.X=b.X",
                "source_col": "X", "target_col": "X"}
        _write_cq_files(
            tmp_path, monkeypatch,
            [_cq_result("CQ-1", "fail", used_edges=[edge])],
            [_cq_result("CQ-1", "pass", used_edges=[])])

        resp = client.post("/selfevolve/cq-regression")
        assert resp.status_code == 200
        data = resp.json()
        assert data["suspects_updated"] == 1

        # 落库了才算数 —— 走 health 端点验物理后果, 不信返回值自述
        health = client.get("/selfevolve/health").json()["health"]
        assert len(health) == 1
        assert health[0]["source"] == "a"
        assert health[0]["suspect_count"] == 1

    def test_没打分时响亮告警_不静默返回0(self, client, tmp_path, monkeypatch):
        """★ 关键: save_current 落盘 verdict=None, 人不打分归因必然空转。

        实测 2026-09-17 两份真实结果文件里的记录全是 None。端点必须把"上轮 0 条已
        打分"讲出来, 否则用户看到 suspects_updated=0 会以为"本体很健康"。
        """
        edge = {"source": "a", "target": "b", "via": "a.X=b.X"}
        _write_cq_files(
            tmp_path, monkeypatch,
            [_cq_result("CQ-1", None, used_edges=[edge])],
            [_cq_result("CQ-1", None, used_edges=[])])

        data = client.post("/selfevolve/cq-regression").json()
        assert data["suspects_updated"] == 0
        assert data["scored_current"] == 0
        assert data["scored_previous"] == 0
        assert data["total_current"] == 1
        assert data["warning"], "两轮都没打分必须给 warning, 不许静默返回 0"
        assert "打分" in data["warning"]

    def test_两轮都打分时无告警(self, client, tmp_path, monkeypatch):
        edge = {"source": "a", "target": "b", "via": "a.X=b.X"}
        _write_cq_files(
            tmp_path, monkeypatch,
            [_cq_result("CQ-1", "pass", used_edges=[edge])],
            [_cq_result("CQ-1", "pass", used_edges=[edge])])

        data = client.post("/selfevolve/cq-regression").json()
        assert data["scored_current"] == 1
        assert data["scored_previous"] == 1
        assert data["warning"] is None

    def test_mismatch累计达阈值_生成retire提议(self, client, tmp_path, monkeypatch):
        """mismatch 路径不依赖 verdict: 同一错误列对在 3 个不同 CQ 出现 → retire 提议。

        ★ 嫌疑人是 legal_edges 里那条**本体现有的边**, 不是错写列对 a.X=b.Y
        派生出的虚构边 (2026-09-18: 后者按 mismatch 定义在 relation 表必然不存在)。
        """
        def _v(cq_id):
            # hit 格式随真实 obqc violation: 'la.lcol = ra.rcol' (等号两侧带空格)
            return _cq_result(cq_id, None, violations=[{
                "type": "join_col_mismatch", "hit": "a.X = b.Y",
                "entities": ["a", "b"],
                "legal_edges": [{"source": "a", "target": "b",
                                 "via": "a.LEGAL_ID=b.LEGAL_ID",
                                 "source_col": "LEGAL_ID",
                                 "target_col": "LEGAL_ID"}]}])
        _write_cq_files(
            tmp_path, monkeypatch,
            [_v("CQ-1"), _v("CQ-2"), _v("CQ-3")], [])

        data = client.post("/selfevolve/cq-regression").json()
        assert data["retire_proposals_created"] == 1

        plist = client.get("/selfevolve/proposals").json()
        if isinstance(plist, dict):
            plist = plist.get("proposals", [])
        retires = [p for p in plist if p["direction"] == "retire"]
        assert len(retires) == 1
        assert retires[0]["kind"] == "relation_col"
        assert json.loads(retires[0]["payload_json"])["via"] == "a.LEGAL_ID=b.LEGAL_ID"

    def test_缺结果文件不炸(self, client, tmp_path, monkeypatch):
        """CQ 从没跑过 (两份文件都不存在) → 返回全 0 + warning, 不是 500。"""
        monkeypatch.setattr("core.runtime.cq_runner.CUR_PATH", tmp_path / "nope.json")
        monkeypatch.setattr("core.runtime.cq_runner.PREV_PATH", tmp_path / "nope2.json")
        resp = client.post("/selfevolve/cq-regression")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_current"] == 0
        assert data["warning"]
