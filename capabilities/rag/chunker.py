"""
chunker: 把长文本切成"块"(chunk)。

阶段 1.4 升级到 D2 策略: 结构感知 + 递归兜底。
设计文档见 memory/stage-1-4-plan.md。

切分管线 (从粗到细):
  [1 级] 按 # / ## / ### 标题切    → 每节一块, 带 heading_path
  [2 级] 段落 \\n\\n 累加切         ← 节超 max_chunk_size 时
  [3 级] 句号 。!？!? 累加切        ← 段落仍超时
  [4 级] 字符硬切 + overlap        ← 最后兜底

为啥不是纯递归切块:
- Markdown 标题是天然语义边界, 一上来按标题切, 后续检索可附带 heading_path 给 LLM 看
- 边界稳定: 编辑某一节不影响其他节的切块结果 (向量库增量重 load 友好)
- KG 友好: heading_path / level / content_hash 三个字段为阶段 5 知识图谱铺地基

接口约束:
- chunk_text / chunk_file 签名向后兼容: chunk_size / chunk_overlap 老 kwarg 保留
- 返回结构是老字段的超集: text / source / index 全在, 多了 heading_path / level / content_hash
- chunk_text 老版本返回 list[str], 新版返回 list[dict] (没有外部直接调用方, 安全)
"""

import hashlib
import re

from core.observability import log_event

# 行首 1-6 个 #, 后面空格 + 标题文本
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)

# 句末标点 (中英双覆盖), 用 lookbehind 保留分隔符在前一句
_SENTENCE_RE = re.compile(r"(?<=[。!？！?])")

# 默认参数 (按 stage-1-4-plan.md 拍板)
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 80
# min_chunk_size 默认 0 = 不合并; 显式传值 (如 100) 才触发跨节合并
DEFAULT_MIN_CHUNK_SIZE = 0


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    min_chunk_size: int = DEFAULT_MIN_CHUNK_SIZE,
) -> list[dict]:
    """
    把 text 按 D2 策略切块。

    返回 [{"text", "heading_path", "level", "content_hash"}, ...]
    其中 text 包含 heading 前缀 (检索时 LLM 看得到上下文层级)。

    min_chunk_size > 0 时, 相邻短块跨节合并, 避免碎片 (heading_path 取合并范围首节)。
    """
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size, 否则会死循环")

    text = text.strip() if text else ""
    if not text:
        return []

    sections = _split_by_headings(text)
    pieces = _split_sections_to_pieces(sections, chunk_size, chunk_overlap)

    if min_chunk_size > 0:
        pieces = _merge_short(pieces, min_chunk_size, chunk_size)

    return [
        {
            "text": pt,
            "heading_path": path,
            "level": level,
            "content_hash": hashlib.sha256(pt.encode("utf-8")).hexdigest()[:16],
        }
        for path, level, pt in pieces
    ]


def chunk_file(
    file_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    min_chunk_size: int = DEFAULT_MIN_CHUNK_SIZE,
) -> list[dict]:
    """
    读 md/txt 文件并切块。

    返回字段是阶段 1.3 老字段的超集:
      老: text / source / index
      新: heading_path / level / content_hash

    末尾埋 chunker.done 事件: file / n_chunks / len_min/max/avg / sample_first (前 200 字)
    """
    with open(file_path, encoding="utf-8") as f:
        text = f.read()

    chunks = chunk_text(text, chunk_size, chunk_overlap, min_chunk_size)
    for i, c in enumerate(chunks):
        c["source"] = file_path
        c["index"] = i

    # 可观测性: 切完落一条统计, 后续调参/排查有依据
    if chunks:
        lens = [len(c["text"]) for c in chunks]
        log_event("chunker.done", {
            "file": file_path,
            "n_chunks": len(chunks),
            "len_min": min(lens),
            "len_max": max(lens),
            "len_avg": sum(lens) // len(lens),
            "sample_first": chunks[0]["text"][:200],
        })
    else:
        log_event("chunker.done", {
            "file": file_path,
            "n_chunks": 0,
            "len_min": 0,
            "len_max": 0,
            "len_avg": 0,
            "sample_first": "",
        })

    return chunks


# ============ 内部: 标题切节 ============


def _split_by_headings(text: str) -> list[tuple[str, int, str]]:
    """
    切节, 返回 [(heading_path, level, body), ...]。

    无标题文档退化: 返回 [("", 0, text)] 一节, 后续走字符切。
    文档开头到第一个 heading 之间的"前言"也视为无标题一节。
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", 0, text)]

    sections: list[tuple[str, int, str]] = []
    path_stack: list[tuple[int, str]] = []  # [(level, title)]

    # 文档开头到第一个 heading 之间的"前言"
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(("", 0, preamble))

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()

        # path 栈维护: 弹出 >= 当前 level 的, 再压栈
        while path_stack and path_stack[-1][0] >= level:
            path_stack.pop()
        path_stack.append((level, title))

        heading_path = " > ".join(t for _, t in path_stack)

        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        sections.append((heading_path, level, body))

    return sections


# ============ 内部: 节 → piece 列表 ============


def _split_sections_to_pieces(
    sections: list[tuple[str, int, str]],
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[str, int, str]]:
    """对每节做递归切, 每块拼上 heading 前缀。"""
    pieces: list[tuple[str, int, str]] = []
    for path, level, body in sections:
        sub_texts = _recursive_split(body, chunk_size, chunk_overlap)
        if not sub_texts:
            # 只有 heading 没正文 → 留一块, text 用 heading 表达
            if path:
                pieces.append((path, level, _format_heading_only(path, level)))
            continue
        for st in sub_texts:
            pieces.append((path, level, _format_piece(path, level, st)))
    return pieces


def _format_piece(heading_path: str, level: int, body: str) -> str:
    """piece 的 text: 有 heading 时加 "# leaf\\n\\nbody" 前缀。"""
    if not heading_path:
        return body
    leaf = heading_path.split(" > ")[-1]
    return f"{'#' * level} {leaf}\n\n{body}"


def _format_heading_only(heading_path: str, level: int) -> str:
    leaf = heading_path.split(" > ")[-1]
    return f"{'#' * level} {leaf}"


# ============ 内部: 递归兜底 (段落 → 句号 → 字符) ============


def _recursive_split(body: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """节内递归切, 不带 heading 前缀 (前缀由 _format_piece 加)。"""
    body = body.strip() if body else ""
    if not body:
        return []
    if len(body) <= chunk_size:
        return [body]

    # 第 2 级: 段落 \n\n
    paragraph_chunks = _split_by_paragraphs(body, chunk_size)

    # 段落仍超 → 第 3 级 + 第 4 级
    result: list[str] = []
    for c in paragraph_chunks:
        if len(c) <= chunk_size:
            result.append(c)
        else:
            result.extend(_split_by_sentences(c, chunk_size, chunk_overlap))
    return result


def _split_by_paragraphs(body: str, chunk_size: int) -> list[str]:
    """按 \\n\\n 分段, 累加 ≤ chunk_size。"""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    return _greedy_accumulate(paragraphs, chunk_size, joiner="\n\n")


def _split_by_sentences(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """按句末标点切, 累加 ≤ chunk_size; 仍超走字符兜底。"""
    sentences = [s for s in _SENTENCE_RE.split(text) if s.strip()]
    sentence_chunks = _greedy_accumulate(sentences, chunk_size, joiner="")

    result: list[str] = []
    for c in sentence_chunks:
        if len(c) <= chunk_size:
            result.append(c)
        else:
            result.extend(_split_by_chars(c, chunk_size, chunk_overlap))
    return result


def _greedy_accumulate(parts: list[str], chunk_size: int, joiner: str) -> list[str]:
    """把 parts 贪心累加到 ≤ chunk_size 的块。"""
    chunks: list[str] = []
    cur = ""
    for p in parts:
        candidate = f"{cur}{joiner}{p}" if cur else p
        if len(candidate) <= chunk_size:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def _split_by_chars(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """最后兜底: 字符硬切 + overlap。"""
    if not text:
        return []
    chunks: list[str] = []
    step = chunk_size - chunk_overlap
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += step
    return chunks


# ============ 内部: min_chunk_size 跨节合并 ============


def _merge_short(
    pieces: list[tuple[str, int, str]],
    min_chunk_size: int,
    max_chunk_size: int,
) -> list[tuple[str, int, str]]:
    """
    相邻短块累积合并 (跨节), 合并后块继承首节 heading_path/level。

    合并条件: 当前块文本长度 < min_chunk_size, 且加上下一块仍 ≤ max_chunk_size。
    用 "\\n\\n" 连接, 保留两节之间的视觉边界。
    """
    merged: list[tuple[str, int, str]] = []
    i = 0
    while i < len(pieces):
        path, level, text = pieces[i]
        j = i + 1
        while (
            j < len(pieces)
            and len(text) < min_chunk_size
            and len(text) + len(pieces[j][2]) + 2 <= max_chunk_size
        ):
            text = f"{text}\n\n{pieces[j][2]}"
            j += 1
        merged.append((path, level, text))
        i = j
    return merged
