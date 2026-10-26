"""阶段 2 Step 0 Task 4.5 — tool calling 双 provider smoke.

跑法:
  uv run python tests/manual/smoke_litellm_tool_calling.py

干啥:
- 定义一个假工具 get_weather(city), 不真调外部 API, 第二轮 mock 返回值
- 两路 provider 各跑一次完整 tool calling 双轮:
    Round 1: 给 LLM 工具定义 + 用户问 "深圳今天天气怎么样?" → 期望 LLM 回 tool_calls
    Round 2: 把 mock 结果回填到 messages (role=tool), 让 LLM 续写自然语言回答
- 重点观察:
    a. 工具 schema 是不是两路同一份 (OpenAI 风格 type/function/parameters)
    b. Round 1 的 finish_reason 是不是 'tool_calls' (非 'stop')
    c. tool_calls 的 id/function.name/function.arguments 字段名两路是否一致
    d. Round 2 回填的 messages 格式两路是否同
    e. Round 2 模型是否真的"用了"工具结果 (回答里有"-2℃"之类)

为啥这么写:
- chat completion 通了不代表 tool calling 通; Step 1 brainstorming 要拿这份观察
  决定 LiteLLM 抽象在 tool 这一层够不够用 (如果两路 schema 不同, LiteLLM 选型理由就削弱了)
- agent loop 的核心循环是 "messages → completion → 看 tool_calls → 执行 → 回填 → completion 续写",
  这个 smoke 就是这套循环的最小骨架, 跑通 = 阶段 2 agent 范式可行

配置依赖:
- `config/model.json` (`core/model_config.py` 读写, 前端"设置 → 模型"页可改);
  清单里每条候选各跑一遍双轮。★ 2026-10-15 LLM 层重构后模型三要素的真相源在
  那里, 不在 env; `.env` 的 `PRIMARY_*` 只是缺文件时的冷启动种子。
- ★ tool calling 的 provider 差异没有自动化测试覆盖, 只有这个脚本能验 ——
  换 provider 前真跑一遍 (多配一条候选即可)。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import litellm  # noqa: E402


# ---- 工具定义 (OpenAI 风格, LiteLLM 默认接受这个; anthropic 协议它会自动转译) ----
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气. 返回温度和天气描述.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名, 例如 '深圳' '北京'",
                    },
                },
                "required": ["city"],
            },
        },
    },
]

USER_QUERY = "深圳今天天气怎么样?"


def mock_get_weather(city: str) -> dict[str, Any]:
    """假工具实现, 永远返回固定值, 但带上 city 让模型知道是真的用了参数."""
    return {
        "city": city,
        "temperature_c": -2,  # 故意用反季节温度, 这样模型回答里有 "-2℃" 就证明它用了工具结果
        "condition": "晴, 微风",
    }


def _preview(text: str, n: int = 100) -> str:
    text = (text or "").replace("\n", "⏎").replace("\r", "")
    return text[:n] + ("..." if len(text) > n else "")


def _print_header(title: str) -> None:
    bar = "=" * 60
    print(f"\n{bar}\n  {title}\n{bar}")


def run_one(label: str, **opts: Any) -> None:
    """对一个 provider 跑完整 tool calling 双轮."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": USER_QUERY}]

    # ---- Round 1: 给工具, 看 LLM 是否回 tool_calls ----
    print(f"\n[{label}] Round 1 (LLM 决定是否调用工具) ...")
    t0 = time.perf_counter()
    try:
        resp1 = litellm.completion(messages=messages, tools=TOOLS, **opts)
    except Exception as e:
        print(f"  R1 FAIL | {type(e).__name__}: {e}")
        return
    dt1 = time.perf_counter() - t0

    choice1 = resp1.choices[0]
    finish1 = getattr(choice1, "finish_reason", "?")
    msg1 = choice1.message
    tool_calls = getattr(msg1, "tool_calls", None) or []
    print(f"  ok  | {dt1:.2f}s | finish_reason={finish1} | tool_calls={len(tool_calls)}")

    if not tool_calls:
        # LLM 没调工具, 直接回答了 (说明工具识别失败 / 模型选择不调用)
        print(f"  ⚠️ LLM 没调用工具, 直接回: {_preview(msg1.content or '')}")
        return

    # 打 tool_call 结构 (这是关键差异点)
    for i, tc in enumerate(tool_calls):
        tc_id = getattr(tc, "id", "?")
        fn = getattr(tc, "function", None)
        fn_name = getattr(fn, "name", "?") if fn else "?"
        fn_args = getattr(fn, "arguments", "?") if fn else "?"
        print(f"  tool_call[{i}]: id={tc_id} name={fn_name} args={fn_args}")

    # ---- 执行假工具 ----
    tc = tool_calls[0]
    args = json.loads(tc.function.arguments)
    tool_result = mock_get_weather(args["city"])
    print(f"  → mock_get_weather({args['city']!r}) = {tool_result}")

    # ---- Round 2: 把工具结果回填, LLM 续写自然语言 ----
    # 必须把 R1 的 assistant 消息原样塞回, 再加 role=tool 的结果
    messages.append({
        "role": "assistant",
        "content": msg1.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
        ],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": tc.id,
        "content": json.dumps(tool_result, ensure_ascii=False),
    })

    print(f"\n[{label}] Round 2 (工具结果回填, LLM 续写) ...")
    t0 = time.perf_counter()
    try:
        resp2 = litellm.completion(messages=messages, tools=TOOLS, **opts)
    except Exception as e:
        print(f"  R2 FAIL | {type(e).__name__}: {e}")
        return
    dt2 = time.perf_counter() - t0

    choice2 = resp2.choices[0]
    finish2 = getattr(choice2, "finish_reason", "?")
    final_text = choice2.message.content or ""
    print(f"  ok  | {dt2:.2f}s | finish_reason={finish2}")
    print(f"  resp: {_preview(final_text)}")

    # 检查模型是否真的"用了"工具结果 (温度 -2 是反季节, 模型回答里有就是真用了)
    used_temp = "-2" in final_text or "零下" in final_text or "晴" in final_text
    print(f"  ✓ 工具结果被使用: {used_temp}")


def main() -> int:
    # core 的 import 放函数内: sys.path 在模块顶部才装好, 顶层 import 会抢跑
    from core import model_config
    from core.config import is_placeholder_api_key

    data = model_config.load()
    candidates = data["models"]
    print(f"候选模型 {len(candidates)} 条 (active={data.get('active', '?')})")

    for i, cfg in enumerate(candidates, 1):
        name = cfg.get("name", f"#{i}")
        model = cfg.get("model", "")
        base = (cfg.get("base_url") or "").strip()
        _print_header(f"Path {i}: {name} → {model}" + (f" (中转 {base})" if base else ""))

        if is_placeholder_api_key(cfg.get("api_key", "")):
            print(f"  SKIP: {name} 的 api_key 还是占位值, 去前端模型页填真 key")
            continue

        opts: dict[str, Any] = {"model": model, "api_key": cfg["api_key"]}
        if base:
            opts["api_base"] = base      # litellm 认 api_base, 不认我们的 base_url 命名
        run_one(name, **opts)

    _print_header("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
