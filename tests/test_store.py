"""
测 rag/store.py 在引入 chromadb 持久化之后的新接口。

为啥 mock embedder?
真 bge 加载要几秒,每个测试都跑会拖到几十秒;而 store 这层的核心逻辑
(持久化、mtime 检测、按 source 删) 跟向量的"语义质量"无关,只关心
"向量进去 -> 能查回来"。所以用一个确定性的假向量替换即可。

跑法: uv run pytest tests/test_store.py -v
"""

import pytest

from capabilities.rag import store as store_mod
from capabilities.rag.store import VectorStore


@pytest.fixture
def fake_embedder(monkeypatch):
    """把 store 内部用到的 embed_texts/embed_text 换成基于字符串哈希的确定性假向量。"""

    def _hash_vec(text: str) -> list[float]:
        # 4 维, 来源是字符串哈希, 再归一化, 模拟"语义向量"
        h = abs(hash(text))
        v = [((h >> (i * 8)) & 0xFF) / 255.0 for i in range(4)]
        norm = sum(x * x for x in v) ** 0.5
        return [x / norm for x in v] if norm else [1.0, 0.0, 0.0, 0.0]

    def fake_embed_texts(texts):
        return [_hash_vec(t) for t in texts]

    def fake_embed_text(text):
        return _hash_vec(text)

    monkeypatch.setattr(store_mod, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(store_mod, "embed_text", fake_embed_text)


# ============ 基本能力: 加 / 查 / 长度 ============


def test_新建空store_长度0_search返回空(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    assert len(store) == 0
    assert store.search("任何问题") == []


def test_add后长度增加_search能查到(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "Python 是动态语言", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "uv 管理依赖", "source": "a.md", "index": 1, "mtime": 1.0},
    ])
    assert len(store) == 2

    # 查同一个 text 一定能命中自己 (score 接近 1)
    results = store.search("Python 是动态语言", top_k=2)
    assert len(results) == 2
    # 命中自己时分数最高
    assert results[0]["text"] == "Python 是动态语言"
    assert results[0]["score"] > 0.9


def test_search结果带source和index字段_便于溯源(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([{"text": "X", "source": "docs/a.md", "index": 3, "mtime": 1.0}])
    results = store.search("X", top_k=1)
    assert results[0]["source"] == "docs/a.md"
    assert results[0]["index"] == 3


# ============ 持久化: 重开 client 能读到老数据 ============


def test_持久化_重新打开store能读到老数据(fake_embedder, tmp_path):
    store1 = VectorStore(persist_dir=str(tmp_path))
    store1.add([
        {"text": "持久化测试", "source": "a.md", "index": 0, "mtime": 1.0},
    ])
    assert len(store1) == 1

    # 模拟重启进程: 丢掉 store1, 用同一目录新开一个 store2
    del store1
    store2 = VectorStore(persist_dir=str(tmp_path))
    assert len(store2) == 1
    results = store2.search("持久化测试", top_k=1)
    assert results[0]["text"] == "持久化测试"


# ============ mtime 检测相关 ============


def test_get_source_mtime_文件不在库返回None(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    assert store.get_source_mtime("不存在的文件.md") is None


def test_get_source_mtime_文件在库返回存的mtime(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "X", "source": "a.md", "index": 0, "mtime": 42.5},
        {"text": "Y", "source": "a.md", "index": 1, "mtime": 42.5},
    ])
    assert store.get_source_mtime("a.md") == 42.5


def test_get_source_mtime_多文件互不干扰(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "A", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "B", "source": "b.md", "index": 0, "mtime": 2.0},
    ])
    assert store.get_source_mtime("a.md") == 1.0
    assert store.get_source_mtime("b.md") == 2.0


# ============ 按 source 删除 ============


def test_delete_by_source_删完只留其他source(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "A1", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "A2", "source": "a.md", "index": 1, "mtime": 1.0},
        {"text": "B1", "source": "b.md", "index": 0, "mtime": 2.0},
    ])
    n = store.delete_by_source("a.md")
    assert n == 2
    assert len(store) == 1
    assert store.get_source_mtime("a.md") is None
    assert store.get_source_mtime("b.md") == 2.0


def test_delete_by_source_文件不在库返回0_不报错(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([{"text": "A", "source": "a.md", "index": 0, "mtime": 1.0}])
    assert store.delete_by_source("不存在.md") == 0
    assert len(store) == 1  # 没误伤


# ============ sources(): 列出库里有哪些文件 ============


def test_sources_去重并列出所有source(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "A1", "source": "a.md", "index": 0, "mtime": 1.0},
        {"text": "A2", "source": "a.md", "index": 1, "mtime": 1.0},
        {"text": "B1", "source": "b.md", "index": 0, "mtime": 2.0},
    ])
    assert sorted(store.sources()) == ["a.md", "b.md"]


def test_sources_空库返回空列表(fake_embedder, tmp_path):
    store = VectorStore(persist_dir=str(tmp_path))
    assert store.sources() == []


# ============ 端到端: 文件改了, 删旧块加新块, 检索结果来自新版本 ============


def test_删旧块加新块_search命中新版本(fake_embedder, tmp_path):
    """模拟 main.py 在 mtime 变化时该走的流程: 先 delete_by_source, 再 add 新切块。"""
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "旧版本内容", "source": "a.md", "index": 0, "mtime": 1.0},
    ])
    # 模拟文件被改了, mtime 变大
    store.delete_by_source("a.md")
    store.add([
        {"text": "新版本内容", "source": "a.md", "index": 0, "mtime": 2.0},
    ])
    assert len(store) == 1
    assert store.get_source_mtime("a.md") == 2.0
    results = store.search("新版本内容", top_k=1)
    assert results[0]["text"] == "新版本内容"


# ============ 阶段 1.4: chunker 新字段透传 (heading_path/level/content_hash) ============


def test_add透传chunker新字段_search结果带heading_path_level_content_hash(fake_embedder, tmp_path):
    """
    chunker 升级到 D2 后产出 heading_path / level / content_hash,
    store 必须原样透传 (写进 metadata, search 返回时带出来), 不能丢字段。
    """
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {
            "text": "## 标题1.2\n\n正文内容",
            "source": "a.md",
            "index": 0,
            "mtime": 1.0,
            "heading_path": "标题1 > 标题1.2",
            "level": 2,
            "content_hash": "a3f2c891d4e5b67a",
        },
    ])

    results = store.search("正文内容", top_k=1)
    assert len(results) == 1
    assert results[0]["heading_path"] == "标题1 > 标题1.2"
    assert results[0]["level"] == 2
    assert results[0]["content_hash"] == "a3f2c891d4e5b67a"


def test_add老调用不传新字段_不崩_返回默认值(fake_embedder, tmp_path):
    """
    向后兼容: 阶段 1.1/1.2 老调用方还可能只传 text/source/index/mtime,
    add 不能因为缺 heading_path/level/content_hash 报错; search 返回默认值即可。
    """
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "无新字段的老块", "source": "a.md", "index": 0, "mtime": 1.0},
    ])

    results = store.search("无新字段的老块", top_k=1)
    assert len(results) == 1
    # 默认值: 字符串字段 "", 数字字段 0
    assert results[0]["heading_path"] == ""
    assert results[0]["level"] == 0
    assert results[0]["content_hash"] == ""


def test_search结果保持老字段不变_新字段是超集(fake_embedder, tmp_path):
    """
    硬合同: 老字段 text/source/index/score 一个不能少, 新字段是 *额外加* 的。
    """
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {
            "text": "X",
            "source": "docs/a.md",
            "index": 3,
            "mtime": 1.0,
            "heading_path": "顶节",
            "level": 1,
            "content_hash": "deadbeefcafebabe",
        },
    ])

    r = store.search("X", top_k=1)[0]
    # 老字段必须在
    assert {"text", "source", "index", "score"} <= set(r.keys())
    # 新字段也必须在
    assert {"heading_path", "level", "content_hash"} <= set(r.keys())


def test_get_by_ids_返回字段是search字段子集(fake_embedder, tmp_path):
    """get_by_ids 给定 id 列表批量取, 返回结构跟 search 对齐 (text/source/index 等)."""
    from capabilities.rag.store import VectorStore
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "A 文本", "source": "a.md", "index": 0, "mtime": 1.0,
         "heading_path": "标题A", "level": 1, "content_hash": "h1"},
        {"text": "B 文本", "source": "b.md", "index": 0, "mtime": 2.0,
         "heading_path": "标题B", "level": 2, "content_hash": "h2"},
    ])
    got = store.get_by_ids(["a.md::0", "b.md::0"])
    assert len(got) == 2
    by_id = {f"{g['source']}::{g['index']}": g for g in got}
    assert by_id["a.md::0"]["text"] == "A 文本"
    assert by_id["a.md::0"]["heading_path"] == "标题A"
    assert by_id["b.md::0"]["content_hash"] == "h2"


def test_get_by_ids_空列表返回空(fake_embedder, tmp_path):
    from capabilities.rag.store import VectorStore
    store = VectorStore(persist_dir=str(tmp_path))
    assert store.get_by_ids([]) == []


def test_get_by_ids_id不存在时不抛错(fake_embedder, tmp_path):
    """id 部分命中: 只返回命中的, 不抛错 (chroma 行为)."""
    from capabilities.rag.store import VectorStore
    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        {"text": "A", "source": "a.md", "index": 0, "mtime": 1.0},
    ])
    got = store.get_by_ids(["a.md::0", "nonexistent::99"])
    # 只命中 a.md::0; chroma get 对未知 id 返回 [], 不报错
    assert len(got) == 1
    assert got[0]["source"] == "a.md"
