"""agent/toolconfig/skills.py — skill 发现 + 启停状态 (真相在文件系统, config 只记状态)。

约定:
- skill = skills/<name>/SKILL.md, name 以目录名为准 (frontmatter name 仅展示)。
- config/skills.json 里没有某个 skill 名 = 视为启用 (缺省全开, 同 tools.py)。
- discover() 逐个 skill 各自 try, 坏的跳过+log, 绝不掀翻整轮。
"""
import json
import re
import shutil
from pathlib import Path

from core.observability import log_event

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _cfg_path() -> Path:
    return _CONFIG_DIR / "skills.json"


# skill name 白名单: 小写字母/数字打头, 后续可含 - _。拒路径分隔符/../点开头/大写/中文/空格。
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _safe_skill_dir(name: str) -> Path:
    """把 name 解析成 _SKILLS_DIR 内的安全目录路径; 非法抛 ValueError。

    三层防护 (写比读危险, 照 mcp_server 的 _resolve_in_sandbox):
    ① 白名单语法层: name 必须匹配 ^[a-z0-9][a-z0-9_-]*$
    ② 拼路径: _SKILLS_DIR / name
    ③ 真实路径层: resolve 后 parent 必须正好是 _SKILLS_DIR (防软链绕过)
    """
    if not _NAME_RE.match(name):
        raise ValueError(f"非法 skill name: {name!r}")
    target = _SKILLS_DIR / name
    if target.resolve().parent != _SKILLS_DIR.resolve():
        raise ValueError(f"越出 skills 目录: {name}")
    return target


def _parse_frontmatter(text: str) -> dict:
    """极简 YAML frontmatter 解析: 取首个 ---...--- 块里的 key: value 行。

    只认 name / description 两个键 (第一版够用, 不引 yaml 依赖)。
    健壮性 (Win11 CRLF 坑): 先归一 CRLF。value 用 partition(":") 只切第一个冒号
    (description 含 URL 的 https:// 不被截断)。frontmatter 未闭合 → 返回 {} (坏 skill),
    绝不把整篇当 frontmatter。缺 description → 返回 {} (视为坏 skill)。
    """
    text = text.replace("\r\n", "\n")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:                       # 未闭合: 绝不把整篇当 frontmatter
        return {}
    meta = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")   # partition 只切第一个冒号, URL 安全
            meta[k.strip()] = v.strip()
    if "description" not in meta:
        return {}
    return meta


def discover() -> list[dict]:
    """扫 skills/ 目录, 逐个读 SKILL.md frontmatter → [{name, description}]。

    name 以目录名为准。坏 frontmatter / 读不了 → log skill_skip + 跳过。
    """
    if not _SKILLS_DIR.exists():
        return []
    out = []
    skipped = 0
    for d in sorted(_SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        try:
            meta = _parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
            if not meta:
                raise ValueError("缺 frontmatter 或 description")
            out.append({"name": d.name, "description": meta["description"]})
        except (OSError, ValueError) as e:
            skipped += 1
            log_event("skillconfig.skill_skip", {"name": d.name, "error": str(e)[:200]})
    log_event("skillconfig.discover", {"n_found": len(out), "n_skipped": skipped})
    return out


def load() -> dict:
    """读 config/skills.json; 缺文件/坏 JSON → {} (全缺省=全开)。"""
    p = _cfg_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log_event("skillconfig.load_error", {"error": str(e)[:200]})
        return {}


def set_enabled(name: str, enabled: bool) -> None:
    """read-modify-write: 只改一个 skill 的 enabled, 落盘。"""
    data = load()
    data[name] = {"enabled": enabled}
    _CONFIG_DIR.mkdir(exist_ok=True)
    _cfg_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("skillconfig.skill_set", {"name": name, "enabled": enabled})


def create(name: str, content: str) -> None:
    """新建 skill: skills/<name>/SKILL.md。

    校验顺序 (全在 mkdir 之前, 避免残留空目录):
    ① _safe_skill_dir 过三层  ② 目录已存在 → ValueError (不覆盖)
    ③ _parse_frontmatter 校验 (缺 name/description/未闭合 → ValueError)
    三者全过才 mkdir + 写盘。content 先 CRLF 归一。
    """
    target = _safe_skill_dir(name)
    if target.exists():
        raise ValueError(f"skill 已存在: {name}")
    content = content.replace("\r\n", "\n")
    if not _parse_frontmatter(content):
        raise ValueError("SKILL.md 缺合法 frontmatter (需 name + description)")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(content, encoding="utf-8", newline="")
    log_event("skillconfig.create", {"name": name, "bytes": len(content.encode("utf-8"))})


def update_body(name: str, content: str) -> None:
    """覆写已有 skill 的 SKILL.md 正文。

    name 过三层; 目录不存在 → KeyError; content 过 frontmatter 校验 (坏 → ValueError,
    老正文不被破坏 —— 校验在写盘前)。CRLF 归一。
    """
    target = _safe_skill_dir(name)
    if not target.exists():
        raise KeyError(f"skill 不存在: {name}")
    content = content.replace("\r\n", "\n")
    if not _parse_frontmatter(content):
        raise ValueError("SKILL.md 缺合法 frontmatter (需 name + description)")
    (target / "SKILL.md").write_text(content, encoding="utf-8", newline="")
    log_event("skillconfig.update", {"name": name, "bytes": len(content.encode("utf-8"))})


def delete(name: str) -> None:
    """递归删除 skills/<name>/ 整个目录。

    name 过三层; 目录不存在 → KeyError。用 shutil.rmtree。
    """
    target = _safe_skill_dir(name)
    if not target.exists():
        raise KeyError(f"skill 不存在: {name}")
    shutil.rmtree(target)
    log_event("skillconfig.delete", {"name": name})


def enabled_catalog() -> list[dict]:
    """discover() ∩ 启用 → [{name, description}]。L1 清单来源。"""
    data = load()
    return [s for s in discover() if data.get(s["name"], {}).get("enabled", True)]


def read_full(name: str) -> str:
    """读某 skill 完整 SKILL.md (含 frontmatter), 供前端编辑用。

    与 read_body 对称: read_body 剥 frontmatter 做只读预览, read_full 保留全文
    让编辑框拿到完整内容 —— 回写 update_body 才过 frontmatter 校验 (读写契约对称)。
    用户能顺手改 frontmatter 的 description。
    - 目录/文件不存在 → KeyError (同 read_body "取不到正文" 语义)
    - CRLF 归一: 前端编辑框不见 \\r (落盘时 update_body 再统一处理)
    注意: 改 frontmatter 的 name 不重命名目录 (目录名才是 skill 真相, 见模块头注释)。
    """
    p = _SKILLS_DIR / name / "SKILL.md"
    if not p.exists():
        raise KeyError(f"skill 不存在: {name}")
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def read_body(name: str) -> str:
    """读某 skill SKILL.md 正文 (去 frontmatter)。

    失败矩阵:
    - 目录/文件不存在, 或目录在但无 SKILL.md → KeyError ("取不到正文" 统一语义)
    - frontmatter 坏但正文可读 → 返回去 frontmatter 后的正文 (正文不依赖 frontmatter 合法性)
    健壮切割 (CRLF 坑): 先归一 CRLF, 用 split 拿正文段, 不手算偏移量。
    """
    p = _SKILLS_DIR / name / "SKILL.md"
    if not p.exists():
        raise KeyError(f"skill 不存在: {name}")
    text = p.read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.startswith("---"):
        parts = text.split("\n---", 1)          # [frontmatter含头, "\n<剩余>"]
        if len(parts) == 2:
            # parts[1] 形如 "\n<正文>" (第二个 --- 后的内容); 去掉紧跟的 --- 行残余
            rest = parts[1].lstrip("-").lstrip()
            return rest
    return text.strip()


def list_status() -> list[dict]:
    """[{name, description, enabled}], 以 discover() (磁盘) 为准, 死配置项忽略。"""
    data = load()
    return [{"name": s["name"], "description": s["description"],
             "enabled": data.get(s["name"], {}).get("enabled", True)}
            for s in discover()]
