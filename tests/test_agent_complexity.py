"""阶段 2 硬指标: 4 个 agent 函数 CC 实测, 全部满足全局规则 ≤ 11.

测法: 用 radon (已是项目 dev 依赖) 实测. 沿用阶段 1.5 R2 M1 教训:
plan 估算可能偏小, 必须实测验证 (M1 to_messages 估 9 实测 13).

run_agent 实测 CC=11, 持平全局硬规则上限 (≤ 11). 三个 helper 实测均在
Task 内部更严预算内 (_llm_step ≤ 9 / _execute_tool_call ≤ 6 / _exit_reason ≤ 3).
_llm_step 预算 7→9: 三模式合一 Step 2 任务二改真流式 (收流循环 + 流式异常分支),
CC 实测上浮但仍在全局 ≤ 11 内, 预算相应松绑。
"""
import subprocess


def _radon_cc(funcname: str) -> int:
    """跑 radon cc 给定函数, 返回它的复杂度数字."""
    result = subprocess.run(
        ["uv", "run", "radon", "cc", "-s", "-n", "A", "core/runtime/agent.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    # 输出格式: "    F 12:0 _llm_step - A (5)" — 取行尾 (X)
    # 精确匹配: 按 " - " 分隔后取函数名比对, 不用前缀/子串匹配,
    # 防止同前缀函数 (如未来再加 run_agent_xxx) 互相误抓 CC。
    for line in result.stdout.splitlines():
        if "(" not in line or " - " not in line:
            continue
        name_part = line.rsplit(" - ", 1)[0].split()[-1]
        if name_part == funcname:
            try:
                return int(line.rsplit("(", 1)[-1].rstrip(")"))
            except ValueError:
                continue
    raise AssertionError(f"radon 输出没找到 {funcname}: {result.stdout}")


class TestAgentComplexity:
    def test_run_agent_CC_within_11(self):
        cc = _radon_cc("run_agent")
        assert cc <= 11, f"run_agent CC={cc} 超过全局规则上限 11"

    def test_llm_step_CC_within_9(self):
        cc = _radon_cc("_llm_step")
        assert cc <= 9, f"_llm_step CC={cc} 超过预算 9"

    def test_execute_tool_call_CC_within_6(self):
        cc = _radon_cc("_execute_tool_call")
        assert cc <= 6, f"_execute_tool_call CC={cc} 超过预算 6"

    def test_exit_reason_CC_within_3(self):
        # 2026-10-20 改名: _should_exit (返回 bool) → _exit_reason (返回退出原因),
        # 为把 truncated 与 stop 分开。预算不变。
        cc = _radon_cc("_exit_reason")
        assert cc <= 3, f"_exit_reason CC={cc} 超过预算 3"
