"""mcp_server/server.py 单元测试 — 直接调用 write_file, 不走 MCP 协议."""
import pytest
import agent.mcp_server.server as srv


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """每个测试把 SANDBOX 重定向到 tmp_path/notes_ai, 不污染真实目录."""
    box = tmp_path / "notes_ai"
    monkeypatch.setattr(srv, "SANDBOX", box)
    return box


class TestWriteFile:
    def test_write_file_正常写入_返回ok_path_bytes(self, sandbox):
        result = srv.write_file("note.md", "hello")
        assert result["ok"] is True
        path = result["path"]
        assert result["bytes"] == len("hello".encode("utf-8"))
        from pathlib import Path
        f = Path(path)
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "hello"

    def test_write_file_路径分隔符斜杠_拒绝(self):
        with pytest.raises(ValueError):
            srv.write_file("a/b.md", "x")

    def test_write_file_路径分隔符反斜杠_拒绝(self):
        with pytest.raises(ValueError):
            srv.write_file("a\\b.md", "x")

    def test_write_file_含dotdot_拒绝(self):
        with pytest.raises(ValueError):
            srv.write_file("../evil.md", "x")

    def test_write_file_时间戳防重名_同名加后缀(self, sandbox):
        r1 = srv.write_file("dup.md", "first")
        r2 = srv.write_file("dup.md", "second")
        assert r1["path"] != r2["path"]
        from pathlib import Path
        assert Path(r1["path"]).exists()
        assert Path(r2["path"]).exists()

    def test_write_file_中文UTF8_正确写入(self, sandbox):
        content = "你好世界"
        result = srv.write_file("中文.md", content)
        assert result["bytes"] == len(content.encode("utf-8"))
        from pathlib import Path
        assert Path(result["path"]).read_text(encoding="utf-8") == content
