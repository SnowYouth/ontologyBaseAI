"""
conftest.py: pytest 的"自动加载"文件。

放这里的东西 pytest 会在跑测试前先执行,适合做全局配置。

两件事:
1. 把项目根目录加到 sys.path,让 tests/ 里能 `from core.xxx import ...`。
2. 守住权威库 —— 测试绝不能往 capabilities/ontology/ontology.db 写数据 (见下)。
"""

import os
import sqlite3
import sys

import pytest

# tests/ 的父目录就是项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_PROD_DB = os.path.join(PROJECT_ROOT, "capabilities", "ontology", "ontology.db")


def _prod_signal_count():
    """权威库 failure_signal 行数; 库不存在/表不存在返回 None。"""
    if not os.path.exists(_PROD_DB):
        return None
    conn = sqlite3.connect(_PROD_DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM failure_signal").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_prod_db_default_path():
    """★ 防护: 每个测试前把 signals._default_db_path 钉死在 None。

    背景 (2026-09-17): _default_db_path 是模块级全局。某个测试触发
    core.runtime.bootstrap._init_selfevolve 把它设成真库路径后不复位, 后续测试文件里
    mock SQL 撞熔断时零参数调 log_failure_signal, 就直写生产库 —— 实测跑一次
    全量 pytest, 权威库 failure_signal 多 2 行。

    钉成 None 而不是 tmp: log_failure_signal 无路径时 warning + 跳过, 正是我们
    要的行为 (测试不该依赖这个默认值; 要落库的测试显式传 db_path)。
    收尾同样复位, 防止测试中途设的真库路径泄漏给后续测试。
    """
    from capabilities.ontology.selfevolve import signals

    saved = signals._default_db_path
    signals._default_db_path = None
    yield
    signals._default_db_path = None if saved == _PROD_DB else saved


@pytest.fixture(autouse=True)
def _isolate_model_config(tmp_path_factory):
    """★ 防护: 每个测试把 model_config 的配置目录钉到独立 tmp 目录。

    背景: config/model.json 是机主本地文件 (带 api_key, 不进 git), 而
    core/llm.py 的 complete() 现读它取 model/api_key/base_url。不隔离的话,
    机主一旦在前端存过模型, test_llm / test_agent 里"PRIMARY_MODEL 应该生效"
    的断言就会被真实配置顶掉 —— 别人 clone 下来绿、机主本地红 (或反过来)。

    同理于 _no_prod_db_default_path: 测试不该读写机主的真实配置。

    ★ 手动存取而不用 monkeypatch: 在 conftest 的 autouse fixture 里请求
    monkeypatch 会把它的实例化提前到 conftest 层, teardown 因而排到
    test_config.py 那个 `reload(core.config)` 之后 —— reload 时 env 还带着
    测试设的非法值 (如 RAG_TOP_K=-1), 一批 teardown 当场 ValidationError。
    """
    from core import model_config

    saved = model_config._CONFIG_DIR
    model_config._CONFIG_DIR = tmp_path_factory.mktemp("model_config")
    yield
    model_config._CONFIG_DIR = saved


@pytest.fixture(scope="session")
def prod_db_signal_baseline():
    """会话开始时权威库的 failure_signal 行数 (供污染守卫测试引用)。"""
    return _prod_signal_count()


_MODEL_CFG = os.path.join(PROJECT_ROOT, "config", "model.json")
_BASELINES = os.path.join(PROJECT_ROOT, "kg_private", "cq_baselines")


def _baseline_file_count():
    """真 kg_private/cq_baselines/ 的文件数; 目录不存在返回 None。"""
    if not os.path.isdir(_BASELINES):
        return None
    return len(os.listdir(_BASELINES))


def pytest_sessionstart(session):
    session.config._prod_signal_baseline = _prod_signal_count()
    session.config._baseline_count = _baseline_file_count()
    session.config._model_cfg_existed = os.path.exists(_MODEL_CFG)
    session.config._model_cfg_mtime = (
        os.path.getmtime(_MODEL_CFG) if session.config._model_cfg_existed else None)


def pytest_sessionfinish(session, exitstatus):
    """★ 会话级污染守卫: 整个会话跑完, 权威库行数必须原样, 否则整轮判失败。

    为什么必须是会话级: 第一版守卫写成普通测试, 全量跑绿灯 —— 因为它按字母序
    早于真正的污染源执行, 查的时候还没脏。污染是会话级现象, 守卫也必须是
    会话级 (同理于"断言时机"这一类坑)。

    退出码用 session.testsfailed 而不是改 session.exitstatus: 后者在
    sessionfinish 阶段赋值已经太晚, pytest 早就按 testsfailed 算好了退出码
    (实测改 exitstatus 无效, 守卫检测到污染但 EXIT=0)。
    """
    before = getattr(session.config, "_prod_signal_baseline", None)
    if before is None:
        return
    after = _prod_signal_count()
    if after is not None and after != before:
        session.testsfailed += 1
        session.exitstatus = 1
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        msg = (f"[污染守卫] 权威库 failure_signal 被测试写入: "
               f"{before} -> {after} 行 ({_PROD_DB}); "
               f"根因通常是 signals._default_db_path 被设成真库后没复位。")
        if reporter is not None:
            reporter.write_line("")
            reporter.write_line(msg, red=True, bold=True)
        else:
            print("\n" + msg)

    _check_model_config_untouched(session)
    _check_baselines_untouched(session)


def _check_baselines_untouched(session) -> None:
    """★ 会话级守卫: 真 kg_private/cq_baselines/ 不许被测试写入。

    背景 (2026-10-16 审计): /apply 端点落库前调 snapshot_baseline(), 它写
    apply._BASELINES_DIR —— 默认就是真目录。test_web_selfevolve.py 的 client
    fixture 当时只 patch 了本体库路径, 于是每跑一次漏几个文件, 攒到几百个文件、
    几十 MB 才被人翻出来。
    上面两道守卫 (failure_signal 行数 / model.json) 都没覆盖这个目录 ——
    污染守卫只能逮住它显式盯着的东西, 所以每新增一处"测试可能写到的真实
    路径", 这里就要同步加一条。
    """
    before = getattr(session.config, "_baseline_count", None)
    if before is None:
        return
    after = _baseline_file_count()
    if after is None or after == before:
        return
    session.testsfailed += 1
    session.exitstatus = 1
    msg = (f"[污染守卫] 真 cq_baselines 被测试写入: {before} -> {after} 个文件 "
           f"({_BASELINES}); 根因通常是某个 fixture 漏了 "
           f"monkeypatch.setattr(apply_mod, '_BASELINES_DIR', tmp_path/...) "
           f"—— 对照 tests/selfevolve/test_gate_counterproof.py 的 env fixture。")
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(msg, red=True, bold=True)
    else:
        print("\n" + msg)


def _check_model_config_untouched(session) -> None:
    """★ 会话级守卫: 真实的 config/model.json 不许被测试建立或改动。

    背景 (2026-10-15, 当场栽过): _isolate_model_config 把目录钉到 tmp, 但
    `importlib.reload(core.model_config)` 会重新执行模块体, 把 _CONFIG_DIR
    复位到真实 config/ —— test_llm 的 _reload 辅助正是这么干的。结果测试把
    自己造的候选 (model-a/model-b) 写进了机主真实配置, 还把 .env 的真 key
    固化进去, 真跑 main.py 才发现模型变成了 "anthropic/model-a"。

    单测的边界就是它 mock 的那一层: 隔离 fixture 管不到 reload 之后的模块体,
    只有会话级的物理后果检查能逮住。
    """
    existed = getattr(session.config, "_model_cfg_existed", None)
    if existed is None:
        return
    now_exists = os.path.exists(_MODEL_CFG)
    changed = (
        (not existed and now_exists) or
        (existed and not now_exists) or
        (existed and now_exists and
         os.path.getmtime(_MODEL_CFG) != session.config._model_cfg_mtime)
    )
    if not changed:
        return
    session.testsfailed += 1
    session.exitstatus = 1
    msg = (f"[污染守卫] 真实模型配置被测试改动: {_MODEL_CFG} "
           f"(会话前存在={existed}, 现在存在={now_exists}); "
           f"根因通常是 reload(core.model_config) 把 _CONFIG_DIR 复位回真实目录, "
           f"reload 之后必须重钉 tmp 路径。")
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(msg, red=True, bold=True)
    else:
        print("\n" + msg)
