"""test_web_cq.py — 验证回路 B 端点. 全 mock cq_runner, 不真调 LLM/不连库。
跑法: uv run pytest tests/test_web_cq.py -v
"""
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import web.app as webapp
    import core.runtime.cq_runner as cqr
    # 把三份数据文件重定向到 tmp
    monkeypatch.setattr(cqr, "CQ_SET_PATH", tmp_path / "cq_set.json")
    monkeypatch.setattr(cqr, "CUR_PATH", tmp_path / "cur.json")
    monkeypatch.setattr(cqr, "PREV_PATH", tmp_path / "prev.json")
    return TestClient(webapp.app), cqr, tmp_path


def _seed_set(tmp_path):
    (tmp_path / "cq_set.json").write_text(json.dumps({"cases": [
        {"id": "CQ-1", "question": "q", "kind": "single", "turns": None,
         "criteria": {"点名": "lib_loan", "走边": "无", "值集": "LIB-ST-01", "断腿": "无"}},
    ]}, ensure_ascii=False), encoding="utf-8")


def test_cq_set_返回考题(client):
    c, cqr, tmp = client
    _seed_set(tmp)
    body = c.get("/cq/set").json()
    assert body["cases"][0]["id"] == "CQ-1"
    assert body["cases"][0]["criteria"]["点名"] == "lib_loan"


def test_cq_set_缺文件返回seed考题(client):
    """★ 这条就是阻断项 #10 的修复判据 (原断言 400):
    clone 后没有 kg_private/cq_set.json 时, 面板必须拿到可用的示例考题,
    而不是"0 条 + 一行红字"。
    """
    c, cqr, tmp = client
    r = c.get("/cq/set")
    assert r.status_code == 200
    seed = json.loads(cqr.SEED_CQ_PATH.read_text(encoding="utf-8"))
    assert len(r.json()["cases"]) == len(seed["cases"])


def test_cq_results_两份都无返回null(client):
    c, cqr, tmp = client
    body = c.get("/cq/results").json()
    assert body["current"] is None and body["previous"] is None


def test_cq_results_返回current(client):
    c, cqr, tmp = client
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}],
                     "db", "t")
    body = c.get("/cq/results").json()
    assert body["current"]["results"][0]["id"] == "CQ-1"
    assert body["previous"] is None


def test_cq_verdict_写回(client):
    c, cqr, tmp = client
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}], "db", "t")
    r = c.put("/cq/verdict/CQ-1", json={"verdict": "partial", "note": "断腿"})
    assert r.status_code == 200
    cur = cqr.read_results(cqr.CUR_PATH)
    assert cur["results"][0]["verdict"] == "partial"


def test_cq_verdict_找不到404(client):
    c, cqr, tmp = client
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}], "db", "t")
    assert c.put("/cq/verdict/CQ-NOPE", json={"verdict": "pass"}).status_code == 404


def test_cq_verdict_非法档422(client):
    c, cqr, tmp = client
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}], "db", "t")
    assert c.put("/cq/verdict/CQ-1", json={"verdict": "excellent"}).status_code == 422


def test_cq_run_sse_逐条推并落盘(client, monkeypatch):
    c, cqr, tmp = client
    _seed_set(tmp)
    # mock run_one_cq: 不真跑, 直接给结果
    monkeypatch.setattr(cqr, "run_one_cq",
                        lambda case: {"id": case["id"], "answer": "在用",
                                      "tool_events": [], "error": None})
    with c.stream("POST", "/cq/run") as resp:
        assert resp.status_code == 200
        frames = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[5:].strip()))
    types = [f["type"] for f in frames]
    assert types[0] == "case_start"
    assert "case_done" in types
    assert types[-1] == "done"
    # 落盘 current
    cur = cqr.read_results(cqr.CUR_PATH)
    assert cur["results"][0]["id"] == "CQ-1"
    assert cur["results"][0]["verdict"] is None


def test_cq_run_考题集坏json推error(client, monkeypatch):
    """守的是"worker 读考题集失败时 SSE 必须以 error 帧终结、不得挂死"
    (review I1, 同本文件下一条注释)。

    ★ 异常源换了: 缺文件现在会从 seed 合成, 不再是异常源; 但坏 JSON 仍抛
    ValueError, 继续守住这条线 —— 别把测试删了。
    """
    c, cqr, tmp = client
    (tmp / "cq_set.json").write_text("{ not json", encoding="utf-8")
    with c.stream("POST", "/cq/run") as resp:
        frames = [json.loads(l[5:].strip()) for l in resp.iter_lines()
                  if l.startswith("data:")]
    assert frames[-1]["type"] == "error"


def test_cq_run_rotate异常推error不挂死(client, monkeypatch):
    """rotate_current_to_previous 抛异常时, SSE 必须以 error 帧终结, 不得挂起。
    复现 review I1: worker() 旧版只兜 load_cq_set, rotate/loop/save 都在 try 外,
    异常致 worker 静默死亡, sse() 的 q.get() 永久阻塞, 连接挂死。
    """
    c, cqr, tmp = client
    _seed_set(tmp)

    def _boom(*a, **kw):
        raise RuntimeError("prev JSON 损坏模拟")

    monkeypatch.setattr(cqr, "rotate_current_to_previous", _boom)
    monkeypatch.setattr(cqr, "run_one_cq",
                        lambda case: {"id": case["id"], "answer": "x",
                                      "tool_events": [], "error": None})

    with c.stream("POST", "/cq/run") as resp:
        assert resp.status_code == 200
        frames = [json.loads(l[5:].strip()) for l in resp.iter_lines()
                  if l.startswith("data:")]
    # 末帧必须是 error (而非挂起超时)
    assert frames, "SSE 没有任何帧 — worker 静默死亡了"
    assert frames[-1]["type"] == "error"
    assert "prev JSON 损坏模拟" in frames[-1]["error"]
