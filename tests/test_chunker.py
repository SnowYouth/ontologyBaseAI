"""
测 rag/chunker.py 的 D2 策略: 结构感知 + 递归兜底。

设计文档: memory/stage-1-4-plan.md
策略:
  [1 级] 按 # / ## / ### 切, 每节带 heading_path
  [2 级] 节超 max_chunk_size → 段落 \\n\\n 兜底
  [3 级] 段落仍超 → 句号 。!? 兜底
  [4 级] 还超 → 字符硬切 + overlap

跑法: uv run pytest tests/test_chunker.py -v

为啥不 mock embedder?
chunker 不依赖向量, 纯字符串处理, 不需要 fixture。
"""

from capabilities.rag.chunker import chunk_file, chunk_text


# ============ 1. 纯文本无标题 → 退化为字符切 ============


def test_纯文本无标题_退化为字符切_heading_path为空():
    text = "这是一段普通文本, 没有任何 markdown 标题, " * 30  # ~600 字符
    chunks = chunk_text(text, chunk_size=200, chunk_overlap=20)

    assert len(chunks) >= 2  # 600 字符配 200 上限至少切 2 块
    for c in chunks:
        assert c["heading_path"] == ""
        assert c["level"] == 0
        assert len(c["text"]) <= 200 + 20  # 允许 overlap 略超


# ============ 2. 单层 # → 每节一块, heading_path 正确 ============


def test_单层标题_每节一块_heading_path为标题名():
    text = "# 章节甲\n\n这是甲的正文。\n\n# 章节乙\n\n这是乙的正文。"
    chunks = chunk_text(text, chunk_size=800)

    assert len(chunks) == 2
    assert chunks[0]["heading_path"] == "章节甲"
    assert chunks[0]["level"] == 1
    assert "甲的正文" in chunks[0]["text"]
    assert chunks[1]["heading_path"] == "章节乙"
    assert chunks[1]["level"] == 1
    assert "乙的正文" in chunks[1]["text"]


# ============ 3. 多层 ## ### → 切到最深层 ============


def test_多层标题_heading_path_用大于号连接_level为最深():
    text = (
        "# 顶\n\n顶层正文\n\n"
        "## 中\n\n中层正文\n\n"
        "### 深一\n\n深一正文\n\n"
        "### 深二\n\n深二正文"
    )
    chunks = chunk_text(text, chunk_size=800)

    paths = {c["heading_path"] for c in chunks}
    # 必须切到最深层 (选项 C: 切到最深, 靠 min_chunk_size 合并)
    assert "顶 > 中 > 深一" in paths
    assert "顶 > 中 > 深二" in paths

    deep_chunks = [c for c in chunks if c["heading_path"].endswith("深一")]
    assert deep_chunks[0]["level"] == 3
    assert "深一正文" in deep_chunks[0]["text"]


# ============ 4. 节超长 → 触发段落兜底 ============


def test_节超长_按段落切_heading_path保留():
    # 每段 200 字符, 4 段拼起来 800+, 配 max=300 必触发段落兜底
    paragraphs = ["甲" * 200, "乙" * 200, "丙" * 200, "丁" * 200]
    body = "\n\n".join(paragraphs)
    text = f"# 大章节\n\n{body}"

    chunks = chunk_text(text, chunk_size=300, min_chunk_size=50)

    # 必须被切多块, 不能一坨返回
    assert len(chunks) >= 2
    # 每块都属于"大章节"
    for c in chunks:
        assert c["heading_path"] == "大章节"
    # 每块都不超过 max
    for c in chunks:
        assert len(c["text"]) <= 320  # 给点 buffer 容忍段落边界
    # 拼起来覆盖所有段落内容
    joined = "".join(c["text"] for c in chunks)
    assert "甲" * 200 in joined
    assert "丁" * 200 in joined


# ============ 5. 段落仍超长 → 触发句号兜底 ============


def test_段落超长_按句号切():
    # 单段 (无 \n\n) 内塞很多句子, 远超 max
    sentences = ["这是第" + str(i) + "句话。" for i in range(50)]
    body = "".join(sentences)  # 单段 ~400 字符
    text = f"# 长段\n\n{body}"

    chunks = chunk_text(text, chunk_size=100, min_chunk_size=20)

    assert len(chunks) >= 3
    for c in chunks:
        assert c["heading_path"] == "长段"
    # 大部分块应该按句号切, 即以 。 结尾 (允许少量字符兜底块)
    sentence_end = sum(1 for c in chunks if c["text"].rstrip().endswith("。"))
    assert sentence_end >= len(chunks) // 2


# ============ 6. 边界稳定性 (D2 核心价值) ============


def test_某节内改字符_其他节text完全不变():
    text_v1 = "# 甲\n\n甲的旧正文\n\n# 乙\n\n乙的稳定正文\n\n# 丙\n\n丙的稳定正文"
    text_v2 = "# 甲\n\n甲的新正文换了好多字\n\n# 乙\n\n乙的稳定正文\n\n# 丙\n\n丙的稳定正文"

    chunks_v1 = chunk_text(text_v1, chunk_size=800)
    chunks_v2 = chunk_text(text_v2, chunk_size=800)

    # 把 v1/v2 按 heading_path 索引
    by_path_v1 = {c["heading_path"]: c["text"] for c in chunks_v1}
    by_path_v2 = {c["heading_path"]: c["text"] for c in chunks_v2}

    # 乙 / 丙两节的 text 应当一字不差
    assert by_path_v1["乙"] == by_path_v2["乙"]
    assert by_path_v1["丙"] == by_path_v2["丙"]
    # 甲必须变
    assert by_path_v1["甲"] != by_path_v2["甲"]

    # content_hash 同步: 乙丙不变, 甲变
    hash_v1 = {c["heading_path"]: c["content_hash"] for c in chunks_v1}
    hash_v2 = {c["heading_path"]: c["content_hash"] for c in chunks_v2}
    assert hash_v1["乙"] == hash_v2["乙"]
    assert hash_v1["丙"] == hash_v2["丙"]
    assert hash_v1["甲"] != hash_v2["甲"]


# ============ 7. 空文档 / 只有标题没正文 → 不崩 ============


def test_空文档_返回空列表():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_只有标题没正文_不崩_至少含标题信息():
    text = "# 孤标题"
    chunks = chunk_text(text)

    # 不允许崩, 不允许返回空 (有结构信息可记)
    assert len(chunks) >= 1
    assert chunks[0]["heading_path"] == "孤标题"
    assert chunks[0]["level"] == 1


# ============ 8. chunk_file 接口向后兼容 → 字段是老字段超集 ============


def test_chunk_file_保留老字段_text_source_index_并新增三字段(tmp_path):
    md = tmp_path / "demo.md"
    md.write_text("# 演示\n\n一些正文内容。", encoding="utf-8")

    chunks = chunk_file(str(md), chunk_size=800, chunk_overlap=80)

    assert len(chunks) >= 1
    c = chunks[0]
    # 老字段必须在
    assert "text" in c
    assert c["source"] == str(md)
    assert c["index"] == 0
    # 新字段必须在
    assert c["heading_path"] == "演示"
    assert c["level"] == 1
    assert "content_hash" in c
    assert isinstance(c["content_hash"], str)
    assert len(c["content_hash"]) == 16  # sha256 前 16 位


def test_chunk_file_多块时index递增(tmp_path):
    md = tmp_path / "multi.md"
    md.write_text(
        "# 一\n\n正文一\n\n# 二\n\n正文二\n\n# 三\n\n正文三", encoding="utf-8"
    )

    chunks = chunk_file(str(md), chunk_size=800)

    assert len(chunks) == 3
    assert [c["index"] for c in chunks] == [0, 1, 2]


# ============ 9. min_chunk_size 触发 → 相邻短块自动合并 ============


def test_短块自动合并_避免碎片():
    # 每节正文都很短 (< min_chunk_size), 期望合并
    text = (
        "# 甲\n\n短\n\n"
        "# 乙\n\n短\n\n"
        "# 丙\n\n短\n\n"
        "# 丁\n\n短"
    )

    # min 设大点, 强制合并
    chunks = chunk_text(text, chunk_size=800, min_chunk_size=50)

    # 4 节内容加标题都很短, 应被合并成 1-2 块, 不会留 4 个碎片块
    assert len(chunks) < 4
    # 合并块的 text 必须覆盖全部 4 段内容
    joined = "".join(c["text"] for c in chunks)
    assert "甲" in joined
    assert "乙" in joined
    assert "丙" in joined
    assert "丁" in joined
