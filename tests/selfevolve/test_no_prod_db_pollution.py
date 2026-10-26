"""tests/selfevolve/test_no_prod_db_pollution.py — 测试不得写权威库 (回归防护).

发现经过 (2026-09-17): 排查"飞轮真实信号"时对日志, 发现库里 3 条
obqc_circuit_break 的 SQL 是 `SELECT * FROM t1 JOIN t2 ON t1.x=t2.y` —— 测试
脚本的 mock SQL, 不是真实失败。当场复现: 跑一次 `uv run pytest -q`,
capabilities/ontology/ontology.db 的 failure_signal 就多 2 行。

根因: signals._default_db_path 是**模块级全局**。某个测试触发 core.runtime.bootstrap
的 _init_selfevolve 把它设成真库路径后不复位, 后续测试文件里 mock SQL 撞熔断
时零参数调 log_failure_signal, 就直写生产库。

危害不止是脏数据: 飞轮的燃料池里真假信号混着, 冷路径扫出来的提议无从判断
可信度 —— 这正是"旧提议全是手工塞的"同类问题的第二种成因。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_PROD_DB = PROJECT_ROOT / "capabilities" / "ontology" / "ontology.db"


def _signal_count() -> int | None:
    """权威库当前 failure_signal 行数; 库不存在返回 None (CI 无真库时跳过)。"""
    if not _PROD_DB.exists():
        return None
    conn = sqlite3.connect(str(_PROD_DB))
    try:
        return conn.execute("SELECT COUNT(*) FROM failure_signal").fetchone()[0]
    finally:
        conn.close()


class TestNoProdDbPollution:

    def test_默认db路径不得指向权威库(self):
        """★ 根因防护: 测试进程里 _default_db_path 绝不能是真库。

        conftest 的 autouse fixture 负责把它钉死在 None/tmp; 这条测试保证
        那层防护真的生效 (防护自己被删掉时这里见红)。
        """
        from capabilities.ontology.selfevolve import signals

        current = signals._default_db_path
        if current is None:
            return
        assert Path(current).resolve() != _PROD_DB.resolve(), (
            f"_default_db_path 指向权威库 {current} — 测试的 mock 失败信号会"
            f"直写生产库污染飞轮燃料池")

    def test_零参数log_failure_signal不写权威库(self):
        """★ 反证: 即便某测试漏设 db_path, 也不能落到真库。"""
        from capabilities.ontology.selfevolve.signals import log_failure_signal

        before = _signal_count()
        if before is None:
            pytest.skip("权威库不存在 (CI 环境), 跳过")

        # 零参数调用 = 依赖 _default_db_path (正是污染路径)
        log_failure_signal("obqc_circuit_break", {
            "sql_fingerprint": "pollution_probe",
            "violations": [{"type": "no_edge"}],
            "no_edge_signals": [{"entities": [], "cols": []}],
        })

        assert _signal_count() == before, (
            "零参数 log_failure_signal 写进了权威库 — 测试污染生产数据")

    def test_权威库行数在整个测试会话期间不变(self, prod_db_signal_baseline):
        """★ 全局守卫: 断言时机必须在**会话末尾**, 不是本文件跑到时。

        踩坑记录: 第一版只在本文件内断言, 全量跑绿灯 —— 因为本文件按字母序
        早于真正的污染源 (tests/test_tools_sql_exec.py) 执行, 查的时候还没脏。
        污染是会话级的, 守卫也必须是会话级的 (conftest 的 session finalizer)。

        这条测试自身只验 baseline 抓到了; 真正的断言在 conftest 会话结束时。
        """
        if prod_db_signal_baseline is None:
            pytest.skip("权威库不存在 (CI 环境), 跳过")
        assert isinstance(prod_db_signal_baseline, int)
