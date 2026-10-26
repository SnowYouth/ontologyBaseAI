"""capabilities/ontology/ontology_store.py — 本体 SQLite 存储层 (权威可编辑源).

设计:
1. 五表 domain/entity/relation/enum_value/filter_rule, 每行一个节点 → 改一节点=update
   一行, 不再整文件覆盖 (解决单大 JSON 痛点, 前端 CRUD 天然映射行级操作)
   (旧 value_set 表已被 enum_value 取代, 2026-08-05 DROP; _load_value_sets 读 enum_value)
2. 柔性字段 (attributes/codes) 存 JSON 列, 不固定表结构 → 后期加字段不改表
3. load_raw() 组装成 core.ontology_view.build_indexes 要的 dict, agent 消费侧零改动
4. status 软删 (=1 存活, =0 删除), load_raw 只取存活行
5. relation 存完整 JOIN 字段对 (source_col/target_col/bridge_count), 供多跳拼 SQL;
   via 是 build_indexes 的边 key, 必须原样保留
"""
from __future__ import annotations

import json
import sqlite3

from capabilities.ontology.normalize_relations_l1 import REL_TYPES

# 建表 DDL: 柔性字段 (attributes/codes/extra) 用 TEXT 存 JSON。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain (
    code          TEXT PRIMARY KEY,
    color         TEXT,
    display_order INTEGER DEFAULT 0,
    description   TEXT,
    status        INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS entity (
    id          TEXT PRIMARY KEY,
    entity_code TEXT,
    displayName TEXT,
    domain      TEXT,
    maps_to     TEXT,
    data_source TEXT DEFAULT '',
    tables_json TEXT DEFAULT '[]',
    attributes_json TEXT DEFAULT '[]',
    summary     TEXT DEFAULT '',
    extra_json  TEXT DEFAULT '{}',
    status      INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS relation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    type        TEXT,
    via         TEXT,
    label       TEXT,
    source_col  TEXT,
    target_col  TEXT,
    bridge_count INTEGER,
    extra_json  TEXT DEFAULT '{}',
    status      INTEGER DEFAULT 1,
    UNIQUE(source, target, via)
);
CREATE TABLE IF NOT EXISTS enum_value (
    entity      TEXT NOT NULL,
    field       TEXT NOT NULL,
    name        TEXT,
    codes_json  TEXT DEFAULT '[]',
    spec_ref    TEXT,
    status      INTEGER DEFAULT 1,
    PRIMARY KEY (entity, field)
);
CREATE TABLE IF NOT EXISTS filter_rule (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    entity  TEXT NOT NULL,
    col     TEXT NOT NULL,
    op      TEXT NOT NULL,
    value   TEXT NOT NULL DEFAULT '',
    mode    TEXT NOT NULL,
    origin  TEXT NOT NULL,
    status  INTEGER DEFAULT 1,
    UNIQUE(entity, col, op, value)
);
CREATE TABLE IF NOT EXISTS filter_rule_check (
    run_id         TEXT NOT NULL,
    checked_at     TEXT NOT NULL,
    entity         TEXT NOT NULL,
    col            TEXT NOT NULL,
    op             TEXT NOT NULL,
    value          TEXT,
    column_exists  INTEGER,
    value_in_enum  INTEGER,
    sql_runs       INTEGER,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_frc_run ON filter_rule_check (run_id);
CREATE TABLE IF NOT EXISTS concept (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    anchor_entity TEXT NOT NULL,
    summary       TEXT DEFAULT '',
    origin        TEXT NOT NULL,
    valid         INTEGER DEFAULT 0,
    status        INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS concept_condition (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id    TEXT NOT NULL,
    entity        TEXT NOT NULL,
    col           TEXT NOT NULL,
    op            TEXT NOT NULL,
    value         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'column',
    agg_fn        TEXT NOT NULL DEFAULT '',
    agg_target    TEXT NOT NULL DEFAULT '',
    group_by_col  TEXT NOT NULL DEFAULT '',
    having_op     TEXT NOT NULL DEFAULT '',
    having_value  TEXT NOT NULL DEFAULT '',
    seq           INTEGER DEFAULT 0,
    status        INTEGER DEFAULT 1,
    UNIQUE(concept_id, entity, kind, col, op, value,
           agg_fn, agg_target, group_by_col, having_op, having_value)
);
CREATE INDEX IF NOT EXISTS idx_cc_concept ON concept_condition (concept_id);

-- ★ 自演化三表 (spec §6.1 / §6.2 / plan v3 Task 1)
CREATE TABLE IF NOT EXISTS failure_signal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source       TEXT,
    created_at   TEXT NOT NULL,
    processed    INTEGER DEFAULT 0,
    proposal_id  INTEGER
);

CREATE TABLE IF NOT EXISTS proposal (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL DEFAULT 'relation'
                          CHECK(kind IN ('relation','relation_col')),
    direction         TEXT NOT NULL DEFAULT 'add'
                          CHECK(direction IN ('add','retire')),
    payload_json      TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    review_status     TEXT NOT NULL DEFAULT 'pending'
                          CHECK(review_status IN ('pending','approved','rejected')),
    patch_status      TEXT NOT NULL DEFAULT 'pending'
                          CHECK(patch_status IN ('pending','applied','failed')),
    proposed_at       TEXT NOT NULL,
    proposed_by       TEXT NOT NULL DEFAULT 'probe',
    reviewed_at       TEXT,
    reviewed_by       TEXT,
    review_note       TEXT DEFAULT '',
    probe_result_json TEXT,
    regression_run_id TEXT,
    regression_ok     INTEGER,
    tombstone_hit     INTEGER DEFAULT 0,
    tombstone_note    TEXT,
    status            INTEGER DEFAULT 1, -- soft delete flag (1=active, 0=deleted, MVP unused)
    apply_journal_json TEXT              -- apply 进行中的落盘日志 (见 save_apply_snapshot)
);
CREATE INDEX IF NOT EXISTS idx_proposal_review ON proposal (review_status, patch_status);

-- ★ spec §6.1: 遥测表，不动 relation 表
CREATE TABLE IF NOT EXISTS relation_health (
    source            TEXT NOT NULL,
    target            TEXT NOT NULL,
    via               TEXT NOT NULL,
    use_count         INTEGER DEFAULT 0,
    last_used_at      TEXT,
    suspect_count     INTEGER DEFAULT 0,
    last_suspect_at   TEXT,
    last_batch_id     TEXT,
    mismatch_details  TEXT DEFAULT '{}',
    PRIMARY KEY (source, target, via)
);
"""

# entity 固定列 (其余键塞进 extra_json 柔性存)。
# public 别名 ENTITY_FIXED_KEYS 供 web 层 import 复用 (消除内联复制的三处同步陷阱)。
_ENTITY_FIXED = {"id", "entityCode", "displayName", "domain", "maps_to",
                 "data_source", "tables", "attributes", "summary"}
ENTITY_FIXED_KEYS = _ENTITY_FIXED
# relation 固定列。
_RELATION_FIXED = {"source", "target", "type", "via", "label",
                   "source_col", "target_col", "bridge_count"}

# filter_rule 字段白名单 (正交二分, 值不交叉)。
_FILTER_MODES = {"hard", "soft"}          # hard=不可推翻(如逻辑删除标志), soft=默认口径可让位
_FILTER_ORIGINS = {"source_code", "manual", "ai"}   # 飞轮信任分级
_VALUELESS_OPS = {"IS NULL", "IS NOT NULL"}   # op 自带语义, 不需值; 其余 op 空值即非法
_CONCEPT_ORIGINS = {"manual", "ai"}   # 概念是人/AI 声明的派生词, 无 source_code 来源
# concept_condition 幂等键字段顺序 (与 _SCHEMA 的 UNIQUE 对齐, 去 concept_id)。
_CC_KEY_FIELDS = ("entity", "kind", "col", "op", "value",
                  "agg_fn", "agg_target", "group_by_col", "having_op", "having_value")


def _cc_norm(cc: dict, field: str) -> str:
    """概念条件单字段归一 (kind→'column', 其余 None→'')。写库与幂等键**共用同一规则**,
    杜绝 INSERT 用 `or ""`、_cc_key 用 `str()` 对 falsy 值 (如 0/False) 产出不同串,
    进而组合保存 diff 误删刚写入行的分歧 (对齐 NOT NULL DEFAULT '')。
    """
    v = cc.get(field)
    if field == "kind":
        return v or "column"
    return "" if v is None else str(v)


def _cc_key(cc: dict) -> str:
    """概念条件幂等键字符串 (单一真相源, 三处写路径复用)。归一见 _cc_norm。"""
    return "|".join(_cc_norm(cc, f) for f in _CC_KEY_FIELDS)


def _derive_via(source: str, source_col: str, target: str, target_col: str) -> str:
    """从 source_col/target_col 派生 via: `{source}.{source_col}={target}.{target_col}`。

    via 是边的稳定标识 (唯一键 + 去重key + _is_noise_via 噪音过滤依据), 不再手编。
    col 为空抛 ValueError —— col 必填, via 必须能派生出非空值。
    """
    if not source_col or not target_col:
        raise ValueError(
            f"source_col/target_col 必填 (source={source!r} target={target!r}), "
            f"via 需从 col 派生, 空 col 无法派生"
        )
    return f"{source}.{source_col}={target}.{target_col}"


class OntologyStore:
    """本体 SQLite 存储; 每个方法一个职责, 复杂度低。"""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """给老库补列: CREATE TABLE IF NOT EXISTS 对已存在的表不补新列。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(proposal)")}
        if "apply_journal_json" not in cols:
            self._conn.execute("ALTER TABLE proposal ADD COLUMN apply_journal_json TEXT")

    def close(self) -> None:
        self._conn.close()

    # ---------- 内省 ----------

    def table_names(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return [r["name"] for r in rows]

    # ---------- domain ----------

    def upsert_domain(self, d: dict) -> None:
        self._conn.execute(
            "INSERT INTO domain (code, color, display_order, description, status) "
            "VALUES (?,?,?,?,1) "
            "ON CONFLICT(code) DO UPDATE SET color=excluded.color, "
            "display_order=excluded.display_order, description=excluded.description, status=1",
            (d["code"], d.get("color"), d.get("displayOrder", 0), d.get("description")),
        )
        self._conn.commit()

    def delete_domain(self, code: str) -> None:
        self._soft_delete("domain", "code", code)

    def delete_domain_cascade(self, code: str) -> None:
        """删域 + 级联把挂靠实体 domain 置空, 单事务 all-or-nothing。

        原来在 web 层是两次独立 store 调用各自 commit, 中途失败留半成品
        (域删了实体没归位 / 反之)。下沉单事务后任一步抛错 → 整体回滚。
        """
        try:
            self._unassign_entities_stmt(code)
            self._soft_delete_stmt("domain", "code", code)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- entity ----------

    def upsert_entity(self, e: dict) -> None:
        extra = {k: v for k, v in e.items() if k not in _ENTITY_FIXED}
        self._conn.execute(
            "INSERT INTO entity (id, entity_code, displayName, domain, maps_to, data_source, "
            "tables_json, attributes_json, summary, extra_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(id) DO UPDATE SET entity_code=excluded.entity_code, "
            "displayName=excluded.displayName, domain=excluded.domain, maps_to=excluded.maps_to, "
            "data_source=excluded.data_source, "
            "tables_json=excluded.tables_json, attributes_json=excluded.attributes_json, "
            "summary=excluded.summary, extra_json=excluded.extra_json, status=1",
            (e["id"], e.get("entityCode"), e.get("displayName"), e.get("domain"),
             e.get("maps_to"), e.get("data_source", ""),
             json.dumps(e.get("tables", []), ensure_ascii=False),
             json.dumps(e.get("attributes", []), ensure_ascii=False),
             e.get("summary", ""), json.dumps(extra, ensure_ascii=False)),
        )
        self._conn.commit()

    def delete_entity(self, entity_id: str) -> None:
        self._soft_delete("entity", "id", entity_id)

    def delete_entity_cascade(self, entity_id: str) -> None:
        """删实体 + 级联软删所有以它为 source 或 target 的存活关系边, 单事务 all-or-nothing。

        原 delete_entity 只软删实体本身, 会留悬挂边 (边指向已删实体)。
        仿 delete_domain_cascade 下沉单事务: 任一步抛错整体回滚。
        保留 delete_entity 不动 (非级联版), 仅端点切到此级联版。
        """
        try:
            self._soft_delete_stmt("entity", "id", entity_id)
            self._conn.execute(
                "UPDATE relation SET status=0 "
                "WHERE (source=? OR target=?) AND status=1",
                (entity_id, entity_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def load_entity(self, entity_id: str) -> dict:
        """取单个存活实体 (extra_json 平铺); 不存在抛 KeyError。前端 PUT load-merge-write 用。"""
        row = self._conn.execute(
            "SELECT * FROM entity WHERE id=? AND status=1", (entity_id,)
        ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        return self._row_to_entity(row)

    def unassign_entities(self, domain_code: str) -> None:
        """把挂靠该域的存活实体 domain 置空串 (删域级联, 归为未分类)。"""
        self._unassign_entities_stmt(domain_code)
        self._conn.commit()

    def _unassign_entities_stmt(self, domain_code: str) -> None:
        """无 commit 版, 供 delete_domain_cascade 单事务复用。"""
        self._conn.execute(
            "UPDATE entity SET domain='' WHERE domain=? AND status=1", (domain_code,)
        )

    # ---------- relation ----------

    def upsert_relation(self, r: dict) -> None:
        self._upsert_relation_stmt(r)
        self._conn.commit()

    def _upsert_relation_stmt(self, r: dict) -> None:
        """无 commit 版, 供 flip_relation 单事务复用。"""
        typ = r.get("type")
        if typ not in REL_TYPES:
            raise ValueError(f"非法 relation type: {typ!r}, 须为 {REL_TYPES}")
        source, target = r["source"], r["target"]
        source_col, target_col = r.get("source_col"), r.get("target_col")
        # via 从 col 派生, 忽略调用方传的 via (不再手编)。col 必填。
        via = _derive_via(source, source_col, target, target_col)
        self._guard_label_distinct(source, target, via, r.get("label"))
        extra = {k: v for k, v in r.items() if k not in _RELATION_FIXED}
        self._conn.execute(
            "INSERT INTO relation (source, target, type, via, label, "
            "source_col, target_col, bridge_count, extra_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(source, target, via) DO UPDATE SET type=excluded.type, "
            "label=excluded.label, source_col=excluded.source_col, "
            "target_col=excluded.target_col, bridge_count=excluded.bridge_count, "
            "extra_json=excluded.extra_json, status=1",
            (source, target, typ, via, r.get("label"),
             source_col, target_col, r.get("bridge_count"),
             json.dumps(extra, ensure_ascii=False)),
        )

    def _guard_label_distinct(self, source: str, target: str,
                              via: str, label) -> None:
        """落库前拦同端点对 label 雷同 (复用 check_relation_label_distinct 的判据)。

        为什么在这里: 这是两条活的写入路径 (web POST /ontology/relation、
        selfevolve apply relation_add) 唯一共用的咽喉, 装一处两处受益。

        为什么不只靠那条真库回归测试: 它只在有人跑 pytest 时才响, 而写入随时
        发生 —— 坏边会先落库, 中间 agent 已按雷同 label 选错边 (同"闸门判得对
        但拦不住"那个形状)。前移到写入点才拦得住。

        口径与离线护栏严格一致:
        - 空 label 不管 (既有裸边债, 真库当前就有 1 条, 拦它会堵死正常写入)
        - 端点对无序 (get_join_path 选边不分方向)
        - 只比 status=1 的存活边 (墓碑边腾出它的 label)
        - **排除自己** (via 相同 = 同一条边的更新/幂等重灌, 不能自我阻塞)
        """
        if not label:
            return
        rows = self._conn.execute(
            "SELECT label FROM relation WHERE status=1 AND via != ? "
            "AND ((source=? AND target=?) OR (source=? AND target=?))",
            (via, source, target, target, source),
        ).fetchall()
        existing = {r["label"] for r in rows if r["label"]}
        if label in existing:
            raise ValueError(
                f"同端点对 {{{source} ↔ {target}}} 已有 active 边共用 label "
                f"{label!r} — 同端点对多条边 label 必须可区分, 否则概念层/agent "
                f"靠 get_join_path 返回的 label 选边时无区分度 (只能靠 source_col "
                f"前缀猜方向)。请换一个能区分方向/语义的 label。"
            )

    def delete_relation(self, source: str, target: str,
                        source_col: str, target_col: str) -> None:
        self._delete_relation_stmt(source, target, source_col, target_col)
        self._conn.commit()

    def _delete_relation_stmt(self, source: str, target: str,
                              source_col: str, target_col: str) -> None:
        """无 commit 版, 供 flip_relation 单事务复用。按 col 定位 (via 派生自 col)。"""
        via = _derive_via(source, source_col, target, target_col)
        self._conn.execute(
            "UPDATE relation SET status=0 WHERE source=? AND target=? AND via=?",
            (source, target, via),
        )

    def flip_relation(self, body_source: str, body_target: str,
                      body_source_col: str, body_target_col: str,
                      new_edge: dict) -> None:
        """改方向: 删旧边 + 建方向翻转的新边, 单事务 all-or-nothing。

        原来在 web 层是 delete_relation + upsert_relation 两次各自 commit,
        中途失败会留"旧边删了新边没建"的丢边半成品 (ontology.db 无备份即永久丢失)。
        下沉单事务后任一步抛错 → 整体回滚, 边不丢。new_edge 由调用方展开
        (翻转 source/target/col, via/type/label/extra 血肉原样保留)。
        via 不再由调用方传 —— 按 body_source_col/body_target_col 派生旧 via 定位删除,
        new_edge 的 via 由 _upsert_relation_stmt 按新 col 派生 (翻转后 via 变 B.y=A.x)。
        """
        try:
            self._delete_relation_stmt(body_source, body_target,
                                       body_source_col, body_target_col)
            self._upsert_relation_stmt(new_edge)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- enum_value (主键 (entity,field)) ----------

    def upsert_enum_value(self, entity: str, field: str, ev: dict) -> None:
        self._conn.execute(
            "INSERT INTO enum_value (entity, field, name, codes_json, spec_ref, status) "
            "VALUES (?,?,?,?,?,1) "
            "ON CONFLICT(entity, field) DO UPDATE SET name=excluded.name, "
            "codes_json=excluded.codes_json, spec_ref=excluded.spec_ref, status=1",
            (entity, field, ev.get("name"),
             json.dumps(ev.get("codes", []), ensure_ascii=False),
             ev.get("spec_ref")),
        )
        self._conn.commit()

    def delete_enum_value(self, entity: str, field: str) -> None:
        self._conn.execute(
            "UPDATE enum_value SET status=0 WHERE entity=? AND field=?",
            (entity, field),
        )
        self._conn.commit()

    def enum_fields(self, entity_id: str) -> set[str]:
        """该实体在 enum_value 表有存活码值行的字段名集合 (hasEnum 硬校验用)。"""
        rows = self._conn.execute(
            "SELECT field FROM enum_value WHERE entity=? AND status=1", (entity_id,)
        ).fetchall()
        return {r["field"] for r in rows}

    # ---------- filter_rule (幂等键 (entity,col,op,value)) ----------

    def upsert_filter_rule(self, fr: dict) -> None:
        """写一条默认过滤规则; mode/origin 过白名单, 非法抛 ValueError。

        value 归一: None → ''(焊死 UNIQUE 对 NULL 不去重的累积口子)。
        写时守门: op 非无值 op(IS NULL/IS NOT NULL)却 value 空 → 报错不落库。
        """
        mode, origin = fr.get("mode"), fr.get("origin")
        if mode not in _FILTER_MODES:
            raise ValueError(f"非法 filter_rule mode: {mode!r}, 须为 {_FILTER_MODES}")
        if origin not in _FILTER_ORIGINS:
            raise ValueError(f"非法 filter_rule origin: {origin!r}, 须为 {_FILTER_ORIGINS}")
        value = fr.get("value") or ""
        op = fr["op"]
        if op.upper() not in _VALUELESS_OPS and value == "":
            raise ValueError(f"filter_rule op={op!r} 需要值, 但 value 为空")
        self._conn.execute(
            "INSERT INTO filter_rule (entity, col, op, value, mode, origin, status) "
            "VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT(entity, col, op, value) DO UPDATE SET "
            "mode=excluded.mode, origin=excluded.origin, status=1",
            (fr["entity"], fr["col"], op, value, mode, origin),
        )
        self._conn.commit()

    def delete_filter_rule(self, entity: str, col: str, op: str, value: str) -> None:
        """软删一条 filter_rule(四键定位)。value None→'' 归一后按等值匹配。"""
        self._conn.execute(
            "UPDATE filter_rule SET status=0 "
            "WHERE entity=? AND col=? AND op=? AND value=?",
            (entity, col, op, value or ""),
        )
        self._conn.commit()

    def _load_filter_rules(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT entity, col, op, value, mode, origin FROM filter_rule "
            "WHERE status=1 ORDER BY entity, col, op, value"
        ).fetchall()
        return [{"entity": r["entity"], "col": r["col"], "op": r["op"],
                 "value": r["value"], "mode": r["mode"], "origin": r["origin"]}
                for r in rows]

    # ---------- filter_rule_check (append-only 校验历史) ----------

    def insert_check_run(self, run_id: str, checked_at: str, rows: list[dict]) -> None:
        """落盘一次校验的全部规则结果 (单事务批量 insert)。

        rows 每项: {entity,col,op,value,column_exists,value_in_enum,sql_runs,detail}。
        column_exists/sql_runs 存 1/0; value_in_enum 存 1/0/None (None=该字段无枚举登记,
        校验器 skipped)。ok 不落盘 (= 三检查纯函数, load 时现算)。
        """
        self._conn.executemany(
            "INSERT INTO filter_rule_check "
            "(run_id, checked_at, entity, col, op, value, "
            " column_exists, value_in_enum, sql_runs, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, checked_at, r["entity"], r["col"], r["op"], r.get("value"),
              r["column_exists"], r["value_in_enum"], r["sql_runs"], r.get("detail", ""))
             for r in rows],
        )
        self._conn.commit()

    def load_check_runs(self, limit: int = 2) -> list[dict]:
        """取最近 limit 个 run (按 checked_at 倒序), 每 run 组装 results (ok 现算)。

        ok = column_exists 真 且 value_in_enum≠0 (1或None都算过) 且 sql_runs 真。
        默认 limit=2 供前端 current/previous 对比。
        """
        run_rows = self._conn.execute(
            "SELECT DISTINCT run_id, checked_at FROM filter_rule_check "
            "ORDER BY checked_at DESC, run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        runs = []
        for rr in run_rows:
            detail_rows = self._conn.execute(
                "SELECT entity, col, op, value, column_exists, value_in_enum, "
                "sql_runs, detail FROM filter_rule_check WHERE run_id=?", (rr["run_id"],)
            ).fetchall()
            runs.append({
                "run_id": rr["run_id"], "checked_at": rr["checked_at"],
                "results": [self._check_row_to_result(r) for r in detail_rows],
            })
        return runs

    @staticmethod
    def _check_row_to_result(r: sqlite3.Row) -> dict:
        """一行校验结果 → dict, 现算 ok (value_in_enum None/1 视为通过)。"""
        ce, vie, sr = r["column_exists"], r["value_in_enum"], r["sql_runs"]
        ok = bool(ce) and (vie != 0) and bool(sr)
        return {"entity": r["entity"], "col": r["col"], "op": r["op"], "value": r["value"],
                "column_exists": ce, "value_in_enum": vie, "sql_runs": sr,
                "detail": r["detail"], "ok": ok}

    # ---------- concept (概念层 TBox; id 自然键, 同 entity.id) ----------

    def upsert_concept(self, c: dict) -> None:
        """写一个概念身份; origin 过白名单。valid 不由本方法覆盖 (改定义须重校验):
        新插入时默认 0, 已存在则保留旧 valid (只更 display_name/anchor/summary/origin)。
        """
        origin = c.get("origin")
        if origin not in _CONCEPT_ORIGINS:
            raise ValueError(f"非法 concept origin: {origin!r}, 须为 {_CONCEPT_ORIGINS}")
        self._conn.execute(
            "INSERT INTO concept (id, display_name, anchor_entity, summary, origin, valid, status) "
            "VALUES (?,?,?,?,?,0,1) "
            "ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, "
            "anchor_entity=excluded.anchor_entity, summary=excluded.summary, "
            "origin=excluded.origin, status=1",
            (c["id"], c["display_name"], c["anchor_entity"],
             c.get("summary", ""), origin),
        )
        self._conn.commit()

    def delete_concept(self, concept_id: str) -> None:
        """软删概念身份 + 级联软删其全部条件, 单事务 (避免悬空条件)。"""
        try:
            self._soft_delete_stmt("concept", "id", concept_id)
            self._conn.execute(
                "UPDATE concept_condition SET status=0 WHERE concept_id=?", (concept_id,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def set_concept_valid(self, concept_id: str, valid: int) -> None:
        """只更 valid 一列 (校验器回写)。"""
        self._conn.execute(
            "UPDATE concept SET valid=? WHERE id=?", (valid, concept_id)
        )
        self._conn.commit()

    def upsert_concept_condition(self, cc: dict) -> None:
        """写一条概念派生条件; 幂等键为 11 列 (spec §4.2)。kind 缺省 column, 新列缺省 ''。"""
        self._conn.execute(
            "INSERT INTO concept_condition "
            "(concept_id, entity, col, op, value, kind, agg_fn, agg_target, "
            " group_by_col, having_op, having_value, seq, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(concept_id, entity, kind, col, op, value, agg_fn, agg_target, "
            " group_by_col, having_op, having_value) DO UPDATE SET seq=excluded.seq, status=1",
            (cc["concept_id"], cc["entity"], cc["col"], cc["op"], _cc_norm(cc, "value"),
             _cc_norm(cc, "kind"), _cc_norm(cc, "agg_fn"), _cc_norm(cc, "agg_target"),
             _cc_norm(cc, "group_by_col"), _cc_norm(cc, "having_op"),
             _cc_norm(cc, "having_value"), cc.get("seq", 0)),
        )
        self._conn.commit()

    def delete_concept_condition(self, condition_id: int) -> None:
        self._conn.execute(
            "UPDATE concept_condition SET status=0 WHERE id=?", (condition_id,)
        )
        self._conn.commit()

    def _live_condition_ids(self, concept_id: str) -> dict:
        """某概念的存活条件 → {幂等键字符串: condition.id} (11 字段, 见 _cc_key)。
        供组合保存 diff 出"库里有、body 没传"的旧条件做级联软删。
        """
        rows = self._conn.execute(
            "SELECT id, entity, kind, col, op, value, agg_fn, agg_target, "
            "group_by_col, having_op, having_value FROM concept_condition "
            "WHERE concept_id=? AND status=1", (concept_id,)
        ).fetchall()
        return {_cc_key(dict(r)): r["id"] for r in rows}

    def save_concept_with_conditions(self, concept: dict, conditions: list[dict]) -> None:
        """组合保存: 概念身份 + 全部条件, 单事务。库里有、conditions 没传的旧条件级联软删。
        valid 由 upsert 逻辑自动回 0 (改定义须重校验)。中途异常 all-or-nothing 回滚。
        """
        origin = concept.get("origin")
        if origin not in _CONCEPT_ORIGINS:
            raise ValueError(f"非法 concept origin: {origin!r}, 须为 {_CONCEPT_ORIGINS}")
        try:
            self._conn.execute(
                "INSERT INTO concept (id, display_name, anchor_entity, summary, origin, valid, status) "
                "VALUES (?,?,?,?,?,0,1) "
                "ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, "
                "anchor_entity=excluded.anchor_entity, summary=excluded.summary, "
                "origin=excluded.origin, status=1",
                (concept["id"], concept["display_name"], concept["anchor_entity"],
                 concept.get("summary", ""), origin),
            )
            keep = set()
            for cc in conditions:
                self._conn.execute(
                    "INSERT INTO concept_condition "
                    "(concept_id, entity, col, op, value, kind, agg_fn, agg_target, "
                    " group_by_col, having_op, having_value, seq, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(concept_id, entity, kind, col, op, value, agg_fn, "
                    " agg_target, group_by_col, having_op, having_value) "
                    "DO UPDATE SET seq=excluded.seq, status=1",
                    (concept["id"], cc["entity"], cc["col"], cc["op"], _cc_norm(cc, "value"),
                     _cc_norm(cc, "kind"), _cc_norm(cc, "agg_fn"),
                     _cc_norm(cc, "agg_target"), _cc_norm(cc, "group_by_col"),
                     _cc_norm(cc, "having_op"), _cc_norm(cc, "having_value"),
                     cc.get("seq", 0)),
                )
                keep.add(_cc_key(cc))
            for key, cid in self._live_condition_ids(concept["id"]).items():
                if key not in keep:
                    self._conn.execute(
                        "UPDATE concept_condition SET status=0 WHERE id=?", (cid,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _load_concepts(self) -> list[dict]:
        """存活概念 + 内嵌存活条件 (conditions 按 seq,id 排序)。"""
        rows = self._conn.execute(
            "SELECT id, display_name, anchor_entity, summary, origin, valid "
            "FROM concept WHERE status=1 ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            conds = self._conn.execute(
                "SELECT entity, col, op, value, kind, agg_fn, agg_target, "
                "group_by_col, having_op, having_value, seq FROM concept_condition "
                "WHERE concept_id=? AND status=1 ORDER BY seq, id", (r["id"],)
            ).fetchall()
            out.append({
                "id": r["id"], "display_name": r["display_name"],
                "anchor_entity": r["anchor_entity"], "summary": r["summary"] or "",
                "origin": r["origin"], "valid": r["valid"],
                "conditions": [{"entity": c["entity"], "col": c["col"], "op": c["op"],
                                "value": c["value"], "kind": c["kind"],
                                "agg_fn": c["agg_fn"], "agg_target": c["agg_target"],
                                "group_by_col": c["group_by_col"], "having_op": c["having_op"],
                                "having_value": c["having_value"], "seq": c["seq"]}
                               for c in conds],
            })
        return out

    # ---------- 自演化: failure_signal ----------

    def mark_signal_processed(self, signal_id: int,
                              proposal_id: int | None = None) -> None:
        """标记信号已处理，可选关联 proposal_id。"""
        self._conn.execute(
            "UPDATE failure_signal SET processed=1, proposal_id=? WHERE id=?",
            (proposal_id, signal_id))
        self._conn.commit()

    def list_unprocessed_signals(self, limit: int = 100) -> list[dict]:
        """返回未处理信号列表。"""
        rows = self._conn.execute(
            "SELECT * FROM failure_signal WHERE processed=0 ORDER BY id LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 自演化: proposal ----------

    def insert_proposal(self, kind: str, payload: dict, reason: str,
                        probe_result: dict | None = None,
                        proposed_by: str = "probe",
                        direction: str = "add",
                        tombstone_hit: bool = False,
                        tombstone_note: str = "") -> int:
        """插入提议，返回 proposal_id。"""
        import datetime
        cur = self._conn.execute(
            "INSERT INTO proposal "
            "(kind, direction, payload_json, reason, proposed_by, proposed_at, "
            " probe_result_json, tombstone_hit, tombstone_note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, direction,
             json.dumps(payload, ensure_ascii=False), reason, proposed_by,
             datetime.datetime.now().isoformat(timespec="seconds"),
             json.dumps(probe_result, ensure_ascii=False) if probe_result else None,
             1 if tombstone_hit else 0,
             tombstone_note or None))
        self._conn.commit()
        return cur.lastrowid

    def list_proposals(self, review_status: str | None = None) -> list[dict]:
        """返回提议列表，可按 review_status 过滤。只返回 status=1。"""
        if review_status:
            rows = self._conn.execute(
                "SELECT * FROM proposal WHERE status=1 AND review_status=? "
                "ORDER BY id", (review_status,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM proposal WHERE status=1 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def patch_proposal_review(self, proposal_id: int, review_status: str,
                              reviewed_by: str = "",
                              review_note: str = "") -> None:
        """更新审批状态。"""
        import datetime
        self._conn.execute(
            "UPDATE proposal SET review_status=?, reviewed_by=?, review_note=?, "
            "reviewed_at=? WHERE id=?",
            (review_status, reviewed_by, review_note,
             datetime.datetime.now().isoformat(timespec="seconds"), proposal_id))
        self._conn.commit()

    def patch_proposal_payload(self, proposal_id: int, payload: dict) -> None:
        """整体替换 payload_json (调用方读出来改完写回)。

        目前唯一调用方是人审补 label (web 层 _review_label_into_payload):
        probe 产的 payload 不带 label, 不补就落库成裸边。
        ensure_ascii=False 与 insert_proposal 同口径, 否则库里两种编码混着。
        """
        self._conn.execute(
            "UPDATE proposal SET payload_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), proposal_id))
        self._conn.commit()

    def claim_for_apply(self, proposal_id: int) -> bool:
        """抢占式占位: approved+pending → applied, 抢到返回 True。

        ★ 审计 #5 TOCTOU (2026-10-27): 守卫若只读调用方传入的 dict, 两个并发
        apply 都能过闸 → 后者快照 existed=False → 回滚硬删前者已过闸的边。
        判断和写入必须是同一条条件 UPDATE (跨连接也成立, 端点每请求一个连接)。
        """
        cur = self._conn.execute(
            "UPDATE proposal SET patch_status='applied', "
            "apply_journal_json='{\"phase\": \"claimed\"}' WHERE id=? "
            "AND review_status='approved' AND patch_status='pending'",
            (proposal_id,))
        self._conn.commit()
        return cur.rowcount == 1

    def release_apply_claim(self, proposal_id: int) -> None:
        """退还占位 (applied → pending): handler 自己拒绝/抛异常时用, 留着能重试。"""
        self._conn.execute(
            "UPDATE proposal SET patch_status='pending', apply_journal_json=NULL "
            "WHERE id=? AND patch_status='applied'", (proposal_id,))
        self._conn.commit()

    def save_apply_snapshot(self, proposal_id: int, snapshot: dict,
                            run_id: str | None) -> None:
        """落库**之前**把回滚快照写盘 (phase=applying)。

        ★ 回归门要重跑 CQ (几分钟)。进程在这期间被杀, 内存里的快照跟着没了,
        except 也跑不到 → 边留在库里、提议停在 applied 却从没过闸。快照落盘后,
        下次启动的 recover_interrupted_applies 能照它精确回滚。
        """
        self._conn.execute(
            "UPDATE proposal SET apply_journal_json=? WHERE id=?",
            (json.dumps({"phase": "applying", "run_id": run_id, "snapshot": snapshot},
                        ensure_ascii=False), proposal_id))
        self._conn.commit()

    def list_interrupted_applies(self) -> list[dict]:
        """apply 没走到终态的提议 (journal 还在 claimed/applying)。"""
        rows = self._conn.execute(
            "SELECT * FROM proposal WHERE apply_journal_json IS NOT NULL "
            "AND json_extract(apply_journal_json, '$.phase') IN ('claimed', 'applying') "
            "ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def finish_interrupted_apply(self, proposal_id: int, patch_status: str,
                                 journal: dict) -> None:
        """崩溃恢复的收尾: 置终态, journal 改成 recovered 留痕 (不清空, 人能查到为什么 failed)。"""
        self._conn.execute(
            "UPDATE proposal SET patch_status=?, regression_ok=?, apply_journal_json=? "
            "WHERE id=?",
            (patch_status, 0 if patch_status == "failed" else None,
             json.dumps(journal, ensure_ascii=False), proposal_id))
        self._conn.commit()

    def patch_proposal_patch_status(self, proposal_id: int, patch_status: str,
                                    regression_run_id: str | None = None,
                                    regression_ok: int | None = None) -> None:
        """更新落地状态和回归结果 (终态, 同时清掉 apply journal)。"""
        self._conn.execute(
            "UPDATE proposal SET patch_status=?, regression_run_id=?, "
            "regression_ok=?, apply_journal_json=NULL WHERE id=?",
            (patch_status, regression_run_id, regression_ok, proposal_id))
        self._conn.commit()

    # ---------- 自演化: relation_health ----------

    def upsert_relation_health(self, source: str, target: str, via: str,
                               use_count_delta: int = 0,
                               suspect_count_delta: int = 0,
                               batch_id: str | None = None) -> None:
        """UPSERT: use_count += delta, suspect_count += delta."""
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO relation_health "
            "(source, target, via, use_count, suspect_count, "
            " last_batch_id, last_used_at, last_suspect_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source, target, via) DO UPDATE SET "
            " use_count = use_count + excluded.use_count, "
            " suspect_count = suspect_count + excluded.suspect_count, "
            " last_batch_id = COALESCE(excluded.last_batch_id, last_batch_id), "
            " last_used_at = CASE WHEN excluded.use_count > 0 "
            "   THEN excluded.last_used_at ELSE last_used_at END, "
            " last_suspect_at = CASE WHEN excluded.suspect_count > 0 "
            "   THEN excluded.last_suspect_at ELSE last_suspect_at END",
            (source, target, via, use_count_delta, suspect_count_delta,
             batch_id,
             now if use_count_delta > 0 else None,
             now if suspect_count_delta > 0 else None))
        self._conn.commit()

    def get_relation_health(self, source: str, target: str,
                            via: str) -> dict | None:
        """返回单条边健康数据，不存在返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM relation_health "
            "WHERE source=? AND target=? AND via=?",
            (source, target, via)).fetchone()
        return dict(row) if row else None

    def list_suspect_relations(self, min_suspect: int = 1) -> list[dict]:
        """suspect_count >= min_suspect 的边，按 suspect_count DESC。"""
        rows = self._conn.execute(
            "SELECT * FROM relation_health "
            "WHERE suspect_count >= ? ORDER BY suspect_count DESC",
            (min_suspect,)).fetchall()
        return [dict(r) for r in rows]

    def update_mismatch_details(self, source: str, target: str, via: str,
                                col_pair: str, cq_id: str) -> None:
        """JSON 原子更新: mismatch_details[col_pair].count++ / cq_ids.append()"""
        # 确保行存在
        self.upsert_relation_health(source, target, via)
        row = self._conn.execute(
            "SELECT mismatch_details FROM relation_health "
            "WHERE source=? AND target=? AND via=?",
            (source, target, via)).fetchone()
        details = json.loads(row["mismatch_details"]) if row else {}
        entry = details.get(col_pair, {"count": 0, "cq_ids": []})
        entry["count"] += 1
        if cq_id not in entry["cq_ids"]:
            entry["cq_ids"].append(cq_id)
        details[col_pair] = entry
        self._conn.execute(
            "UPDATE relation_health SET mismatch_details=? "
            "WHERE source=? AND target=? AND via=?",
            (json.dumps(details, ensure_ascii=False), source, target, via))
        self._conn.commit()

    # ---------- 自演化: relation soft_delete ----------

    def soft_delete_relation(self, source: str, target: str, via: str) -> None:
        """软删边: UPDATE relation SET status=0 WHERE ...（用于淘汰）"""
        self._conn.execute(
            "UPDATE relation SET status=0 "
            "WHERE source=? AND target=? AND via=?",
            (source, target, via))
        self._conn.commit()

    # ---------- 组装 build_indexes 契约 dict ----------

    def load_raw(self) -> dict:
        return {
            "entities": self._load_entities(),
            "relationships": self._load_relationships(),
            "valueSets": self._load_value_sets(),
            "domains": self._load_domains(),
            "filterRules": self._load_filter_rules(),
            "concepts": self._load_concepts(),
        }

    def _load_domains(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code, color, display_order, description FROM domain "
            "WHERE status=1 ORDER BY display_order, code"
        ).fetchall()
        return [{"code": r["code"], "color": r["color"],
                 "displayOrder": r["display_order"] or 0,
                 "description": r["description"] or ""} for r in rows]

    def _load_entities(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM entity WHERE status=1 ORDER BY id"
        ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def _row_to_entity(self, r: sqlite3.Row) -> dict:
        e = dict(json.loads(r["extra_json"] or "{}"))
        e.update({
            "id": r["id"],
            "entityCode": r["entity_code"],
            "displayName": r["displayName"],
            "domain": r["domain"],
            "maps_to": r["maps_to"],
            "data_source": r["data_source"] or "",
            "tables": json.loads(r["tables_json"] or "[]"),
            "attributes": json.loads(r["attributes_json"] or "[]"),
            "summary": r["summary"] or "",
        })
        return e

    def _load_relationships(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM relation WHERE status=1 ORDER BY id"
        ).fetchall()
        return [self._row_to_relation(r) for r in rows]

    def _row_to_relation(self, r: sqlite3.Row) -> dict:
        rel = dict(json.loads(r["extra_json"] or "{}"))
        rel.update({
            "source": r["source"], "target": r["target"], "type": r["type"],
            "via": r["via"], "label": r["label"],
            "source_col": r["source_col"], "target_col": r["target_col"],
            "bridge_count": r["bridge_count"],
        })
        return rel

    def _load_value_sets(self) -> dict:
        rows = self._conn.execute(
            "SELECT * FROM enum_value WHERE status=1"
        ).fetchall()
        return {
            (r["entity"], r["field"]): {
                "name": r["name"], "spec_ref": r["spec_ref"],
                "codes": json.loads(r["codes_json"] or "[]"),
            }
            for r in rows
        }

    # ---------- 私有 ----------

    def _soft_delete(self, table: str, key_col: str, key: str) -> None:
        self._soft_delete_stmt(table, key_col, key)
        self._conn.commit()

    def _soft_delete_stmt(self, table: str, key_col: str, key: str) -> None:
        """无 commit 版, 供复合单事务方法复用。table/key_col 是内部常量非用户输入。"""
        self._conn.execute(
            f"UPDATE {table} SET status=0 WHERE {key_col}=?", (key,)
        )
