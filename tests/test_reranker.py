"""
测 rag/reranker.py — Cross-encoder 精排。

mock 策略: monkeypatch CrossEncoder.predict, 让它按"query 词在 text 里出现次数"
给一个确定性分, 避免下载/加载 568MB 真模型。

跑法: uv run pytest tests/test_reranker.py -v
"""

import pytest


@pytest.fixture
def fake_cross_encoder(monkeypatch):
    """
    把 CrossEncoder 替换成一个假的 predict: 按 query 在 passage 里出现次数返回分.
    返回的分先是 logits (任意尺度), reranker 内部会 sigmoid 化.
    """
    class _FakeCE:
        def __init__(self, *a, **kw):
            pass

        def predict(self, pairs, batch_size=16, show_progress_bar=False):
            scores = []
            for q, p in pairs:
                # 简单确定性: q 在 p 出现次数 * 2 - 1, 让分有正有负
                n = p.count(q) if q else 0
                scores.append(float(n) * 2.0 - 1.0)
            return scores

    import capabilities.rag.reranker as r_mod
    monkeypatch.setattr(r_mod, "CrossEncoder", _FakeCE)
    # 模型路径检查也得跳过 (任务 1 已下载本机有, 但 CI 环境没有)
    monkeypatch.setattr(r_mod, "_assert_model_exists", lambda: None)


# ============ 接口测试 ============


def test_rerank_空候选返回空(fake_cross_encoder):
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    assert rr.rerank("Python", [], top_k=5) == []


def test_rerank_按相关性降序排(fake_cross_encoder):
    """假 CE 按 query 出现次数打分, 出现 2 次的应该排在 1 次和 0 次前面。"""
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": "完全无关", "source": "a.md", "index": 0, "score": 0.5},
        {"text": "Python Python", "source": "b.md", "index": 0, "score": 0.3},
        {"text": "Python 是动态语言", "source": "c.md", "index": 0, "score": 0.9},
    ]
    out = rr.rerank("Python", candidates, top_k=3)
    assert len(out) == 3
    # b 出现 2 次, c 出现 1 次, a 出现 0 次
    assert [r["source"] for r in out] == ["b.md", "c.md", "a.md"]


def test_rerank_score字段被rerank分覆盖(fake_cross_encoder):
    """
    覆盖原 cosine score. rerank 分 sigmoid 化后 ∈ [0, 1].
    这是设计取舍, 详见模块 doc 和 1.5 GUIDE.
    """
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": "Python 是动态语言", "source": "a.md", "index": 0, "score": 0.99},
    ]
    out = rr.rerank("Python", candidates, top_k=1)
    # 原 score 0.99 应该被覆盖
    assert out[0]["score"] != 0.99
    # sigmoid 后 ∈ [0, 1]
    assert 0.0 <= out[0]["score"] <= 1.0


def test_rerank_top_k截断(fake_cross_encoder):
    """30 进 5 出: 给 30 条候选, 取 top 5。"""
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": f"text {i}", "source": f"f{i}.md", "index": 0, "score": 0.0}
        for i in range(30)
    ]
    out = rr.rerank("text", candidates, top_k=5)
    assert len(out) == 5


def test_rerank_保留候选其他字段(fake_cross_encoder):
    """heading_path / level / content_hash 等字段必须原样保留, 让 chunker D2 链路不断。"""
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {
            "text": "Python",
            "source": "a.md",
            "index": 0,
            "score": 0.5,
            "heading_path": "标题1",
            "level": 1,
            "content_hash": "abc123",
        },
    ]
    out = rr.rerank("Python", candidates, top_k=1)
    r = out[0]
    assert r["heading_path"] == "标题1"
    assert r["level"] == 1
    assert r["content_hash"] == "abc123"


def test_rerank_top_k大于候选数_返回全部(fake_cross_encoder):
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": "a", "source": "1.md", "index": 0, "score": 0.0},
        {"text": "b", "source": "2.md", "index": 0, "score": 0.0},
    ]
    out = rr.rerank("x", candidates, top_k=10)
    assert len(out) == 2


# ============ 边界测试 (审视发现 4: NaN / 空 query) ============


def test_rerank_predict返回NaN_排到末尾不抛错(monkeypatch):
    """
    cross-encoder 偶发 NaN (尤其 FP16). 替换 predict 让中间那条返回 NaN,
    NaN 候选不应导致排序崩溃, 应排末尾 (sigmoid(NaN)=NaN, NaN 比任何数都小的实现兜底).
    """
    import math
    class _NaNCE:
        def __init__(self, *a, **kw): pass
        def predict(self, pairs, batch_size=16, show_progress_bar=False):
            return [1.0, float("nan"), 0.5]

    import capabilities.rag.reranker as r_mod
    monkeypatch.setattr(r_mod, "CrossEncoder", _NaNCE)
    monkeypatch.setattr(r_mod, "_assert_model_exists", lambda: None)

    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": "x1", "source": "1.md", "index": 0, "score": 0.0},
        {"text": "x2", "source": "2.md", "index": 0, "score": 0.0},  # 这条 NaN
        {"text": "x3", "source": "3.md", "index": 0, "score": 0.0},
    ]
    out = rr.rerank("q", candidates, top_k=3)
    assert len(out) == 3
    # 第一个 source 应该是 1.md (logit 1.0 最大), 最后一个应该是 2.md (NaN)
    assert out[0]["source"] == "1.md"
    assert out[-1]["source"] == "2.md"
    # NaN 候选的 score 字段允许是 NaN 或被替换成 0.0, 至少不抛错
    last_score = out[-1]["score"]
    assert math.isnan(last_score) or last_score == 0.0


def test_rerank_空query_行为明确(fake_cross_encoder):
    """
    空 query 是上层传错的边界. 应该不崩, 要么返回 [] 要么按原顺序原 score 返回.
    我们选: 返回 [] (语义上"没问题就没结果", 跟空候选对称).
    """
    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": "x", "source": "1.md", "index": 0, "score": 0.5},
    ]
    out = rr.rerank("", candidates, top_k=3)
    assert out == []


# ============ 埋点测试 (跟 retriever 同款 spy) ============


@pytest.fixture
def spy_log_event(monkeypatch):
    events: list[tuple[str, dict]] = []

    def fake_log_event(event, payload=None):
        events.append((event, payload or {}))

    def install(module_path: str) -> None:
        monkeypatch.setattr(f"{module_path}.log_event", fake_log_event)

    return events, install


def test_rerank_发rerank事件_带before_after_elapsed(
    spy_log_event, fake_cross_encoder
):
    events, install = spy_log_event
    install("capabilities.rag.reranker")

    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    candidates = [
        {"text": "Python A", "source": "a.md", "index": 0, "score": 0.5},
        {"text": "Python B B", "source": "b.md", "index": 0, "score": 0.3},
    ]
    rr.rerank("Python", candidates, top_k=2)

    rerank_events = [e for e in events if e[0] == "reranker.rerank"]
    assert len(rerank_events) == 1
    payload = rerank_events[0][1]
    assert payload["query"] == "Python"
    # before / after 摘要: id + 原 score / rerank 后 score
    assert "before" in payload
    assert "after" in payload
    assert len(payload["before"]) == 2
    assert len(payload["after"]) == 2
    assert "elapsed_ms" in payload
    assert payload["elapsed_ms"] >= 0


def test_rerank_发rerank事件_空候选也发(spy_log_event, fake_cross_encoder):
    """空候选时也要发事件 (虽然没干活), 让日志能看出"这次走了 reranker 但没东西可排"。"""
    events, install = spy_log_event
    install("capabilities.rag.reranker")

    from capabilities.rag.reranker import Reranker
    rr = Reranker()
    rr.rerank("Python", [], top_k=5)

    rerank_events = [e for e in events if e[0] == "reranker.rerank"]
    assert len(rerank_events) == 1
    assert rerank_events[0][1]["before"] == []
    assert rerank_events[0][1]["after"] == []


# ============ 懒加载 + 路径校验 (不 mock _assert_model_exists) ============


def test_reranker_init_模型目录不存在_抛FileNotFoundError(monkeypatch, tmp_path):
    """
    bge-reranker-v2-m3 必须存在本地, 不许偷偷连 HF.
    把 MODEL_PATH 指到一个不存在的子目录, 期望构造时抛 FileNotFoundError —
    SystemExit 不行 (BaseException 子类), 调用方 main.py 的 except Exception 兜不住.
    错误信息需包含中文 "reranker 模型不可用" 提示, 让用户能直接看到下载命令.
    """
    import capabilities.rag.reranker as r_mod
    monkeypatch.setattr(r_mod, "MODEL_PATH", str(tmp_path / "nope"))

    with pytest.raises(FileNotFoundError) as exc_info:
        r_mod.Reranker()
    assert "reranker 模型不可用" in str(exc_info.value)


def test_reranker_init_config_json_缺失_抛FileNotFoundError(monkeypatch, tmp_path):
    """
    模型目录存在但 config.json 缺失 — 典型断点下载场景.
    期望抛 FileNotFoundError 且错误信息含 "config.json 缺失", 跟"目录不存在"
    走的是同一兜底路径 (调用方 main.py 宽 catch).
    """
    import capabilities.rag.reranker as r_mod
    # tmp_path 是真实存在的空目录, 不放 config.json
    monkeypatch.setattr(r_mod, "MODEL_PATH", str(tmp_path))

    with pytest.raises(FileNotFoundError) as exc_info:
        r_mod.Reranker()
    assert "config.json 缺失" in str(exc_info.value)


def test_reranker_init_失败_可被except_Exception兜住(monkeypatch, tmp_path):
    """
    回归测试: Task 4 reviewer ⚠️ #2 — 之前用 SystemExit (BaseException 子类),
    main.py `except Exception` 兜不住, log_event("reranker.load_failed", ...) 成死码.
    本测试直接验证: 模型缺失时, 用 `except Exception` 能兜住 Reranker(),
    这跟 main.py:_load_reranker_if_enabled 的 try/except 行为完全一致.
    """
    import capabilities.rag.reranker as r_mod
    monkeypatch.setattr(r_mod, "MODEL_PATH", str(tmp_path / "nope"))

    caught = False
    try:
        r_mod.Reranker()
    except Exception:  # noqa: BLE001 — 故意宽 catch, 模拟 main.py
        caught = True
    assert caught, "Reranker 模型缺失时应被 except Exception 兜住 (FileNotFoundError 是 Exception 子类)"


# ============ 债20: 库内加载日志走 logging, 不污染 stdout ============


def test_reranker_加载日志走logging不进stdout(fake_cross_encoder, caplog, capsys):
    """债20 (主线债务清算): 库内"加载模型"状态日志属于开发者诊断信息,
    应走 logging (默认 stderr), 不该 print 到 stdout。

    理由: stdout 是给"用户输出/协议数据"的, 库内进度日志混进去会污染 (MCP server
    场景下直接炸协议)。这里 rag 库虽不走 MCP, 但统一纪律: 库内日志一律 logging。

    断言两面:
    - caplog 能抓到 "加载" 字样 (确实走了 logging)
    - capsys 的 stdout 不含 "加载本地 reranker" (确实没 print 到 stdout)
    """
    import logging
    from capabilities.rag.reranker import Reranker

    with caplog.at_level(logging.INFO):
        Reranker()

    logged = caplog.text
    out = capsys.readouterr().out
    assert "加载" in logged, f"加载日志应走 logging, caplog 没抓到: {logged!r}"
    assert "加载本地 reranker" not in out, f"加载日志不该进 stdout, 但 stdout 有: {out!r}"
