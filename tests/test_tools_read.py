"""tools/read_file.py 单元测试 — 用 tmp_path 造文件, monkeypatch PROJECT_ROOT."""
import pytest


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """把 PROJECT_ROOT monkeypatch 到 tmp_path, 测试隔离."""
    monkeypatch.setattr("agent.tools.base.PROJECT_ROOT", str(tmp_path))
    return tmp_path


class TestReadFile:
    def test_正常读_短文件(self, fake_project):
        from agent.tools.read_file import read_file
        f = fake_project / "hello.md"
        f.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
        result = read_file(path="hello.md")
        assert result["total_lines"] == 3
        assert result["start"] == 0
        assert result["end"] == 3
        assert result["truncated"] is False
        assert "line 1" in result["content"]

    def test_offset_limit_分页(self, fake_project):
        from agent.tools.read_file import read_file
        lines = "\n".join(f"L{i}" for i in range(500)) + "\n"
        (fake_project / "big.md").write_text(lines, encoding="utf-8")
        result = read_file(path="big.md", offset=100, limit=50)
        assert result["start"] == 100
        assert result["end"] == 150
        assert result["truncated"] is True
        assert "L100" in result["content"]
        assert "L150" not in result["content"]

    def test_truncated_最后一页_为_false(self, fake_project):
        from agent.tools.read_file import read_file
        lines = "\n".join(f"L{i}" for i in range(100)) + "\n"
        (fake_project / "f.md").write_text(lines, encoding="utf-8")
        result = read_file(path="f.md", offset=90, limit=50)
        assert result["end"] == 100
        assert result["truncated"] is False

    def test_路径越界_抛(self, fake_project):
        from agent.tools.read_file import read_file
        with pytest.raises(ValueError, match="越出项目根"):
            read_file(path="../../etc/passwd")

    def test_不存在文件_抛(self, fake_project):
        from agent.tools.read_file import read_file
        with pytest.raises(FileNotFoundError):
            read_file(path="nonexistent.md")

    def test_limit_上限_2000_裁剪(self, fake_project):
        from agent.tools.read_file import read_file
        lines = "\n".join(f"L{i}" for i in range(3000)) + "\n"
        (fake_project / "huge.md").write_text(lines, encoding="utf-8")
        result = read_file(path="huge.md", offset=0, limit=99999)
        assert result["end"] == 2000  # limit 被裁到 2000
        assert result["truncated"] is True
