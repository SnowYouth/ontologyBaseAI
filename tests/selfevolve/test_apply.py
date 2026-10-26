"""tests/selfevolve/test_apply.py — apply_proposal + CQ 回归门 单测 (TDD, Task 6).

覆盖:
- apply_proposal 审批守卫 (approved+pending 才执行 / relation_col MVP 拒绝)
- _APPLY_DISPATCH 分发
- _apply_relation_proposal: upsert_relation + 本体刷新
- _apply_retire_proposal: 依赖检查拦 / 无依赖软删
- check_relation_dependencies: concept_condition 多实体组 / filter_rule entity 命中
  (★ 已知坑: 真实表是结构化列非 JSON join_path, 按 entity 列查)
- run_regression: baseline 对比 / new hard_block 拒绝 / sha256 钉死 (mock run_one_cq)

CQ 回归门的真库端到端验证走 tests/manual/smoke (spec §9: 单测跳过 run_agent 路径),
这里全 mock。
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capabilities.ontology.ontology_store import OntologyStore
from capabilities.ontology.selfevolve.apply import (
    apply_proposal,
    check_relation_dependencies,
    rollback_apply,
    run_regression,
)


@pytest.fixture
def store(tmp_path):
    s = OntologyStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _approved_relation_proposal(store, source="a", target="b",
                                sc="X", tc="X", label="甲的乙") -> int:
    """造一条已批准待落地的 relation add 提议。

    label 默认非空: 自 2026-09-19 起 apply 拒绝无 label 的新增边 (见
    TestApplyLabelGuard), 真实链路里由人审那一步填进 payload。
    """
    via = f"{source}.{sc}={target}.{tc}"
    payload = {"source": source, "target": target, "source_col": sc,
               "target_col": tc, "via": via, "type": "MANY_TO_ONE"}
    if label is not None:
        payload["label"] = label
    pid = store.insert_proposal("relation", payload, reason="test")
    store.patch_proposal_review(pid, "approved", reviewed_by="human")
    return pid


class TestApplyGuard:
    """审批守卫 + dispatch + relation_col 拒绝。"""

    def test_未批准拒绝(self, store):
        pid = store.insert_proposal("relation", {"source": "a"}, "r")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "review" in out["detail"] or "审批" in out["detail"]

    def test_已applied拒绝(self, store):
        pid = _approved_relation_proposal(store)
        store.patch_proposal_patch_status(pid, "applied")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "applied" in out["detail"] or "已" in out["detail"]

    def test_拿着过期dict并发apply_第二个抢不到且不碰库(self, store):
        """★ 审计 #5 TOCTOU: 两个请求先后读到 patch_status=pending 的 dict,
        第一个已落库并占位后, 第二个不许过闸 —— 否则它拍到 existed=True 之外的
        快照、回滚时可能硬删第一个已过闸的边 (ontology.db 不进 git, 不可逆)。

        用"过期 dict"确定性复现交错, 不靠线程碰运气。
        """
        _approved_relation_proposal(store)
        stale = store.list_proposals()[0]
        assert apply_proposal(dict(stale), store)["ok"] is True

        upserts = []
        orig = store.upsert_relation
        store.upsert_relation = lambda p: (upserts.append(p), orig(p))[1]
        out = apply_proposal(dict(stale), store)
        assert out["ok"] is False
        assert "applied" in out["detail"] or "已" in out["detail"]
        assert upserts == [], "抢占失败后还落了库"
        assert "rollback_snapshot" not in out

    def test_handler抛异常时退还占位(self, store, monkeypatch):
        """CLI 等直接调用方没有端点那层兜底, 异常后不能卡在 applied 再也重试不了。"""
        _approved_relation_proposal(store)
        p = store.list_proposals()[0]

        def boom(_payload):
            raise RuntimeError("db down")
        monkeypatch.setattr(store, "upsert_relation", boom)
        with pytest.raises(RuntimeError):
            apply_proposal(p, store)
        assert store.list_proposals()[0]["patch_status"] == "pending"

    def test_relation_col无via拒绝(self, store):
        """★ 守卫: payload 缺 via (定位不到边) → 拒绝, 不许静默匹配 0 行。

        老行为是无条件拒 relation_col (MVP 限制); 2026-09-18 起支持 apply,
        但必须带 via —— 没有 via 就无从定位, soft_delete 会匹配 0 行且返回成功,
        正是本项目反复栽的"安静的无害失败"。
        """
        pid = store.insert_proposal(
            "relation_col", {"source": "a", "target": "b",
                             "col_pair": "X=Y"}, "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "via" in out["detail"]

    def test_relation_col软删真边成功(self, store):
        """★ 2026-09-18: relation_col retire 落地 = 软删 legal_edges 指认的那条真边。"""
        store.upsert_relation({
            "source": "lib_loan", "target": "lib_reader",
            "source_col": "READER_ID", "target_col": "MEMBER_ID",
            "type": "MANY_TO_ONE"})
        via = "lib_loan.READER_ID=lib_reader.MEMBER_ID"
        pid = store.insert_proposal(
            "relation_col",
            {"source": "lib_loan", "target": "lib_reader", "via": via,
             "source_col": "READER_ID", "target_col": "MEMBER_ID",
             "col_pair": "MEMBER_ID=ZZZ_ID"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is True, out["detail"]
        raw = store.load_raw()
        assert not [r for r in raw["relationships"] if r["via"] == via], "边没被软删"

    def test_relation_col边不存在时响亮失败(self, store):
        """★ 反证: via 在 relation 表查无此行 → 必须报错, 不许静默"成功"。

        这条守的是本次修复的核心 —— 老代码的 retire 提议指向虚构边,
        若 apply 照做且不校验, 就会匹配 0 行然后欢快地返回 ok=True。
        """
        pid = store.insert_proposal(
            "relation_col",
            {"source": "lib_loan", "target": "lib_reader",
             "via": "lib_loan.GHOST_ID=lib_reader.GHOST_ID",
             "col_pair": "GHOST_ID=GHOST_ID"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "不存在" in out["detail"] or "查无" in out["detail"]

    def test_relation_col有依赖被闸1拦(self, store):
        """retire 闸1 对 relation_col 同样生效 (concept_condition 挂着 → 拦)。"""
        store.upsert_relation({
            "source": "lib_loan", "target": "lib_reader",
            "source_col": "READER_ID", "target_col": "MEMBER_ID",
            "type": "MANY_TO_ONE"})
        via = "lib_loan.READER_ID=lib_reader.MEMBER_ID"
        store._conn.execute(
            "INSERT INTO concept_condition (concept_id, entity, col, op, value, status) "
            "VALUES (?,?,?,?,?,1)", ("c1", "lib_loan", "X", "=", "1"))
        store._conn.execute(
            "INSERT INTO concept_condition (concept_id, entity, col, op, value, status) "
            "VALUES (?,?,?,?,?,1)", ("c1", "lib_reader", "Y", "=", "2"))
        store._conn.commit()
        pid = store.insert_proposal(
            "relation_col",
            {"source": "lib_loan", "target": "lib_reader", "via": via,
             "col_pair": "MEMBER_ID=ZZZ_ID"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "依赖" in out["detail"]
        raw = store.load_raw()
        assert [r for r in raw["relationships"] if r["via"] == via], "被闸1拦了还是删了"

    def test_relation_col_add方向无分支(self, store):
        """relation_col 只有 retire 语义; add 方向应落到"无 apply 分支"。"""
        pid = store.insert_proposal(
            "relation_col", {"source": "a", "target": "b", "via": "a.X=b.Y"},
            "r", direction="add")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False

    def test_payload只有via时快照仍可回滚(self, store):
        """★ 闸2 反证: payload 无 source_col/target_col 时, 回滚不许静默失效。

        _edge_snapshot 原来只从 source_col/target_col 拼 via, 缺列就返回 {},
        rollback_apply 见空快照直接 return —— 回归门判拒绝了, 边却留在软删状态。
        这正是 [[selfevolve-gate-counterproof]] 那条教训 (闸门必须断言物理后果)
        在 relation_col 路径上的复现: mismatch payload 不保证带 col。
        """
        store.upsert_relation({
            "source": "lib_loan", "target": "lib_reader",
            "source_col": "READER_ID", "target_col": "MEMBER_ID",
            "type": "MANY_TO_ONE"})
        via = "lib_loan.READER_ID=lib_reader.MEMBER_ID"
        pid = store.insert_proposal(
            "relation_col",
            {"source": "lib_loan", "target": "lib_reader", "via": via,
             "col_pair": "MEMBER_ID=ZZZ_ID"},  # ← 故意不带 source_col/target_col
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is True
        snap = out.get("rollback_snapshot")
        assert snap, "快照为空 → 回归门拒绝时无法还原"

        rollback_apply(store, snap)
        row = store._conn.execute(
            "SELECT status FROM relation WHERE via=?", (via,)).fetchone()
        assert row is not None and row["status"] == 1, "回滚没把边恢复成 active"

    def test_add落库成功(self, store):
        pid = _approved_relation_proposal(store)
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is True
        # relation 表有这条边了
        raw = store.load_raw()
        rels = [r for r in raw["relationships"]
                if r["source"] == "a" and r["target"] == "b"]
        assert len(rels) == 1
        assert rels[0]["source_col"] == "X"
        assert rels[0]["label"] == "甲的乙", "人审填的 label 要落到库里"


class TestApplyLabelGuard:
    """★ apply 拒绝无 label 的新增边 (2026-09-19, 兜底闸)。

    首道闸在人审端点 (web 层 review 必填 label)。这道是兜底 —— 有人绕过前端
    直接 PATCH review 再 POST apply 时仍要拦住。两道都要有的理由同本项目
    反复栽的那条: 只在一处把关, 那一处被绕过就静默产坏数据。

    危害: 落库的裸边让概念层/agent 靠 get_join_path 选边时没区分度
    (见 check_relation_label_distinct), 与 R10 那轮"裸边清零"对冲。
    """

    def test_无label的add被拒(self, store):
        _approved_relation_proposal(store, label=None)
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "label" in out["detail"]

    def test_空白label的add被拒(self, store):
        _approved_relation_proposal(store, label="   ")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False

    def test_被拒时边没落库(self, store):
        """★ 反证: 不能只是返回 ok=False 而边已经进去了 (安静的无害失败)。"""
        _approved_relation_proposal(store, label=None)
        p = store.list_proposals()[0]
        apply_proposal(p, store)
        row = store._conn.execute(
            "SELECT * FROM relation WHERE via=?", ("a.X=b.X",)).fetchone()
        assert row is None, "apply 拒绝了但边还是落库了"

    def test_被拒时patch_status不能变applied(self, store):
        """拒绝要留在 pending, 否则人补了 label 也没法重试。"""
        _approved_relation_proposal(store, label=None)
        p = store.list_proposals()[0]
        apply_proposal(p, store)
        assert store.list_proposals()[0]["patch_status"] == "pending"

    def test_retire不受label约束(self, store):
        """retire 软删现有边, payload 抄自 legal_edges 不带 label。"""
        store.upsert_relation({
            "source": "a", "target": "b", "via": "a.X=b.X",
            "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE",
            "label": "库里早有的 label"})
        pid = store.insert_proposal(
            "relation",
            {"source": "a", "target": "b", "via": "a.X=b.X",
             "source_col": "X", "target_col": "X"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is True

    def test_retire无依赖软删成功(self, store):
        """先落一条边再 retire 它。"""
        store.upsert_relation({
            "source": "a", "target": "b", "via": "a.X=b.X",
            "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE"})
        pid = store.insert_proposal(
            "relation",
            {"source": "a", "target": "b", "via": "a.X=b.X",
             "source_col": "X", "target_col": "X"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is True
        raw = store.load_raw()
        assert not [r for r in raw["relationships"]
                    if r["source"] == "a" and r["target"] == "b"]


class TestRollbackRestoresHumanFields:
    """★★ 回滚必须还原**整行**, 不只是 status (2026-10-19 审计第 3 条)。

    `_edge_snapshot` 原先只记 existed+status, 而 `_upsert_relation_stmt` 的
    `ON CONFLICT DO UPDATE` 把 type/label/source_col/target_col/bridge_count/
    extra_json **全列覆盖**。回归门拒绝时只写回 status → 人工精校的 label 和
    extra_json 里的 origin 永久丢失, 而界面报的是"改动已回滚"。

    为什么是硬伤: `ontology.db` 不进 git, R10 那轮"裸边清零"的 label 是人一条条
    填的, 丢了无从恢复。这是本项目"闸拒绝 = 改动不得生效"那条约定的字面要求 ——
    status 回去了但 label 没回去, 改动其实生效了一半。
    """

    @pytest.fixture
    def store(self, tmp_path):
        s = OntologyStore(str(tmp_path / "t.db"))
        yield s
        s.close()

    def _existing_edge(self, store):
        """库里一条人工精校过的边: label + extra_json.origin 都是人填的。"""
        store.upsert_relation({
            "source": "a", "target": "b",
            "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE",
            "label": "人工精校的业务语义", "bridge_count": 7,
            "origin": "manual",          # → 落进 extra_json
            "curated_by": "张三",         # → 落进 extra_json
        })

    def _row(self, store, via="a.X=b.X"):
        return store._conn.execute(
            "SELECT * FROM relation WHERE via=?", (via,)).fetchone()

    def test_回滚还原人工label(self, store):
        """apply 用新 label 覆盖 → 回滚必须把人工 label 还回来。"""
        self._existing_edge(store)
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "source_col": "X",
                         "target_col": "X", "via": "a.X=b.X",
                         "type": "MANY_TO_ONE", "label": "机器猜的 label"},
            "r", direction="add")
        store.patch_proposal_review(pid, "approved", reviewed_by="human")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is True
        assert self._row(store)["label"] == "机器猜的 label", "前提: apply 确实覆盖了"

        rollback_apply(store, out["rollback_snapshot"])
        assert self._row(store)["label"] == "人工精校的业务语义", (
            "回滚只还原了 status, 人工 label 永久丢失 (而界面报的是'改动已回滚')")

    def test_回滚还原extra_json里的人工字段(self, store):
        """extra_json 装着 origin / curated_by 这类飞轮信任分级依据。"""
        self._existing_edge(store)
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "source_col": "X",
                         "target_col": "X", "via": "a.X=b.X",
                         "type": "MANY_TO_ONE", "label": "机器猜的",
                         "origin": "ai"},
            "r", direction="add")
        store.patch_proposal_review(pid, "approved", reviewed_by="human")
        out = apply_proposal(store.list_proposals()[0], store)
        rollback_apply(store, out["rollback_snapshot"])

        extra = json.loads(self._row(store)["extra_json"] or "{}")
        assert extra.get("origin") == "manual", "extra_json 的人工 origin 没还原"
        assert extra.get("curated_by") == "张三", "extra_json 的人工字段丢了"

    def test_回滚还原type与bridge_count(self, store):
        """DO UPDATE 覆盖的六列都要还原, 不是只挑 label 一个。"""
        self._existing_edge(store)
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "source_col": "X",
                         "target_col": "X", "via": "a.X=b.X",
                         "type": "ONE_TO_ONE", "label": "机器猜的",
                         "bridge_count": 99},
            "r", direction="add")
        store.patch_proposal_review(pid, "approved", reviewed_by="human")
        out = apply_proposal(store.list_proposals()[0], store)
        rollback_apply(store, out["rollback_snapshot"])

        row = self._row(store)
        assert row["type"] == "MANY_TO_ONE", "type 没还原"
        assert row["bridge_count"] == 7, "bridge_count 没还原"

    def test_原先不存在的边回滚仍是硬删(self, store):
        """★ 反证: 不许为了"还原整行"把新增边也留成 status=0 幽灵行。

        幽灵行会污染墓碑查询 (§5.7) —— 一条从没被人处置过的边,
        下次提议时却显示"软删边命中"。原行为是对的, 这条钉住它别被改坏。
        """
        pid = store.insert_proposal(
            "relation", {"source": "n", "target": "m", "source_col": "X",
                         "target_col": "X", "via": "n.X=m.X",
                         "type": "MANY_TO_ONE", "label": "全新的边"},
            "r", direction="add")
        store.patch_proposal_review(pid, "approved", reviewed_by="human")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is True
        rollback_apply(store, out["rollback_snapshot"])
        assert self._row(store, "n.X=m.X") is None, (
            "原先不存在的边回滚后留下了行 → 幽灵行污染墓碑查询")

    def test_retire路径回滚也还原整行(self, store):
        """retire 只改 status, 回滚后整行各列都该跟原样一致 (无回归)。"""
        self._existing_edge(store)
        before = dict(self._row(store))
        pid = store.insert_proposal(
            "relation", {"source": "a", "target": "b", "source_col": "X",
                         "target_col": "X", "via": "a.X=b.X"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved", reviewed_by="human")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is True
        assert self._row(store)["status"] == 0, "前提: retire 确实软删了"

        rollback_apply(store, out["rollback_snapshot"])
        after = dict(self._row(store))
        assert after == before, f"retire 回滚后整行不一致: {before} → {after}"


class TestRetireGuardChecksSameRow:
    """★★ retire 守卫查的行必须就是要删的那行 (2026-10-19 审计第 4 条)。

    守卫 `WHERE via=?` 单键, 软删 `WHERE source=? AND target=? AND via=?` 三键。
    真库 `via` **非唯一** —— 实测存在若干组各 2 行, 每组都是同一条边的正反两向
    (如 `lib_fee_rule→title` 与 `title→lib_fee_rule` 共用一个 via),
    UNIQUE 建在三元组上不是 via 上。

    后果: `fetchone()` 读到反向那行的 status → 守卫放行 → 软删匹配 0 行 →
    返回 ok=True → 回归门**真跑一次 LLM CQ**(几十秒) → 边其实没动。
    `_apply_relation_col_proposal` 的 docstring 自称就是要防这个"安静的无害失败"。
    """

    @pytest.fixture
    def store(self, tmp_path):
        s = OntologyStore(str(tmp_path / "t.db"))
        yield s
        s.close()

    def _two_directions_same_via(self, store):
        """造真库那个形状: 同一个 via 两行, 端点方向相反。

        via 由 col 派生, 所以两行的 col 要交叉填才能撞出同一个 via。
        """
        store._conn.execute(
            "INSERT INTO relation (source,target,type,via,label,"
            "source_col,target_col,status) VALUES (?,?,?,?,?,?,?,1)",
            ("p", "q", "MANY_TO_ONE", "p.ID=q.ID", "正向", "ID", "ID"))
        store._conn.execute(
            "INSERT INTO relation (source,target,type,via,label,"
            "source_col,target_col,status) VALUES (?,?,?,?,?,?,?,1)",
            ("q", "p", "MANY_TO_ONE", "p.ID=q.ID", "反向", "ID", "ID"))
        store._conn.commit()

    def test_via非唯一时守卫不许被另一行放行(self, store):
        """payload 指 q→p 且该行已软删 → 守卫必须看 q→p 那行, 不是 p→q 那行。"""
        self._two_directions_same_via(store)
        store._conn.execute(
            "UPDATE relation SET status=0 WHERE source='q' AND target='p'")
        store._conn.commit()

        pid = store.insert_proposal(
            "relation_col",
            {"source": "q", "target": "p", "via": "p.ID=q.ID",
             "col_pair": "ID=ZZZ"}, "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is False, (
            "守卫读到了 p→q 那行的 status=1 就放行, 而要删的 q→p 已是软删态 "
            "→ 软删匹配 0 行却报成功 (安静的无害失败)")
        assert "已是软删" in out["detail"] or "软删" in out["detail"]

    def test_payload端点在relation表查无此行要响亮失败(self, store):
        """★ 核心: via 存在但**这个方向**不存在 → 必须拒, 不许静默删 0 行。"""
        self._two_directions_same_via(store)
        pid = store.insert_proposal(
            "relation_col",
            {"source": "ghost", "target": "q", "via": "p.ID=q.ID",
             "col_pair": "ID=ZZZ"}, "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is False, (
            "via 查得到就放行 → soft_delete 按三键匹配 0 行 → ok=True 且边没动")
        assert "不存在" in out["detail"] or "查无" in out["detail"]

    def test_正常三键命中照旧成功(self, store):
        """反证: 收紧成三键不许把正常 retire 也拦掉。"""
        self._two_directions_same_via(store)
        pid = store.insert_proposal(
            "relation_col",
            {"source": "q", "target": "p", "via": "p.ID=q.ID",
             "col_pair": "ID=ZZZ"}, "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        out = apply_proposal(store.list_proposals()[0], store)
        assert out["ok"] is True, out["detail"]
        row = store._conn.execute(
            "SELECT status FROM relation WHERE source='q' AND target='p'"
        ).fetchone()
        assert row["status"] == 0, "该删的那行没删"

    def test_软删只动payload指认的那一行(self, store):
        """★ 物理后果: 反向那行必须**不受影响** (三键定位的字面要求)。"""
        self._two_directions_same_via(store)
        pid = store.insert_proposal(
            "relation_col",
            {"source": "q", "target": "p", "via": "p.ID=q.ID",
             "col_pair": "ID=ZZZ"}, "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        apply_proposal(store.list_proposals()[0], store)
        other = store._conn.execute(
            "SELECT status FROM relation WHERE source='p' AND target='q'"
        ).fetchone()
        assert other["status"] == 1, "连带把反向那条边也软删了"


class TestCheckRelationDependencies:
    """retire 闸1 依赖检查 (★ 结构化列非 JSON, 按 entity 判)。

    判据:
    - concept_condition 按 concept_id 分组, 组内 distinct entity 集合
      同时包含边的 source 和 target → 该 concept 依赖这条边 (跨表条件要走 JOIN)
    - filter_rule.entity 命中边任一端点 → 依赖 (保守)
    """

    def test_concept条件跨边两端_算依赖(self, store):
        store.upsert_concept({"id": "跨表概念", "display_name": "跨表概念",
                              "anchor_entity": "a", "summary": "",
                              "origin": "manual"})
        store.upsert_concept_condition({
            "concept_id": "跨表概念", "entity": "a", "col": "X",
            "op": "=", "value": "1", "seq": 0})
        store.upsert_concept_condition({
            "concept_id": "跨表概念", "entity": "b", "col": "Y",
            "op": "=", "value": "2", "seq": 1})
        deps = check_relation_dependencies(
            {"source": "a", "target": "b", "via": "a.X=b.X"}, store)
        assert len(deps) == 1
        assert "跨表概念" in deps[0]

    def test_concept条件只碰一端_不算边依赖(self, store):
        store.upsert_concept({"id": "单表概念", "display_name": "单表概念",
                              "anchor_entity": "a", "summary": "",
                              "origin": "manual"})
        store.upsert_concept_condition({
            "concept_id": "单表概念", "entity": "a", "col": "X",
            "op": "=", "value": "1", "seq": 0})
        deps = check_relation_dependencies(
            {"source": "a", "target": "b", "via": "a.X=b.X"}, store)
        assert deps == []

    def test_filter_rule命中端点_算依赖(self, store):
        store.upsert_filter_rule({"entity": "b", "col": "REC_STATUS",
                                  "op": "=", "value": "1000",
                                  "mode": "hard", "origin": "source_code"})
        deps = check_relation_dependencies(
            {"source": "a", "target": "b", "via": "a.X=b.X"}, store)
        assert len(deps) == 1
        assert "filter_rule" in deps[0] or "b" in deps[0]

    def test_retire有依赖被拦(self, store):
        store.upsert_relation({
            "source": "a", "target": "b", "via": "a.X=b.X",
            "source_col": "X", "target_col": "X", "type": "MANY_TO_ONE"})
        store.upsert_filter_rule({"entity": "b", "col": "REC_STATUS",
                                  "op": "=", "value": "1000",
                                  "mode": "hard", "origin": "source_code"})
        pid = store.insert_proposal(
            "relation",
            {"source": "a", "target": "b", "via": "a.X=b.X",
             "source_col": "X", "target_col": "X"},
            "r", direction="retire")
        store.patch_proposal_review(pid, "approved")
        p = store.list_proposals()[0]
        out = apply_proposal(p, store)
        assert out["ok"] is False
        assert "依赖" in out["detail"]
        # 边还在 (没被软删)
        raw = store.load_raw()
        assert len([r for r in raw["relationships"]
                    if r["source"] == "a"]) == 1


class TestRunRegression:
    """CQ 回归门 (mock run_one_cq; 真库端到端走 manual smoke)。"""

    def _setup_cq_env(self, tmp_path, monkeypatch, baseline_results,
                      rerun_results, case_ids=None):
        """造 kg_private 环境: baseline 文件 + mock cq_runner。"""
        kg = tmp_path / "kg_private"
        kg.mkdir()
        baselines = kg / "cq_baselines"
        baselines.mkdir()
        # baseline: apply 前的 current 快照
        (baselines / "run1.json").write_text(json.dumps({
            "ran_at": "t0", "ontology_path": "x",
            "results": baseline_results}), encoding="utf-8")
        # cq_set 文件 (sha256 钉死对象)
        cq_set = kg / "cq_set.json"
        cq_set.write_text(json.dumps({"cases": [
            {"id": cid, "question": "q", "kind": "single",
             "turns": None, "criteria": {}} for cid in (case_ids or ["CQ-1"])]}),
            encoding="utf-8")
        monkeypatch.setattr("core.runtime.cq_runner.CQ_SET_PATH", cq_set)
        # mock run_one_cq: 按顺序吐 rerun_results
        it = iter(rerun_results)

        def fake_run_one_cq(case):
            return next(it)
        monkeypatch.setattr("core.runtime.cq_runner.run_one_cq", fake_run_one_cq)
        return cq_set

    def _te_with_hard_block(self, blocked: bool, vtype: str = "no_edge"):
        """造一条 tool_event: blocked=True 模拟 obqc 硬拦 (默认 ①JOIN 类)。"""
        return {"name": "execute_sql", "args": "", "result": "返回 dict",
                "ms": 5, "ok": None, "used_edges": [], "violations": [
                    {"type": vtype, "hit": "a JOIN b"}] if blocked else []}

    def test_无新增hard_block_回归ok(self, store, tmp_path, monkeypatch):
        baseline = [{"id": "CQ-1", "answer": "a",
                     "tool_events": [self._te_with_hard_block(False)],
                     "error": None, "verdict": "pass", "note": ""}]
        rerun = [{"id": "CQ-1", "answer": "a",
                  "tool_events": [self._te_with_hard_block(False)],
                  "error": None, "verdict": "pass", "note": ""}]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal, baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is True
        assert out["new_hard_blocks"] == []

    def test_新增hard_block_回归fail(self, store, tmp_path, monkeypatch):
        """★ 反证: baseline 没 hard_block, 重跑出现 → 拒绝。"""
        baseline = [{"id": "CQ-1", "answer": "a",
                     "tool_events": [self._te_with_hard_block(False)],
                     "error": None, "verdict": "pass", "note": ""}]
        rerun = [{"id": "CQ-1", "answer": "a",
                  "tool_events": [self._te_with_hard_block(True)],
                  "error": None, "verdict": "fail", "note": ""}]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal, baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is False
        assert len(out["new_hard_blocks"]) == 1

    def test_baseline已有hard_block_不算新增(self, store, tmp_path, monkeypatch):
        """baseline 本来就挂 (老问题), 重跑还挂 → 不是新引入。"""
        baseline = [{"id": "CQ-1", "answer": "a",
                     "tool_events": [self._te_with_hard_block(True)],
                     "error": None, "verdict": "fail", "note": ""}]
        rerun = [{"id": "CQ-1", "answer": "a",
                  "tool_events": [self._te_with_hard_block(True)],
                  "error": None, "verdict": "fail", "note": ""}]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal, baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is True

    def test_sha256钉死_运行中被换题抛RuntimeError(self, store, tmp_path, monkeypatch):
        baseline = [{"id": "CQ-1", "answer": "a", "tool_events": [],
                     "error": None, "verdict": "pass", "note": ""}]
        rerun = [{"id": "CQ-1", "answer": "a", "tool_events": [],
                  "error": None, "verdict": "pass", "note": ""}]
        cq_set = self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)

        # mock run_one_cq 跑的时候偷换考题
        real_run = cq_set.read_bytes()

        def fake_run_and_swap(case):
            cq_set.write_bytes(real_run + b" ")  # 内容变了
            return {"id": "CQ-1", "answer": "a", "tool_events": [],
                    "error": None, "verdict": "pass", "note": ""}
        monkeypatch.setattr("core.runtime.cq_runner.run_one_cq", fake_run_and_swap)

        proposal = {"regression_run_id": "run1"}
        with pytest.raises(RuntimeError, match="cq_set"):
            run_regression(store, proposal, baselines_dir=tmp_path / "kg_private" / "cq_baselines")

    def _result(self, cq_id: str, blocked: bool):
        return {"id": cq_id, "answer": "a",
                "tool_events": [self._te_with_hard_block(blocked)],
                "error": None, "verdict": "fail" if blocked else "pass",
                "note": ""}

    def test_baseline没跑过的CQ硬拦_不算新增但要响亮报出(
            self, store, tmp_path, monkeypatch):
        """★ 真库缺陷 (2026-09-19 取证): cq_set 的题数多于 baseline 覆盖的题数。

        新扩的那批 CQ 从没进过 baseline → 它们硬拦一次就被算成"新增",
        闸必拒, 而这跟被审的提议毫无关系 (拒得没道理)。
        回归门问的是"改动有没有弄坏**原本好的**", baseline 里没有的 CQ
        无从判断"原本" → 不能算新增。但**不许静默缩覆盖面**: 必须把
        未覆盖的 CQ 列出来, 否则 regression_ok=True 会被误读成"全守住了"。
        """
        baseline = [self._result("CQ-1", False)]
        rerun = [self._result("CQ-1", False), self._result("CQ-2", True)]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun,
                           case_ids=["CQ-1", "CQ-2"])
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal,
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is True, "baseline 没跑过的 CQ 不该判成新增"
        assert out["new_hard_blocks"] == []
        assert out["uncovered_cqs"] == ["CQ-2"], "未被回归门守住的 CQ 必须响亮报出"

    def test_baseline全覆盖时uncovered为空(self, store, tmp_path, monkeypatch):
        baseline = [self._result("CQ-1", False), self._result("CQ-2", False)]
        rerun = [self._result("CQ-1", False), self._result("CQ-2", True)]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun,
                           case_ids=["CQ-1", "CQ-2"])
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal,
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["uncovered_cqs"] == []
        assert out["regression_ok"] is False, "baseline 覆盖到的 CQ 新硬拦照旧拒绝"
        assert out["new_hard_blocks"] == ["CQ-2"]

    def test_截断的CQ判不了_必须报出而不是静默放行(
            self, store, tmp_path, monkeypatch):
        """★ 第三种"判不了" (2026-10-20): CQ 撞 max_tokens 被截断。

        闸的定罪依据是 tool_events 里的 violations。CQ 被截断时 agent 可能
        根本没走到那句会被 obqc 拦下的 execute_sql —— 于是零 violations,
        闸照常放行。这跟 uncovered_cqs 是同一类错误: "放行"一个信号同时
        代表"真没坏"和"根本没看完", 人无从分辨。

        CQ-14 在 2026-09-19 真库跑批里就是截断的 (三轮里第三轮截断), 它同时
        也是当时被闸拿去定罪/洗清的那条 —— 判据建立在一次没跑完的运行上。
        """
        baseline = [self._result("CQ-1", False), self._result("CQ-2", False)]
        rerun = [self._result("CQ-1", False), self._result("CQ-2", False)]
        rerun[1]["truncated"] = True          # CQ-2 没跑完 → 它的"没违规"不可信
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun,
                           case_ids=["CQ-1", "CQ-2"])
        out = run_regression(store, {"regression_run_id": "run1"},
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["truncated_cqs"] == ["CQ-2"], \
            "截断的 CQ 必须响亮报出, 否则等于静默放行一条没跑完的考题"

    def test_没截断时truncated_cqs为空(self, store, tmp_path, monkeypatch):
        """反证: 别把正常跑完的 CQ 也报成截断。"""
        baseline = [self._result("CQ-1", False)]
        rerun = [self._result("CQ-1", False)]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        out = run_regression(store, {"regression_run_id": "run1"},
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["truncated_cqs"] == []

    def test_老结果没truncated键_不炸也不误报(self, store, tmp_path, monkeypatch):
        """老 baseline/结果文件没有 truncated 键 (2026-10-20 前) → 当没截断。"""
        baseline = [self._result("CQ-1", False)]
        rerun = [self._result("CQ-1", False)]
        assert "truncated" not in rerun[0], "这条测的就是缺键的情况"
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        out = run_regression(store, {"regression_run_id": "run1"},
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["truncated_cqs"] == []
        assert out["regression_ok"] is True

    def test_没查到库的CQ判不了_必须报出(self, store, tmp_path, monkeypatch):
        """★ 第四种"判不了" (2026-10-21): 整轮 execute_sql 没一次成功。

        零成功 SQL = 零被拦 SQL, 闸照常放行; 但这条答案根本没碰过真库
        (2026-10-20 CQ-5 实测: 库没初始化, 答案是从旧结果文件抄的)。
        """
        baseline = [self._result("CQ-1", False), self._result("CQ-2", False)]
        rerun = [self._result("CQ-1", False), self._result("CQ-2", False)]
        rerun[0]["no_db_evidence"] = True
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun,
                           case_ids=["CQ-1", "CQ-2"])
        out = run_regression(store, {"regression_run_id": "run1"},
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["no_db_evidence_cqs"] == ["CQ-1"]

    def test_老结果没no_db_evidence键_不误报(self, store, tmp_path, monkeypatch):
        baseline = [self._result("CQ-1", False)]
        rerun = [self._result("CQ-1", False)]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        out = run_regression(store, {"regression_run_id": "run1"},
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["no_db_evidence_cqs"] == []

    def _result_typed(self, cq_id: str, vtype: str | None):
        """vtype=None → 不硬拦; 否则造该类型的 violation。"""
        return {"id": cq_id, "answer": "a",
                "tool_events": [self._te_with_hard_block(
                    vtype is not None, vtype or "no_edge")],
                "error": None, "verdict": "fail" if vtype else "pass",
                "note": ""}

    def test_非JOIN类违规不算改动引入_但要报出(self, store, tmp_path, monkeypatch):
        """★ 真数据首跑实测的误拒 (2026-09-19): 闸拿无关噪音否了提议。

        apply proposal #3 (lib_reader_order→lib_copy_change) 时闸拒绝, 理由是
        CQ-14 新增 hard_block。查日志: 违规是 `column_not_found:
        LIB_LOAN_ACCT_REL.REL_ID` —— agent 自己猜了个物理表和本体都没有的
        列名。三点证明与被审的边无因果:
          1. 那是 obqc **②列归属**校验, 新边属 **①JOIN 路径**, 不同校验
          2. 全程所有 SQL 零次 JOIN 到新边 (老套路 WHERE IN 字面量)
          3. 该列名错误在全部历史真实运行里只出现过这一次 = LLM 非确定性噪音

        CQ 重跑是非确定性过程, 把"任何 violations"都算改动引入 → 闸会随机
        误拒任何提议。只有 ①JOIN 类 (no_edge / join_col_mismatch) 才与
        relation 增删有因果关系, 其余类型报出但不定罪。
        """
        baseline = [self._result_typed("CQ-1", None)]
        rerun = [self._result_typed("CQ-1", "column_not_found")]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal,
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is True, "非 JOIN 类违规不该定罪 (与边无因果)"
        assert out["new_hard_blocks"] == []
        assert out["unrelated_blocks"] == ["CQ-1"], "噪音要报出, 不许静默吞掉"

    def test_JOIN类违规照旧定罪(self, store, tmp_path, monkeypatch):
        """反证: 收窄定罪面不能把真信号一起放过。"""
        baseline = [self._result_typed("CQ-1", None),
                    self._result_typed("CQ-2", None)]
        rerun = [self._result_typed("CQ-1", "no_edge"),
                 self._result_typed("CQ-2", "join_col_mismatch")]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun,
                           case_ids=["CQ-1", "CQ-2"])
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal,
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is False
        assert out["new_hard_blocks"] == ["CQ-1", "CQ-2"]
        assert out["unrelated_blocks"] == []

    def test_混合违规_JOIN类在同一CQ里则定罪(self, store, tmp_path, monkeypatch):
        """同一条 CQ 既有噪音又有 JOIN 类违规 → 按 JOIN 类算, 定罪。"""
        baseline = [self._result_typed("CQ-1", None)]
        rerun = [{"id": "CQ-1", "answer": "a", "tool_events": [
            self._te_with_hard_block(True, "column_not_found"),
            self._te_with_hard_block(True, "no_edge")],
            "error": None, "verdict": "fail", "note": ""}]
        self._setup_cq_env(tmp_path, monkeypatch, baseline, rerun)
        proposal = {"regression_run_id": "run1"}
        out = run_regression(store, proposal,
                             baselines_dir=tmp_path / "kg_private" / "cq_baselines")
        assert out["regression_ok"] is False
        assert out["new_hard_blocks"] == ["CQ-1"]
