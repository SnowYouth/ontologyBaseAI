"""LiteLLM 薄壳: stderr 噪音抑制 + 同步 completion (单模型)。

设计原则:
1. 不写 LLMClient class — 一个 complete() 函数足够, YAGNI
2. import 顺序硬约定: env var 必须在 import litellm **之前** setdefault,
   否则 cost map 远程拉取已经触发. 测试硬验.
3. 单模型, 无 fallback (2026-10-15 拍板): litellm 1.89.3 的 fallbacks 参数
   跟流式调用有 bug (sync 迭代器拿 async_generator 必崩), 而主挂了静默退化到
   备用反而更难查 (中转抽风时备用摆烂寒暄, 看不出换了模型). 挂就直接报错暴露.
4. 模型三要素每次调用现读 model_config, 不读 settings — settings 是启动期
   单例, 前端改完要重启才生效; 现读才能"下一轮对话即生效".
"""
from __future__ import annotations

import logging
import os

# 关键: 必须在 import litellm 之前设, 否则 cost map 远程拉取已经触发.
# setdefault 允许用户 .env 显式覆盖 (未来想关 cost map 时)
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

from core import model_config  # noqa: E402
from core.config import settings  # noqa: E402


def complete(messages: list[dict], tools: list[dict] | None = None, stream: bool = False):
    """同步 completion (单模型, 无 fallback).

    模型/key/base_url 取自 model_config.active() —— 每次调用现读, 前端换模型
    后下一次调用即生效。max_tokens/timeout 仍走 settings (改这两个值重启无妨)。

    Returns: LiteLLM ModelResponse (resp.choices[0].message + resp.usage + resp.model)
    Raises: litellm.APIError / litellm.Timeout / 其他 (调用方接管)
    """
    cfg = model_config.active()
    kwargs: dict = {
        "model": cfg["model"],
        "api_key": cfg.get("api_key", ""),
        "messages": messages,
        "max_tokens": settings.max_tokens,
        "timeout": settings.request_timeout,
        "stream": stream,
    }
    # base_url 非空 = 走中转, 显式传 api_base (litellm 认 *_API_BASE, 不认我们的
    # *_BASE_URL 命名; 空则走 provider 官方端点)。
    if cfg.get("base_url"):
        kwargs["api_base"] = cfg["base_url"]
    if tools:
        kwargs["tools"] = tools

    return litellm.completion(**kwargs)
