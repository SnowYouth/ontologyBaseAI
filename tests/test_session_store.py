"""test_session_store.py — SessionStore 磁盘 CRUD + 隔离性回归。

用 tmp_path 隔离, 不碰真 sessions/ 目录。
跑法: uv run pytest tests/test_session_store.py -v
"""
from core.session_store import SessionStore


def test_create_返回带uuid的完整会话并写出文件(tmp_path):
    store = SessionStore(str(tmp_path))
    sess = store.create()
    assert "id" in sess and len(sess["id"]) > 0
    assert sess["title"] == ""
    assert sess["messages"] == []
    assert "created_at" in sess and "updated_at" in sess
    # 文件真落盘
    assert (tmp_path / f"{sess['id']}.json").exists()


def test_load_读回与create一致(tmp_path):
    store = SessionStore(str(tmp_path))
    sess = store.create()
    loaded = store.load(sess["id"])
    assert loaded["id"] == sess["id"]
    assert loaded["messages"] == []


def test_load_不存在抛KeyError(tmp_path):
    import pytest
    store = SessionStore(str(tmp_path))
    with pytest.raises(KeyError):
        store.load("nonexistent-id")


def test_save_覆盖内容并刷新updated_at(tmp_path):
    store = SessionStore(str(tmp_path))
    sess = store.create()
    old_updated = sess["updated_at"]
    sess["messages"] = [{"role": "user", "content": "q"}]
    store.save(sess["id"], sess)
    loaded = store.load(sess["id"])
    assert loaded["messages"] == [{"role": "user", "content": "q"}]
    assert loaded["updated_at"] >= old_updated  # ISO 字符串可比大小


def test_delete_后load抛KeyError(tmp_path):
    import pytest
    store = SessionStore(str(tmp_path))
    sess = store.create()
    store.delete(sess["id"])
    with pytest.raises(KeyError):
        store.load(sess["id"])


def test_delete_不存在是noop不报错(tmp_path):
    store = SessionStore(str(tmp_path))
    store.delete("nonexistent")  # 不抛


def test_list_meta_只含元字段且按updated_at倒序(tmp_path):
    store = SessionStore(str(tmp_path))
    a = store.create()
    b = store.create()
    # 手动把 b 的 updated_at 弄得更新 (save 会刷新)
    store.save(b["id"], {**b, "title": "较新"})
    metas = store.list_meta()
    assert len(metas) == 2
    # 只含元字段, 不含 messages
    assert set(metas[0].keys()) == {"id", "title", "updated_at"}
    # 倒序: 最近 save 的 b 在前
    assert metas[0]["id"] == b["id"]


def test_连续操作updated_at严格递增不因Windows时钟粗分辨率退化(tmp_path):
    """回归: Windows 连续 datetime.now() 常返回同一微秒值, 若 _now() 不单调追赶,
    同秒内多次 create/save 的 updated_at 字典序全等 → list_meta 排序随机 (偶发红)。
    守: 连续 create 的 updated_at 必须严格字典序递增。"""
    store = SessionStore(str(tmp_path))
    ts = [store.create()["updated_at"] for _ in range(5)]
    assert ts == sorted(ts)              # 字典序递增
    assert len(set(ts)) == 5             # 严格不重复 (无退化)


def test_两会话交替save各读各的互不覆盖(tmp_path):
    """隔离性回归: 守 '不串台' —— 两会话物理隔离。"""
    store = SessionStore(str(tmp_path))
    a = store.create()
    b = store.create()
    store.save(a["id"], {**a, "messages": [{"role": "user", "content": "A的消息"}]})
    store.save(b["id"], {**b, "messages": [{"role": "user", "content": "B的消息"}]})
    store.save(a["id"], {**a, "messages": [{"role": "user", "content": "A更新"}]})
    assert store.load(a["id"])["messages"][0]["content"] == "A更新"
    assert store.load(b["id"])["messages"][0]["content"] == "B的消息"
