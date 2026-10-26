"""
测 Conversation 类的纯逻辑能力。

这里只测"数据结构"层面的事:加消息、转格式、清空、长度。
不测 LLM 调用 (那是 IO,不在这一层)。

跑法: uv run pytest tests/test_conversation.py -v
"""

import pytest

from core.conversation import Conversation


def test_新建对话是空的():
    conv = Conversation()
    assert len(conv) == 0
    assert conv.to_messages() == []


def test_加一条用户消息后长度变成1():
    conv = Conversation()
    conv.add_user("你好")
    assert len(conv) == 1


def test_to_messages_格式符合anthropic_SDK要求():
    """SDK 要的是 [{"role": "user"|"assistant", "content": str}, ...]"""
    conv = Conversation()
    conv.add_user("你好")
    conv.add_assistant("您好!")
    msgs = conv.to_messages()
    assert msgs == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好!"},
    ]


def test_clear_清空所有消息():
    conv = Conversation()
    conv.add_user("a")
    conv.add_assistant("b")
    conv.add_user("c")
    conv.clear()
    assert len(conv) == 0
    assert conv.to_messages() == []


def test_pop_last_去掉最后一条_用于出错回滚():
    """
    main.py 现有逻辑:LLM 调用出错时撤回刚塞进去的 user 消息。
    用 OO 包一下,变成 conv.pop_last()。
    """
    conv = Conversation()
    conv.add_user("a")
    conv.add_user("b")
    conv.pop_last()
    assert len(conv) == 1
    assert conv.to_messages() == [{"role": "user", "content": "a"}]


def test_pop_last_空对话不报错():
    """空对话调 pop_last 应该是 no-op,不能炸。"""
    conv = Conversation()
    conv.pop_last()  # 不应抛异常
    assert len(conv) == 0


def test_snapshot_返回可遍历的副本_用于_history命令():
    """
    /history 命令要打印每条消息。snapshot() 返回 list[dict] 的浅拷贝
    —— 外部拿到后改它不影响内部状态。
    """
    conv = Conversation()
    conv.add_user("a")
    conv.add_assistant("b")

    snap = conv.snapshot()
    assert snap == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]

    # 改副本不影响原对话
    snap.append({"role": "user", "content": "篡改"})
    assert len(conv) == 2


def test_交替加消息_保持顺序():
    conv = Conversation()
    conv.add_user("Q1")
    conv.add_assistant("A1")
    conv.add_user("Q2")
    conv.add_assistant("A2")

    msgs = conv.to_messages()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in msgs] == ["Q1", "A1", "Q2", "A2"]


# ============ Task 3 新增: to_messages 截断 ============


def test_to_messages_无参_零回归():
    """to_messages() 不带参数 -> 跟改造前完全一致。"""
    conv = Conversation()
    conv.add_user("a")
    conv.add_assistant("b")
    conv.add_user("c")
    # 无参调用应当返回所有消息, 顺序不变
    assert conv.to_messages() == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


def test_to_messages_max_rounds_砍掉前面老的():
    """max_rounds=2 -> 保留最后 2 轮 = 最后 4 条 (一轮 user+assistant 算一对)。"""
    conv = Conversation()
    for i in range(5):  # 共 10 条, 5 轮
        conv.add_user(f"u{i}")
        conv.add_assistant(f"a{i}")

    out = conv.to_messages(max_rounds=2)
    # 保留 u3 a3 u4 a4 4 条
    assert out == [
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]


def test_to_messages_max_chars_累计字符砍头():
    """max_chars=15 时累计字符 > 15 -> 从头丢, 直到累计 ≤ 15。"""
    conv = Conversation()
    conv.add_user("x" * 10)  # 10 字符
    conv.add_assistant("y" * 10)  # 10 字符 (累计 20, 超)
    conv.add_user("z" * 5)  # 5 字符 (累计 25 -> 砍头)

    out = conv.to_messages(max_chars=15)
    # 砍掉第 1 条 (剩 15 字符) -> assistant y...y + user z...z 累计 15, ≤ 15 通过
    assert out == [
        {"role": "assistant", "content": "y" * 10},
        {"role": "user", "content": "z" * 5},
    ]


def test_to_messages_全部消息总字符不超_不变():
    """max_chars 足够大 -> 一条不丢, 原样返回。"""
    conv = Conversation()
    conv.add_user("a")
    conv.add_assistant("b")
    out = conv.to_messages(max_chars=10000)
    assert out == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]


def test_to_messages_单条超字符上限_保留并落_oversize():
    """单条 200 字 + max_chars=100 -> 保留单条 (不切碎), 继续走累计逻辑。"""
    conv = Conversation()
    conv.add_user("x" * 200)  # 单条 200, 超
    out = conv.to_messages(max_chars=100)
    # 保留单条 (即使超), 这是 spec §4.1 的明文要求
    assert out == [{"role": "user", "content": "x" * 200}]


def test_to_messages_先_max_rounds_再_max_chars():
    """两参数同时给 -> 先按 max_rounds 砍, 再按 max_chars 砍累计字符。"""
    conv = Conversation()
    for i in range(5):
        conv.add_user("u" + str(i) * 10)
        conv.add_assistant("a" + str(i) * 10)

    # 5 轮 -> max_rounds=3 砍到最后 3 轮 (6 条) -> 再 max_chars=20 砍头
    out = conv.to_messages(max_rounds=3, max_chars=20)
    # 6 条每条 11 字 = 累计 66, 超过 20, 砍头到累计 ≤ 20
    total_chars = sum(len(m["content"]) for m in out)
    assert total_chars <= 20 or len(out) == 1  # 单条超额则保留单条


def test_to_messages_不动内部_messages_不影响后续调用():
    """to_messages(max_rounds=2) 返回截断版本, 但 conv 内部状态不变,
    再次无参调用必须能拿回全部消息。"""
    conv = Conversation()
    for i in range(5):
        conv.add_user(f"u{i}")
        conv.add_assistant(f"a{i}")

    _ = conv.to_messages(max_rounds=1)
    # 内部应该仍有 10 条
    assert len(conv) == 10
    # 再次无参调用还能拿到全部
    all_msgs = conv.to_messages()
    assert len(all_msgs) == 10


def test_to_messages_带参返回独立_list_反向不污染():
    """to_messages(max_rounds=...) 返回新 list, 调用方修改不影响 conv 内部状态。

    防御 #2: 无参版本返回内部引用 (零回归), 带参版本必须返回拷贝。
    """
    conv = Conversation()
    conv.add_user("u")
    conv.add_assistant("a")

    out = conv.to_messages(max_rounds=10)  # max_rounds 不触发砍头, 但走的是拷贝路径
    out.append({"role": "user", "content": "篡改"})

    # 篡改 out 不影响 conv
    assert len(conv) == 2
    assert conv.to_messages() == [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]


# ============ Task 2 新增: to_dict / load_from 序列化 ============


def test_to_dict_导出内部messages():
    conv = Conversation()
    conv.add_user("q")
    conv.add_assistant("a")
    assert conv.to_dict() == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]


def test_to_dict_是浅拷贝改返回值不污染内部():
    conv = Conversation()
    conv.add_user("q")
    dumped = conv.to_dict()
    dumped.append({"role": "user", "content": "偷加的"})
    assert len(conv) == 1  # 内部不受影响


def test_load_from_整体替换内部状态():
    conv = Conversation()
    conv.add_user("旧的")
    conv.load_from([{"role": "user", "content": "新的"}])
    assert conv.to_dict() == [{"role": "user", "content": "新的"}]


def test_load_from_round_trip一致():
    conv = Conversation()
    conv.add_user("q")
    conv.add_assistant("a")
    data = conv.to_dict()
    conv2 = Conversation()
    conv2.load_from(data)
    assert conv2.to_dict() == data
