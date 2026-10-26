"""test_cq_crud.py — 考题集的合成 (ensure_cq_set) 与增删改 (写端点).

守的是开源发布阻断项 #10: `kg_private/cq_set.json` 不进 git, 别人 clone 后
CQ 面板显示"0 条"+ 一行红字且没有任何途径让它变好。前半测合成三件事:
  1. 缺文件 → 从 `scripts/seed_cq.json` 合成 (内容逐字相等)
  2. 已有文件 → **绝不覆盖** (那是用户自己的考题)
  3. 两个挂点都生效 —— `load_cq_set` 和 `run_regression`。
     ★ 后者单独测: apply.py 的 `sha_before = _cq_set_sha256(path)` 在
     `load_cq_set(path)` **前一行**就 read_bytes(), 只挂 load_cq_set 时
     apply 路径照样炸 FileNotFoundError。

后半 (Task 16) 测 POST/PUT/DELETE /cq/case: 合成出来的 4 条只是起点, 别人
还得能照自己的本体增删改, 否则考题集仍是死的。

全部用 tmp_path, 绝不碰 kg_private/ 下机主的真考题集 (前半显式传路径,
后半靠 autouse 的 `_隔离考题集` 把 CQ_SET_PATH 重定向)。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import capabilities.ontology.selfevolve.apply as ap
import core.runtime.cq_runner as cqr


@pytest.fixture(autouse=True)
def _隔离考题集(tmp_path, monkeypatch):
    """★ 别让测试写坏本机的真考题集 (kg_private/cq_set.json)。

    对前半那几条用例无影响 —— 它们都显式传 path, 不读这个模块级常量。
    """
    monkeypatch.setattr(cqr, "CQ_SET_PATH", tmp_path / "cq_set.json")
    yield


@pytest.fixture
def client():
    """单个 TestClient (不是 test_web_cq.py 那个三元组), 配上面的隔离 fixture 用。"""
    import web.app as webapp
    return TestClient(webapp.app)


def _case(cq_id: str, question: str = "q", **kw) -> dict:
    body = {"id": cq_id, "question": question, "kind": "single", "turns": None,
            "criteria": {"点名": "reader", "走边": "x", "值集": "y", "断腿": "无"}}
    body.update(kw)
    return body


def _seed_json() -> dict:
    """直接读仓库里的 scripts/seed_cq.json, 作为逐字比对的真相源。"""
    return json.loads(cqr.SEED_CQ_PATH.read_text(encoding="utf-8"))


def test_ensure_cq_set缺文件时从seed合成(tmp_path):
    target = tmp_path / "nested" / "cq_set.json"
    out = cqr.ensure_cq_set(target)
    assert out == target
    assert target.exists(), "合成后文件必须真落盘"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data["cases"]) >= 3
    assert data == _seed_json(), "合成内容必须与 seed 逐字相等"


def test_ensure_cq_set已有文件时不覆盖(tmp_path):
    target = tmp_path / "cq_set.json"
    mine = json.dumps({"cases": [
        {"id": "MY-1", "question": "我自己的考题", "kind": "single", "turns": None},
    ]}, ensure_ascii=False)
    target.write_text(mine, encoding="utf-8")
    cqr.ensure_cq_set(target)
    assert target.read_text(encoding="utf-8") == mine, "用户数据绝不许被 seed 覆盖"


def test_load_cq_set缺文件不再抛异常(tmp_path):
    cases = cqr.load_cq_set(tmp_path / "cq_set.json")
    assert len(cases) >= 3


def test_run_regression缺cq_set不炸(tmp_path, monkeypatch):
    """★ apply 路径必须自己挂一次 ensure —— sha_before 比 load_cq_set 更早读盘。

    只断言"不炸 FileNotFoundError 且文件真被合成"; 缺 store 之类的其它异常
    不是本用例关心的。
    """
    target = tmp_path / "cq_set.json"
    monkeypatch.setattr(cqr, "run_one_cq",
                        lambda case: {"id": case["id"], "answer": "x",
                                      "tool_events": [], "error": None})
    try:
        ap.run_regression(None, {"regression_run_id": ""},
                          cq_set_path=target, baselines_dir=tmp_path)
    except FileNotFoundError as e:
        pytest.fail(f"apply 路径没挂 ensure_cq_set, 仍炸缺文件: {e}")
    except Exception:
        pass  # 其它异常与本用例无关
    assert target.exists(), "run_regression 必须把缺的 cq_set 合成出来"


# ---------- Task 16: 写端点 POST / PUT / DELETE ----------

def test_POST新建考题(client):
    r = client.post("/cq/case", json=_case("NEW-1", "新考题"))
    assert r.status_code == 200, r.text
    cases = client.get("/cq/set").json()["cases"]
    assert any(c["id"] == "NEW-1" for c in cases)


def test_POST重复id返回409(client):
    body = _case("DUP-1")
    assert client.post("/cq/case", json=body).status_code == 200
    r = client.post("/cq/case", json=body)
    assert r.status_code == 409, f"重复 id 应 409, 得到 {r.status_code}"


def test_PUT改考题(client):
    client.post("/cq/case", json=_case("EDIT-1", "原问题"))
    r = client.put("/cq/case/EDIT-1", json=_case(
        "EDIT-1", "改后问题",
        criteria={"点名": "book", "走边": "z", "值集": "w", "断腿": "无"}))
    assert r.status_code == 200, r.text
    cases = client.get("/cq/set").json()["cases"]
    got = [c for c in cases if c["id"] == "EDIT-1"][0]
    assert got["question"] == "改后问题"
    assert got["criteria"]["点名"] == "book"


def test_PUT不存在的id返回404(client):
    r = client.put("/cq/case/NOPE", json=_case("NOPE"))
    assert r.status_code == 404


def test_PUT路径id与body不一致返回400(client):
    """路径 id 是权威, body.id 不符必须响亮失败 —— 否则会静默改到另一条
    (或凭空新建一条), 而前端看到 200 以为改对了。
    """
    client.post("/cq/case", json=_case("KEEP-1", "别动我"))
    r = client.put("/cq/case/KEEP-1", json=_case("OTHER-1"))
    assert r.status_code == 400, f"id 不一致应 400, 得到 {r.status_code}"
    cases = {c["id"] for c in client.get("/cq/set").json()["cases"]}
    assert "OTHER-1" not in cases, "400 却还是落盘了 (被当新建处理)"


def test_DELETE删考题(client):
    client.post("/cq/case", json=_case("DEL-1", "待删"))
    r = client.delete("/cq/case/DEL-1")
    assert r.status_code == 200, r.text
    cases = client.get("/cq/set").json()["cases"]
    assert not any(c["id"] == "DEL-1" for c in cases)


def test_DELETE不存在的id返回404(client):
    assert client.delete("/cq/case/NOPE").status_code == 404


def test_写操作真落盘到cq_set文件(client):
    """★ 断言物理后果 (文件内容), 不只断言 HTTP 200。"""
    client.post("/cq/case", json=_case("DISK-1", "落盘验证"))
    raw = json.loads(cqr.CQ_SET_PATH.read_text(encoding="utf-8"))
    assert any(c["id"] == "DISK-1" for c in raw["cases"]), "没真落盘"


def test_multi_turn缺turns被拒422(client):
    """★ 守一个静默失败: `run_one_cq` 对 multi_turn 走
    `for turn in case.get("turns") or []` —— 没给 turns 就循环零次,
    answer 恒为空串且不报任何错。用户在前端建一条 multi_turn 忘填轮次,
    跑出来是"空答案"且无从知道为什么。所以写端点必须在入口拦掉。
    """
    for bad in (None, [], ["只有一轮"], ["", "第二轮"]):
        r = client.post("/cq/case", json=_case("MT-BAD", kind="multi_turn", turns=bad))
        assert r.status_code == 422, f"multi_turn turns={bad!r} 应 422, 得到 {r.status_code}"
    cases = {c["id"] for c in client.get("/cq/set").json()["cases"]}
    assert "MT-BAD" not in cases, "422 却还是落盘了"


def test_single不带turns正常200(client):
    """对照组: multi_turn 的校验不能连带把正常的 single (turns=None) 也拦了。
    合法 multi_turn (≥2 轮) 同样要放过。
    """
    assert client.post("/cq/case", json=_case("OK-1")).status_code == 200
    r = client.post("/cq/case", json=_case("MT-OK", kind="multi_turn",
                                           turns=["第一轮", "第二轮"]))
    assert r.status_code == 200, r.text
    got = [c for c in client.get("/cq/set").json()["cases"] if c["id"] == "MT-OK"][0]
    assert got["turns"] == ["第一轮", "第二轮"]


def test_写操作保住顶层_comment键(client):
    """★ 防"只写回 {"cases": ...}" 把 seed 带的文件头注释吃掉。

    读改写必须整体保留顶层其它键 —— 文件是人也会手改的, 注释是给人看的。
    """
    seed = _seed_json()
    assert "_comment" in seed, "前提变了: seed 不再带 _comment, 本用例需重写"
    client.get("/cq/set")          # 触发从 seed 合成
    client.post("/cq/case", json=_case("KEEP-COMMENT-1"))
    raw = json.loads(cqr.CQ_SET_PATH.read_text(encoding="utf-8"))
    assert raw.get("_comment") == seed["_comment"], "写操作把顶层 _comment 吃掉了"


def test_坏json时写端点返400而非500(client):
    """★ 浏览器取证逮到的: 考题集坏了 (手改歪一个逗号) 时, 增删改全炸 500。

    判据不是"500 不好看", 而是 #10 的死局没真正解开: 面板读不出考题 ->
    红字 + "重跑全部 0 条" -> 用户点 ➕ 新建自救 -> upsert_case 的裸
    json.loads 抛 JSONDecodeError 直穿 500 -> 前端 fetch 只拿到
    "Failed to fetch", 连"文件坏在第几行"都看不到。缺文件那条路通了
    (ensure_cq_set 合成), 坏文件这条还堵着。

    修法是让写路径跟 load_cq_set 同口径: JSONDecodeError -> ValueError
    -> 端点 400 + 原始解析错误 (含行列号, 人能照着去改那个文件)。
    刻意不自动覆盖坏文件: 那是用户手写的内容, 悄悄用 seed 顶掉等于删数据。
    """
    cqr.CQ_SET_PATH.write_text("{ this is not json", encoding="utf-8")
    r = client.post("/cq/case", json=_case("X-1"))
    assert r.status_code == 400, f"坏 JSON 下 POST 应 400, 得到 {r.status_code}"
    assert "JSON" in r.json()["detail"], r.json()
    r = client.put("/cq/case/CQ-1", json=_case("CQ-1"))
    assert r.status_code == 400, f"坏 JSON 下 PUT 应 400, 得到 {r.status_code}"
    r = client.delete("/cq/case/CQ-1")
    assert r.status_code == 400, f"坏 JSON 下 DELETE 应 400, 得到 {r.status_code}"
    # 坏文件必须原样留着 (用户手写内容, 不许悄悄顶掉)
    assert cqr.CQ_SET_PATH.read_text(encoding="utf-8") == "{ this is not json"
