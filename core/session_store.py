"""core/session_store.py — 多会话磁盘持久化 (一会话一 JSON 文件)。

责任只有一件: sessions/ 目录的 CRUD。不碰 Conversation 内部, 不碰 agent。
文件名 = session_id = uuid4, 文件系统天然隔离 → 不同会话物理上不可能串台。

schema:
  {id, title, created_at, updated_at, messages: [{role, content, steps?}]}
  steps 是 assistant 消息的可选字段, 只用于前端展示 (喂 LLM 时由 web 层剥离)。
"""
import json
import os
import uuid
from datetime import datetime, timedelta

from core.observability import log_event


# 进程内上次 _now() 返回的 datetime, 用于单调追赶。
# Windows 时钟分辨率粗 (连续 datetime.now() 常返回同一微秒值), 会导致同秒内多次
# create/save 的 updated_at 字典序全等 → list_meta 倒序排序随机 (test_list_meta 偶发红)。
# 追赶: 若 now 不比上次新, 就在上次基础上 +1μs, 保证返回值严格递增, 字典序可靠。
_last_now: datetime | None = None


def _now() -> str:
    # Windows 无时区, 本地时间 ISO8601 字符串够用 (可字典序比大小)
    global _last_now
    now = datetime.now()
    if _last_now is not None and now <= _last_now:
        now = _last_now + timedelta(microseconds=1)
    _last_now = now
    return now.isoformat()


class SessionStore:
    def __init__(self, sessions_dir: str) -> None:
        self._dir = sessions_dir
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, sid: str) -> str:
        return os.path.join(self._dir, f"{sid}.json")

    def create(self) -> dict:
        sid = str(uuid.uuid4())
        now = _now()
        sess = {
            "id": sid,
            "title": "",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        self._write(sid, sess)
        log_event("session.create", {"id": sid})
        return sess

    def load(self, sid: str) -> dict:
        path = self._path(sid)
        if not os.path.exists(path):
            raise KeyError(f"会话不存在: {sid}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, sid: str, data: dict) -> None:
        data["updated_at"] = _now()
        self._write(sid, data)
        log_event("session.save", {"id": sid, "n_msgs": len(data.get("messages", []))})

    def delete(self, sid: str) -> None:
        path = self._path(sid)
        if os.path.exists(path):
            os.remove(path)
            log_event("session.delete", {"id": sid})

    def list_meta(self) -> list[dict]:
        metas = []
        for fn in os.listdir(self._dir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self._dir, fn), encoding="utf-8") as f:
                    d = json.load(f)
                metas.append({"id": d["id"], "title": d["title"],
                              "updated_at": d["updated_at"]})
            except (json.JSONDecodeError, KeyError):
                continue  # 跳过损坏/非会话文件, 不让列表炸
        metas.sort(key=lambda m: m["updated_at"], reverse=True)
        return metas

    def _write(self, sid: str, data: dict) -> None:
        with open(self._path(sid), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
