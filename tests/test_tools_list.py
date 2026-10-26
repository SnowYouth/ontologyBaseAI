"""tools/list_dir.py 单元测试."""
import pytest


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.base.PROJECT_ROOT", str(tmp_path))
    return tmp_path


class TestListDir:
    def test_列出文件和目录(self, fake_project):
        from agent.tools.list_dir import list_dir
        (fake_project / "a.md").write_text("hello", encoding="utf-8")
        (fake_project / "b.txt").write_text("world", encoding="utf-8")
        (fake_project / "sub").mkdir()
        result = list_dir(path=".")
        assert result["n"] == 3
        names = {item["name"] for item in result["items"]}
        assert names == {"a.md", "b.txt", "sub"}
        types = {item["name"]: item["type"] for item in result["items"]}
        assert types["a.md"] == "file"
        assert types["sub"] == "dir"

    def test_文件_size_字段(self, fake_project):
        from agent.tools.list_dir import list_dir
        (fake_project / "x.md").write_text("12345", encoding="utf-8")
        result = list_dir(path=".")
        x = next(i for i in result["items"] if i["name"] == "x.md")
        assert x["size"] == 5

    def test_目录_size_为_0(self, fake_project):
        from agent.tools.list_dir import list_dir
        (fake_project / "sub").mkdir()
        result = list_dir(path=".")
        sub = next(i for i in result["items"] if i["name"] == "sub")
        assert sub["size"] == 0

    def test_隐藏文件被跳过(self, fake_project):
        from agent.tools.list_dir import list_dir
        (fake_project / ".env").write_text("secret", encoding="utf-8")
        (fake_project / ".git").mkdir()
        (fake_project / "visible.md").write_text("x", encoding="utf-8")
        result = list_dir(path=".")
        names = {item["name"] for item in result["items"]}
        assert names == {"visible.md"}

    def test_路径越界_抛(self, fake_project):
        from agent.tools.list_dir import list_dir
        with pytest.raises(ValueError, match="越出项目根"):
            list_dir(path="../../etc")

    def test_非目录_返回_error(self, fake_project):
        from agent.tools.list_dir import list_dir
        (fake_project / "f.md").write_text("x", encoding="utf-8")
        result = list_dir(path="f.md")
        assert "error" in result
        assert "not a directory" in result["error"]
