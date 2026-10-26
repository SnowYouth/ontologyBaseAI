"""test_toolconfig_skills.py — skill 发现 + 启停状态 CRUD。

真相在文件系统 (扫 skills/<name>/SKILL.md); config/skills.json 只记关了谁 (缺省全开)。
monkeypatch 把 _CONFIG_DIR 和 _SKILLS_DIR 都指到 tmp_path 隔离磁盘。
跑法: uv run pytest tests/test_toolconfig_skills.py -v
"""
import pytest

from agent.toolconfig import skills


@pytest.fixture
def env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(skills, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(skills, "_SKILLS_DIR", skills_dir)
    return skills_dir


def _make_skill(skills_dir, name, description, body="正文内容"):
    d = skills_dir / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )
    return d


def test_discover_扫出frontmatter(env):
    _make_skill(env, "alpha", "阿尔法技能")
    _make_skill(env, "beta", "贝塔技能")
    found = {s["name"]: s["description"] for s in skills.discover()}
    assert found == {"alpha": "阿尔法技能", "beta": "贝塔技能"}


def test_discover_坏frontmatter跳过不炸(env):
    _make_skill(env, "good", "好的")
    bad = env / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("没有 frontmatter 的裸文本", encoding="utf-8")
    names = [s["name"] for s in skills.discover()]
    assert names == ["good"]


def test_discover_缺description跳过(env):
    d = env / "noDesc"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: noDesc\n---\n正文", encoding="utf-8")
    assert skills.discover() == []


def test_enabled_catalog_缺省全开(env):
    _make_skill(env, "a", "AAA")
    _make_skill(env, "b", "BBB")
    cat = {s["name"] for s in skills.enabled_catalog()}
    assert cat == {"a", "b"}


def test_set_enabled_关掉后catalog排除它(env):
    _make_skill(env, "a", "AAA")
    _make_skill(env, "b", "BBB")
    skills.set_enabled("b", False)
    cat = {s["name"] for s in skills.enabled_catalog()}
    assert cat == {"a"}


def test_set_enabled_落盘可读回(env):
    skills.set_enabled("x", False)
    assert skills.load() == {"x": {"enabled": False}}


def test_read_body_去frontmatter只留正文(env):
    _make_skill(env, "a", "AAA", body="这是能力包正文")
    assert skills.read_body("a").strip() == "这是能力包正文"


def test_read_body_不存在抛KeyError(env):
    with pytest.raises(KeyError):
        skills.read_body("nope")


def test_read_body_目录在但无SKILLmd抛KeyError(env):
    (env / "empty").mkdir()
    with pytest.raises(KeyError):
        skills.read_body("empty")


def test_read_body_CRLF正文干净(env):
    d = env / "crlf"
    d.mkdir()
    (d / "SKILL.md").write_bytes(
        "---\r\nname: crlf\r\ndescription: X\r\n---\r\n\r\n正文一行".encode("utf-8"))
    assert skills.read_body("crlf") == "正文一行"


def test_read_body_frontmatter坏但正文可读(env):
    # 无 description 的 frontmatter → discover 会跳过, 但 read_body 仍应给出正文
    d = env / "badmeta"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: badmeta\n---\n\n正文还在", encoding="utf-8")
    assert skills.read_body("badmeta") == "正文还在"


def test_read_full_返回含frontmatter完整内容(env):
    # read_full 给编辑用: 必须含 frontmatter, 这样回写 update_body 才过校验
    _make_skill(env, "f", "FFF", body="正文段")
    full = skills.read_full("f")
    assert full.startswith("---")
    assert "description: FFF" in full
    assert "正文段" in full
    # 关键闭环: read_full 的产物直接喂 update_body 必须过校验 (读写契约对称)
    skills.update_body("f", full)


def test_read_full_不存在抛KeyError(env):
    with pytest.raises(KeyError):
        skills.read_full("nope")


def test_read_full_CRLF归一(env):
    d = env / "crlf2"
    d.mkdir()
    (d / "SKILL.md").write_bytes(
        "---\r\nname: crlf2\r\ndescription: X\r\n---\r\n\r\n正文".encode("utf-8"))
    full = skills.read_full("crlf2")
    assert "\r" not in full          # 读端归一, 前端编辑框不见 \r


def test_discover_CRLF文件也能扫出(env):
    d = env / "win"
    d.mkdir()
    (d / "SKILL.md").write_bytes(
        "---\r\nname: win\r\ndescription: 温\r\n---\r\n正文".encode("utf-8"))
    found = {s["name"]: s["description"] for s in skills.discover()}
    assert found == {"win": "温"}   # description 无残留 \r


def test_discover_description含冒号URL不截断(env):
    d = env / "url"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: url\ndescription: 见 https://x.com 文档\n---\n正文", encoding="utf-8")
    found = {s["name"]: s["description"] for s in skills.discover()}
    assert found["url"] == "见 https://x.com 文档"


def test_discover_未闭合frontmatter跳过(env):
    d = env / "unclosed"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: x\ndescription: y\n正文没有闭合", encoding="utf-8")
    assert skills.discover() == []


def test_discover_子目录无SKILLmd跳过不炸(env):
    _make_skill(env, "good", "好的")
    (env / "empty").mkdir()      # 空目录, 无 SKILL.md → FileNotFoundError(OSError子类)被兜
    names = [s["name"] for s in skills.discover()]
    assert names == ["good"]


def test_list_status_以磁盘为准_带description和enabled(env):
    _make_skill(env, "a", "AAA")
    _make_skill(env, "b", "BBB")
    skills.set_enabled("a", False)
    status = {s["name"]: s for s in skills.list_status()}
    assert status["a"] == {"name": "a", "description": "AAA", "enabled": False}
    assert status["b"] == {"name": "b", "description": "BBB", "enabled": True}


def test_load_坏json不炸回退空dict(env):
    (env.parent / "skills.json").write_text("{ 坏", encoding="utf-8")
    assert skills.load() == {}


# ---------- 写能力: _safe_skill_dir 安全校验 ----------

def test_safe_skill_dir_合法name返回路径(env):
    assert skills._safe_skill_dir("my-skill") == env / "my-skill"
    assert skills._safe_skill_dir("abc_123") == env / "abc_123"


@pytest.mark.parametrize("bad", [
    "", "a/b", "a\\b", "..", "../x", ".hidden", "Abc", "中文", "a b", "a.b",
])
def test_safe_skill_dir_非法name抛ValueError(env, bad):
    with pytest.raises(ValueError):
        skills._safe_skill_dir(bad)


# ---------- 写能力: create ----------

_GOOD_MD = "---\nname: fresh\ndescription: 新技能\n---\n\n正文内容"


def test_create_建目录写SKILLmd_discover能发现(env):
    skills.create("fresh", _GOOD_MD)
    assert (env / "fresh" / "SKILL.md").exists()
    assert {s["name"] for s in skills.discover()} == {"fresh"}


def test_create_目录已存在拒绝不覆盖(env):
    _make_skill(env, "dup", "老的")
    with pytest.raises(ValueError):
        skills.create("dup", _GOOD_MD)
    # 老正文未被动
    assert "老的" in (env / "dup" / "SKILL.md").read_text(encoding="utf-8")


def test_create_frontmatter坏拒写且目录不残留(env):
    with pytest.raises(ValueError):
        skills.create("nofm", "没有 frontmatter 的裸文本")
    assert not (env / "nofm").exists()


def test_create_缺description拒写(env):
    with pytest.raises(ValueError):
        skills.create("nodesc", "---\nname: nodesc\n---\n正文")
    assert not (env / "nodesc").exists()


def test_create_CRLF输入落盘已归一(env):
    skills.create("crlfw", "---\r\nname: crlfw\r\ndescription: X\r\n---\r\n\r\n正文")
    raw = (env / "crlfw" / "SKILL.md").read_bytes()
    assert b"\r\n" not in raw


def test_create_非法name抛ValueError(env):
    with pytest.raises(ValueError):
        skills.create("../evil", _GOOD_MD)


# ---------- 写能力: update_body ----------

def test_update_body_正常覆写(env):
    _make_skill(env, "edit", "旧描述", body="旧正文")
    skills.update_body("edit", "---\nname: edit\ndescription: 新描述\n---\n\n新正文")
    assert skills.read_body("edit") == "新正文"
    assert {s["name"]: s["description"] for s in skills.discover()}["edit"] == "新描述"


def test_update_body_目录不存在抛KeyError(env):
    with pytest.raises(KeyError):
        skills.update_body("nope", _GOOD_MD)


def test_update_body_frontmatter坏拒写且老正文不变(env):
    _make_skill(env, "keep", "描述", body="原正文保住")
    with pytest.raises(ValueError):
        skills.update_body("keep", "坏内容无 frontmatter")
    assert skills.read_body("keep") == "原正文保住"


def test_update_body_非法name抛ValueError(env):
    with pytest.raises(ValueError):
        skills.update_body("../evil", _GOOD_MD)


# ---------- 写能力: delete ----------

def test_delete_递归删目录(env):
    _make_skill(env, "gone", "待删")
    skills.delete("gone")
    assert not (env / "gone").exists()
    assert skills.discover() == []


def test_delete_不存在抛KeyError(env):
    with pytest.raises(KeyError):
        skills.delete("nope")


def test_delete_非法name抛ValueError(env):
    with pytest.raises(ValueError):
        skills.delete("../evil")
