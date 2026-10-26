"""
Conversation: 把对话历史封装成对象,而不是裸 list[dict] 满项目飞。

为啥要封装?
- 现在很简单 (就是塞 user/assistant 两种消息),裸 list 也能用
- 但后面阶段会越来越复杂:
  * tool_use (assistant 消息可能是 list 不是 str)
  * 长期/短期记忆注入 (要决定从哪里召回、插哪里)
  * 多会话 (会有多个对话实例并存)
  * RAG 答案要不要标记来源
- 这些扩展点全都得动"消息怎么存、怎么转给 SDK"这个核心
- 提前把它收口到一个类里,以后扩展只动这一处,main.py 永远只调方法

设计原则: 这一层只管"对话数据",不管 LLM 调用、不管 token 计数 —— 那些是上层的事。
"""

from core.observability import log_event


class Conversation:
    """单个会话的消息历史。Anthropic SDK 直接吃 to_messages() 的返回值。"""

    def __init__(self) -> None:
        # 内部就是裸 list[dict],跟之前 main.py 里用的格式完全一致。
        # 这里"OO 封装"不是因为想搞复杂,是因为接下来加方法很多——
        # 类的好处就是这些方法都自然挂在数据旁边。
        self._messages: list[dict] = []

    # ---------- 写 ----------

    def add_user(self, content: str) -> None:
        """加一条用户消息。"""
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        """加一条助手消息。"""
        self._messages.append({"role": "assistant", "content": content})

    def pop_last(self) -> None:
        """
        撤回最后一条消息。
        典型场景: LLM 调用炸了,刚塞进去的 user 消息要回滚,免得下一轮发送时
        SDK 看见连续两条 user 报错。空对话调用是 no-op。
        """
        if self._messages:
            self._messages.pop()

    def clear(self) -> None:
        """清空对话历史。/clear 命令、/system 换人设时会用。"""
        self._messages.clear()

    # ---------- 读 ----------

    def _truncate_by_rounds(
        self, msgs: list[dict], max_rounds: int
    ) -> tuple[list[dict], str | None]:
        """轮数砍头: 一轮 = 2 条 (user + assistant). 真砍了返回 reason='rounds'."""
        keep = max_rounds * 2
        if len(msgs) > keep:
            return msgs[-keep:], "rounds"
        return msgs, None

    def _truncate_by_chars(
        self, msgs: list[dict], max_chars: int
    ) -> tuple[list[dict], str | None]:
        """
        累计字符砍头.
        - 单条超额: 保留 (不切碎) + 落 oversize 埋点
        - 累计超额: 从头一条一条砍, 直到 ≤ max_chars 或只剩 1 条
        真砍了返回 reason='chars'.
        """
        # 单条超额先落 oversize 但保留
        for idx, m in enumerate(msgs):
            if len(m["content"]) > max_chars:
                log_event("history.oversize", {
                    "msg_idx": idx,
                    "chars": len(m["content"]),
                    "max_chars": max_chars,
                })
        # 累计字符砍头
        total = sum(len(m["content"]) for m in msgs)
        reason = None
        while total > max_chars and len(msgs) > 1:
            dropped = msgs.pop(0)
            total -= len(dropped["content"])
            reason = "chars"
        return msgs, reason

    def to_messages(
        self,
        *,
        max_rounds: int | None = None,
        max_chars: int | None = None,
    ) -> list[dict]:
        """
        返回 SDK 需要的格式: [{"role": ..., "content": ...}, ...]

        **返回值语义** (调用方注意):
        - **无参** (`to_messages()`) = 老行为, 返回**内部 list 引用** (零回归).
          调用方如果改返回值, 会反向污染 conversation. 老 main.py 调用方依赖这个引用语义.
        - **带参** = 返回**新 list 拷贝** (内部状态不动), 调用方修改不影响 conversation.

        给值时:
        - max_rounds: 按"轮数"(user+assistant 一对算一轮) 保留最后 N 轮, 多余从头丢
        - max_chars: 在 max_rounds 处理后, 按累计字符上限再砍头
        - 单条字符 > max_chars: 保留这一条 (不切碎), 落 history.oversize 埋点
        - 真正触发截断时落 history.truncated 埋点
        """
        # 老行为: 无参调用零回归 (返回内部引用, 不拷贝)
        if max_rounds is None and max_chars is None:
            return self._messages

        msgs = list(self._messages)  # 拷贝, 不动内部
        n_before = len(msgs)
        reason = None

        if max_rounds is not None:
            msgs, reason = self._truncate_by_rounds(msgs, max_rounds)

        if max_chars is not None:
            msgs, chars_reason = self._truncate_by_chars(msgs, max_chars)
            if chars_reason is not None:
                reason = chars_reason  # chars 是后一刀, 优先于 rounds

        # 真正截断了才落 truncated
        if reason is not None and len(msgs) < n_before:
            log_event("history.truncated", {
                "kept_msgs": len(msgs),
                "dropped_msgs": n_before - len(msgs),
                "reason": reason,
            })

        return msgs

    def snapshot(self) -> list[dict]:
        """
        返回一份浅拷贝,供 /history 这种"只读遍历"用。
        拷贝是为了防止调用方拿到引用后乱改影响内部状态。
        """
        return list(self._messages)

    def to_dict(self) -> list[dict]:
        """导出内部 messages (浅拷贝), 给 SessionStore 落盘用。"""
        return list(self._messages)

    def load_from(self, messages: list[dict]) -> None:
        """从磁盘 messages 恢复内部状态 (整体替换, 非追加)。"""
        self._messages = list(messages)

    def __len__(self) -> int:
        return len(self._messages)
