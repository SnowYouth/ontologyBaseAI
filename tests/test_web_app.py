"""test_web_app.py — web 多会话端点测试. mock run_agent + 标题 LLM, 不真跑。

用 monkeypatch 把 _store 指到 tmp_path, 隔离磁盘。
跑法: uv run pytest tests/test_web_app.py -v
"""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个测试独立 tmp_path 会话目录, 清空缓存。"""
    import web.app as webapp
    from core.session_store import SessionStore
    monkeypatch.setattr(webapp, "_store", SessionStore(str(tmp_path)))
    webapp._cache.clear()
    return TestClient(webapp.app)


def _parse_sse(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            out.append(json.loads(block[len("data:"):].strip()))
    return out


# ---------- 会话 CRUD ----------

def test_create_session_返回带id的会话(client):
    resp = client.post("/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body and body["messages"] == []


def test_list_sessions_含新建的会话(client):
    sid = client.post("/sessions").json()["id"]
    metas = client.get("/sessions").json()
    assert any(m["id"] == sid for m in metas)


def test_get_session_返回完整messages(client):
    sid = client.post("/sessions").json()["id"]
    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


def test_get_session_不存在返回404(client):
    resp = client.get("/sessions/nope")
    assert resp.status_code == 404


def test_delete_session_后get返回404(client):
    sid = client.post("/sessions").json()["id"]
    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}").status_code == 404


# ---------- /agent/stream 多会话 ----------

def test_stream_把steps落盘且能读回(client):
    sid = client.post("/sessions").json()["id"]

    def fake_run(message, conversation=None, on_event=None):
        on_event({"type": "tool_start", "name": "rag_query", "args": "{}"})
        on_event({"type": "tool_end", "name": "rag_query", "result": "返回3个chunk",
                  "latency_ms": 10, "success": True})
        on_event({"type": "answer", "text": "答案"})
        conversation.add_user(message)
        conversation.add_assistant("答案")
        return "答案"

    # 标题线程也 mock 掉, 避免真调 LLM
    with patch("web.app.run_agent", side_effect=fake_run), \
         patch("web.app._gen_title_async"):
        resp = client.post("/agent/stream",
                           json={"message": "q1", "session_id": sid})
    assert resp.status_code == 200
    # 读回: assistant 消息带 steps
    msgs = client.get(f"/sessions/{sid}").json()["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["steps"][0]["name"] == "rag_query"


def test_stream_会话不存在返回404(client):
    with patch("web.app.run_agent"):
        resp = client.post("/agent/stream",
                           json={"message": "q", "session_id": "nope"})
    assert resp.status_code == 404


def test_stream_喂给run_agent的历史被剥掉steps(client):
    """关键隔离: 磁盘存 steps, 但喂 LLM 的 conversation 不含 steps。"""
    sid = client.post("/sessions").json()["id"]

    # 先手动塞一条带 steps 的历史进磁盘
    import web.app as webapp
    sess = webapp._store.load(sid)
    sess["messages"] = [
        {"role": "user", "content": "老问题"},
        {"role": "assistant", "content": "老答案",
         "steps": [{"name": "x", "result": "y", "ms": 1, "ok": True}]},
    ]
    webapp._store.save(sid, sess)

    captured = {}
    def fake_run(message, conversation=None, on_event=None):
        captured["history"] = conversation.to_dict()
        return "新答案"

    with patch("web.app.run_agent", side_effect=fake_run), \
         patch("web.app._gen_title_async"):
        client.post("/agent/stream", json={"message": "q2", "session_id": sid})
    # 喂进 run_agent 的历史里, assistant 消息不含 steps 字段
    assistant_msgs = [m for m in captured["history"] if m["role"] == "assistant"]
    assert all("steps" not in m for m in assistant_msgs)


def test_stream_run_agent抛异常推error帧(client):
    sid = client.post("/sessions").json()["id"]
    with patch("web.app.run_agent", side_effect=RuntimeError("boom")), \
         patch("web.app._gen_title_async"):
        resp = client.post("/agent/stream",
                           json={"message": "x", "session_id": sid})
    assert resp.status_code == 200  # SSE 已开流, 错误走 error 帧
    events = _parse_sse(resp.text)
    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["error"]


# ---------- 工具 / MCP 配置管理 ----------

@pytest.fixture
def cfg_client(tmp_path, monkeypatch):
    """在 client 基础上把 toolconfig 配置目录也指到 tmp_path。"""
    import web.app as webapp
    from core.session_store import SessionStore
    from agent.toolconfig import tools as tc_tools
    from agent.toolconfig import mcp_servers as tc_mcp
    monkeypatch.setattr(webapp, "_store", SessionStore(str(tmp_path / "sess")))
    monkeypatch.setattr(tc_tools, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tc_mcp, "_CONFIG_DIR", tmp_path)
    webapp._cache.clear()
    from fastapi.testclient import TestClient
    return TestClient(webapp.app)


def test_get_config_tools_返回注册表全量带enabled(cfg_client):
    body = cfg_client.get("/config/tools").json()
    names = {t["name"] for t in body}
    assert "rag_query" in names       # 真实注册工具
    assert all(t["enabled"] for t in body)   # 缺省全开


def test_put_config_tool_关掉后再get显示false(cfg_client):
    cfg_client.put("/config/tools/rag_query", json={"enabled": False})
    body = cfg_client.get("/config/tools").json()
    row = [t for t in body if t["name"] == "rag_query"][0]
    assert row["enabled"] is False


def test_serving_profile下面板只显示serving工具(cfg_client, monkeypatch):
    # 面板与 agent 共用 profile 门控: serving 下 local 工具(read_file/list_dir)
    # 根本不该出现在管理面板, 否则误导"agent 会用它们"(实际不会)。
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
    body = cfg_client.get("/config/tools").json()
    names = {t["name"] for t in body}
    assert "read_file" not in names
    assert "list_dir" not in names
    assert "rag_query" in names
    assert "execute_sql" in names


def test_serving_profile下手动关掉的serving工具仍在面板(cfg_client, monkeypatch):
    # 关键回归: 面板只过 profile 门控, 不过 tools.json 启停 —— 否则关掉一个
    # serving 工具它就从面板消失、再也打不开。关掉的仍要显示(enabled=false)。
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
    cfg_client.put("/config/tools/rag_query", json={"enabled": False})
    body = cfg_client.get("/config/tools").json()
    row = [t for t in body if t["name"] == "rag_query"]
    assert row and row[0]["enabled"] is False


def test_local_profile下面板显示全部工具(cfg_client, monkeypatch):
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "local")
    body = cfg_client.get("/config/tools").json()
    names = {t["name"] for t in body}
    assert "read_file" in names
    assert "rag_query" in names


def test_get_config_mcp_servers_含默认notes_writer(cfg_client):
    body = cfg_client.get("/config/mcp_servers").json()
    assert any(s["name"] == "notes-writer" for s in body)


def test_post_config_mcp_server_新增可读回(cfg_client):
    cfg_client.post("/config/mcp_servers",
                    json={"name": "s2", "command": "uv", "args": ["x"], "enabled": True})
    names = {s["name"] for s in cfg_client.get("/config/mcp_servers").json()}
    assert "s2" in names


def test_post_config_mcp_server_重名409(cfg_client):
    resp = cfg_client.post("/config/mcp_servers",
                           json={"name": "notes-writer", "command": "x", "args": [], "enabled": True})
    assert resp.status_code == 409


def test_delete_config_mcp_server(cfg_client):
    cfg_client.delete("/config/mcp_servers/notes-writer")
    names = {s["name"] for s in cfg_client.get("/config/mcp_servers").json()}
    assert "notes-writer" not in names


def test_put_config_mcp_server_不存在404(cfg_client):
    resp = cfg_client.put("/config/mcp_servers/nope", json={"enabled": False})
    assert resp.status_code == 404


# ---------- MCP server 的 profile 门控 (2026-10-16 审计第 2 条) ----------

def test_mcp_server_缺省category是local(cfg_client):
    """最小暴露: 没写 category 的 server 按 local 算, serving 下不加载。"""
    body = cfg_client.get("/config/mcp_servers").json()
    row = [s for s in body if s["name"] == "notes-writer"][0]
    assert row["category"] == "local"


def test_serving下local_server_显示启用但active为false(cfg_client, monkeypatch):
    """★ 面板要把"配置启用"和"本轮真加载"分开讲。

    本地工具面板的办法是 serving 下直接不列, 这里不能照搬 —— 不列就没有
    途径把它改成 serving。所以全列 + active 字段。
    """
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
    row = [s for s in cfg_client.get("/config/mcp_servers").json()
           if s["name"] == "notes-writer"][0]
    assert row["enabled"] is True      # 配置上是启用的
    assert row["active"] is False      # 但 serving 下 agent 不会加载它


def test_serving下标serving的server_active为true(cfg_client, monkeypatch):
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "serving")
    cfg_client.put("/config/mcp_servers/notes-writer",
                   json={"enabled": True, "category": "serving"})
    row = [s for s in cfg_client.get("/config/mcp_servers").json()
           if s["name"] == "notes-writer"][0]
    assert row["active"] is True


def test_local下local_server_active为true(cfg_client, monkeypatch):
    """零回归: local profile 下照旧全加载。"""
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "local")
    row = [s for s in cfg_client.get("/config/mcp_servers").json()
           if s["name"] == "notes-writer"][0]
    assert row["active"] is True


def test_关掉的server_active必为false(cfg_client, monkeypatch):
    """active 要同时反映 enabled —— 关掉的 server 在哪个 profile 都不加载。"""
    import core.runtime.agent as agent
    monkeypatch.setattr(agent.settings, "deploy_profile", "local")
    cfg_client.put("/config/mcp_servers/notes-writer", json={"enabled": False})
    row = [s for s in cfg_client.get("/config/mcp_servers").json()
           if s["name"] == "notes-writer"][0]
    assert row["active"] is False


def test_put_省略category不动该字段(cfg_client):
    """★ PATCH 语义: 点开关不许把 category 悄悄洗回 local。

    照 EntityEditIn 那次的教训 (用 `str = "local"` 做默认值, 前端每次保存都
    把字段清空)。这里 category 必须是 `str | None = None`。
    """
    cfg_client.put("/config/mcp_servers/notes-writer",
                   json={"enabled": True, "category": "serving"})
    cfg_client.put("/config/mcp_servers/notes-writer", json={"enabled": False})
    row = [s for s in cfg_client.get("/config/mcp_servers").json()
           if s["name"] == "notes-writer"][0]
    assert row["category"] == "serving", "点开关把 category 洗回了 local"


def test_put_非法category_400(cfg_client):
    resp = cfg_client.put("/config/mcp_servers/notes-writer",
                          json={"enabled": True, "category": "serveing"})
    assert resp.status_code == 400


def test_post_新增server缺省category为local(cfg_client):
    """新增表单不填 category → local (最小暴露, 不是继承 serving)。"""
    cfg_client.post("/config/mcp_servers",
                    json={"name": "s3", "command": "uv", "args": ["x"], "enabled": True})
    row = [s for s in cfg_client.get("/config/mcp_servers").json()
           if s["name"] == "s3"][0]
    assert row["category"] == "local"


# ---------- Skill 配置管理 (只读: 列表/正文/启停) ----------
# 自包含写法: skills 需要在磁盘铺 _SKILLS_DIR 目录结构 (共享 cfg_client 只 patch
# 各 config 的 _CONFIG_DIR, 覆盖不到 skills 的 _SKILLS_DIR), 故各测试自建 TestClient
# 并 monkeypatch skills._CONFIG_DIR / _SKILLS_DIR。

def test_get_skills_列表(monkeypatch, tmp_path):
    """GET /config/skills 返回 [{name, description, enabled}]。"""
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    d = sd / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示技能\n---\n正文", encoding="utf-8")
    monkeypatch.setattr(skills, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.get("/config/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert data == [{"name": "demo", "description": "演示技能", "enabled": True}]


def test_get_skill_body(monkeypatch, tmp_path):
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    d = sd / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: X\n---\n\n正文主体", encoding="utf-8")
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.get("/config/skills/demo")
    assert resp.status_code == 200
    assert "正文主体" in resp.json()["content"]


def test_get_skill_body_不存在404(monkeypatch, tmp_path):
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.get("/config/skills/nope")
    assert resp.status_code == 404


def test_get_skill_full_含frontmatter(monkeypatch, tmp_path):
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    d = sd / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: X\n---\n\n正文主体", encoding="utf-8")
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.get("/config/skills/demo/full")
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert content.startswith("---")          # 含 frontmatter
    assert "description: X" in content
    assert "正文主体" in content


def test_get_skill_full_不存在404(monkeypatch, tmp_path):
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.get("/config/skills/nope/full")
    assert resp.status_code == 404


def test_put_skill_enabled(monkeypatch, tmp_path):
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    monkeypatch.setattr(skills, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.put("/config/skills/demo", json={"enabled": False})
    assert resp.status_code == 200
    assert skills.load() == {"demo": {"enabled": False}}


def test_put_skill_不存在的name也返回200_宽松语义(monkeypatch, tmp_path):
    """决策守卫: PUT 沿袭 tools 宽松语义, 不校验存在性 (死配置由 list_status 忽略)。"""
    from agent.toolconfig import skills
    from web.app import app
    from fastapi.testclient import TestClient

    sd = tmp_path / "skills"
    sd.mkdir()
    monkeypatch.setattr(skills, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(skills, "_SKILLS_DIR", sd)

    client = TestClient(app)
    resp = client.put("/config/skills/ghost", json={"enabled": False})
    assert resp.status_code == 200
    # 死配置写下了, 但 list_status 以 discover() 为准 → 不列 ghost
    assert client.get("/config/skills").json() == []


# ---------- Skill CRUD 端点 ----------

@pytest.fixture
def skill_client(tmp_path, monkeypatch):
    """隔离 skills 目录的 client。"""
    import web.app as webapp
    from core.session_store import SessionStore
    from agent.toolconfig import skills as skmod
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    monkeypatch.setattr(skmod, "_SKILLS_DIR", sk_dir)
    monkeypatch.setattr(skmod, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_store", SessionStore(str(tmp_path / "sess")))
    webapp._cache.clear()
    return TestClient(webapp.app), sk_dir


_MD = "---\nname: viaweb\ndescription: 经由web\n---\n\n正文"


def test_post_skill_新建成功(skill_client):
    client, sk_dir = skill_client
    resp = client.post("/config/skills", json={"name": "viaweb", "content": _MD})
    assert resp.status_code == 200
    assert (sk_dir / "viaweb" / "SKILL.md").exists()


def test_post_skill_非法name返回400(skill_client):
    client, _ = skill_client
    resp = client.post("/config/skills", json={"name": "../evil", "content": _MD})
    assert resp.status_code == 400


def test_post_skill_frontmatter坏返回400(skill_client):
    client, _ = skill_client
    resp = client.post("/config/skills", json={"name": "bad", "content": "裸文本"})
    assert resp.status_code == 400


def test_put_body_编辑成功(skill_client):
    client, sk_dir = skill_client
    client.post("/config/skills", json={"name": "e", "content":
        "---\nname: e\ndescription: d\n---\n老"})
    resp = client.put("/config/skills/e/body", json={"content":
        "---\nname: e\ndescription: d\n---\n新"})
    assert resp.status_code == 200
    assert "新" in (sk_dir / "e" / "SKILL.md").read_text(encoding="utf-8")


def test_put_body_不存在返回404(skill_client):
    client, _ = skill_client
    resp = client.put("/config/skills/nope/body", json={"content": _MD})
    assert resp.status_code == 404


def test_delete_skill_成功后目录没了(skill_client):
    client, sk_dir = skill_client
    client.post("/config/skills", json={"name": "d", "content":
        "---\nname: d\ndescription: x\n---\n正文"})
    resp = client.delete("/config/skills/d")
    assert resp.status_code == 200
    assert not (sk_dir / "d").exists()


def test_delete_skill_不存在返回404(skill_client):
    client, _ = skill_client
    resp = client.delete("/config/skills/nope")
    assert resp.status_code == 404


# ---------- filter_rule 校验历史落盘 (Task 3) ----------

def test_flatten_checks_三态与detail拼接():
    from web.app import _flatten_check_result
    vr = {
        "entity": "lib_reader_order", "col": "REC_STATUS", "op": "=", "value": "1000",
        "checks": [
            {"name": "column_exists", "ok": True, "detail": ""},
            {"name": "value_in_enum", "ok": True, "skipped": True, "detail": "该字段无枚举登记, 跳过值校验"},
            {"name": "sql_runs", "ok": False, "detail": "OperationalError: x"},
        ],
        "ok": False,
    }
    row = _flatten_check_result(vr)
    assert row["entity"] == "lib_reader_order"
    assert row["col"] == "REC_STATUS"
    assert row["column_exists"] == 1
    assert row["value_in_enum"] is None          # skipped → None
    assert row["sql_runs"] == 0
    assert "OperationalError" in row["detail"]    # 红灯 detail 进拼接


def test_flatten_checks_value_in_enum不通过存0():
    from web.app import _flatten_check_result
    vr = {
        "entity": "e", "col": "c", "op": "=", "value": "bad",
        "checks": [
            {"name": "column_exists", "ok": True, "detail": ""},
            {"name": "value_in_enum", "ok": False, "detail": "值 ['bad'] 不在枚举码值内"},
            {"name": "sql_runs", "ok": True, "detail": ""},
        ],
        "ok": False,
    }
    row = _flatten_check_result(vr)
    assert row["value_in_enum"] == 0             # 非 skipped 且 ok=False → 0


def test_validate端点落盘并可回读(monkeypatch, tmp_path):
    import web.app as app_mod
    from capabilities.ontology.ontology_store import OntologyStore

    # 造一个带 filter_rules 的假单例
    class _FakeOnto:
        filter_rules = {"lib_reader_order": [
            {"col": "REC_STATUS", "op": "=", "value": "1000", "mode": "hard", "origin": "manual"},
        ]}
        entities = {"lib_reader_order": {"tables": ["lib_reader_order"], "data_source": "lib"}}
    monkeypatch.setattr(app_mod, "_ontology_or_404", lambda: _FakeOnto())

    # 校验逐条替换成固定绿灯 (隔离真库)
    monkeypatch.setattr(app_mod, "_validate_one_rule", lambda rule, onto, cache: {
        "entity": rule["entity"], "col": rule["col"], "op": rule["op"], "value": rule.get("value"),
        "checks": [
            {"name": "column_exists", "ok": True, "detail": ""},
            {"name": "value_in_enum", "ok": True, "skipped": True, "detail": ""},
            {"name": "sql_runs", "ok": True, "detail": ""},
        ], "ok": True,
    })

    # 落盘走临时库
    db = str(tmp_path / "onto.db")
    OntologyStore(db).close()   # 建 schema
    monkeypatch.setattr(app_mod.settings, "obda_ontology_path", db)

    out = app_mod.post_validate_filter_rules()
    assert len(out["results"]) == 1
    assert "run_id" in out

    hist = app_mod.get_filter_rule_check_history(limit=2)
    assert len(hist["runs"]) == 1
    assert hist["runs"][0]["results"][0]["ok"] is True
    assert hist["runs"][0]["results"][0]["value_in_enum"] is None
