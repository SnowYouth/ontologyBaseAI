"""test_model_config.py — 模型候选清单 CRUD + 激活项 (config/model.json)。

跑法: uv run pytest tests/test_model_config.py -v

隔离: _CONFIG_DIR 指到 tmp_path (conftest 另有 autouse 守卫, 这里显式再钉一次,
单跑本文件也不碰机主真实 config/model.json)。
"""
import json

import pytest

from core import model_config as mc


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_CONFIG_DIR", tmp_path)
    # 种子来源钉死, 不受机主 .env 影响
    monkeypatch.setattr(mc.settings, "primary_model", "anthropic/seed-model")
    monkeypatch.setattr(mc.settings, "primary_api_key", "sk-seed-key-123456")
    monkeypatch.setattr(mc.settings, "primary_base_url", "https://seed.example.com")
    return tmp_path


# ---------- 缺文件 / 坏文件: 不落盘不掀翻 ----------

def test_缺文件_走env种子(cfg):
    data = mc.load()
    assert data["active"] == "default"
    assert len(data["models"]) == 1
    e = data["models"][0]
    assert e["name"] == "default"
    assert e["model"] == "anthropic/seed-model"
    assert e["api_key"] == "sk-seed-key-123456"
    assert e["base_url"] == "https://seed.example.com"


def test_缺文件_不落盘(cfg):
    """★ 种子只在内存里合成: 落盘等于把 .env 的密钥复制出第二份。

    只有用户在 UI 真点保存 (add/update/set_active) 才建文件。
    """
    mc.load()
    assert not (cfg / "model.json").exists()


def test_坏JSON_回退种子_不抛(cfg):
    (cfg / "model.json").write_text("{坏", encoding="utf-8")
    data = mc.load()
    assert data["models"][0]["name"] == "default"


def test_坏JSON_落一条log事件(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(mc, "log_event", lambda ev, p=None: seen.append(ev))
    (cfg / "model.json").write_text("{坏", encoding="utf-8")
    mc.load()
    assert "model_config.load_error" in seen


# ---------- active ----------

def test_active_返回激活条目三要素(cfg):
    e = mc.active()
    assert (e["model"], e["api_key"], e["base_url"]) == (
        "anthropic/seed-model", "sk-seed-key-123456", "https://seed.example.com")


def test_active_指向不存在时回退第一条(cfg, monkeypatch):
    """手工编辑过的文件里 active 可能指向已删条目 —— 回退第一条并响亮 log,
    不抛 (抛了等于一个笔误让整个对话功能瘫掉)。"""
    seen = []
    monkeypatch.setattr(mc, "log_event", lambda ev, p=None: seen.append(ev))
    (cfg / "model.json").write_text(json.dumps({
        "active": "ghost",
        "models": [{"name": "a", "model": "p/m", "api_key": "k", "base_url": ""}],
    }), encoding="utf-8")
    assert mc.active()["name"] == "a"
    assert "model_config.active_missing" in seen


def test_active_models为空时回退env种子(cfg):
    (cfg / "model.json").write_text(json.dumps({"active": "", "models": []}),
                                    encoding="utf-8")
    assert mc.active()["model"] == "anthropic/seed-model"


# ---------- add ----------

def test_add_新增可读回_并落盘(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    assert (cfg / "model.json").exists()
    names = [e["name"] for e in mc.load()["models"]]
    assert names == ["default", "mid"]   # 种子被固化 + 新条目


def test_add_不改变active(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    assert mc.load()["active"] == "default"


def test_add_重名报ValueError(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    with pytest.raises(ValueError):
        mc.add({"name": "mid", "model": "anthropic/y", "api_key": "sk-2", "base_url": ""})


@pytest.mark.parametrize("bad", ["", "   ", "a b", "../etc", "名字", "a/b"])
def test_add_非法名报ValueError(cfg, bad):
    """name 要进 URL path (PUT /config/model/{name}), 只许字母数字下划线连字符。"""
    with pytest.raises(ValueError):
        mc.add({"name": bad, "model": "anthropic/x", "api_key": "k", "base_url": ""})


def test_add_model为空报ValueError(cfg):
    with pytest.raises(ValueError):
        mc.add({"name": "ok", "model": "  ", "api_key": "k", "base_url": ""})


# ---------- update ----------

def test_update_只改model_key不动(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-keep", "base_url": ""})
    mc.update("mid", model="anthropic/y")
    e = [x for x in mc.load()["models"] if x["name"] == "mid"][0]
    assert e["model"] == "anthropic/y"
    assert e["api_key"] == "sk-keep"


def test_update_空key不清空原key(cfg):
    """★ 前端拿到的是掩码串, 用户不改 key 时提交空值 —— 空必须解释成
    "不动", 否则每次改 base_url 都把 key 洗掉。"""
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-keep", "base_url": ""})
    mc.update("mid", api_key="")
    e = [x for x in mc.load()["models"] if x["name"] == "mid"][0]
    assert e["api_key"] == "sk-keep"


def test_update_给了新key就换(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-old", "base_url": ""})
    mc.update("mid", api_key="sk-new")
    e = [x for x in mc.load()["models"] if x["name"] == "mid"][0]
    assert e["api_key"] == "sk-new"


def test_update_base_url_可清空(cfg):
    """base_url 空有明确语义 (走 provider 官方端点), 跟 key 不同: 空就是空。"""
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "k",
            "base_url": "https://mid.example.com"})
    mc.update("mid", base_url="")
    e = [x for x in mc.load()["models"] if x["name"] == "mid"][0]
    assert e["base_url"] == ""


def test_update_不存在报KeyError(cfg):
    with pytest.raises(KeyError):
        mc.update("nope", model="anthropic/x")


# ---------- set_active ----------

def test_set_active_切换(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    mc.set_active("mid")
    assert mc.load()["active"] == "mid"
    assert mc.active()["model"] == "anthropic/x"


def test_set_active_不存在报KeyError(cfg):
    with pytest.raises(KeyError):
        mc.set_active("nope")


# ---------- delete ----------

def test_delete_非active可删(cfg):
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    mc.delete("mid")
    assert [e["name"] for e in mc.load()["models"]] == ["default"]


def test_delete_active被拒(cfg):
    """删掉正在用的那条 = 下一句对话没模型可用, 拒掉并要求先切。"""
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-1", "base_url": ""})
    with pytest.raises(ValueError):
        mc.delete("default")        # default 是 active
    assert [e["name"] for e in mc.load()["models"]] == ["default", "mid"]


def test_delete_不存在报KeyError(cfg):
    with pytest.raises(KeyError):
        mc.delete("nope")


# ---------- 掩码 ----------

def test_mask_key_保留头尾(cfg):
    assert mc.mask_key("sk-REPLACE-ME") == "sk-••••ME"


def test_mask_key_短key全掩(cfg):
    """短 key 不留头尾: 8 字以内露 5 个字符等于露大半。"""
    assert mc.mask_key("sk-12345") == "••••••••"


def test_mask_key_空返回空串(cfg):
    assert mc.mask_key("") == ""


def test_list_status_不含明文key(cfg):
    mc.add({"name": "mid", "model": "anthropic/x",
            "api_key": "sk-super-secret-xyz", "base_url": ""})
    rows = mc.list_status()
    blob = json.dumps(rows, ensure_ascii=False)
    assert "sk-super-secret-xyz" not in blob
    mid = [r for r in rows if r["name"] == "mid"][0]
    assert mid["api_key_masked"] == "sk-••••yz"
    assert mid["api_key_set"] is True
    assert mid["active"] is False


def test_list_status_标出active(cfg):
    rows = mc.list_status()
    assert [r["name"] for r in rows if r["active"]] == ["default"]


def test_list_status_占位key算未配(cfg, monkeypatch):
    """.env.example 的占位值照抄没改 = 还没配 —— 判据复用 config 那一份。"""
    monkeypatch.setattr(mc.settings, "primary_api_key", "sk-REPLACE-ME")
    rows = mc.list_status()
    assert rows[0]["api_key_set"] is False


def test_get_key_返回明文(cfg):
    """唯一一个回明文的出口 (前端"看明文"按钮), 端点侧另有 profile 闸。"""
    mc.add({"name": "mid", "model": "anthropic/x", "api_key": "sk-plain", "base_url": ""})
    assert mc.get_key("mid") == "sk-plain"


def test_get_key_不存在报KeyError(cfg):
    with pytest.raises(KeyError):
        mc.get_key("nope")
