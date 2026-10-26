"""test_web_model_config.py — /config/model 端点 (模型清单 CRUD + 激活 + 明文闸)。

跑法: uv run pytest tests/test_web_model_config.py -v

配置目录由 conftest 的 _isolate_model_config 钉到 tmp (本文件不另设), 另有
会话级污染守卫兜底 —— 这些测试会真写盘, 绝不能写到机主真实 config/model.json。
"""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import web.app as webapp
    from core import model_config
    # 种子钉死, 断言不受机主 .env 影响
    monkeypatch.setattr(model_config.settings, "primary_model", "anthropic/seed")
    monkeypatch.setattr(model_config.settings, "primary_api_key", "sk-seed-key-abcdef")
    monkeypatch.setattr(model_config.settings, "primary_base_url", "")
    return TestClient(webapp.app)


@pytest.fixture
def local_profile(monkeypatch):
    """把 deploy_profile 切到 local (明文 key 端点的放行条件)。"""
    import web.app as webapp
    monkeypatch.setattr(webapp.settings, "deploy_profile", "local")


# ---------- GET 列表 ----------

def test_get_列出候选_缺文件时给env种子(client):
    r = client.get("/config/model")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "default"
    assert rows[0]["active"] is True


def test_get_不含明文key(client):
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x",
        "api_key": "sk-super-secret-xyz", "base_url": ""})
    r = client.get("/config/model")
    assert "sk-super-secret-xyz" not in r.text
    mid = [x for x in r.json() if x["name"] == "mid"][0]
    assert mid["api_key_masked"] == "sk-••••yz"
    assert mid["api_key_set"] is True


# ---------- POST 新增 ----------

def test_post_新增可读回(client):
    r = client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    assert r.status_code == 200
    assert "mid" in [x["name"] for x in client.get("/config/model").json()]


def test_post_重名409(client):
    body = {"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""}
    client.post("/config/model", json=body)
    r = client.post("/config/model", json=body)
    assert r.status_code == 409


def test_post_非法名400(client):
    r = client.post("/config/model", json={
        "name": "有空格 的", "model": "anthropic/x", "api_key": "k", "base_url": ""})
    assert r.status_code == 400


def test_post_空model_400(client):
    r = client.post("/config/model", json={
        "name": "ok", "model": "   ", "api_key": "k", "base_url": ""})
    assert r.status_code == 400


# ---------- PUT 改 ----------

def test_put_改model(client):
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-keep", "base_url": ""})
    r = client.put("/config/model/mid", json={"model": "anthropic/y"})
    assert r.status_code == 200
    mid = [x for x in client.get("/config/model").json() if x["name"] == "mid"][0]
    assert mid["model"] == "anthropic/y"


def test_put_省略key不洗掉原key(client, local_profile):
    """★ 前端表单里 key 框是空的 (显示掩码), 提交时不该把 key 清空。"""
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-keep-me", "base_url": ""})
    client.put("/config/model/mid", json={"base_url": "https://new.example.com"})
    assert client.get("/config/model/mid/key").json()["api_key"] == "sk-keep-me"


def test_put_给新key就换(client, local_profile):
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-old", "base_url": ""})
    client.put("/config/model/mid", json={"api_key": "sk-new"})
    assert client.get("/config/model/mid/key").json()["api_key"] == "sk-new"


def test_put_不存在404(client):
    r = client.put("/config/model/nope", json={"model": "anthropic/x"})
    assert r.status_code == 404


# ---------- 激活 ----------

def test_put_active_切换(client):
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    r = client.put("/config/model/active", json={"name": "mid"})
    assert r.status_code == 200
    rows = client.get("/config/model").json()
    assert [x["name"] for x in rows if x["active"]] == ["mid"]


def test_put_active_不存在404(client):
    r = client.put("/config/model/active", json={"name": "nope"})
    assert r.status_code == 404


def test_active_路由不被name路由抢走(client):
    """/config/model/active 必须命中激活端点, 不能被 /config/model/{name} 吃掉
    (FastAPI 按声明顺序匹配; active 的声明必须在 {name} 之前)。"""
    r = client.put("/config/model/active", json={"name": "default"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------- 删除 ----------

def test_delete_非active可删(client):
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    assert client.delete("/config/model/mid").status_code == 200
    assert [x["name"] for x in client.get("/config/model").json()] == ["default"]


def test_delete_active_409(client):
    """删正在用的那条 → 409, 且清单不变。"""
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    r = client.delete("/config/model/default")   # default 是 active
    assert r.status_code == 409
    assert len(client.get("/config/model").json()) == 2


def test_delete_不存在404(client):
    assert client.delete("/config/model/nope").status_code == 404


# ---------- 明文 key 闸 (profile 门控) ----------

def test_get_key_local下放行(client, local_profile):
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-plain-1", "base_url": ""})
    r = client.get("/config/model/mid/key")
    assert r.status_code == 200
    assert r.json()["api_key"] == "sk-plain-1"


def test_get_key_serving下403(client, monkeypatch):
    """★ 缺省 profile 是 serving, 明文出口默认关闭 (fail-safe)。

    web 无任何认证 + CORS 放行 localhost:*, 明文 key 端点是真实暴露面 ——
    只在本机开发 (local) 时放行, 部署形态下一律拒。
    """
    import web.app as webapp
    monkeypatch.setattr(webapp.settings, "deploy_profile", "serving")
    client.post("/config/model", json={
        "name": "mid", "model": "anthropic/x", "api_key": "sk-plain-2", "base_url": ""})
    r = client.get("/config/model/mid/key")
    assert r.status_code == 403
    assert "sk-plain-2" not in r.text


def test_get_key_不存在404(client, local_profile):
    assert client.get("/config/model/nope/key").status_code == 404


# ---------- 热生效接缝 ----------

def test_改完active_complete取到新模型(client, monkeypatch):
    """端点改完 → core.llm.complete() 下一次调用就该用新模型 (不重启)。"""
    import core.llm as llm
    client.post("/config/model", json={
        "name": "hot", "model": "anthropic/hot-model",
        "api_key": "sk-hot", "base_url": "https://hot.example.com"})
    client.put("/config/model/active", json={"name": "hot"})

    called = {}
    monkeypatch.setattr("core.llm.litellm.completion",
                        lambda **kw: called.update(kw) or "mock")
    llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert called["model"] == "anthropic/hot-model"
    assert called["api_key"] == "sk-hot"
    assert called["api_base"] == "https://hot.example.com"
