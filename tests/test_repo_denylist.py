"""仓库级脱敏闸: git 跟踪文件不许含真实业务标识符。

★ 本闸防的是"以后有人(含 AI)再把真名写回去"。
★ 黑名单只存 sha256 (tests/_denylist_sha256.txt), 不存明文 —— 黑名单文件本身
  会随仓库公开, 存明文等于公开发布一份真名清单 (2026-10-23 最终产物复核逮到)。
★ 判据: 文件里每个 [A-Za-z0-9_]+ token 转小写, 枚举其所有长度 >= _MIN_LEN 的子串,
  任一子串哈希命中即红。等价于原先的"子串 + 大小写不敏感"口径, 只是比对对象换成哈希。
  中文按连续 CJK 段枚举 _CJK_MIN.._CJK_MAX 字的子串 (中文条目不得超过 _CJK_MAX 字)。
★ 豁免只认一个精确路径 (_DENYLIST_PATH), 不许按目录/按内容豁免 ——
  写宽了闸就首跑即绿、反证也绿 (= 从没测它声称测的)。
★ 反证用例用**合成**黑名单 (_FAKE), 不依赖真名 —— 真名明文不在仓库里。
"""
import hashlib
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DENYLIST_PATH = "tests/_denylist_sha256.txt"   # 闸的唯一豁免路径 (精确匹配)

_MIN_LEN = 3      # 黑名单最短条目长度; 更短的子串不必算哈希
_TOKEN = re.compile(r"[A-Za-z0-9_]+")
# 中文没有词边界, 按连续 CJK 段枚举子串; 段可能很长 (一整句注释), 故子串长度设上限
_CJK = re.compile(r"[一-鿿]+")
_CJK_MIN, _CJK_MAX = 2, 8

# 含黑名单子串但**不是**真名的整 token 白名单 (存哈希, 理由同黑名单):
# 一个已废弃治理位的键名 (test_web_ontology.py 的 extra=forbid 防回潮仍在用)
# + pnpm-lock.yaml 里一个无关的 npm 包名。
_ALLOW_TOKEN_HASHES = {
    "4341a78a444795ff55d42a5f7d773c99c1584eb3751b6bca141cb647e66f3699",
    "64d0008662b695518c654b3ce356e28fe67dda76553b08748c2f6de68349cfec",
}

# 合成真名: 反证用例的探针, 与真黑名单无关
_FAKE = "zq_fake_tbl"
_FAKE_HASHES = {hashlib.sha256(_FAKE.encode()).hexdigest()}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_denylist(path: Path | None = None) -> set[str]:
    """读黑名单哈希 (跳过空行与 # 注释)。"""
    p = path or (_ROOT / _DENYLIST_PATH)
    return {s.strip() for s in p.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.strip().startswith("#")}


def _tracked_files() -> list[str]:
    """git 跟踪的文件清单 (读 index, 故 git add 过的新文件也算)。"""
    r = subprocess.run(["git", "ls-files", "-z"], cwd=_ROOT,
                       capture_output=True, check=True)
    return [f for f in r.stdout.decode("utf-8").split("\0") if f]


def _scan_text(text: str, hashes: set[str],
               allow: set[str] | None = None) -> list[str]:
    """一段文本里命中的黑名单哈希 (去重排序)。**匹配判据的唯一实现** ——
    文件扫描、反证用例、最终产物复核共用这一口。

    token 转小写后枚举全部 >= _MIN_LEN 的子串比哈希 = "子串 + 大小写不敏感"。
    ★ 不能只比整 token: `\\b`/整词口径会漏掉 `<真名>_X_REL` (2026-10-16 真漏过)。
    """
    if not hashes:
        return []
    allow = _ALLOW_TOKEN_HASHES if allow is None else allow
    found: set[str] = set()
    for tok in {t.lower() for t in _TOKEN.findall(text)}:
        if len(tok) < _MIN_LEN or _sha(tok) in allow:
            continue
        n = len(tok)
        for i in range(n - _MIN_LEN + 1):
            for j in range(i + _MIN_LEN, n + 1):
                h = _sha(tok[i:j])
                if h in hashes:
                    found.add(h)
    for seg in set(_CJK.findall(text)):
        n = len(seg)
        for i in range(n - _CJK_MIN + 1):
            for j in range(i + _CJK_MIN, min(i + _CJK_MAX, n) + 1):
                h = _sha(seg[i:j])
                if h in hashes:
                    found.add(h)
    return sorted(found)


def _scan(hashes: set[str]) -> dict[str, list[str]]:
    """扫所有跟踪文件, 返回 {文件路径: [命中的哈希]}。判据见 _scan_text。"""
    if not hashes:
        return {}
    hits: dict[str, list[str]] = {}
    for rel in _tracked_files():
        if rel == _DENYLIST_PATH:       # ★ 唯一豁免: 精确路径
            continue
        f = _ROOT / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                     # 二进制/不可读 跳过
        found = _scan_text(text, hashes)
        if found:
            hits[rel] = found
    return hits


def test_scan_text子串命中且大小写不敏感():
    for 变体 in (_FAKE, _FAKE.upper(), f"SELECT * FROM {_FAKE}"):
        assert _scan_text(变体, _FAKE_HASHES), f"漏掉 {变体!r}"


def test_反证四_真名当更长标识符前缀也必须逮住():
    r"""反证 4: 真名拼成更长的标识符闸仍必须逮住。

    ★ 钉死 2026-10-16 那个真漏网: 闸原先用 `\\b` 词边界, 而 `\\b` 的"词"含下划线
    → 真名当前缀/后缀时整个漏过去。没有这一条, 改回整 token 比对不会有任何测试变红。
    """
    for 变体 in (f"{_FAKE}_X_REL", f"X_{_FAKE}", f"{_FAKE.upper()}_ID", f"a{_FAKE}b"):
        assert _scan_text(变体, _FAKE_HASHES), f"漏掉 {变体!r}"


def test_反证五_中文真名也必须逮住():
    """★ 2026-10-23: 真库的业务域名是中文, 原 token 只认 [A-Za-z0-9_], 中文条目
    进了黑名单也永远匹配不上 (= 闸对中文真名是摆设)。"""
    fake_cn = "甲乙丙域"
    h = {_sha(fake_cn)}
    for 变体 in (fake_cn, f'"code": "{fake_cn}"', f"// 真库里 `{fake_cn}` 域", f"名下{fake_cn}数"):
        assert _scan_text(变体, h), f"漏掉 {变体!r}"
    assert _scan_text("甲乙丁域", h) == []


def test_不含真名的文本不命中():
    assert _scan_text("SELECT id FROM lib_book WHERE zq_fake = 1", _FAKE_HASHES) == []


def test_白名单token整个跳过():
    allow_tok = "xx" + _FAKE + "yy"
    allow = {hashlib.sha256(allow_tok.encode()).hexdigest()}
    assert _scan_text(allow_tok.upper(), _FAKE_HASHES, allow) == []
    assert _scan_text(allow_tok + " " + _FAKE, _FAKE_HASHES, allow), \
        "白名单不许连带放过同行真名"


def test_命中返回的是哈希不是明文():
    hits = _scan_text(_FAKE, _FAKE_HASHES)
    assert hits == sorted(_FAKE_HASHES)


def test_git跟踪文件不含真实业务标识符():
    """正向: 脱敏完成后这条必须绿。"""
    hits = _scan(_load_denylist())
    assert not hits, (
        f"以下 git 跟踪文件含真实业务标识符 ({len(hits)} 个文件, 命中项为哈希前缀):\n" +
        "\n".join(f"  {f}: {', '.join(h[:12] for h in hs)}"
                  for f, hs in sorted(hits.items())[:30]))


def test_反证一_index里塞真名必红():
    """反证 1: git add 一个含(合成)真名的文件到 index → 闸必须逮住。

    ★ 为什么走 git index 而不是 tmp_path: 闸扫的是 `git ls-files` (读 index),
    tmp_path 的文件压根不在扫描集里 —— 那种反证要么必然绿要么必然红, 都证不了判据。
    ★ 探针放两处 (仓库根 + 与黑名单同目录的 tests/): 豁免若被写宽成"跳过 tests/
    目录", 只放根目录的探针照样绿, 逮不住。
    """
    rels = ["_denylist_probe.tmp.py", "tests/_denylist_probe.tmp.py"]
    for rel in rels:
        (_ROOT / rel).write_text(f"SQL = 'SELECT * FROM {_FAKE.upper()}_REL'\n",
                                 encoding="utf-8")
    try:
        subprocess.run(["git", "add", "-f", *rels], cwd=_ROOT, check=True,
                       capture_output=True)
        hits = _scan(_FAKE_HASHES)
        missed = [r for r in rels if r not in hits]
        assert not missed, f"闸没逮住 index 里的真名 = 假闸 (漏: {missed})"
    finally:
        subprocess.run(["git", "rm", "-f", "--cached", "--quiet", *rels],
                       cwd=_ROOT, capture_output=True)
        for rel in rels:
            (_ROOT / rel).unlink(missing_ok=True)


def test_反证二_黑名单清空则永绿(tmp_path):
    """反证 2: 黑名单为空时闸必须退化成永绿 —— 专门逮"豁免写宽了"。"""
    empty = tmp_path / "empty.txt"
    empty.write_text("# 全注释, 零条目\n", encoding="utf-8")
    assert _load_denylist(empty) == set()
    assert _scan(_load_denylist(empty)) == {}


def test_反证三_豁免只认精确路径():
    assert _DENYLIST_PATH == "tests/_denylist_sha256.txt"
    assert not _DENYLIST_PATH.endswith("/"), "豁免不许是目录前缀"
    assert "*" not in _DENYLIST_PATH, "豁免不许是通配模式"


def test_黑名单只含哈希不含明文():
    """★ 黑名单文件随仓库公开: 每个非注释行必须是 64 位小写十六进制。
    有人把明文名字直接追加进来, 这条立刻红。"""
    raw = (_ROOT / _DENYLIST_PATH).read_text(encoding="utf-8")
    entries = [s.strip() for s in raw.splitlines() if s.strip() and not s.startswith("#")]
    assert len(entries) >= 10, f"黑名单只有 {len(entries)} 条, 疑似读错"
    bad = [e for e in entries if not re.fullmatch(r"[0-9a-f]{64}", e)]
    assert not bad, f"黑名单里有 {len(bad)} 行不是 sha256 (疑似明文混入)"


def test_黑名单覆盖真本体库的结构标识符():
    """★ 2026-10-23 最终复核: 原 17 条只是真名的一小撮, 拿真 ontology.db 全量
    标识符一比, 产物里还漏着 ~35 个真列名/表名。黑名单扩到 50 条后, 这条钉住下限,
    防有人把条目删回去。(真库不随仓分发, 无法在 CI 里直接比对真库。)"""
    assert len(_load_denylist()) >= 50


def test_仓库不再跟踪明文黑名单():
    r = subprocess.run(["git", "ls-files", "tests/_denylist.txt"], cwd=_ROOT,
                       capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "", "明文黑名单仍在 git 跟踪里, 会随仓库公开"
