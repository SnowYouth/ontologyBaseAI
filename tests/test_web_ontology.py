"""test_web_ontology.py — 本体后端接口 (Task4). 读接口从单例, 写接口落 SQLite + 热重载.

隔离: 每个测试用 tmp_path 建一个新 .db, monkeypatch settings.obda_ontology_path 指过去,
播种最小本体 → set_ontology 建单例 → TestClient。写接口改库后应能立刻从读接口看到。
跑法: uv run pytest tests/test_web_ontology.py -v
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import web.app as webapp
    from core.config import settings
    from core.ontology_view import set_ontology
    from capabilities.ontology.ontology_store import OntologyStore

    db_path = str(tmp_path / "onto.db")
    store = OntologyStore(db_path)
    store.upsert_domain({"code": "ORDER", "color": "#f00", "displayOrder": 1})
    store.upsert_domain({"code": "PRODUCT", "color": "#0f0", "displayOrder": 2})
    store.upsert_entity({
        "id": "order", "displayName": "订单", "domain": "ORDER",
        "maps_to": "t_order", "tables": ["t_order"], "attributes": [], "summary": "订单实体",
        "recallKeywords": ["订单号"],
    })
    store.upsert_entity({
        "id": "product", "displayName": "产品", "domain": "PRODUCT",
        "maps_to": "t_product", "tables": ["t_product"], "attributes": [], "summary": "产品实体",
    })
    store.upsert_relation({
        "source": "order", "target": "product", "type": "MANY_TO_ONE", "via": "order_product",
        "label": "订购", "source_col": "holding_id", "target_col": "id", "bridge_count": 5,
        "crossDomain": True,   # _RELATION_FIXED 之外 → 进 extra_json (跨域断腿信号, flip 不能抹)
    })
    store.upsert_enum_value("order", "REC_STATUS", {
        "name": "订单状态", "spec_ref": "LIB-ORD-A",
        "codes": [{"code": "1", "name": "新建", "desc": ""},
                  {"code": "2", "name": "完成", "desc": ""}],
    })
    store.upsert_filter_rule({
        "entity": "order", "col": "REC_STATUS", "op": "=", "value": "1",
        "mode": "hard", "origin": "source_code",
    })
    store.close()

    # ★ 双保险: 同时 patch 当前 core.config.settings 和 web.app 模块级绑定。
    # test_llm.py 的 importlib.reload(core.config) 会换 settings 对象——若本文件
    # 在 reload 后跑, from core.config import settings 拿到新对象, 而 web.app
    # 早前绑定的还是旧对象 → patch 落在新对象上, 写接口读旧对象指错库 (存量隔离 bug,
    # 在 test_web_selfevolve.py 改变 import 顺序后显形)。
    monkeypatch.setattr(settings, "obda_ontology_path", db_path)
    if webapp.settings is not settings:
        monkeypatch.setattr(webapp, "settings", settings)
    set_ontology(db_path)
    return TestClient(webapp.app)


# ---------- 读接口 ----------

# /ontology/graph 已于 2026-09-21 接上前端消费方
# (frontend/src/components/GraphPanel.tsx, 本体视图 "网络图" tab)。
# /ontology/catalogs 仍无消费方。
#
# 此处原有一段注释警告 "这两个端点写好没调用 + 测试替它站岗", 提醒别把
# 下面几个绿灯误判成 "图功能已交付" —— graph 那半边现已不成立, 故改写。
# catalogs 那半边仍然成立: 它的测试目前仍在替一个无人消费的端点站岗。
#
# 注意区分: 下面 test_graph_* / test_catalogs_* 是把端点当**功能**测;
# 而 CRUD 热生效那几组 (test_*_热生效 等) 也 GET /ontology/graph, 但那是拿它
# 当**读回探针**验证写接口生效, 属正当用途。

def test_graph_返回节点边和域(client):
    body = client.get("/ontology/graph").json()
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {"order", "product"}
    assert any(e["source"] == "order" and e["target"] == "product" for e in body["edges"])
    assert {d["code"] for d in body["domains"]} == {"ORDER", "PRODUCT"}


def test_graph_节点带域标签(client):
    body = client.get("/ontology/graph").json()
    order = next(n for n in body["nodes"] if n["id"] == "order")
    assert order["domain"] == "ORDER"
    assert order["displayName"] == "订单"


def test_graph_节点带数据源(client):
    """节点要带 data_source: 前端网络图靠它判跨库边 (画虚线)。

    跨库边不能拼进一条 SQL, 在图上必须与库内边视觉区分 —— 而 domain 判不出
    跨库 (读者账户域既是一个域也恰好是另一个库, 靠域名判是巧合不是判据)。
    """
    body = client.get("/ontology/graph").json()
    assert all("dataSource" in n for n in body["nodes"])


def test_domains_返回域列表(client):
    body = client.get("/ontology/domains").json()
    assert {d["code"] for d in body} == {"ORDER", "PRODUCT"}


def test_catalogs_返回检索卡片(client):
    body = client.get("/ontology/catalogs").json()
    assert {c["id"] for c in body} == {"order", "product"}
    assert all("displayName" in c for c in body)


def test_catalogs_keywords读recallKeywords(client):
    """catalogs 占位端点的 keywords 字段读新键 recallKeywords (fixture 里 order 带 ["订单号"])。"""
    body = client.get("/ontology/catalogs").json()
    order = next(c for c in body if c["id"] == "order")
    assert order["keywords"] == ["订单号"]


# ---------- 写接口 (CRUD + 热生效) ----------

def test_post_entity_新增后图里可见(client):
    resp = client.post("/ontology/entity", json={
        "id": "invoice", "displayName": "发票", "domain": "ORDER",
        "maps_to": "t_invoice", "tables": ["t_invoice"], "attributes": [], "summary": "",
    })
    assert resp.status_code == 200
    ids = {n["id"] for n in client.get("/ontology/graph").json()["nodes"]}
    assert "invoice" in ids


def test_put_entity_改displayName后热生效(client):
    client.put("/ontology/entity/order", json={
        "id": "order", "displayName": "订单改名", "domain": "ORDER",
        "maps_to": "t_order", "tables": ["t_order"], "attributes": [], "summary": "",
    })
    order = next(n for n in client.get("/ontology/graph").json()["nodes"] if n["id"] == "order")
    assert order["displayName"] == "订单改名"


def test_delete_entity_删除后图里消失(client):
    assert client.delete("/ontology/entity/product").status_code == 200
    ids = {n["id"] for n in client.get("/ontology/graph").json()["nodes"]}
    assert "product" not in ids


def test_delete_entity_端点级联删相关边(client):
    for eid in ("EA", "EB"):
        client.post("/ontology/entity", json={"id": eid, "displayName": eid})
    client.post("/ontology/relation",
                json={"source": "EA", "target": "EB", "source_col": "x", "target_col": "y"})

    resp = client.delete("/ontology/entity/EA")
    assert resp.status_code == 200

    ent_ids = {e["id"] for e in client.get("/ontology/entities").json()}
    assert "EA" not in ent_ids
    edges = {(r["source"], r["target"], r["via"])
             for r in client.get("/ontology/relations").json()}
    assert ("EA", "EB", "EA.x=EB.y") not in edges


def test_post_domain_新增域后可见(client):
    client.post("/ontology/domain", json={"code": "ACQUISITION", "color": "#00f", "displayOrder": 3})
    codes = {d["code"] for d in client.get("/ontology/domains").json()}
    assert "ACQUISITION" in codes


def test_delete_domain_删除后消失(client):
    assert client.delete("/ontology/domain/PRODUCT").status_code == 200
    codes = {d["code"] for d in client.get("/ontology/domains").json()}
    assert "PRODUCT" not in codes


def test_delete_domain_挂靠实体归未分类(client):
    resp = client.delete("/ontology/domain/ORDER")
    assert resp.status_code == 200
    codes = {d["code"] for d in client.get("/ontology/domains").json()}
    assert "ORDER" not in codes                       # 域没了
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["domain"] == ""                           # 实体归未分类


def test_post_relation_新增边后图里可见(client):
    client.post("/ontology/relation", json={
        "source": "product", "target": "order", "type": "MANY_TO_ONE",
        "label": "反向", "source_col": "id", "target_col": "holding_id",
    })
    edges = client.get("/ontology/graph").json()["edges"]
    assert any(e["via"] == "product.id=order.holding_id" for e in edges)


def test_post_relation_非法type_422(client):
    resp = client.post("/ontology/relation", json={
        "source": "product", "target": "order",
        "source_col": "id", "target_col": "holding_id", "type": "BRIDGE",
    })
    assert resp.status_code == 422


def test_post_relation_col空_422(client):
    resp = client.post("/ontology/relation", json={
        "source": "product", "target": "order",
        "source_col": "", "target_col": "holding_id", "type": "MANY_TO_ONE",
    })
    assert resp.status_code == 422


def test_post_relation_默认type为unknown(client):
    client.post("/ontology/relation", json={
        "source": "product", "target": "order",
        "source_col": "id", "target_col": "holding_id",
    })
    r = [x for x in client.get("/ontology/relations").json()
         if x["via"] == "product.id=order.holding_id"][0]
    assert r["type"] == "UNKNOWN"


def test_post_relation_origin默认manual落库(client):
    client.post("/ontology/relation", json={
        "source": "product", "target": "order",
        "source_col": "id2", "target_col": "holding_id2", "type": "MANY_TO_ONE",
    })
    # origin 在 extra_json, 经 load_raw 平铺进关系 dict
    r = [x for x in client.get("/ontology/relations").json()
         if x["via"] == "product.id2=order.holding_id2"][0]
    assert r.get("origin") == "manual"


def test_post_relation_非法origin_422(client):
    resp = client.post("/ontology/relation", json={
        "source": "product", "target": "order",
        "source_col": "id3", "target_col": "holding_id3",
        "type": "MANY_TO_ONE", "origin": "garbage",
    })
    assert resp.status_code == 422


def test_delete_relation_删除后图里消失(client):
    resp = client.delete("/ontology/relation", params={
        "source": "order", "target": "product",
        "source_col": "holding_id", "target_col": "id",
    })
    assert resp.status_code == 200
    edges = client.get("/ontology/graph").json()["edges"]
    assert not any(e["via"] == "order.holding_id=product.id" for e in edges)


# ---------- 全字段读接口 (Task2: 编辑器只读区血肉/JOIN字段对/枚举码值) ----------

def test_entities_返回全字段含血肉(client):
    body = client.get("/ontology/entities").json()
    order = next(e for e in body if e["id"] == "order")
    assert order["displayName"] == "订单"
    assert "attributes" in order          # 只读区结构体在
    assert order.get("recallKeywords") == ["订单号"]  # extra_json 血肉在


def test_relations_返回JOIN字段对且去重(client):
    body = client.get("/ontology/relations").json()
    edges = [e for e in body if e["via"] == "order.holding_id=product.id"]
    assert len(edges) == 1                 # 双向记但去重后只 1 条
    assert edges[0]["source_col"] == "holding_id"
    assert edges[0]["label"] == "订购"


def test_enums_返回码表列表(client):
    body = client.get("/ontology/enums").json()
    ev = next(e for e in body if e["entity"] == "order" and e["field"] == "REC_STATUS")
    assert ev["name"] == "订单状态"
    assert {c["code"] for c in ev["codes"]} == {"1", "2"}


def test_get_filter_rules_返回规则列表(client):
    rules = client.get("/filter-rules").json()["rules"]
    assert any(r["entity"] == "order" and r["col"] == "REC_STATUS"
               and r["mode"] == "hard" for r in rules)


def test_post_filter_rule_新增后可见(client):
    resp = client.post("/filter-rules", json={
        "entity": "product", "col": "ON_SHELF", "op": "=", "value": "Y",
        "mode": "soft", "origin": "manual",
    })
    assert resp.status_code == 200
    rules = client.get("/filter-rules").json()["rules"]
    assert any(r["entity"] == "product" and r["col"] == "ON_SHELF" for r in rules)


def test_post_filter_rule_非法mode_422不落库(client):
    resp = client.post("/filter-rules", json={
        "entity": "product", "col": "X", "op": "=", "value": "1",
        "mode": "bogus", "origin": "manual",
    })
    assert resp.status_code == 422
    rules = client.get("/filter-rules").json()["rules"]
    assert not any(r["col"] == "X" for r in rules)


def test_post_filter_rule_非法op_422(client):
    resp = client.post("/filter-rules", json={
        "entity": "product", "col": "X", "op": "LIKE", "value": "1",
        "mode": "hard", "origin": "manual",
    })
    assert resp.status_code == 422


def test_delete_filter_rule_软删后消失(client):
    # fixture 已播种 order/REC_STATUS/=/1
    resp = client.request("DELETE", "/filter-rules", json={
        "entity": "order", "col": "REC_STATUS", "op": "=", "value": "1",
    })
    assert resp.status_code == 200
    rules = client.get("/filter-rules").json()["rules"]
    assert not any(r["entity"] == "order" and r["col"] == "REC_STATUS" for r in rules)


# ---------- POST /filter-rules/validate 一键校验 (Task5: 注入隔离, 不连真库) ----------

def test_validate_全绿_列在值在SQL通(client, monkeypatch):
    import web.app as webapp
    monkeypatch.setattr(webapp, "list_table_columns",
                        lambda table, source: {"REC_STATUS", "MEMBER_ID"})
    monkeypatch.setattr(webapp, "execute_sql",
                        lambda sql, entity=None, source=None: {"rows": [], "n": 0})
    body = client.post("/filter-rules/validate").json()
    r = next(x for x in body["results"] if x["col"] == "REC_STATUS")
    assert r["ok"] is True
    assert all(c["ok"] for c in r["checks"])


def test_validate_列不存在_红灯(client, monkeypatch):
    import web.app as webapp
    monkeypatch.setattr(webapp, "list_table_columns",
                        lambda table, source: {"OTHER_COL"})   # REC_STATUS 不在
    monkeypatch.setattr(webapp, "execute_sql",
                        lambda sql, entity=None, source=None: {"rows": [], "n": 0})
    body = client.post("/filter-rules/validate").json()
    r = next(x for x in body["results"] if x["col"] == "REC_STATUS")
    assert r["ok"] is False
    assert any(c["name"] == "column_exists" and not c["ok"] for c in r["checks"])


def test_validate_SQL报错_红灯(client, monkeypatch):
    import web.app as webapp
    monkeypatch.setattr(webapp, "list_table_columns",
                        lambda table, source: {"REC_STATUS"})
    monkeypatch.setattr(webapp, "execute_sql",
                        lambda sql, entity=None, source=None: {"error": "Unknown column"})
    body = client.post("/filter-rules/validate").json()
    r = next(x for x in body["results"] if x["col"] == "REC_STATUS")
    assert any(c["name"] == "sql_runs" and not c["ok"] for c in r["checks"])


def test_validate_取列异常_兜底红灯不抛500(client, monkeypatch):
    import web.app as webapp
    def boom(table, source): raise RuntimeError("table gone")
    monkeypatch.setattr(webapp, "list_table_columns", boom)
    monkeypatch.setattr(webapp, "execute_sql",
                        lambda sql, entity=None, source=None: {"rows": []})
    resp = client.post("/filter-rules/validate")
    assert resp.status_code == 200
    r = next(x for x in resp.json()["results"] if x["col"] == "REC_STATUS")
    assert r["ok"] is False
    assert any(c["name"] == "table_access" and not c["ok"] for c in r["checks"])


# ---------- entity PUT load-merge-write (Task3: 防抹血肉) ----------

def test_put_entity_只改标量不抹血肉(client):
    # order 实体在 fixture 里带 recallKeywords 血肉 (Task 2 已补)
    resp = client.put("/ontology/entity/order", json={
        "displayName": "订单改名", "domain": "PRODUCT",
        "summary": "新摘要", "maps_to": "t_order", "tables": ["t_order"],
    })
    assert resp.status_code == 200
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["displayName"] == "订单改名"     # 可编字段变了
    assert e["domain"] == "PRODUCT"
    assert e.get("recallKeywords") == ["订单号"]  # 血肉没被抹


def test_put_entity_不存在返回404(client):
    resp = client.put("/ontology/entity/nope", json={
        "displayName": "x", "domain": "", "summary": "", "maps_to": "", "tables": [],
    })
    assert resp.status_code == 404


# ---------- data_source 可编辑 (开源发布 Task 12) ----------

def test_post_entity_可指定data_source(client):
    """POST 新建实体带 data_source → 落库且 GET 读回一致。"""
    resp = client.post("/ontology/entity", json={
        "id": "invoice2", "displayName": "发票2", "domain": "ORDER",
        "maps_to": "t_invoice2", "tables": ["t_invoice2"], "attributes": [], "summary": "",
        "data_source": "lib_x",
    })
    assert resp.status_code == 200
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "invoice2")
    assert e["data_source"] == "lib_x"


def test_put_entity_可修改data_source(client):
    """PUT 是前端唯一编辑路径, 带 data_source 必须能把旧值改掉。"""
    resp = client.put("/ontology/entity/order", json={
        "displayName": "订单", "domain": "ORDER", "summary": "订单实体",
        "maps_to": "t_order", "tables": ["t_order"],
        "data_source": "lib_y",
    })
    assert resp.status_code == 200
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["data_source"] == "lib_y"


def test_put_entity_不传data_source则不动(client):
    """默认值陷阱守护: EntityEditIn.data_source 必须是 None=不动语义,
    否则前端 Task13 之前的窗口期每次保存都会把 data_source 静默清空。"""
    client.put("/ontology/entity/order", json={
        "displayName": "订单", "domain": "ORDER", "summary": "订单实体",
        "maps_to": "t_order", "tables": ["t_order"],
        "data_source": "lib_z",
    })
    # 再 PUT 一次, 只改 displayName, 不带 data_source
    resp = client.put("/ontology/entity/order", json={
        "displayName": "订单改名2", "domain": "ORDER", "summary": "订单实体",
        "maps_to": "t_order", "tables": ["t_order"],
    })
    assert resp.status_code == 200
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["displayName"] == "订单改名2"
    assert e["data_source"] == "lib_z"   # 没被清空


def test_get_config_sources_返回源清单(client, monkeypatch):
    """GET /config/sources 给前端下拉提供本机 DB_SOURCES 配的源名列表。"""
    import web.app as webapp
    from core.config import DbSourceConfig

    fake_sources = {
        "lib": DbSourceConfig(host="h", port=3306, user="u",
                              password="p", dbname="lib", dialect="mysql"),
        "crm": DbSourceConfig(host="h2", port=1521, user="u2",
                              password="p2", dbname="crm", dialect="oracle"),
    }
    monkeypatch.setattr(webapp.settings, "db_sources", fake_sources)
    resp = client.get("/config/sources")
    assert resp.status_code == 200
    assert set(resp.json()["sources"]) == {"lib", "crm"}


def test_post_entity_已存在id返回409且不抹血肉(client):
    # POST /ontology/entity 命中已存在 id 不能整行 upsert (会抹 attributes/extra_json 血肉,
    # 是 T3 防抹血肉哲学的另一入口)。已存在 → 409, 改标量走 PUT load-merge。
    resp = client.post("/ontology/entity", json={
        "id": "order", "displayName": "覆盖", "domain": "PRODUCT",
        "maps_to": "x", "tables": [], "attributes": [], "summary": "",
    })
    assert resp.status_code == 409
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["displayName"] == "订单"              # 原标量没被覆盖
    assert e.get("recallKeywords") == ["订单号"]  # 血肉没被抹


# ---------- enum_value 全套端点 (Task5: POST upsert + DELETE 软删) ----------

def test_post_enum_新增码表后可读(client):
    resp = client.post("/ontology/enum", json={
        "entity": "product", "field": "ITEM_STATE", "name": "产品状态",
        "spec_ref": "LIB-P-A",
        "codes": [{"code": "A", "name": "在用", "desc": ""}],
    })
    assert resp.status_code == 200
    ev = next(e for e in client.get("/ontology/enums").json()
              if e["entity"] == "product" and e["field"] == "ITEM_STATE")
    assert ev["name"] == "产品状态"
    assert ev["codes"][0]["code"] == "A"


def test_post_enum_改码值条数(client):
    # order/REC_STATUS 在 fixture 里 2 条码值, 覆盖成 3 条
    client.post("/ontology/enum", json={
        "entity": "order", "field": "REC_STATUS", "name": "订单状态", "spec_ref": "LIB-ORD-A",
        "codes": [{"code": "1", "name": "新建", "desc": ""},
                  {"code": "2", "name": "完成", "desc": ""},
                  {"code": "3", "name": "取消", "desc": ""}],
    })
    ev = next(e for e in client.get("/ontology/enums").json()
              if e["entity"] == "order" and e["field"] == "REC_STATUS")
    assert len(ev["codes"]) == 3


def test_delete_enum_软删后消失(client):
    resp = client.delete("/ontology/enum", params={"entity": "order", "field": "REC_STATUS"})
    assert resp.status_code == 200
    keys = {(e["entity"], e["field"]) for e in client.get("/ontology/enums").json()}
    assert ("order", "REC_STATUS") not in keys


# ---------- relation 改方向 (Task6: 删旧建新一操作内) ----------

def test_relation_flip_删旧建新(client):
    # fixture 有 order→product (source_col=holding_id, target_col=id → via 派生 order.holding_id=product.id)
    resp = client.post("/ontology/relation/flip", json={
        "source": "order", "target": "product",
        "source_col": "holding_id", "target_col": "id",
    })
    assert resp.status_code == 200
    rels = client.get("/ontology/relations").json()
    fwd = [r for r in rels if r["source"] == "order" and r["target"] == "product"
           and r["via"] == "order.holding_id=product.id"]
    rev = [r for r in rels if r["source"] == "product" and r["target"] == "order"
           and r["via"] == "product.id=order.holding_id"]
    assert fwd == []                        # 旧边没了 (软删, 读接口只取存活)
    assert len(rev) == 1                    # 新反向边在 (via 翻转后派生)
    assert rev[0]["source_col"] == "id"     # col 也互换 (旧 target_col)
    assert rev[0]["target_col"] == "holding_id"
    assert rev[0]["label"] == "订购"         # label 保留
    # extra_json 血肉 (跨域断腿信号) flip 后不能被抹。读接口按白名单裁字段拿不到,
    # 直接查库 load_raw (即喂 _collect_notes_for 的路径) 验证新反向边仍带 crossDomain。
    from core.config import settings
    from capabilities.ontology.ontology_store import OntologyStore
    st = OntologyStore(settings.obda_ontology_path)
    try:
        rels_raw = st.load_raw()["relationships"]
    finally:
        st.close()
    rev_raw = [r for r in rels_raw if r["source"] == "product" and r["target"] == "order"
               and r["via"] == "product.id=order.holding_id"]
    assert len(rev_raw) == 1
    assert rev_raw[0].get("crossDomain") is True   # extra 键原样保留


# ---------- entity extra_json 可编辑 (本轮新增) ----------

def _seed_flesh_entity(db_path):
    """补种一个含 2 血肉键 + 一个白名单外旧键的实体, 供 extra 用例断言。"""
    from capabilities.ontology.ontology_store import OntologyStore
    s = OntologyStore(db_path)
    s.upsert_entity({
        "id": "e1", "entityCode": "E1", "displayName": "实体1", "domain": "ORDER",
        "maps_to": "", "tables": ["t1"],
        "attributes": [{"name": "F1", "kind": "pk", "type": "bigint", "desc": "血肉"}],
        "summary": "s",
        "recallKeywords": ["k1", "k2", "场景A"],
        "disambiguateFrom": ["排除B"],
        "legacyGhost": "白名单外旧键",
    })
    s.close()


def _load_e1(db_path):
    from capabilities.ontology.ontology_store import OntologyStore
    s = OntologyStore(db_path)
    try:
        return s.load_entity("e1")
    finally:
        s.close()


def test_put_extra_两键落库_attributes不变(client, tmp_path):
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
        "extra": {
            "recallKeywords": ["k1", "k2", "k3"],
            "disambiguateFrom": ["排除B"],
        },
    })
    assert resp.status_code == 200
    e = _load_e1(db)
    assert e["recallKeywords"] == ["k1", "k2", "k3"]
    assert e["disambiguateFrom"] == ["排除B"]
    assert e["attributes"][0]["name"] == "F1" and e["attributes"][0]["desc"] == "血肉"  # 防抹血肉


def test_put_extra_旧键cocVerified被拒422(client, tmp_path):
    """(422 防回潮) cocVerified 已于 2026-08-05 废除; 再传该键应被 extra=forbid 拒绝。"""
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
        "extra": {"cocVerified": True},
    })
    assert resp.status_code == 422


def test_put_extra_旧键inSpec被拒422(client, tmp_path):
    """(422 防回潮) inSpec(实体级) 已于 2026-08-05 废除; 再传该键应被 extra=forbid 拒绝。"""
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
        "extra": {"inSpec": False},
    })
    assert resp.status_code == 422


def test_put_extra_白名单外key_422(client, tmp_path):
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
        "extra": {"bogusKey": "x"},
    })
    assert resp.status_code == 422       # extra="forbid" (纯白名单外键, 归因唯一)


def test_put_extra_query_hints已废除返回422(client, tmp_path):
    """query_hints 字段已废除; 再传该键应被 extra=forbid 拒绝 (防回潮)。"""
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
        "extra": {"query_hints": ["x"]},
    })
    assert resp.status_code == 422


def test_put_extra_清除白名单外旧键(client, tmp_path):
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
        "extra": {"recallKeywords": ["k1"]},
    })
    assert resp.status_code == 200
    e = _load_e1(db)
    assert "legacyGhost" not in e        # 封闭语义: 白名单外旧键被清


def test_put_不带extra_血肉一字不动(client, tmp_path):
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1改", "domain": "ORDER", "summary": "s", "maps_to": "", "tables": ["t1"],
    })
    assert resp.status_code == 200
    e = _load_e1(db)
    assert e["displayName"] == "实体1改"
    assert e["recallKeywords"] == ["k1", "k2", "场景A"]              # 不带 extra → 血肉原样
    assert e["disambiguateFrom"] == ["排除B"]
    assert e["legacyGhost"] == "白名单外旧键"                         # 白名单外键也留


def test_put_extra_旧键identityKeywords被拒(client, tmp_path):
    """(422 路径) 旧键彻底废除: PUT payload 带 identityKeywords → extra=forbid 拒。"""
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s",
        "maps_to": "", "tables": ["t1"],
        "extra": {"identityKeywords": ["x"]},
    })
    assert resp.status_code == 422


def test_put_extra_旧键scenarioKeywords被拒(client, tmp_path):
    """(422 路径) 旧键 scenarioKeywords 带进 PUT payload → 422。"""
    db = str(tmp_path / "onto.db")
    _seed_flesh_entity(db)
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s",
        "maps_to": "", "tables": ["t1"],
        "extra": {"scenarioKeywords": {"includes": ["x"], "excludes": []}},
    })
    assert resp.status_code == 422


def test_put_extra_库里残留旧键_带新extra保存后被重建清除(client, tmp_path):
    """(静默清除路径) fixture 预置旧键 (模拟未迁移库残留), PUT 带合法新 extra
    → load-merge-write 封闭语义清掉所有非固定键再按新白名单重建, 旧键消失、新键落库。"""
    from capabilities.ontology.ontology_store import OntologyStore
    db = str(tmp_path / "onto.db")
    s = OntologyStore(db)
    s.upsert_entity({
        "id": "e1", "entityCode": "E1", "displayName": "实体1", "domain": "ORDER",
        "maps_to": "", "tables": ["t1"],
        "attributes": [{"name": "F1", "kind": "pk", "type": "bigint", "desc": "血肉"}],
        "summary": "s",
        "identityKeywords": ["旧残留"],                              # 未迁移库里的旧键
        "scenarioKeywords": {"includes": ["旧"], "excludes": ["旧E"]},
    })
    s.close()
    resp = client.put("/ontology/entity/e1", json={
        "displayName": "实体1", "domain": "ORDER", "summary": "s",
        "maps_to": "", "tables": ["t1"],
        "extra": {"recallKeywords": ["新召回"], "disambiguateFrom": ["新消歧"]},
    })
    assert resp.status_code == 200
    e = _load_e1(db)
    assert e["recallKeywords"] == ["新召回"]
    assert e["disambiguateFrom"] == ["新消歧"]
    assert "identityKeywords" not in e     # 旧键被封闭语义清除
    assert "scenarioKeywords" not in e


# ---------- attributes 编辑 (整列表替换 + 9 键白名单 + hasEnum 硬校验) ----------

def _put_order_attrs(client, attrs):
    """order 实体标量原样 + attributes 整列表, 少打样板。"""
    return client.put("/ontology/entity/order", json={
        "displayName": "订单", "domain": "ORDER", "summary": "订单实体",
        "maps_to": "t_order", "tables": ["t_order"], "attributes": attrs,
    })


def test_put_attributes_整列表替换且省缺省(client):
    resp = _put_order_attrs(client, [{
        "name": "REC_STATUS", "kind": "enum", "type": "varchar",
        "desc": "订单状态", "desc_source": "code",
        "hasEnum": True,
    }])
    assert resp.status_code == 200
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    # 恒写 name/kind/type/desc_source; 有值的写; 空串/None 的键 (disambiguation/sensitive) 不落
    assert e["attributes"] == [{
        "name": "REC_STATUS", "kind": "enum", "type": "varchar", "desc": "订单状态",
        "desc_source": "code",
        "hasEnum": True,
    }]


def test_put_attributes_旧键valueSet被拒422(client):
    """(422 防回潮) valueSet 已于 2026-08-05 废除; 属性带该键 → AttributeIn extra=forbid 拒。"""
    resp = _put_order_attrs(client, [{
        "name": "REC_STATUS", "kind": "enum", "type": "varchar", "valueSet": "LIB-ORD-A",
    }])
    assert resp.status_code == 422


def test_put_attributes_旧键inSpec被拒422(client):
    """(422 防回潮) inSpec(属性级) 已于 2026-08-05 废除; 属性带该键 → 422。"""
    resp = _put_order_attrs(client, [{
        "name": "REC_STATUS", "kind": "enum", "type": "varchar", "inSpec": True,
    }])
    assert resp.status_code == 422


def test_put_attributes_旧键note被拒422(client):
    """(422 防回潮) note 已于 2026-08-05 废除; 属性带该键 → 422。"""
    resp = _put_order_attrs(client, [{
        "name": "REC_STATUS", "kind": "enum", "type": "varchar", "note": "一些备注",
    }])
    assert resp.status_code == 422


def test_put_attributes_旧键_valueName被拒422(client):
    """(422 防回潮) _valueName 已于 2026-08-05 废除; 属性带该键 → AttributeIn extra=forbid 拒。"""
    resp = _put_order_attrs(client, [{
        "name": "REC_STATUS", "kind": "enum", "type": "varchar", "_valueName": "订单状态",
    }])
    assert resp.status_code == 422


def test_put_不带attributes原样保留(client):
    _put_order_attrs(client, [{"name": "ORDER_ID", "kind": "pk", "type": "bigint"}])
    # 再 PUT 不带 attributes (现有前端老语义) → 不动
    client.put("/ontology/entity/order", json={
        "displayName": "订单改", "domain": "ORDER", "summary": "订单实体",
        "maps_to": "t_order", "tables": ["t_order"],
    })
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["displayName"] == "订单改"
    assert e["attributes"][0]["name"] == "ORDER_ID"


def test_put_attributes_白名单外键422(client):
    resp = _put_order_attrs(client, [{
        "name": "X", "kind": "attr", "type": "varchar", "evil": "y",
    }])
    assert resp.status_code == 422


def test_put_attributes_kind越界422(client):
    resp = _put_order_attrs(client, [{"name": "X", "kind": "wat", "type": "varchar"}])
    assert resp.status_code == 422
    resp = _put_order_attrs(client, [{"name": "X", "kind": "attr", "type": "decimal"}])
    assert resp.status_code == 422
    resp = _put_order_attrs(client, [{
        "name": "X", "kind": "attr", "type": "varchar", "desc_source": "inferred",
    }])
    assert resp.status_code == 422


def test_put_attributes_name空或重名400(client):
    resp = _put_order_attrs(client, [{"name": "  ", "kind": "attr", "type": "varchar"}])
    assert resp.status_code == 400
    resp = _put_order_attrs(client, [
        {"name": "A", "kind": "attr", "type": "varchar"},
        {"name": "A", "kind": "fk", "type": "bigint"},
    ])
    assert resp.status_code == 400
    assert "A" in resp.json()["detail"]


def test_put_attributes_超20条400(client):
    attrs = [{"name": f"C{i}", "kind": "attr", "type": "varchar"} for i in range(21)]
    resp = _put_order_attrs(client, attrs)
    assert resp.status_code == 400


def test_put_attributes_hasEnum断链400且不落库(client):
    resp = _put_order_attrs(client, [{
        "name": "NO_TABLE_CD", "kind": "enum", "type": "varchar", "hasEnum": True,
    }])
    assert resp.status_code == 400
    assert "NO_TABLE_CD" in resp.json()["detail"]
    e = next(x for x in client.get("/ontology/entities").json() if x["id"] == "order")
    assert e["attributes"] == []          # 400 时整个 PUT 不写


def test_put_attributes_反向不拦(client):
    # 码表有 REC_STATUS 行但属性没标 hasEnum → 放行
    resp = _put_order_attrs(client, [{"name": "REC_STATUS", "kind": "enum", "type": "varchar"}])
    assert resp.status_code == 200


# ---------- /concepts 组合保存 + 级联删 (Task4) ----------

def test_get_post_concepts(client):
    r = client.post("/concepts", json={
        "id": "c_master", "display_name": "主号", "anchor_entity": "lib_loan",
        "summary": "主号提示", "origin": "manual",
        "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                        "op": "=", "value": "1000", "seq": 0}]})
    assert r.status_code == 200 and r.json()["ok"] is True

    got = client.get("/concepts").json()["concepts"]
    c = next(x for x in got if x["id"] == "c_master")
    assert c["display_name"] == "主号"
    assert c["valid"] == 0
    assert len(c["conditions"]) == 1
    assert c["conditions"][0]["col"] == "REC_STATUS"


def test_post_concept_删掉被移除条件(client):
    client.post("/concepts", json={
        "id": "c_m", "display_name": "M", "anchor_entity": "e1", "origin": "manual",
        "conditions": [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0},
                       {"entity": "e2", "col": "B", "op": "=", "value": "2", "seq": 1}]})
    client.post("/concepts", json={
        "id": "c_m", "display_name": "M", "anchor_entity": "e1", "origin": "manual",
        "conditions": [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0}]})
    c = next(x for x in client.get("/concepts").json()["concepts"] if x["id"] == "c_m")
    assert {cc["col"] for cc in c["conditions"]} == {"A"}


def test_delete_concept_端点(client):
    client.post("/concepts", json={
        "id": "c_d", "display_name": "D", "anchor_entity": "e1", "origin": "manual",
        "conditions": [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0}]})
    r = client.request("DELETE", "/concepts", json={"id": "c_d"})
    assert r.status_code == 200
    assert all(x["id"] != "c_d" for x in client.get("/concepts").json()["concepts"])


def test_post_concept_非法origin_422(client):
    r = client.post("/concepts", json={
        "id": "c_bad", "display_name": "B", "anchor_entity": "e1",
        "origin": "source_code", "conditions": []})
    assert r.status_code == 422


def test_validate_concepts_回写valid(client, monkeypatch):
    import web.app as appmod
    monkeypatch.setattr(appmod, "list_table_columns",
                        lambda table, source: {"REC_STATUS", "LIB_LOAN_ID"})
    monkeypatch.setattr(appmod, "find_join_paths",
                        lambda onto, a, b: [{"via": "x"}])

    client.post("/concepts", json={
        "id": "c_master", "display_name": "主号", "anchor_entity": "lib_loan",
        "origin": "manual",
        "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                        "op": "=", "value": "1000", "seq": 0}]})

    r = client.post("/concepts/validate")
    assert r.status_code == 200
    res = r.json()["results"]
    row = next(x for x in res if x["id"] == "c_master")
    assert "checks" in row and "ok" in row

    c = next(x for x in client.get("/concepts").json()["concepts"] if x["id"] == "c_master")
    assert c["valid"] == (1 if row["ok"] else 0)


# ---------- /concepts 录入闸门: 受控聚合表达式 (Task6) ----------

def test_post_agg_concept_全绿落库(client):
    r = client.post("/concepts", json={
        "id": "hv", "display_name": "高价值客户", "anchor_entity": "lib_loan",
        "origin": "manual",
        "conditions": [{"entity": "lib_loan", "kind": "agg",
                        "col": "REC_STATUS", "op": "=", "value": "10000",
                        "agg_fn": "COUNT", "agg_target": "*",
                        "group_by_col": "READER_ID",
                        "having_op": ">=", "having_value": "5", "seq": 0}]})
    assert r.status_code == 200
    c = next(x for x in client.get("/concepts").json()["concepts"] if x["id"] == "hv")
    assert c["conditions"][0]["kind"] == "agg"
    assert c["conditions"][0]["agg_fn"] == "COUNT"


def test_post_agg_非法函数422(client):
    r = client.post("/concepts", json={
        "id": "bad", "display_name": "B", "anchor_entity": "lib_loan", "origin": "manual",
        "conditions": [{"entity": "lib_loan", "kind": "agg", "col": "", "op": "=", "value": "",
                        "agg_fn": "MEDIAN", "agg_target": "*", "group_by_col": "READER_ID",
                        "having_op": ">=", "having_value": "5", "seq": 0}]})
    assert r.status_code == 422
    assert all(x["id"] != "bad" for x in client.get("/concepts").json()["concepts"])


def test_post_agg_非法having符号422(client):
    r = client.post("/concepts", json={
        "id": "bad2", "display_name": "B", "anchor_entity": "lib_loan", "origin": "manual",
        "conditions": [{"entity": "lib_loan", "kind": "agg", "col": "", "op": "=", "value": "",
                        "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "READER_ID",
                        "having_op": "LIKE", "having_value": "5", "seq": 0}]})
    assert r.status_code == 422


def test_post_未知kind_422(client):
    r = client.post("/concepts", json={
        "id": "bad3", "display_name": "B", "anchor_entity": "lib_loan", "origin": "manual",
        "conditions": [{"entity": "lib_loan", "kind": "fn", "col": "", "op": "=", "value": "",
                        "agg_fn": "", "agg_target": "", "group_by_col": "",
                        "having_op": "", "having_value": "", "seq": 0}]})
    assert r.status_code == 422


def test_post_agg_缺groupby_400或422(client):
    r = client.post("/concepts", json={
        "id": "bad4", "display_name": "B", "anchor_entity": "lib_loan", "origin": "manual",
        "conditions": [{"entity": "lib_loan", "kind": "agg", "col": "", "op": "=", "value": "",
                        "agg_fn": "COUNT", "agg_target": "*", "group_by_col": "",
                        "having_op": ">=", "having_value": "5", "seq": 0}]})
    assert r.status_code in (400, 422)


def test_post_老column_concept仍200(client):
    # 回归: 不带 kind 的老 payload 仍能存 (向后兼容)
    r = client.post("/concepts", json={
        "id": "c_old", "display_name": "老", "anchor_entity": "e1", "origin": "manual",
        "conditions": [{"entity": "e1", "col": "A", "op": "=", "value": "1", "seq": 0}]})
    assert r.status_code == 200
