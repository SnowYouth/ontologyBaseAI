"""tests/selfevolve/test_probe.py — probe_relation 三层探针 + 孤儿列集合差 单测 (TDD).

覆盖 (spec §4.1 三层判据 / §4.2 无向 PK / §4.2.1 两端PK也pass / §4.6 孤儿列):
- 第一层: 至少一端 PK/UNIQUE (无向) / 两端都 PK 也 pass / 任一表空 → unknown
- 第二层: 包含率不足 → fail / NULL 比例高 → unknown
- 第三层: 命名无关 → unknown / 基数悬殊 → unknown
- 全通过 → pass
- find_orphan_cols: 未被边使用的列 / attributes 漏登记退化点
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capabilities.ontology.selfevolve.probe import ProbeResult, probe_relation, find_orphan_cols


# ---------- 测试脚手架: 注入 run_sql / get_index_info ----------
# probe 约定: get_index_info(table, source) — 真实调用方注入带 source 路由的回调,
# 测试按表名查 dict (source 参数忽略, 走什么库不影响判据逻辑)。

def _make_index_info(pk_cols=(), unique_cols=(), row_count=100, data_source=""):
    """默认 row_count=100, 与 mock run_sql 的 'n = rate*100' 口径对齐。

    data_source 默认空串 (2026-10-15 起 probe.py 不再兜底写死源名); 需要校验
    连接池路由的用例 (TestProbeDataSourceRouting) 显式传入。
    """
    return {"pk_cols": list(pk_cols), "unique_cols": list(unique_cols),
            "row_count": row_count, "data_source": data_source}


def _make_run_sql(inclusion_rate=1.0, null_rate=0.0):
    """造假 run_sql, 按实现的探针 SQL 形态路由 (spec §4.1 第二层)。

    约定表行数 = 100 (与 _make_index_info 默认 row_count 一致):
    - 包含率命中: COUNT(DISTINCT) ... IN (SELECT DISTINCT ...) → n = rate*100
    - 包含率总数: COUNT(DISTINCT) ... IS NOT NULL (无 IN)    → n = 100
    - NULL 计数:  COUNT(*) ... IS NULL                        → n = rate*100
    """
    calls = []

    def run_sql(sql: str, source: str) -> list[dict]:
        calls.append((sql, source))
        s = sql.upper()
        if "COUNT(*)" in s and "IS NULL" in s:
            return [{"n": int(null_rate * 100)}]
        if "COUNT(DISTINCT" in s and " IN (" in s:
            return [{"n": int(inclusion_rate * 100)}]
        if "COUNT(DISTINCT" in s:
            return [{"n": 100}]
        raise AssertionError(f"unexpected sql: {sql}")

    run_sql.calls = calls
    return run_sql


class TestProbeLayer1:
    """第一层硬门 (无向 PK, spec §4.2)。

    注意: Layer1/2 测试用同名列对 (如 SRC_ID vs SRC_ID), 只让被测层生效,
    避免第三层命名 Jaccard 的干扰 (SOME_COL vs TGT_ID Jaccard=0 会误判 unknown)。
    """

    def test_至少一端是PK则通过第一层(self):
        """source_col 是 PK → 过; 无向, 交换过来也过。"""
        idx = {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SRC_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "pass"
        # 第一层 check 应记录 ok
        layer1 = [c for c in result.checks if c["name"] == "pk_or_unique"]
        assert layer1 and layer1[0]["ok"] is True

    def test_无向_target端是PK也过(self):
        """spec §4.2: 无向——target_col 是 PK 同样过第一层。"""
        idx = {
            "src": _make_index_info(pk_cols=["SRC_PK"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_ID"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "TGT_ID", "TGT_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "pass"

    def test_两端都PK也pass(self):
        """spec §4.2.1: 两端都是 PK 的 1:1 扩展表是真边, 不判 unknown。"""
        idx = {
            "src": _make_index_info(pk_cols=["HOLDING_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["HOLDING_ID"], row_count=100),
        }
        result = probe_relation(
            "src", "tgt", "HOLDING_ID", "HOLDING_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "pass"

    def test_UNIQUE端也算(self):
        """unique 索引列同样满足第一层。"""
        idx = {
            "src": _make_index_info(pk_cols=["ID"], unique_cols=["MEMBER_CARD_NO"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "MEMBER_CARD_NO", "MEMBER_CARD_NO",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "pass"

    def test_两端都不是PK则fail(self):
        """spec §4.2: 引用要求至少一端唯一标识一行。"""
        idx = {
            "src": _make_index_info(pk_cols=["SRC_PK"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "SOME_COL", "OTHER_COL",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "fail"
        layer1 = [c for c in result.checks if c["name"] == "pk_or_unique"]
        assert layer1 and layer1[0]["ok"] is False

    def test_任一表行数为0则unknown(self):
        """空表无法判断 → unknown, 不生成提议。"""
        idx = {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=0),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SOME_COL",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "unknown"


class TestProbeLayer2:
    """第二层量化 (包含率 + NULL 比例)。"""

    def _idx(self):
        return {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=100),
        }

    def test_包含率不足则fail(self):
        """inclusion < INCLUSION_THRESHOLD (校准后 0.5) → fail。"""
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SRC_ID",
            run_sql=_make_run_sql(inclusion_rate=0.3),
            get_index_info=lambda table, source: self._idx()[table],
        )
        assert result.verdict == "fail"
        layer2 = [c for c in result.checks if c["name"] == "inclusion"]
        assert layer2 and layer2[0]["ok"] is False
        assert result.evidence["inclusion_rate"] == pytest.approx(0.3)

    def test_包含率刚好达阈值则pass(self):
        """边界: inclusion_rate == 0.5 (真库校准后阈值) → ok (>= 语义)。"""
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SRC_ID",
            run_sql=_make_run_sql(inclusion_rate=0.5),
            get_index_info=lambda table, source: self._idx()[table],
        )
        assert result.verdict == "pass"

    def test_NULL比例高则unknown(self):
        """spec §4.1 ⑤: null_rate > 0.5 → unknown (此列不是 FK 锚点)。"""
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SRC_ID",
            run_sql=_make_run_sql(null_rate=0.8),
            get_index_info=lambda table, source: self._idx()[table],
        )
        assert result.verdict == "unknown"
        layer2 = [c for c in result.checks if c["name"] == "null_rate"]
        assert layer2 and layer2[0]["ok"] is False


class TestProbeLayer3:
    """第三层兜底护栏 (命名 Jaccard + 基数比, spec §4.3)。"""

    def _idx(self, tgt_rows=500):
        return {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=1000),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=tgt_rows),
        }

    def test_命名token_jaccard_低则unknown(self):
        """spec §4.3 ⑥: Jaccard < 0.20 → unknown (不 fail)。

        SEQ_NO vs MEMBER_ID: token {seq,no} ∩ {cust,id} = ∅ → Jaccard=0。
        """
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SEQ_NO",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: self._idx()[table],
        )
        assert result.verdict == "unknown"
        layer3 = [c for c in result.checks if c["name"] == "col_name_similarity"]
        assert layer3 and layer3[0]["ok"] is False

    def test_命名缩写变体可过(self):
        """MEMBER_ID vs LIB_READER_ID: {cust,id} vs {lib_reader,id} Jaccard=0.5 > 0.2。"""
        result = probe_relation(
            "src", "tgt", "SRC_ID", "LIB_READER_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: self._idx()[table],
        )
        # 需要一端是 PK: MEMBER_ID 不是 → 先给 target 端 UNIQUE
        # (这里 source_col=SRC_ID 是 PK, 命名是 SRC_ID vs LIB_READER_ID)
        # 重构: 直接验证同名列对场景下命名过
        assert result.verdict in ("pass", "fail")  # 命名不是 unknown 原因
        sim = [c for c in result.checks if c["name"] == "col_name_similarity"]
        assert sim and sim[0]["ok"] is True

    def test_基数比悬殊则unknown(self):
        """spec §4.1 ⑦: target 极小 = 枚举表巧合 → unknown。

        row_count(target)/row_count(source) = 5/1000 = 0.005... 但阈值 0.001:
        用 0/1000? 行数 0 是第一层的 unknown。用 target=0 会先被第一层拦。
        改: target_rows=0? 不行。构造 target_rows=1, source=10000 → 0.0001 < 0.001。
        """
        idx = {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=10000),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=1),
        }
        result = probe_relation(
            "src", "tgt", "SRC_ID", "TGT_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert result.verdict == "unknown"
        layer3 = [c for c in result.checks if c["name"] == "cardinality_ratio"]
        assert layer3 and layer3[0]["ok"] is False

    def test_全通过则pass(self):
        """三层全 ok → pass (生成提议)。"""
        result = probe_relation(
            "src", "tgt", "SRC_ID", "SRC_ID",
            run_sql=_make_run_sql(inclusion_rate=0.99, null_rate=0.05),
            get_index_info=lambda table, source: self._idx()[table],
        )
        assert result.verdict == "pass"
        assert all(c["ok"] for c in result.checks)


class TestProbeResultShape:
    """ProbeResult 返回结构。"""

    def test_checks_含三层检查名(self):
        idx = {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "SRC_ID", "TGT_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        names = {c["name"] for c in result.checks}
        assert "pk_or_unique" in names
        assert "inclusion" in names
        assert "null_rate" in names
        assert "col_name_similarity" in names
        assert "cardinality_ratio" in names

    def test_evidence_含四个数值(self):
        idx = {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=500),
        }
        result = probe_relation(
            "src", "tgt", "SRC_ID", "TGT_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table],
        )
        assert set(result.evidence.keys()) >= {
            "inclusion_rate", "null_rate", "col_similarity", "cardinality_ratio"}


class TestFindOrphanCols:
    """孤儿列集合差 (spec §4.6)。"""

    def test_未被边使用的列是孤儿(self):
        entities = {
            "lib_loan": {"attributes": [
                {"name": "LIB_LOAN_ID", "kind": "pk"},
                {"name": "READER_ID"},
                {"name": "CARD_NO"},
                {"name": "REC_STATUS"},
            ]},
            "lib_reader": {"attributes": [
                {"name": "MEMBER_ID", "kind": "pk"},
            ]},
        }
        relations = [
            {"source": "lib_loan", "target": "lib_reader",
             "source_col": "READER_ID", "target_col": "MEMBER_ID"},
        ]
        orphans = find_orphan_cols("lib_loan", entities, relations)
        assert orphans == {"LIB_LOAN_ID", "CARD_NO", "REC_STATUS"}

    def test_边上的列不算孤儿(self):
        """两端列都被边使用 → 移出。"""
        entities = {
            "a": {"attributes": [{"name": "ID"}, {"name": "X"}]},
            "b": {"attributes": [{"name": "ID"}]},
        }
        relations = [
            {"source": "a", "target": "b", "source_col": "X", "target_col": "ID"},
        ]
        orphans = find_orphan_cols("a", entities, relations)
        assert orphans == {"ID"}

    def test_所有边参与实体匹配(self):
        """边可能从 b 指向 a (方向无关), 只看该实体的列是否被任何边碰到。"""
        entities = {
            "a": {"attributes": [{"name": "ID"}, {"name": "X"}]},
            "b": {"attributes": [{"name": "ID"}]},
        }
        relations = [
            {"source": "b", "target": "a", "source_col": "ID", "target_col": "X"},
        ]
        orphans = find_orphan_cols("a", entities, relations)
        assert orphans == {"ID"}

    def test_无任何边则全部列是孤儿(self):
        entities = {
            "a": {"attributes": [{"name": "ID"}, {"name": "X"}]},
        }
        orphans = find_orphan_cols("a", entities, [])
        assert orphans == {"ID", "X"}

    def test_实体不在entities里返回空集(self):
        """防御: entity_id 查不到 → 空集 (调用方处理)。"""
        orphans = find_orphan_cols("ghost", {"a": {"attributes": []}}, [])
        assert orphans == set()

    def test_已知退化_漏登记attributes扫不出(self):
        """spec §4.6 退化点: 孤儿列基于本体 attributes, 物理表多出来的列扫不到。"""
        # 物理表有 NOTE_COL 但本体 attributes 没登记 → 不在 orphans 里
        entities = {
            "a": {"attributes": [{"name": "ID"}]},  # NOTE_COL 漏登记
        }
        orphans = find_orphan_cols("a", entities, [])
        assert "NOTE_COL" not in orphans
        assert orphans == {"ID"}


class TestProbeDataSourceRouting:
    """run_sql 的 source 实参必须是数据源名, 不是表名 (2026-09-09 线上 bug)。

    真实调用方 (web/app.py) 的 run_sql 按 source 查连接池 _connections;
    探针若把 source_entity (表名) 传进去 → 池里查不到 → 抛异常 →
    被 _single_count 吞成 0 → inclusion_rate 恒 0.0 → 一切边恒判 fail。

    实证: 本体里一批已知正确边全被判 fail, 但手工 JOIN 显示几乎完全一致。
    """

    def test_run_sql_收到的source是数据源名而非表名(self):
        idx = {
            "lib_loan": _make_index_info(pk_cols=["LIB_LOAN_ID"], row_count=100),
            "acct_rel": _make_index_info(pk_cols=["LIBCARD_ID"], row_count=100),
        }
        seen = []
        inner = _make_run_sql()

        def run_sql(sql, source):
            seen.append(source)
            return inner(sql, source)

        probe_relation("lib_loan", "acct_rel", "LIB_LOAN_ID", "LIB_LOAN_ID",
                       run_sql=run_sql,
                       get_index_info=lambda table, source: idx[table])
        assert seen, "run_sql 没被调用"
        assert "lib_loan" not in seen, (
            f"探针把表名当数据源名传给 run_sql: {seen}")

    def test_按数据源名路由的run_sql能正常判pass(self):
        """模拟生产 run_sql (按 source 查连接池): 数据全一致时应判 pass, 不是 fail。"""
        connections = {"lib": "fake-conn"}
        idx = {
            "lib_loan": _make_index_info(pk_cols=["LIB_LOAN_ID"], row_count=100,
                                         data_source="lib"),
            "acct_rel": _make_index_info(pk_cols=["LIBCARD_ID"], row_count=100,
                                         data_source="lib"),
        }
        inner = _make_run_sql(inclusion_rate=1.0, null_rate=0.0)

        def run_sql_like_prod(sql, source):
            if source not in connections:
                raise ValueError(f"data source '{source}' not found")
            return inner(sql, source)

        result = probe_relation(
            "lib_loan", "acct_rel", "LIB_LOAN_ID", "LIB_LOAN_ID",
            run_sql=run_sql_like_prod,
            get_index_info=lambda table, source: idx[table])
        assert result.verdict == "pass", (
            f"生产路径下真外键被误判 {result.verdict}: {result.evidence}")


class TestProbeColumnCaseNormalization:
    """PK/UNIQUE 比对必须大小写归一 (2026-09-09 线上 bug)。

    information_schema 返回大写列名, 而本体 relation.via 存在小写形态
    (如 lib_loan.lib_loan_id=lib_loan_ext.lib_loan_id) → 大小写敏感的
    `in` 比对使这些边全挂第一层硬门, 连第二层都进不去 (inclusion=None)。
    与已知的 "分库中间件表名大小写敏感需 .upper() 归一" 是同一坑的列名版。
    """

    def test_小写列名对大写PK也能过硬门(self):
        idx = {
            "src": _make_index_info(pk_cols=["LIB_LOAN_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["OTHER_ID"], row_count=100),
        }
        result = probe_relation(
            "src", "tgt", "lib_loan_id", "lib_loan_id",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table])
        layer1 = [c for c in result.checks if c["name"] == "pk_or_unique"][0]
        assert layer1["ok"], f"小写列名被硬门误拦: {layer1['detail']}"

    def test_大写列名对小写PK也能过硬门(self):
        """反向: 库返回小写 (lower_case 配置) 而本体是大写。"""
        idx = {
            "src": _make_index_info(pk_cols=["lib_loan_id"], row_count=100),
            "tgt": _make_index_info(pk_cols=["other_id"], row_count=100),
        }
        result = probe_relation(
            "src", "tgt", "LIB_LOAN_ID", "LIB_LOAN_ID",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table])
        layer1 = [c for c in result.checks if c["name"] == "pk_or_unique"][0]
        assert layer1["ok"], f"大写列名被硬门误拦: {layer1['detail']}"

    def test_真不是PK的列仍被硬门拦下(self):
        """归一不能放水成'什么都过': 两端都不是 PK/UNIQUE 仍要 fail。"""
        idx = {
            "src": _make_index_info(pk_cols=["SRC_PK"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_PK"], row_count=100),
        }
        result = probe_relation(
            "src", "tgt", "random_col", "random_col",
            run_sql=_make_run_sql(),
            get_index_info=lambda table, source: idx[table])
        assert result.verdict == "fail"
        layer1 = [c for c in result.checks if c["name"] == "pk_or_unique"][0]
        assert not layer1["ok"]


class TestProbeQueryErrorIsUnknown:
    """探针 SQL 出错应判 unknown, 不是 fail (memory 已知坑 #1)。

    原 _single_count 的 `except Exception: return 0` 把列不存在/表不存在/
    权限错误/数据源错误一律当"包含率=0"→ fail, 会误拒真外键; 更糟的是它
    静默吞掉了 2026-09-09 那个数据源传参 bug, 让探针恒 fail 好几天没人发现。

    正确语义: 查不了 ≠ 查了发现不成立。查询失败 = 证据缺失 = unknown。
    """

    def _idx(self):
        return {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["TGT_ID"], row_count=100),
        }

    def test_列不存在时判unknown而非fail(self):
        idx = self._idx()

        def run_sql_boom(sql, source):
            raise Exception("(1054, \"Unknown column 'GHOST' in 'field list'\")")

        result = probe_relation("src", "tgt", "SRC_ID", "SRC_ID",
                                run_sql=run_sql_boom,
                                get_index_info=lambda t, s: idx[t])
        assert result.verdict == "unknown", (
            f"SQL 报错被误判成 {result.verdict}, 会误拒真外键")

    def test_数据源不存在时判unknown而非fail(self):
        """正是 2026-09-09 那个 bug 的形态: 连接池查不到 → ValueError。"""
        idx = self._idx()

        def run_sql_no_source(sql, source):
            raise ValueError(f"data source '{source}' not found")

        result = probe_relation("src", "tgt", "SRC_ID", "SRC_ID",
                                run_sql=run_sql_no_source,
                                get_index_info=lambda t, s: idx[t])
        assert result.verdict == "unknown"

    def test_查询正常时不受影响仍判fail(self):
        """回归: 真查到了、真不包含 → 仍是 fail (不能把 fail 全变 unknown)。"""
        idx = self._idx()
        result = probe_relation("src", "tgt", "SRC_ID", "SRC_ID",
                                run_sql=_make_run_sql(inclusion_rate=0.0),
                                get_index_info=lambda t, s: idx[t])
        assert result.verdict == "fail"

    def test_查询正常且包含率高时仍判pass(self):
        idx = self._idx()
        result = probe_relation("src", "tgt", "SRC_ID", "SRC_ID",
                                run_sql=_make_run_sql(inclusion_rate=1.0),
                                get_index_info=lambda t, s: idx[t])
        assert result.verdict == "pass"


class TestProbeIdentWhitelist:
    r"""第零层: 标识符白名单 (2026-10-16 审计发现的注入路径)。

    ★ 端到端的事实: 列名是外部输入。agent 写 `ON a.X = b.\`PK_ID FROM evil
    WHERE 1=1 OR 1=1 -- \`` 时, extract.py 保留反引号内原文 → check.py 的
    no_edge hint 原样带走 → failure_signal 落库 → _candidate_pairs 的 hint
    项 (按设计不预筛, 它是 agent 的行为证据) → probe 的 f-string。
    实测能让探针发出子查询被改写的 SQL。表名那侧有 _safe_table_ident, 列名
    这侧原先什么都没有。

    判 unknown 而非 fail: 见 _invalid_idents docstring (fail 会让这列对进墓碑,
    永久挡住同名的真列对)。
    """

    def _idx(self):
        return {
            "src": _make_index_info(pk_cols=["SRC_ID"], row_count=100),
            "tgt": _make_index_info(pk_cols=["SRC_ID"], row_count=500),
        }

    def _boom_run_sql(self):
        """一旦被调用就炸 —— 证明白名单在拼 SQL **之前**就拦住了。"""
        def run_sql(sql, source):
            raise AssertionError(f"白名单没拦住, SQL 已发出: {sql}")
        return run_sql

    @pytest.mark.parametrize("payload", [
        "PK_ID FROM evil WHERE 1=1 OR 1=1 -- ",   # 真实取证载荷
        "X` = 1 OR 1=1 -- ",                       # 反引号闭合
        "COL; DROP TABLE t",                       # 分号多语句
        "COL)",                                    # 括号配平
        "COL WITH SPACE",
        "",                                        # 空串
        "1COL",                                    # 数字开头
        "COL-NAME",
        "列名",                                     # 非 ASCII
    ])
    def test_非法列名判unknown且不发SQL(self, payload):
        idx = self._idx()
        result = probe_relation("src", "tgt", "SRC_ID", payload,
                                run_sql=self._boom_run_sql(),
                                get_index_info=lambda t, s: idx[t])
        assert result.verdict == "unknown"
        assert result.evidence["invalid_idents"] == [payload]
        c = [c for c in result.checks if c["name"] == "ident_whitelist"]
        assert c and c[0]["ok"] is False

    def test_source_col侧同样拦(self):
        idx = self._idx()
        result = probe_relation("src", "tgt", "X FROM evil -- ", "SRC_ID",
                                run_sql=self._boom_run_sql(),
                                get_index_info=lambda t, s: idx[t])
        assert result.verdict == "unknown"

    def test_表名两侧同样拦(self):
        """表名这侧 web.app 已有闸, 但探针不该依赖"上游一定过了那道闸"。"""
        idx = self._idx()
        for src, tgt in (("src WHERE 1=1 -- ", "tgt"), ("src", "tgt; DROP t")):
            result = probe_relation(src, tgt, "SRC_ID", "SRC_ID",
                                    run_sql=self._boom_run_sql(),
                                    get_index_info=lambda t, s: self._idx()["src"])
            assert result.verdict == "unknown"

    def test_多个非法标识符全部报出(self):
        idx = self._idx()
        result = probe_relation("src", "tgt", "BAD COL", "ALSO BAD",
                                run_sql=self._boom_run_sql(),
                                get_index_info=lambda t, s: idx[t])
        assert result.evidence["invalid_idents"] == ["BAD COL", "ALSO BAD"]

    def test_反证_合法标识符不受影响(self):
        """★ 反证: 闸不许把正常列名也拦了, 否则探针恒 unknown = 飞轮停转。"""
        idx = self._idx()
        for col in ("SRC_ID", "_PRIVATE", "col_2", "A1", "a"):
            idx2 = {"src": _make_index_info(pk_cols=[col], row_count=100),
                    "tgt": _make_index_info(pk_cols=[col], row_count=500)}
            result = probe_relation("src", "tgt", col, col,
                                    run_sql=_make_run_sql(inclusion_rate=1.0),
                                    get_index_info=lambda t, s: idx2[t])
            assert result.verdict == "pass", f"合法列名 {col!r} 被误拦"

    def test_反证_白名单与web层同口径(self):
        """两处分叉会让"表名过了 web 闸却过不了探针闸"这类静默不一致出现。"""
        from web.app import _safe_table_ident
        from capabilities.ontology.selfevolve.probe import _invalid_idents

        for ident in ("GOOD_NAME", "_x", "a1", "bad name", "x;y", "1x", ""):
            try:
                _safe_table_ident(ident)
                web_ok = True
            except ValueError:
                web_ok = False
            probe_ok = not _invalid_idents(ident)
            assert web_ok == probe_ok, f"{ident!r}: web={web_ok} probe={probe_ok}"
