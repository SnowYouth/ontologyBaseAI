"""tools/ontology_join.py 单元测试 — get_join_path 按需拉精确 JOIN 路径。"""
import pytest


@pytest.fixture(autouse=True)
def _reset_ontology():
    import core.ontology_view as mod
    mod._ontology = None
    yield
    mod._ontology = None


def _inject():
    from core.ontology_view import build_indexes
    raw = {
        "domains": [], "entities": [
            {"id": "a", "entityCode": "A", "displayName": "A", "domain": "d",
             "maps_to": "m", "tables": ["a"], "attributes": []},
            {"id": "b", "entityCode": "B", "displayName": "B", "domain": "d",
             "maps_to": "m", "tables": ["b"], "attributes": []},
        ],
        "relationships": [
            {"source": "a", "target": "b", "type": "MANY_TO_ONE", "via": "a.B_ID",
             "label": "语义边", "source_col": "a.B_ID", "target_col": "b.ID",
             "bridge_count": 10},
        ],
        "valueSets": {},
    }
    import core.ontology_view as mod
    mod._ontology = build_indexes(raw)


class TestGetJoinPath:
    def test_未初始化返回error(self):
        from agent.tools.ontology_join import get_join_path
        assert "error" in get_join_path(from_entity="a", to_entity="b")

    def test_命中返回路径(self):
        _inject()
        from agent.tools.ontology_join import get_join_path
        result = get_join_path(from_entity="a", to_entity="b")
        assert len(result["paths"]) == 1
        p = result["paths"][0]
        assert p["from"] == "a" and p["to"] == "b"
        assert p["source_col"] == "a.B_ID"

    def test_无直连返回空路径不炸(self):
        _inject()
        from agent.tools.ontology_join import get_join_path
        result = get_join_path(from_entity="a", to_entity="ghost")
        assert result["paths"] == []


def _inject_two_hop():
    """a -c- b: 无 a↔b 直连, 两跳经 c 可达 (复刻真实 lib_loan→lib_book 形态)。"""
    from core.ontology_view import build_indexes
    raw = {
        "domains": [], "entities": [
            {"id": x, "entityCode": x.upper(), "displayName": x.upper(), "domain": "d",
             "maps_to": "m", "tables": [x], "attributes": []}
            for x in ("a", "b", "c", "lone")
        ],
        "relationships": [
            {"source": "a", "target": "c", "type": "MANY_TO_ONE", "via": "a.C_ID",
             "label": "a到c", "source_col": "a.C_ID", "target_col": "c.ID",
             "bridge_count": 10},
            {"source": "c", "target": "b", "type": "MANY_TO_ONE", "via": "c.B_ID",
             "label": "c到b", "source_col": "c.B_ID", "target_col": "b.ID",
             "bridge_count": 20},
        ],
        "valueSets": {},
    }
    import core.ontology_view as mod
    mod._ontology = build_indexes(raw)


class TestGetJoinPathMultiHopFallback:
    """一跳没有时回退 find_join_paths_multi (工具签名退化修复)。

    真实证据: 历史调用里有一批 n_paths=0, 其中**大半是"一跳无两跳有"**
    (如 lib_loan→lib_book 经 lib_item_tag_rel 可达), 本体是对的, 只是
    get_join_path 从没调过早已存在的多跳版。agent 几乎全自己绕开了 (改走单表/
    IN 字面量), 答案出来了但没人知道它绕了路。
    """

    def test_一跳有时不回退_paths契约零破坏(self):
        """一跳命中 → paths 保持扁平边视图, 不冒出 multi_hop_paths。

        paths 是 9 个调用方的硬契约 (validate_concepts 只判真假, smoke 读
        paths[0]["source_col"]); 多跳结构塞进 paths 会让概念 connectable 翻转。
        """
        _inject()
        from agent.tools.ontology_join import get_join_path
        result = get_join_path(from_entity="a", to_entity="b")
        assert len(result["paths"]) == 1
        assert result["paths"][0]["source_col"] == "a.B_ID"
        assert "multi_hop_paths" not in result

    def test_一跳无两跳有_给出多跳路径(self):
        _inject_two_hop()
        from agent.tools.ontology_join import get_join_path
        result = get_join_path(from_entity="a", to_entity="b")
        assert result["paths"] == []          # 一跳仍然是空, 语义不变
        multi = result["multi_hop_paths"]
        assert len(multi) == 1
        assert multi[0]["hops"] == 2

    def test_多跳边序列连通且列对对齐(self):
        """e[i].to == e[i+1].from 且每条边 from.source_col = to.target_col。

        agent 要照着这个序列直接拼 JOIN, 断裂或列对没对齐就会拼出笛卡尔积。
        """
        _inject_two_hop()
        from agent.tools.ontology_join import get_join_path
        edges = get_join_path(from_entity="a", to_entity="b")["multi_hop_paths"][0]["edges"]
        assert [e["from"] for e in edges] == ["a", "c"]
        assert [e["to"] for e in edges] == ["c", "b"]
        assert edges[0]["source_col"] == "a.C_ID" and edges[0]["target_col"] == "c.ID"
        assert edges[1]["source_col"] == "c.B_ID" and edges[1]["target_col"] == "b.ID"

    def test_一跳无多跳也无_保持老行为(self):
        """图上真断开 → 两个键都空。这才是真缺口 (A 方向要采的燃料)。"""
        _inject_two_hop()
        from agent.tools.ontology_join import get_join_path
        result = get_join_path(from_entity="a", to_entity="lone")
        assert result["paths"] == []
        assert result.get("multi_hop_paths", []) == []

    def test_反向查询也回退(self):
        """b→a 与 a→b 同样该回退 (agent 问法方向不固定)。"""
        _inject_two_hop()
        from agent.tools.ontology_join import get_join_path
        multi = get_join_path(from_entity="b", to_entity="a")["multi_hop_paths"]
        assert multi[0]["hops"] == 2
        assert [e["from"] for e in multi[0]["edges"]] == ["b", "c"]

    def test_日志区分多跳命中与真断开(self, monkeypatch):
        """n_paths=0 过去是个混着 A/B 两类的黑盒; 落 n_multi 才能把真缺口切出来。"""
        _inject_two_hop()
        import agent.tools.ontology_join as mod
        events: list = []
        monkeypatch.setattr(mod, "log_event",
                            lambda name, payload: events.append((name, payload)))
        mod.get_join_path(from_entity="a", to_entity="b")
        mod.get_join_path(from_entity="a", to_entity="lone")
        hit, broken = events[0][1], events[1][1]
        assert hit["n_paths"] == 0 and hit["n_multi"] == 1 and hit["hops"] == 2
        assert broken["n_paths"] == 0 and broken["n_multi"] == 0
        assert broken["hops"] is None


def _inject_cross_source():
    """a(lib) -c(lib)- b(lib2): 中转同库, 终点跨库 (复刻真实 lib_loan→lib_reader→account)。"""
    from core.ontology_view import build_indexes
    raw = {
        "domains": [], "entities": [
            {"id": "a", "entityCode": "A", "displayName": "A", "domain": "d",
             "maps_to": "m", "tables": ["a"], "attributes": [], "data_source": "lib"},
            {"id": "c", "entityCode": "C", "displayName": "C", "domain": "d",
             "maps_to": "m", "tables": ["c"], "attributes": [], "data_source": "lib"},
            {"id": "b", "entityCode": "B", "displayName": "B", "domain": "d",
             "maps_to": "m", "tables": ["b"], "attributes": [], "data_source": "lib2"},
        ],
        "relationships": [
            {"source": "a", "target": "c", "type": "MANY_TO_ONE", "via": "a.C_ID",
             "label": "a到c", "source_col": "a.C_ID", "target_col": "c.ID",
             "bridge_count": 10},
            {"source": "c", "target": "b", "type": "MANY_TO_ONE", "via": "c.B_ID",
             "label": "c到b", "source_col": "c.B_ID", "target_col": "b.ID",
             "bridge_count": 20},
        ],
        "valueSets": {},
    }
    import core.ontology_view as mod
    mod._ontology = build_indexes(raw)


class TestMultiHopCrossSource:
    """多跳路径跨库时必须标出来 —— 否则本次改动等于新造一个坑。

    真实案例: lib_loan(lib) → lib_reader(lib2) → account(lib2), 两跳可达但
    物理上不能写一条 SQL JOIN。回退多跳之前 agent 看到空路径会自己绕开;
    回退之后它会照着拼, 然后在 execute_sql 那里撞墙 —— 危害是我引入的。
    """

    def test_跨库多跳带segments分段(self):
        _inject_cross_source()
        from agent.tools.ontology_join import get_join_path
        p = get_join_path(from_entity="a", to_entity="b")["multi_hop_paths"][0]
        assert p["cross_source"] is True
        segs = p["segments"]
        assert [s["source"] for s in segs] == ["lib", "lib2"]
        assert segs[0]["exports"] == ["b.ID"] and segs[1]["needs"] == ["b.ID"]

    def test_同库多跳不带segments免膨胀(self):
        """同库路径的 segments 只有一段, 纯冗余; 不给才不白占上下文。"""
        _inject_two_hop()
        from agent.tools.ontology_join import get_join_path
        p = get_join_path(from_entity="a", to_entity="b")["multi_hop_paths"][0]
        assert p["cross_source"] is False
        assert "segments" not in p


class TestNoPathFuelCollection:
    """★ A 方向: "两端都登记、一跳没有、多跳也没有" = 图上真断开, 采成燃料。

    此前 `get_join_path` 的 n_paths=0 只 log_event 记一笔就丢了 —— 冷路径只订
    obqc 的 hard_block。而真实日志里 n_paths=0 的次数**比 no_edge 硬拦还多出一截**,
    且**危害完全不可见**: 那些调用之后 agent 几乎全自己绕过去了
    (改走单表 / IN 字面量, 极少报错), 答案出来了没人知道它绕了路。

    ★ 跨库的不进队列 (但要采): 真实 B 类 entity 对**大多数是跨库的** (lib_reader/account
    在 lib2, 另一端在 lib), 相关提问也绝大部分落在这些对上。跨库物理上不能加边, 提议
    出来只会被人审 reject, 白烧探针的真库开销。所以**采集全部让它可见, 只把
    同库的送冷路径**。
    """

    @pytest.fixture(autouse=True)
    def _clear_fuel(self):
        """去重集是进程级的, 不清会让后跑的用例被先跑的污染 (首跑就栽在这)。"""
        import agent.tools.ontology_join as mod
        mod._reset_nopath_fuel()
        yield
        mod._reset_nopath_fuel()

    def test_真断开时采集燃料(self, monkeypatch):
        _inject_two_hop()
        import agent.tools.ontology_join as mod
        got: list = []
        monkeypatch.setattr(mod, "log_failure_signal",
                            lambda kind, payload, source=None: got.append((kind, payload)))
        mod.get_join_path(from_entity="a", to_entity="lone")
        assert len(got) == 1
        kind, payload = got[0]
        assert kind == "obqc_circuit_break"          # 复用现有冷路径 kind
        ne = payload["no_edge_signals"]
        assert ne == [{"entities": ["a", "lone"], "cols": []}]
        assert payload["origin"] == "get_join_path"  # 与 obqc 硬拦来的信号区分

    def test_多跳命中时不采集(self, monkeypatch):
        """本体是对的 (A 类), 不是缺口 —— 采了就是往燃料库灌噪音。"""
        _inject_two_hop()
        import agent.tools.ontology_join as mod
        got: list = []
        monkeypatch.setattr(mod, "log_failure_signal",
                            lambda kind, payload, source=None: got.append(payload))
        mod.get_join_path(from_entity="a", to_entity="b")
        assert got == []

    def test_一跳命中时不采集(self, monkeypatch):
        _inject()
        import agent.tools.ontology_join as mod
        got: list = []
        monkeypatch.setattr(mod, "log_failure_signal",
                            lambda kind, payload, source=None: got.append(payload))
        mod.get_join_path(from_entity="a", to_entity="b")
        assert got == []

    def test_端点未登记不采集(self, monkeypatch):
        """实体缺口归另一类债 (补实体), 不是"缺边"。混进来冷路径探针无表可探。"""
        _inject_two_hop()
        import agent.tools.ontology_join as mod
        got: list = []
        monkeypatch.setattr(mod, "log_failure_signal",
                            lambda kind, payload, source=None: got.append(payload))
        mod.get_join_path(from_entity="a", to_entity="ghost")
        assert got == []

    def test_跨库对不采集(self, monkeypatch):
        """★ 跨库物理上加不了边, 提议出来必被 reject —— 白烧探针的真库开销。"""
        _inject_cross_source()
        import core.ontology_view as core_mod
        # 把 c→b 那条边去掉, 让 a(lib) 与 b(lib2) 真断开且跨库
        for edges in core_mod._ontology.adjacency.values():
            edges[:] = [e for e in edges if e.get("via") != "c.B_ID"]
        import agent.tools.ontology_join as mod
        got: list = []
        monkeypatch.setattr(mod, "log_failure_signal",
                            lambda kind, payload, source=None: got.append(payload))
        r = mod.get_join_path(from_entity="a", to_entity="b")
        assert r["paths"] == [] and not r.get("multi_hop_paths")
        assert got == []

    def test_同一对只采一次(self, monkeypatch):
        """agent 会重试; 同一对反复问不该把燃料库刷屏 (对齐热路径 _fuel_collected)。"""
        _inject_two_hop()
        import agent.tools.ontology_join as mod
        mod._reset_nopath_fuel()
        got: list = []
        monkeypatch.setattr(mod, "log_failure_signal",
                            lambda kind, payload, source=None: got.append(payload))
        mod.get_join_path(from_entity="a", to_entity="lone")
        mod.get_join_path(from_entity="a", to_entity="lone")
        mod.get_join_path(from_entity="lone", to_entity="a")   # 反向也是同一对
        assert len(got) == 1

    def test_采集异常不影响返回(self, monkeypatch):
        """热路径纪律: 埋点炸了也得把路径结果交出去 (同 _collect_fuel)。"""
        _inject_two_hop()
        import agent.tools.ontology_join as mod
        mod._reset_nopath_fuel()

        def _boom(kind, payload, source=None):
            raise RuntimeError("db down")

        monkeypatch.setattr(mod, "log_failure_signal", _boom)
        assert mod.get_join_path(from_entity="a", to_entity="lone") == {"paths": []}


class TestGetConceptJoin:
    """薄封装: 取单例 + 调 core.build_concept_join + 落日志。"""

    def test_本体未初始化返回error(self, monkeypatch):
        import agent.tools.ontology_join as mod
        monkeypatch.setattr(mod, "get_ontology", lambda: None)
        assert mod.get_concept_join("vip", "C") == {"error": "ontology not initialized"}

    def test_正例透传并落日志(self, monkeypatch):
        import agent.tools.ontology_join as mod
        fake = {"concept_id": "vip", "output_key": {"entity": "A", "col": "AID"},
                "target_entity": "C",
                "path": {"hops": 2, "edges": [{"to": "B"}, {"to": "C"}], "segments": []},
                "alternatives": []}
        events: list = []
        monkeypatch.setattr(mod, "get_ontology", lambda: object())
        monkeypatch.setattr(mod, "build_concept_join",
                            lambda onto, cid, tgt, max_hops: fake)
        monkeypatch.setattr(mod, "log_event",
                            lambda name, payload: events.append((name, payload)))
        assert mod.get_concept_join("vip", "C") == fake
        assert events[0][0] == "ontology.concept_join"
        assert events[0][1]["concept_id"] == "vip"
        assert events[0][1]["hops"] == 2
        assert events[0][1]["n_alternatives"] == 0

    def test_失败也落日志(self, monkeypatch):
        """error 分支同样要有可观测记录 (黑盒 I/O 必落 JSONL 硬约定)。"""
        import agent.tools.ontology_join as mod
        events: list = []
        monkeypatch.setattr(mod, "get_ontology", lambda: object())
        monkeypatch.setattr(mod, "build_concept_join",
                            lambda onto, cid, tgt, max_hops: {"error": "概念不存在: x"})
        monkeypatch.setattr(mod, "log_event",
                            lambda name, payload: events.append((name, payload)))
        r = mod.get_concept_join("x", "C")
        assert "error" in r
        assert events[0][1]["error"] == "概念不存在: x"

    def test_已注册为serving工具(self):
        import agent.tools.ontology_join  # noqa: F401  触发注册
        from agent.tools.base import _REGISTRY
        assert _REGISTRY["get_concept_join"].category == "serving"
