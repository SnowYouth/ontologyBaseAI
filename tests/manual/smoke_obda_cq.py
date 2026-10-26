"""阶段 6 OBDA — CQ-3~8/10/12~15 共 11 条真库真跑 (CQ-1/2/9 已在 Task 9 验证过, 不重复).

需 .env 配好数据源 lib (DB_SOURCES 含 lib + DB_LIB_* 六项) + 一条可用模型
(前端"设置 → 模型"页配, 或直接编辑 `config/model.json`; 模型三要素的真相源
在那里, 不在 .env —— `.env` 的 `PRIMARY_*` 只是冷启动种子).
跑法 (在项目根):
    uv run python tests/manual/smoke_obda_cq.py

每条 CQ 真跑一次 /agent 等价调用 (真调 LLM + 真查库), 只读 SELECT, 无写入风险。
CQ-14 是 3 轮同一 conversation (验证对话记忆接住 OBDA 多轮引用)。
CQ-15 预期"断腿": 无 session 注入, agent 应体面回问员工 ID, 不算失败。

判定: 每条打印 agent 回答前 400 字, 人工过目是否符合 cq_draft.md 里的验收标准
(点名对不对 / 走边对不对 / 值集翻译对不对 / 断腿处理体面不体面)。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

from agent.tools import db_adapters

from core.runtime.agent import run_agent
from core.config import settings
from core.conversation import Conversation
from core.ontology_view import build_catalog_text, get_ontology
import agent.tools as tool_pkg


def _setup_obda() -> None:
    """跟 main.py 启动期一样: 注入本体 + 只读库连接 + 拼三层 system prompt."""
    tool_pkg.set_ontology(settings.obda_ontology_path)
    onto = get_ontology()
    sensitive_cols = {
        attr["name"]
        for entity in onto.entities.values()
        for attr in entity["attributes"]
        if attr.get("sensitive")
    }
    tool_pkg.set_sensitive_cols(sensitive_cols)

    db_conn = db_adapters.connect_source(settings.db_sources["lib"])
    tool_pkg.set_db_conn(db_conn)

    settings.system_prompt = (
        settings.system_prompt
        + "\n\n" + settings.ontology_usage_prompt
        + "\n\n" + build_catalog_text(onto)
    )


# 单轮 CQ (CQ-14 三轮单独处理)
SINGLE_TURN_CQS = [
    ("CQ-3", "号码 9202102000 是什么付费模式?"),
    ("CQ-4", "号码 9202102000 属于哪个产品?"),
    ("CQ-5", "号码 SFV-00000000159 有没有关联的其他号码? 是什么关系类型?"),
    ("CQ-6", "订单 82005060004451 办的是什么业务, 当前什么状态?"),
    ("CQ-7", "号码 9202102000 最近的订单记录是什么?"),
    ("CQ-8", "叫 hui li 的客户下过哪些订单?"),
    ("CQ-10", "客户 ID 128 名下所有号码的状态分布?"),
    ("CQ-12", "客户 ID 128 总共有哪些号码, 分别用了哪些书目包?"),
    ("CQ-13", "叫 hui li 的客户历史上有哪些订单, 共花多少钱, 用哪些账户支付?"),
    ("CQ-15", "当前员工的购物车里有哪些条目, 都办的什么业务?"),
]

CQ14_TURNS = [
    "hui li 有哪些订单?",
    "那些订单花了多少钱?",
    "用哪些账户付的?",
]


def _run_single(label: str, task: str) -> bool:
    print(f"\n{'='*70}\n[{label}] {task}\n{'='*70}")
    try:
        answer = run_agent(task)
        print(f"✅ 答案前 400 字:\n{answer[:400]}")
        return True
    except Exception as e:
        print(f"❌ FAIL: {type(e).__name__}: {e}")
        return False


def _run_cq14() -> bool:
    print(f"\n{'='*70}\n[CQ-14] 三轮对话记忆\n{'='*70}")
    conversation = Conversation()
    ok = True
    for i, turn in enumerate(CQ14_TURNS, 1):
        print(f"\n--- 第 {i} 轮: {turn} ---")
        try:
            answer = run_agent(turn, conversation=conversation)
            print(f"✅ 答案前 400 字:\n{answer[:400]}")
        except Exception as e:
            print(f"❌ FAIL: {type(e).__name__}: {e}")
            ok = False
    return ok


def main():
    _setup_obda()

    results = []
    for label, task in SINGLE_TURN_CQS:
        ok = _run_single(label, task)
        results.append((label, ok))

    ok14 = _run_cq14()
    results.append(("CQ-14", ok14))

    print(f"\n\n{'='*70}\nSmoke 结果总结\n{'='*70}")
    for label, ok in results:
        print(f"  {'✅' if ok else '❌'} {label}")

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\n无异常通过 {n_ok}/{len(results)} (不代表答案语义正确, 需人工过目上面输出)")
    print("\nJSONL 在 logs/run-*.jsonl, 逐条核对 sql.query 事件确认打码 + JOIN 路径")


if __name__ == "__main__":
    main()
