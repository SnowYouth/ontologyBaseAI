"""tests/selfevolve/test_label_distinct_gate.py — label 雷同护栏的"门"(接线测试).

## 为什么有这个文件

`check_relation_label_distinct` 是"防 label 雷同复发"的护栏, 但它**只有离线纯函数
+ 自己的单测**, 生产写入路径零调用者 —— 复发时不会有任何东西报警, 与其 docstring
意图相反 (本阶段第 8 处"写好没调用")。

★ 纠正一个容易搞错的判断: 它并非"完全没有门"。
`capabilities/ontology/test_check_relation_label_distinct.py::test_真库回归_全库0雷同`
会在例行 `uv run pytest -q` 里对**真库**跑一遍校验器 (capabilities/ontology/test_*.py 确实
被采集)。所以**存量**复发是有哨兵的。
真正的缺口是**时序**: 那条测试只在有人跑 pytest 时才响, 而写入是随时发生的 ——
坏边会先落库, 直到下次跑测试才被发现, 中间 agent 已经按雷同 label 选错边了。
这与本阶段"闸门判得对但拦不住(闸在落库后跑)"是同一个形状的缺陷。

## 这里装的门

把校验前移到唯一的写入咽喉 `OntologyStore.upsert_relation`: 落库**前**先看这条
边会不会与同端点对的现有边 label 雷同, 雷同则抛 ValueError 拒绝写入。
两条活的写入路径 (web POST /ontology/relation、selfevolve apply relation_add)
都经过它, 一次接线两处受益。

下面每条测试都断言**物理后果**(库里到底有几条边), 不只看抛没抛异常 ——
"安静的无害失败"(报成功却没落库/报失败却落了库)是本项目反复栽的坑。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capabilities.ontology.ontology_store import OntologyStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """tmp_path 隔离; 绝不碰权威库 capabilities/ontology/ontology.db。"""
    s = OntologyStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _edge(source, target, source_col, target_col, label):
    return {"source": source, "target": target, "type": "MANY_TO_ONE",
            "label": label, "source_col": source_col, "target_col": target_col}


def _n_active(store, source, target) -> int:
    """该无序端点对的存活边条数 (物理后果, 不看返回值)。"""
    return sum(
        1 for r in store.load_raw()["relationships"]
        if {r["source"], r["target"]} == {source, target}
    )


class TestLabelDistinctGate:

    def test_同端点对label雷同_拒绝写入且库里不多出边(self, store):
        """核心: 第二条雷同 label 的边必须被拦在库外。"""
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "同一句"))
        assert _n_active(store, "a", "b") == 1

        with pytest.raises(ValueError, match="label"):
            store.upsert_relation(_edge("a", "b", "A_CODE", "B_CODE", "同一句"))

        # ★ 物理后果: 仍只有 1 条, 坏边没落库
        assert _n_active(store, "a", "b") == 1

    def test_反向边label雷同也拦(self, store):
        """端点对是无序的 (get_join_path 选边不分方向), b→a 雷同同样要拦。"""
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "同一句"))
        with pytest.raises(ValueError, match="label"):
            store.upsert_relation(_edge("b", "a", "B_X", "A_X", "同一句"))
        assert _n_active(store, "a", "b") == 1

    def test_同端点对label可区分_正常写入(self, store):
        """不误伤: 这才是同端点对多条边的正常形态。"""
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "按归属"))
        store.upsert_relation(_edge("a", "b", "A_CODE", "B_CODE", "按编码"))
        assert _n_active(store, "a", "b") == 2

    def test_空label不拦(self, store):
        """空 label 是既有裸边债 (RelationsPanel 已有裸边置顶告警), 不归本护栏。

        真库当前就有 1 条空 label 边, 拦它会把正常写入路径堵死。
        """
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", ""))
        store.upsert_relation(_edge("a", "b", "A_CODE", "B_CODE", ""))
        assert _n_active(store, "a", "b") == 2

    def test_不同端点对label雷同_不拦(self, store):
        """护栏只管"同端点对内"; 不同端点对复用同一句 label 是合法的。"""
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "按归属"))
        store.upsert_relation(_edge("c", "d", "C_ID", "D_ID", "按归属"))
        assert _n_active(store, "a", "b") == 1
        assert _n_active(store, "c", "d") == 1

    def test_同一条边改label_幂等不自我阻塞(self, store):
        """★ 关键边界: upsert 同一条边 (via 相同) 是更新, 不该被自己的 label 拦住。

        via 由 col 派生, 所以"同一条边"= 同 source/target/col 对。
        若护栏拿全量 label 比较而不排除自己, 重复灌库/幂等 apply 会假报雷同。
        """
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "按归属"))
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "按归属"))  # 原样重灌
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "改个说法"))  # 改 label
        assert _n_active(store, "a", "b") == 1

    def test_软删的边不参与雷同判定(self, store):
        """被淘汰(status=0)的边腾出它的 label —— load_raw 已滤 active, 口径一致。"""
        store.upsert_relation(_edge("a", "b", "A_ID", "B_ID", "同一句"))
        store.soft_delete_relation("a", "b", "a.A_ID=b.B_ID")
        # 墓碑边的 label 应可被新边复用
        store.upsert_relation(_edge("a", "b", "A_CODE", "B_CODE", "同一句"))
        assert _n_active(store, "a", "b") == 1
