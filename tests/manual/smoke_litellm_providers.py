"""阶段 2 Step 0 双 provider smoke.

跑法:
  uv run python tests/manual/smoke_litellm_providers.py

干啥:
- 用 LiteLLM 同一套 completion() 接口, 对 `config/model.json` 里**每一条**
  候选模型各调一次 (多 provider 对比就靠多配几条候选)
- 每条同步 + 流式各一次
- 打印: 耗时 / 模型名 / 响应首 100 字 / token 计数 / finish_reason
- 失败也打印, 不抛异常 (smoke 不是 pytest, 只观测能不能通)

为啥这么写:
- Step 1 brainstorming 要拿这份观察决定 LiteLLM 抽象到底够不够用,
  以及各路 provider 行为差异在哪 (流式格式 / token 字段 / 错误码).
- 不接 conversation/agent, 直接打 litellm, 保持纯净 smoke.

配置依赖:
- `config/model.json` (`core/model_config.py` 读写, 前端"设置 → 模型"页可改)。
  ★ 2026-10-15 LLM 层重构后模型三要素的真相源在这里, 不在 env; `.env` 的
  `PRIMARY_*` 只是缺文件时的冷启动种子。
- 只读 model_config 这一个 core 模块 (不碰 agent / conversation) —— 候选清单
  现在只有那里有, 脚本自己重新发明一套 env 约定等于再造一个会过期的真相源。

观察落地:
- 控制台打印 + 不落 JSONL (smoke 一次性, 不需要 observability)
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

# 跳过 litellm 启动拉远程 model cost map (受限网络下 SSL 会卡住, 自动 fallback 但慢)
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# 让脚本能 import 项目模块 (虽然 smoke 暂不用, 防后续扩展)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Windows 控制台 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import litellm  # noqa: E402


# ---- 测试 prompt (短, 省 token) ----
PROMPT = "用一句话解释什么是 RAG。"


def _preview(text: str, n: int = 100) -> str:
    """截首 n 字, 折行符替成 ⏎ 方便单行打印."""
    text = text.replace("\n", "⏎").replace("\r", "")
    return text[:n] + ("..." if len(text) > n else "")


def _print_header(title: str) -> None:
    bar = "=" * 60
    print(f"\n{bar}\n  {title}\n{bar}")


def call_sync(label: str, **kwargs: Any) -> None:
    """同步调用一次, 打耗时 + 首 100 字 + token."""
    print(f"\n[{label}] sync ...")
    t0 = time.perf_counter()
    try:
        resp = litellm.completion(
            messages=[{"role": "user", "content": PROMPT}],
            **kwargs,
        )
        dt = time.perf_counter() - t0
        choice = resp.choices[0]
        msg = choice.message.content or ""
        usage = getattr(resp, "usage", None)
        finish = getattr(choice, "finish_reason", "?")
        print(f"  ok  | {dt:.2f}s | finish={finish}")
        print(f"  resp: {_preview(msg)}")
        if usage:
            print(f"  usage: prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
    except Exception as e:
        dt = time.perf_counter() - t0
        print(f"  FAIL | {dt:.2f}s | {type(e).__name__}: {e}")


def call_stream(label: str, **kwargs: Any) -> None:
    """流式调用一次, 打 chunk 数 + 首 100 字 + 首字延迟."""
    print(f"\n[{label}] stream ...")
    t0 = time.perf_counter()
    first_chunk_at: float | None = None
    chunks = 0
    buf: list[str] = []
    try:
        for chunk in litellm.completion(
            messages=[{"role": "user", "content": PROMPT}],
            stream=True,
            **kwargs,
        ):
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter() - t0
            chunks += 1
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None) or ""
            buf.append(piece)
        dt = time.perf_counter() - t0
        msg = "".join(buf)
        ttft = f"{first_chunk_at:.2f}s" if first_chunk_at is not None else "n/a"
        print(f"  ok  | total={dt:.2f}s ttft={ttft} chunks={chunks}")
        print(f"  resp: {_preview(msg)}")
    except Exception as e:
        dt = time.perf_counter() - t0
        print(f"  FAIL | {dt:.2f}s | {type(e).__name__}: {e}")


def main() -> int:
    print(f"litellm version: from importlib.metadata.version('litellm')")
    from importlib.metadata import version

    print(f"  → {version('litellm')}")

    # core 的 import 放函数内: sys.path 在模块顶部才装好, 顶层 import 会抢跑
    from core import model_config
    from core.config import is_placeholder_api_key

    data = model_config.load()
    candidates = data["models"]
    print(f"\n候选模型 {len(candidates)} 条 (active={data.get('active', '?')})")

    for i, cfg in enumerate(candidates, 1):
        name = cfg.get("name", f"#{i}")
        model = cfg.get("model", "")
        base = (cfg.get("base_url") or "").strip()
        _print_header(f"Path {i}: {name} → {model}" + (f" (中转 {base})" if base else ""))

        if is_placeholder_api_key(cfg.get("api_key", "")):
            print(f"  SKIP: {name} 的 api_key 还是占位值, 去前端模型页填真 key")
            continue

        # 模型名自带 provider 前缀 (litellm 格式 provider/model), 由它决定走哪套协议
        opts: dict[str, Any] = {"model": model, "api_key": cfg["api_key"]}
        if base:
            opts["api_base"] = base      # litellm 认 api_base, 不认我们的 base_url 命名
        call_sync(name, **opts)
        call_stream(name, **opts)

    _print_header("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
