# tests/test_cq_runner.py
import json
import pytest
from pathlib import Path
import core.runtime.cq_runner as cqr


def _seed_set(tmp_path: Path) -> Path:
    p = tmp_path / "cq_set.json"
    p.write_text(json.dumps({"cases": [
        {"id": "CQ-1", "question": "号码状态?", "kind": "single", "turns": None,
         "criteria": {"点名": "lib_loan", "走边": "无", "值集": "LIB-ST-01", "断腿": "无"}},
    ]}, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_cq_set_读出cases(tmp_path):
    p = _seed_set(tmp_path)
    cases = cqr.load_cq_set(p)
    assert len(cases) == 1
    assert cases[0]["id"] == "CQ-1"
    assert cases[0]["criteria"]["点名"] == "lib_loan"


def test_load_cq_set_缺文件从seed合成(tmp_path):
    """★ 语义被刻意改了 (原断言 FileNotFoundError): 缺文件改成从 seed 合成,
    否则别人 clone 后 CQ 面板"0 条"且没有任何途径让它变好。
    """
    target = tmp_path / "nope.json"
    cases = cqr.load_cq_set(target)
    seed = json.loads(cqr.SEED_CQ_PATH.read_text(encoding="utf-8"))
    assert cases == seed["cases"]
    assert target.exists()


def test_load_cq_set_坏json抛ValueError(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        cqr.load_cq_set(p)


def test_read_results_不存在返回None(tmp_path):
    assert cqr.read_results(tmp_path / "cur.json") is None


def test_write_then_read_往返(tmp_path):
    p = tmp_path / "cur.json"
    cqr.write_json(p, {"ran_at": "t", "results": [{"id": "CQ-1", "note": "中文"}]})
    back = cqr.read_results(p)
    assert back["results"][0]["note"] == "中文"


def _fake_run_agent_factory(answer, events):
    """造一个假 run_agent: 调 on_event 逐个 emit events, 返回 answer。"""
    def _fake(query, conversation=None, on_event=None):
        for ev in events:
            if on_event:
                on_event(ev)
        return answer
    return _fake


def test_run_one_cq_单轮_收集工具轨迹(monkeypatch):
    events = [
        {"type": "tool_start", "name": "execute_sql", "args": "SELECT REC_STATUS FROM lib_loan"},
        {"type": "tool_end", "name": "execute_sql", "result": "返回 1 个 item",
         "latency_ms": 12, "success": True},
    ]
    monkeypatch.setattr(cqr, "run_agent", _fake_run_agent_factory("在用", events))
    case = {"id": "CQ-1", "question": "号码状态?", "kind": "single", "turns": None}
    r = cqr.run_one_cq(case)
    assert r["id"] == "CQ-1"
    assert r["answer"] == "在用"
    assert r["error"] is None
    assert len(r["tool_events"]) == 1
    te = r["tool_events"][0]
    assert te["name"] == "execute_sql"
    assert "SELECT REC_STATUS" in te["args"]
    assert te["ms"] == 12 and te["ok"] is True


def test_run_one_cq_异常记error不外抛(monkeypatch):
    def _boom(query, conversation=None, on_event=None):
        raise RuntimeError("max_iters 触顶")
    monkeypatch.setattr(cqr, "run_agent", _boom)
    r = cqr.run_one_cq({"id": "CQ-X", "question": "q", "kind": "single", "turns": None})
    assert r["answer"] == ""
    assert "max_iters" in r["error"]
    assert r["tool_events"] == []


def test_run_one_cq_多轮喂同一conversation(monkeypatch):
    seen = []
    def _fake(query, conversation=None, on_event=None):
        seen.append((query, id(conversation)))
        return f"ans:{query}"
    monkeypatch.setattr(cqr, "run_agent", _fake)
    case = {"id": "CQ-14", "question": "多轮", "kind": "multi_turn",
            "turns": ["Q1", "Q2", "Q3"]}
    r = cqr.run_one_cq(case)
    assert [q for q, _ in seen] == ["Q1", "Q2", "Q3"]
    assert len({cid for _, cid in seen}) == 1          # 同一个 conversation 实例
    assert r["answer"] == "ans:Q3"                      # 末轮答案


def test_run_one_cq_截断记truncated(monkeypatch):
    """agent 报 done+truncated → 结果里要留痕, 否则判分看不出是截断.

    真实背景: 2026-09-19 跑批 CQ-14/CQ-18 答案为空且 error=None, 判分时
    无从区分"模型被切断"与"模型真的无话可说"。
    """
    events = [{"type": "done", "truncated": True}]
    monkeypatch.setattr(cqr, "run_agent", _fake_run_agent_factory("半截答案", events))
    r = cqr.run_one_cq({"id": "CQ-T", "question": "q", "kind": "single", "turns": None})
    assert r["answer"] == "半截答案"
    assert r["truncated"] is True
    assert r["error"] is None          # 截断不是异常, 不该污染 error 字段


def test_run_one_cq_正常结束不记truncated(monkeypatch):
    """反证: 别把正常收尾也标成截断."""
    events = [{"type": "done"}]
    monkeypatch.setattr(cqr, "run_agent", _fake_run_agent_factory("完整答案", events))
    r = cqr.run_one_cq({"id": "CQ-N", "question": "q", "kind": "single", "turns": None})
    assert r["truncated"] is False


def test_run_one_cq_多轮任一轮截断都记(monkeypatch):
    """多轮里**中间轮**截断也要留痕 —— CQ-14 正是三轮里第三轮截断.

    若只看末轮的 done 事件, 中间轮截断会被漏掉。
    """
    calls = {"n": 0}
    def _fake(query, conversation=None, on_event=None):
        calls["n"] += 1
        if calls["n"] == 2 and on_event:          # 只有第 2 轮截断
            on_event({"type": "done", "truncated": True})
        elif on_event:
            on_event({"type": "done"})
        return f"ans:{query}"
    monkeypatch.setattr(cqr, "run_agent", _fake)
    r = cqr.run_one_cq({"id": "CQ-M", "question": "多轮", "kind": "multi_turn",
                        "turns": ["Q1", "Q2", "Q3"]})
    assert r["answer"] == "ans:Q3"
    assert r["truncated"] is True, "中间轮截断不能被末轮的正常 done 冲掉"


def _fake_with_tools(answer, tool_pairs):
    """tool_pairs: [(name, success)] → 依次发 tool_start/tool_end。"""
    def _fake(query, conversation=None, on_event=None):
        for name, ok in tool_pairs:
            on_event({"type": "tool_start", "name": name, "args": "{}"})
            on_event({"type": "tool_end", "name": name, "result": "r",
                      "latency_ms": 1, "success": ok})
        on_event({"type": "done"})
        return answer
    return _fake


def test_run_one_cq_execute_sql全失败记no_db_evidence(monkeypatch):
    """★ 2026-10-20 真实事故: 库没初始化, execute_sql 全报 'db not initialized',
    agent 转去 read_file 读旧结果编出一篇像样的长答案, error=None 面板看不出异常。
    一条真实数据都没查到的答案必须留痕。"""
    monkeypatch.setattr(cqr, "run_agent", _fake_with_tools("编的答案", [
        ("execute_sql", False), ("execute_sql", False), ("read_file", True)]))
    r = cqr.run_one_cq({"id": "CQ-5", "question": "q", "kind": "single", "turns": None})
    assert r["no_db_evidence"] is True
    assert r["error"] is None


def test_run_one_cq_一次都没调execute_sql也记no_db_evidence(monkeypatch):
    monkeypatch.setattr(cqr, "run_agent", _fake_with_tools("a", [("read_file", True)]))
    r = cqr.run_one_cq({"id": "CQ-X", "question": "q", "kind": "single", "turns": None})
    assert r["no_db_evidence"] is True


def test_run_one_cq_有一次execute_sql成功就不记(monkeypatch):
    """反证: 先失败后修正成功是正常形态 (CQ-18 撞 1054 后去掉 trial_run 重查)。"""
    monkeypatch.setattr(cqr, "run_agent", _fake_with_tools("a", [
        ("execute_sql", False), ("execute_sql", True)]))
    r = cqr.run_one_cq({"id": "CQ-18", "question": "q", "kind": "single", "turns": None})
    assert r["no_db_evidence"] is False


def test_rotate_current无则不动previous(tmp_path, monkeypatch):
    monkeypatch.setattr(cqr, "CUR_PATH", tmp_path / "cur.json")
    monkeypatch.setattr(cqr, "PREV_PATH", tmp_path / "prev.json")
    cqr.rotate_current_to_previous()                    # current 不存在
    assert cqr.read_results(tmp_path / "prev.json") is None


def test_rotate_把current内容搬到previous(tmp_path, monkeypatch):
    cur = tmp_path / "cur.json"
    monkeypatch.setattr(cqr, "CUR_PATH", cur)
    monkeypatch.setattr(cqr, "PREV_PATH", tmp_path / "prev.json")
    cqr.write_json(cur, {"ran_at": "old", "results": [{"id": "CQ-1", "verdict": "pass"}]})
    cqr.rotate_current_to_previous()
    prev = cqr.read_results(tmp_path / "prev.json")
    assert prev["ran_at"] == "old"
    assert prev["results"][0]["verdict"] == "pass"      # 上次打的分随之存档


def test_save_current_补verdict默认(tmp_path, monkeypatch):
    monkeypatch.setattr(cqr, "CUR_PATH", tmp_path / "cur.json")
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}],
                     "capabilities/ontology/ontology.db", "2026-07-20T10:00:00")
    cur = cqr.read_results(tmp_path / "cur.json")
    assert cur["ontology_path"].endswith("ontology.db")
    assert cur["results"][0]["verdict"] is None
    assert cur["results"][0]["note"] == ""


def test_set_verdict_写回current(tmp_path, monkeypatch):
    monkeypatch.setattr(cqr, "CUR_PATH", tmp_path / "cur.json")
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}],
                     "db", "t")
    assert cqr.set_verdict("CQ-1", "partial", "段②断腿") is True
    cur = cqr.read_results(tmp_path / "cur.json")
    assert cur["results"][0]["verdict"] == "partial"
    assert cur["results"][0]["note"] == "段②断腿"


def test_set_verdict_找不到id返回False(tmp_path, monkeypatch):
    monkeypatch.setattr(cqr, "CUR_PATH", tmp_path / "cur.json")
    cqr.save_current([{"id": "CQ-1", "answer": "a", "tool_events": [], "error": None}], "db", "t")
    assert cqr.set_verdict("CQ-NOPE", "pass", "") is False
