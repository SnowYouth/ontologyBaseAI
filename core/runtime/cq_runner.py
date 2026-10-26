"""core/runtime/cq_runner.py — 验证回路 B: 读考题集 + 跑单条 CQ + 结果轮转落盘.

CQ (Competency Question) = 考本体建得好不好的问题集。本模块只做"跑 + 收集素材 +
落盘", 判定由人在前端做。数据文件全在 kg_private/(不进 git)。
"""
import json
from pathlib import Path

from core.runtime.agent import run_agent
from core.conversation import Conversation

_ROOT = Path(__file__).resolve().parent.parent.parent
_KG = _ROOT / "kg_private"
CQ_SET_PATH = _KG / "cq_set.json"
SEED_CQ_PATH = _ROOT / "scripts" / "seed_cq.json"
CUR_PATH = _KG / "cq_result_current.json"
PREV_PATH = _KG / "cq_result_previous.json"


def ensure_cq_set(path: Path | None = None) -> Path:
    """保证考题集存在: 缺文件时从 scripts/seed_cq.json 合成, 返回该路径.

    已存在 → 原样返回, **绝不覆盖** (那是用户自己攒的考题)。
    seed 本身也缺 → 让 FileNotFoundError 抛出来 (响亮失败, 别吞)。

    ★ 为什么挂在本模块而不是 scripts/init_ontology.py: 后者只在冷启动建库时
    跑一次, 对"库早就建好了但没 cq_set"的人 (即机主自己, 以及任何先建库
    后更新代码的人) 不生效。考题集的生命周期与本体库无关 —— 谁读它谁负责
    保证它在, 所以闸挂在读取路径上。
    """
    p = path or CQ_SET_PATH
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SEED_CQ_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def _read_cq_set(path: Path | None = None) -> tuple[Path, dict]:
    """ensure + 读整份 data; 坏 JSON 抛 ValueError。

    ★ 读写两路共用这一个口子: 写端点若各自裸 json.loads, 考题集被手改坏时
    JSONDecodeError 会直穿 500, 前端只看到 "Failed to fetch" —— 用户点
    ➕ 新建想自救, 却连"坏在第几行"都看不到, #10 的死局就只解开了一半
    (缺文件能合成, 坏文件仍堵着)。统一成 ValueError → 端点 400 + 行列号。
    """
    p = ensure_cq_set(path)
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"考题集 JSON 解析失败: {e}") from e


def load_cq_set(path: Path | None = None) -> list[dict]:
    """读考题集的 cases 列表; 缺文件从 seed 合成, 坏 JSON 抛 ValueError。"""
    _, data = _read_cq_set(path)
    return data.get("cases", [])


def upsert_case(case: dict, path: Path | None = None, *, create_only: bool = False) -> str:
    """新建或改一条考题, 落盘。返回 "created" | "updated"。

    create_only=True 时若 id 已存在则抛 KeyError (给 POST 用, 转 409)。
    ★ 读改写整份 data 再回写, 不是只写 {"cases": ...} —— 顶层 `_comment`
    (seed 带的文件头注释) 以及人手加的其它键必须保住。
    """
    p, data = _read_cq_set(path)
    cases = data.get("cases", [])
    idx = next((i for i, c in enumerate(cases) if c["id"] == case["id"]), -1)
    if idx >= 0:
        if create_only:
            raise KeyError(f"考题 id 已存在: {case['id']}")
        cases[idx] = case
        action = "updated"
    else:
        cases.append(case)
        action = "created"
    data["cases"] = cases
    write_json(p, data)
    return action


def delete_case(cq_id: str, path: Path | None = None) -> bool:
    """删一条考题; 不存在返回 False (给端点转 404)。"""
    p, data = _read_cq_set(path)
    cases = data.get("cases", [])
    kept = [c for c in cases if c["id"] != cq_id]
    if len(kept) == len(cases):
        return False
    data["cases"] = kept
    write_json(p, data)
    return True


def read_results(path: Path) -> dict | None:
    """读一份结果 JSON; 不存在返回 None。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    """utf-8 落盘 (缩进 2, 不转义非 ASCII)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _collect_on_event(tool_events: list[dict], flags: dict):
    """返回一个 on_event 回调: tool_start 记 args 开一条, tool_end 就近回填。

    ★ used_edges 字段 (自演化 spec §3.2): tool_start 初始化 None,
    tool_end 从事件读取 (execute_sql 结果含此字段时透传)。
    ★ violations 字段 (自演化 spec §5.3): blocked 响应的 obqc violations,
    mismatch 累计消费; 同 used_edges 模式。
    ★ flags["truncated"] (2026-10-20): agent 撞 max_tokens 被切断时置 True。
    截断不抛异常, error 字段是 None, 光看 answer 认不出来 —— 空答案尤其。
    ★ no_db_evidence 不在这里收: 由 run_one_cq 事后从 tool_events 算 (_no_db_evidence)。
    """
    def on_event(ev):
        if ev.get("type") == "done" and ev.get("truncated"):
            # 多轮里任一轮截断都要留痕: 只置 True 不复位, 否则末轮的正常
            # done 会把中间轮的截断冲掉 (CQ-14 正是三轮里第三轮截断)。
            flags["truncated"] = True
        elif ev.get("type") == "tool_start":
            tool_events.append({"name": ev["name"], "args": ev.get("args", ""),
                                "result": None, "ms": None, "ok": None,
                                "used_edges": None, "violations": None})
        elif ev.get("type") == "tool_end":
            for t in reversed(tool_events):
                if t["name"] == ev["name"] and t["result"] is None:
                    t["result"] = ev.get("result")
                    t["ms"] = ev.get("latency_ms")
                    t["ok"] = ev.get("success")
                    t["used_edges"] = ev.get("used_edges")
                    t["violations"] = ev.get("violations")
                    break
    return on_event


def _no_db_evidence(tool_events: list[dict]) -> bool:
    """整轮没有一次 execute_sql 成功 → 答案没碰过真库 (2026-10-21)。

    真实事故: 2026-10-20 CQ-5 库没初始化, execute_sql 全失败, agent 转去
    read_file 读旧结果文件编出一篇带表格的长答案, error=None。先失败后修正
    成功是正常形态 (有的 CQ 撞了 1054 之后重查就对了), 所以判据是"零成功"而非"有失败"。
    """
    return not any(t["name"] == "execute_sql" and t["ok"] for t in tool_events)


def run_one_cq(case: dict) -> dict:
    """跑单条 CQ, 收集工具轨迹; 异常不外抛记进 error。不写 session 磁盘。"""
    tool_events: list[dict] = []
    flags: dict = {"truncated": False}
    on_event = _collect_on_event(tool_events, flags)
    answer, error = "", None
    try:
        if case.get("kind") == "multi_turn":
            conv = Conversation()
            for turn in case.get("turns") or []:
                answer = run_agent(turn, conversation=conv, on_event=on_event)
        else:
            answer = run_agent(case["question"], on_event=on_event)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    # ★ 自演化埋点: CQ 执行异常/error → failure_signal
    if error:
        try:
            from capabilities.ontology.selfevolve.signals import log_failure_signal
            log_failure_signal("cq_fail", {
                "cq_id": case["id"],
                "error": error,
            })
        except Exception:
            pass  # 热路径: 埋点失败不影响 CQ 收集
    return {"id": case["id"], "answer": answer, "tool_events": tool_events,
            "error": error, "truncated": flags["truncated"],
            "no_db_evidence": _no_db_evidence(tool_events)}


def rotate_current_to_previous() -> None:
    """把 current 内容搬到 previous(覆盖); current 不存在则不动。"""
    cur = read_results(CUR_PATH)
    if cur is not None:
        write_json(PREV_PATH, cur)


def save_current(results: list[dict], ontology_path: str, ran_at: str) -> None:
    """组装并落盘 current; 每条结果补 verdict=None/note=''(若无)。"""
    norm = []
    for r in results:
        r = {**r}
        r.setdefault("verdict", None)
        r.setdefault("note", "")
        norm.append(r)
    write_json(CUR_PATH, {"ran_at": ran_at, "ontology_path": ontology_path, "results": norm})


def set_verdict(cq_id: str, verdict: str, note: str) -> bool:
    """把某条 CQ 的人工打分/备注写回 current; 成功 True, current 缺或找不到 id False。"""
    cur = read_results(CUR_PATH)
    if cur is None:
        return False
    for r in cur["results"]:
        if r["id"] == cq_id:
            r["verdict"] = verdict
            r["note"] = note
            write_json(CUR_PATH, cur)
            return True
    return False
