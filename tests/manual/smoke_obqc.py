"""tests/manual/smoke_obqc.py — OBQC 端到端 smoke (真库 lib)。

计划一 ①JOIN + 计划二 ②列归属/③枚举/I1 熔断/I2 上浮 六特性联动, 走真实
init_agent_runtime() (真本体 + 真 lib 连接) + execute_sql 全链路。

跑: uv run python tests/manual/smoke_obqc.py

★ 用例里的表/列/枚举/边全部核对过真库 lib 与真本体 (2026-08-12):
  - 合法边: lib_loan -> lib_loan_acct_rel ON LIB_LOAN_ID = LIB_LOAN_ID (本体真有此边, 两表两列真库都在)
  - REC_STATUS 枚举合法值 = {000, 001, 002} (故 '9999' 触发 ③ soft_warn)
  - lib_loan 无 filter_rule (故 ④ 不在本 smoke 断言范围)
"""
import sys
from pathlib import Path

# Windows GBK 控制台需要切 utf-8 (同 main.py)
sys.stdout.reconfigure(encoding="utf-8")

# manual smoke 不走 pytest 框架, 手动把项目根加进 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from core.runtime.bootstrap import init_agent_runtime
from agent.tools.sql_exec import execute_sql, _reset_circuit_breaker


def main():
    print("=== OBQC Plan 2 Smoke (真库 lib) ===\n")
    init_agent_runtime()
    _reset_circuit_breaker()
    ok = True

    # Smoke 1: ①合法 JOIN (真边 lib_loan↔lib_loan_acct_rel) + ②列真存在 → 不拦, 照常执行
    print("Smoke 1: 合法 JOIN + 列真存在 → 放行执行")
    sql1 = ("SELECT p.LIB_LOAN_ID FROM LIB_LOAN p "
            "JOIN LIB_LOAN_ACCT_REL r ON p.LIB_LOAN_ID = r.LIB_LOAN_ID "
            "LIMIT 1")
    r1 = execute_sql(sql1, entity="lib_loan")
    if r1.get("blocked"):
        ok = False
        print(f"  ✗ 期望放行却被拦: {r1}\n")
    else:
        print(f"  ✓ 放行, n={r1.get('n')} 行 (warnings={r1.get('warnings', '无')})\n")

    # Smoke 2: ②幻觉列 (本体 attributes 无 + 物理表无) → hard_block, 不碰库
    print("Smoke 2: 幻觉列 → ② hard_block")
    sql2 = "SELECT PHANTOM_COLUMN_XYZ FROM LIB_LOAN"
    r2 = execute_sql(sql2, entity="lib_loan")
    if r2.get("blocked") is True and any(
        v["type"] == "column_not_found" for v in r2.get("violations", [])
    ):
        print(f"  ✓ Blocked: {r2['violations'][0]['reason']}\n")
    else:
        ok = False
        print(f"  ✗ 期望 hard_block(column_not_found) 却: {r2}\n")

    # Smoke 3: ③枚举值可疑 (REC_STATUS 合法值 000/001/002, 用 9999) → soft_warn, 照常执行 + I2 上浮
    print("Smoke 3: REC_STATUS='9999' 越枚举 → ③ soft_warn (执行 + I2 上浮 warnings)")
    sql3 = "SELECT LIB_LOAN_ID FROM LIB_LOAN WHERE REC_STATUS = '9999' LIMIT 1"
    r3 = execute_sql(sql3, entity="lib_loan")
    if r3.get("blocked"):
        ok = False
        print(f"  ✗ soft_warn 不该拦: {r3}\n")
    elif "warnings" in r3 and any("枚举" in w or "9999" in w for w in r3["warnings"]):
        print(f"  ✓ Soft warn 上浮: {r3['warnings']}\n")
    else:
        ok = False
        print(f"  ✗ 期望 warnings 含枚举提示却: {r3}\n")

    # Smoke 4: I1 熔断 — 同一被硬拦 SQL 第 2 次 → 仍 hard_block 且加强 hint
    print("Smoke 4: I1 熔断 (同一幻觉列 SQL 第 2 次硬拦加强 hint)")
    sql4 = "SELECT PHANTOM_AGAIN_ZZZ FROM LIB_LOAN"
    execute_sql(sql4, entity="lib_loan")            # 第 1 次
    r4 = execute_sql(sql4, entity="lib_loan")       # 第 2 次
    has_circuit = any(
        h.get("kind") == "circuit_breaker"
        or "第二次" in str(h) or "等价" in str(h) or "重复" in str(h)
        for h in r4.get("hints", [])
    )
    if r4.get("blocked") is True and has_circuit:
        print(f"  ✓ 熔断触发: {r4['hints'][-1]}\n")
    else:
        ok = False
        print(f"  ✗ 期望第 2 次带熔断加强 hint 却: {r4}\n")

    print("=== Smoke:", "全过 ✓" if ok else "有失败 ✗", "===")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
