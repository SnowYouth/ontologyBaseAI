"""废弃 env 闸: git 跟踪文件不许再引用 LLM 层重构前的那几个 env 变量名。

★ 本闸防的是"以后有人(含 AI)照老文档把那几个变量名写回去"。
★ 两条判据缺一不可:
  1. 这些名字在 core/config.py 里确实已经不存在 (= 它们真的是废弃的);
  2. git 跟踪文件里零命中 (= 没有残留引用在误导读者)。
  只有 1 没有 2 = 留着误导; 只有 2 没有 1 = 闸在禁一个还在用的变量。
★ 闸自身不留废弃名字面量 (从 _DEPRECATED_PATH 运行时读) —— 否则它会自我命中,
  正向用例永远绿不了 (2026-10-15 test_repo_denylist 真踩过)。
"""
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEPRECATED_PATH = "tests/_deprecated_env.txt"   # 闸的唯一豁免路径 (精确匹配)


def _load_names(path: Path | None = None) -> list[str]:
    """读废弃变量名清单 (跳过空行与 # 注释)。"""
    p = path or (_ROOT / _DEPRECATED_PATH)
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _tracked_files() -> list[str]:
    """git 跟踪的文件清单 (读 index, 故 git add 过的新文件也算)。"""
    r = subprocess.run(["git", "ls-files", "-z"], cwd=_ROOT,
                       capture_output=True, check=True)
    return [f for f in r.stdout.decode("utf-8").split("\0") if f]


def _scan(names: list[str]) -> dict[str, list[str]]:
    r"""扫所有跟踪文件, 返回 {文件路径: [命中的废弃变量名]}。

    词边界匹配 (\\b), **大小写敏感** —— env 名按惯例全大写, 小写同名词
    (如文档里的 "agent model") 不该算命中。
    """
    if not names:
        return {}
    pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
    hits: dict[str, list[str]] = {}
    for rel in _tracked_files():
        if rel == _DEPRECATED_PATH:      # ★ 唯一豁免: 精确路径
            continue
        f = _ROOT / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                     # 二进制/不可读 跳过
        found = {m.group(1) for m in pat.finditer(text)}
        if found:
            hits[rel] = sorted(found)
    return hits


def test_这些变量在config里确实已不存在():
    """判据 1: 名单里的名字必须在 core/config.py 零命中。

    ★ 没有这一条, 闸就可能在禁一个还在用的变量 —— 那时正确做法是接线而非删引用。
    """
    cfg = (_ROOT / "core" / "config.py").read_text(encoding="utf-8")
    names = _load_names()
    still_used = [n for n in names if re.search(r"\b" + re.escape(n) + r"\b", cfg)]
    assert not still_used, (
        f"这些名字仍在 core/config.py 里被读: {still_used} —— 它们不是废弃变量, "
        "该从名单里去掉, 而不是删别处的引用")


def test_git跟踪文件不含废弃env():
    """判据 2: 清理完成后这条必须绿。"""
    hits = _scan(_load_names())
    assert not hits, (
        f"以下 git 跟踪文件引用了废弃 env 变量 ({len(hits)} 个文件):\n" +
        "\n".join(f"  {f}: {', '.join(ns)}" for f, ns in sorted(hits.items())[:30]) +
        f"\n真相源是 config/model.json (core/model_config.py)。名单见 {_DEPRECATED_PATH}")


def test_反证一_index里塞废弃env必红():
    """反证 1: git add 一个含废弃 env 的文件到 index → 闸必须逮住。

    ★ 为什么走 git index 而不是 tmp_path: 闸扫的是 `git ls-files` (读 index),
    tmp_path 的文件压根不在扫描集里 —— 那种反证要么必然绿要么必然红, 都证不了判据。
    """
    names = _load_names()
    废弃名 = names[0]
    probe = _ROOT / "_deprecated_env_probe.tmp.py"
    probe.write_text(f'import os\nx = os.getenv("{废弃名}")\n', encoding="utf-8")
    try:
        subprocess.run(["git", "add", "-f", str(probe)], cwd=_ROOT, check=True,
                       capture_output=True)
        hits = _scan(names)
        assert "_deprecated_env_probe.tmp.py" in hits, "闸没逮住 index 里的废弃 env = 假闸"
        assert 废弃名 in hits["_deprecated_env_probe.tmp.py"]
    finally:
        subprocess.run(["git", "rm", "-f", "--cached", "--quiet", str(probe)],
                       cwd=_ROOT, capture_output=True)
        probe.unlink(missing_ok=True)


def test_反证二_名单清空则永绿(tmp_path):
    """反证 2: 名单为空时闸必须退化成永绿 —— 专门逮"豁免写宽了"。

    ★ 若豁免逻辑写宽 (如按 tests/manual/ 整目录跳过), 正向用例在没清理时也会绿;
    这一向能证明"绿"确实来自名单生效, 而不是来自扫描集被豁免掉了。
    """
    empty = tmp_path / "empty.txt"
    empty.write_text("# 全注释, 零条目\n", encoding="utf-8")
    assert _load_names(empty) == []
    assert _scan(_load_names(empty)) == {}


def test_反证三_豁免只认精确路径且闸自身不留废弃名():
    """反证 3: 豁免必须是单一精确路径, 且必须是"必要的"; 闸自身不许含废弃名。"""
    assert _DEPRECATED_PATH == "tests/_deprecated_env.txt"
    assert not _DEPRECATED_PATH.endswith("/"), "豁免不许是目录前缀"
    assert "*" not in _DEPRECATED_PATH, "豁免不许是通配模式"

    names = _load_names()
    pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
    # (1) 豁免是必要的: 名单文件必然被自己的正则命中
    raw = (_ROOT / _DEPRECATED_PATH).read_text(encoding="utf-8")
    assert pat.search(raw), "名单文件自己不含废弃名? 那豁免就没有意义"
    # (2) ★ 闸自身不许含废弃名字面量 —— 否则正向用例永远绿不了
    gate = Path(__file__).read_text(encoding="utf-8")
    assert not pat.search(gate), (
        "闸文件自身含废弃名字面量 -> 正向用例永远绿不了。要从名单文件运行时读")


def test_名单文件本身可读且非空():
    names = _load_names()
    assert len(names) >= 3, f"名单只有 {len(names)} 条, 疑似读错"
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", n) for n in names), \
        f"名单该是全大写 env 名, 得到 {names}"
