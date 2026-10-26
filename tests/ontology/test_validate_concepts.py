"""capabilities/ontology/validate_concepts.py 测试: 4 类校验各正/负样本 (真库 I/O 注入 mock)。"""
from core.ontology_view import build_indexes
from capabilities.ontology.validate_concepts import validate_concept


def _onto():
    return build_indexes({
        "entities": [
            {"id": "lib_loan", "displayName": "借阅实例", "maps_to": "读者",
             "tables": ["lib_loan"], "attributes": [], "summary": ""},
            {"id": "lib_loan_rel", "displayName": "主从号关系", "maps_to": "关系",
             "tables": ["lib_loan_rel"], "attributes": [], "summary": ""},
        ],
        "relationships": [], "domains": [],
        "valueSets": {("lib_loan_rel", "REC_STATUS"): {
            "name": "关系状态", "codes": [{"code": "1000", "name": "有效"},
                                          {"code": "1200", "name": "失效"}]}},
        "concepts": [],
    })


def _concept(entity="lib_loan_rel", col="REC_STATUS", value="1000",
             anchor="lib_loan"):
    return {"id": "active_member", "anchor_entity": anchor,
            "conditions": [{"entity": entity, "col": col, "op": "=", "value": value}]}


# 注入回调: table_cols_map[entity] = 物理列集合; join_ok 决定连通性
_COLS = {"lib_loan": {"LIB_LOAN_ID", "REC_STATUS"},
         "lib_loan_rel": {"LIB_LOAN_REL_ID", "A_LIB_LOAN_ID", "REC_STATUS"}}


def _validate(onto, concept, cols=None, join_ok=True):
    return validate_concept(
        concept, onto,
        table_cols_map=cols if cols is not None else _COLS,
        join_path=lambda a, b: [{"dummy": "path"}] if join_ok else [],
    )


def test_全绿():
    r = _validate(_onto(), _concept())
    assert r["ok"] is True


def test_实体不存在():
    r = _validate(_onto(), _concept(entity="ghost_entity"))
    assert r["ok"] is False
    assert any(c["name"] == "entity_exists" and not c["ok"] for c in r["checks"])


def test_列不存在():
    cols = dict(_COLS); cols["lib_loan_rel"] = {"LIB_LOAN_REL_ID"}  # 无 REC_STATUS
    r = _validate(_onto(), _concept(), cols=cols)
    assert r["ok"] is False
    assert any(c["name"] == "column_exists" and not c["ok"] for c in r["checks"])


def test_枚举码错():
    r = _validate(_onto(), _concept(value="9999"))   # 不在 {1000,1200}
    assert r["ok"] is False
    assert any(c["name"] == "value_in_enum" and not c["ok"] for c in r["checks"])


def test_表间断腿():
    r = _validate(_onto(), _concept(), join_ok=False)
    assert r["ok"] is False
    assert any(c["name"] == "connectable" and not c["ok"] for c in r["checks"])


def test_单表概念不查连通性():
    # anchor == condition.entity, involved 只 1 个, 不调 join_path → connectable 通过
    c = _concept(entity="lib_loan", col="REC_STATUS", anchor="lib_loan")
    r = _validate(_onto(), c, join_ok=False)   # 即便 join_ok=False 也不该拦
    conn = [x for x in r["checks"] if x["name"] == "connectable"][0]
    assert conn["ok"] is True


def test_IS_NULL条件空值不查枚举():
    # value='' 的 IS NULL 条件, 该列有枚举登记, value_in_enum 也应 ok(空值不查)。
    # schema 根治后落库 value 为 ''(非 None), _values 须把 '' 也视为无值。
    c = {"id": "active_member", "anchor_entity": "lib_loan",
         "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                         "op": "IS NULL", "value": ""}]}
    r = _validate(_onto(), c)
    enum = [x for x in r["checks"] if x["name"] == "value_in_enum"][0]
    assert enum["ok"] is True


def test_value为None时枚举校验通过():
    # value=None → _values 返回 [] → 无值可校验 → value_in_enum 直接通过
    # (entity/col/anchor 均合法, 单看 value_in_enum 这一条不被别的拖累)
    c = {"id": "active_member", "anchor_entity": "lib_loan",
         "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                         "op": "IS NOT NULL", "value": None}]}
    r = _validate(_onto(), c)
    enum = [x for x in r["checks"] if x["name"] == "value_in_enum"][0]
    assert enum["ok"] is True


def test_op为IN多值部分不在枚举():
    # op=IN 逗号分割: 1000 在枚举 {1000,1200} 合法, 9999 非法 → 只 9999 进 bad
    c = {"id": "active_member", "anchor_entity": "lib_loan",
         "conditions": [{"entity": "lib_loan_rel", "col": "REC_STATUS",
                         "op": "IN", "value": "1000,9999"}]}
    r = _validate(_onto(), c)
    enum = [x for x in r["checks"] if x["name"] == "value_in_enum"][0]
    assert enum["ok"] is False
    assert "9999" in enum["detail"]
    assert "1000" not in enum["detail"]


# ---- Task 4: kind='agg' 聚合断言校验 ----

def _agg_concept(agg_fn="COUNT", agg_target="*", group_by="READER_ID",
                 having_op=">=", having_value="5", filter_col="REC_STATUS",
                 filter_op="=", filter_value="10000", entity="lib_loan"):
    return {"id": "high_value", "anchor_entity": "lib_loan",
            "conditions": [{"entity": entity, "kind": "agg",
                            "col": filter_col, "op": filter_op, "value": filter_value,
                            "agg_fn": agg_fn, "agg_target": agg_target,
                            "group_by_col": group_by,
                            "having_op": having_op, "having_value": having_value}]}


_AGG_COLS = {"lib_loan": {"READER_ID", "REC_STATUS", "AMT", "LIB_LOAN_ID"}}


def test_agg全绿():
    r = _validate(_onto(), _agg_concept(), cols=_AGG_COLS)
    assert r["ok"] is True
    assert any(c["name"] == "agg_valid" and c["ok"] for c in r["checks"])


def test_agg函数不在白名单():
    r = _validate(_onto(), _agg_concept(agg_fn="MEDIAN"), cols=_AGG_COLS)
    assert r["ok"] is False
    assert any(c["name"] == "agg_valid" and not c["ok"] for c in r["checks"])


def test_agg_SUM目标列不存在():
    r = _validate(_onto(), _agg_concept(agg_fn="SUM", agg_target="GHOST"), cols=_AGG_COLS)
    assert r["ok"] is False


def test_agg_COUNT星号免列检查():
    r = _validate(_onto(), _agg_concept(agg_fn="COUNT", agg_target="*"), cols=_AGG_COLS)
    assert any(c["name"] == "agg_valid" and c["ok"] for c in r["checks"])


def test_agg_groupby列不存在():
    r = _validate(_onto(), _agg_concept(group_by="GHOST"), cols=_AGG_COLS)
    assert r["ok"] is False


def test_agg_having符号非法():
    r = _validate(_onto(), _agg_concept(having_op="LIKE"), cols=_AGG_COLS)
    assert r["ok"] is False


def test_agg_having值为空():
    r = _validate(_onto(), _agg_concept(having_value=""), cols=_AGG_COLS)
    assert r["ok"] is False


def test_agg前置过滤列不存在():
    r = _validate(_onto(), _agg_concept(filter_col="GHOST"), cols=_AGG_COLS)
    assert r["ok"] is False


def test_agg前置过滤为空_不检查列():
    # col/op/value 全空 = 无前置过滤, 合法
    r = _validate(_onto(), _agg_concept(filter_col="", filter_op="", filter_value=""),
                  cols=_AGG_COLS)
    assert r["ok"] is True


def test_未知kind被跳过不红不崩():
    c = {"id": "x", "anchor_entity": "lib_loan",
         "conditions": [{"entity": "lib_loan", "kind": "fn", "col": "", "op": "", "value": "",
                         "agg_fn": "", "agg_target": "", "group_by_col": "",
                         "having_op": "", "having_value": ""}]}
    r = _validate(_onto(), c, cols=_AGG_COLS)
    # 未知 kind: column_exists/value_in_enum/agg_valid 均不因它红灯
    assert r["ok"] is True
