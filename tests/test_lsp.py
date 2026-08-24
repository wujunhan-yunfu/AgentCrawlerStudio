"""backend.services.lsp 测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


FAKE_LANGSERVER = r"""
import json, sys
def read(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get(b"content-length", 0))
    if length <= 0:
        return None
    return json.loads(stream.read(length).decode("utf-8"))
def write(stream, msg):
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
    stream.flush()
while True:
    msg = read(sys.stdin.buffer)
    if msg is None:
        break
    mid = msg.get("id")
    method = msg.get("method")
    result = {}
    if method == "initialize":
        result = {"capabilities": {"textDocumentSync": 1}}
    elif method == "shutdown":
        result = None
    if method in ("textDocument/didOpen", "textDocument/didChange", "textDocument/didSave",
                  "workspace/didChangeConfiguration"):
        continue
    if mid is not None:
        write(sys.stdout.buffer, {"jsonrpc": "2.0", "id": mid, "result": result})
    if method == "exit":
        break
"""


@pytest.fixture(scope="module")
def fake_langserver(tmp_path_factory):
    p = tmp_path_factory.mktemp("lsp") / "fake_langserver.py"
    p.write_text(FAKE_LANGSERVER, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- 模块级工具


def test_project_root():
    from backend.services.lsp import _project_root

    root = _project_root()
    assert (root / "pyproject.toml").exists()


def test_python_path(monkeypatch):
    from backend.services.lsp import _python_path

    path = _python_path()
    assert path.endswith("python") or "python" in path


def test_pyright_settings():
    from backend.services.lsp import _pyright_settings

    s = _pyright_settings()
    assert s["python"]["analysis"]["typeCheckingMode"] == "basic"
    assert "pythonPath" in s["python"]


def test_langserver_command(monkeypatch, tmp_path):
    from backend.services.lsp import _langserver_command

    # frontend 路径存在
    root = tmp_path
    monkeypatch.setattr("backend.services.lsp._project_root", lambda: root)
    (root / "frontend" / "node_modules" / ".bin").mkdir(parents=True)
    (root / "frontend" / "node_modules" / ".bin" / "pyright-langserver").touch()
    cmd = _langserver_command()
    assert cmd == [str(root / "frontend" / "node_modules" / ".bin" / "pyright-langserver"), "--stdio"]

    # root node_modules 存在
    (root / "node_modules" / ".bin").mkdir(parents=True)
    (root / "node_modules" / ".bin" / "pyright-langserver").touch()
    cmd2 = _langserver_command()
    assert "node_modules" in cmd2[0]

    # which 命中
    monkeypatch.setattr("backend.services.lsp._project_root", lambda: tmp_path / "empty")
    monkeypatch.setattr("backend.services.lsp.shutil.which", lambda name: "/usr/bin/pyright-langserver")
    cmd3 = _langserver_command()
    assert cmd3 == ["/usr/bin/pyright-langserver", "--stdio"]

    # 找不到 -> 抛错
    monkeypatch.setattr("backend.services.lsp.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="pyright-langserver"):
        _langserver_command()


def test_filter_injected_globals():
    from backend.services.lsp import _filter_injected_globals

    assert _filter_injected_globals("Undefined name 'page'") is False
    assert _filter_injected_globals('Name "browser" is not defined') is False
    assert _filter_injected_globals("Undefined name 'foo'") is True
    assert _filter_injected_globals("ok") is True


def test_jsonrpc_roundtrip(monkeypatch):
    from backend.services.lsp import _read_jsonrpc, _write_jsonrpc

    async def main():
        reader = asyncio.StreamReader()
        writer = _MemoryStreamWriter()
        await _write_jsonrpc(writer, {"a": 1})
        reader.feed_data(writer.getvalue())
        reader.feed_eof()
        msg = await _read_jsonrpc(reader)
        assert msg == {"a": 1}
        assert await _read_jsonrpc(reader) is None

    asyncio.run(main())


def test_read_jsonrpc_errors():
    from backend.services.lsp import _read_jsonrpc

    async def main():
        reader = asyncio.StreamReader()
        # 无 content-length
        reader.feed_data(b"\r\n")
        assert await _read_jsonrpc(reader) is None
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 0\r\n\r\n")
        assert await _read_jsonrpc(reader) is None
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 5\r\n\r\nnotjson")
        assert await _read_jsonrpc(reader) is None
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: abc\r\n\r\n")
        with pytest.raises(ValueError):
            await _read_jsonrpc(reader)

    asyncio.run(main())


class _MemoryStreamWriter:
    def __init__(self):
        self._buf = b""

    def write(self, data):
        self._buf += data

    async def drain(self):
        return None

    def getvalue(self):
        return self._buf


# --------------------------------------------------------------------------- LspManager


def test_lsp_manager_workspace_and_info():
    from backend.services.lsp import LspManager

    mgr = LspManager()
    ws, uri, doc = mgr.ensure_workspace()
    assert (ws / "xvfb_env.py").exists()
    assert doc == ws / "main.py"
    assert uri.endswith("main.py")
    # 复用
    ws2, uri2, doc2 = mgr.ensure_workspace()
    assert ws2 == ws

    info = mgr.info()
    assert info["name"] == "pyright"
    assert "page" in info["injected_globals"]


async def test_lsp_manager_create_drop_session(fake_langserver, monkeypatch):
    from backend.services.lsp import LspManager

    monkeypatch.setattr("backend.services.lsp._langserver_command", lambda: [sys.executable, fake_langserver])
    mgr = LspManager()

    class FakeWS:
        pass

    session = await mgr.create_session(FakeWS())
    assert len(mgr._sessions) == 1
    mgr.drop_session(session)
    assert mgr._sessions == []
    mgr.drop_session(session)  # 幂等
    await session.stop()


# --------------------------------------------------------------------------- LspSession


@pytest.fixture()
async def lsp_session(fake_langserver, monkeypatch):
    from backend.services.lsp import LspManager

    monkeypatch.setattr("backend.services.lsp._langserver_command", lambda: [sys.executable, fake_langserver])
    mgr = LspManager()
    ws, uri, doc = mgr.ensure_workspace()
    ws_obj = FakeWebSocket()
    session = await mgr.create_session(ws_obj)
    await session.start()
    yield session, ws_obj
    await session.stop()


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self._queue = asyncio.Queue()

    def queue(self, msg):
        self._queue.put_nowait(msg)

    def end(self):
        self._queue.put_nowait(_END)

    async def receive_text(self):
        item = await self._queue.get()
        if item is _END:
            raise _FakeDisconnect()
        return item

    async def send_text(self, text):
        self.sent.append(text)


class _FakeDisconnect(Exception):
    pass


_END = object()


async def test_lsp_session_initialize(lsp_session):
    session, ws = lsp_session
    await session._write_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"capabilities": {}},
    })
    # _read_pyright 转发响应到 ws, 轮询等待
    for _ in range(100):
        for text in list(ws.sent):
            msg = json.loads(text)
            if msg.get("id") == 1:
                assert "capabilities" in msg["result"]
                return
        await asyncio.sleep(0.05)
    raise AssertionError("no initialize response")


async def test_lsp_session_patch_request_initialize(lsp_session):
    session, ws = lsp_session
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    await session._patch_request(msg)
    assert msg["params"]["rootUri"].startswith("file://")
    assert msg["params"]["workspaceFolders"][0]["name"] == "workspace"
    assert msg["params"]["initializationOptions"]["python"]["pythonPath"]


async def test_lsp_session_patch_request_did_open_change(lsp_session):
    session, ws = lsp_session
    msg = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
           "params": {"textDocument": {"text": "print(1)", "uri": "file:///main.py"}}}
    await session._patch_request(msg)
    assert session._doc_text == "print(1)"
    assert "from xvfb_env import page" in msg["params"]["textDocument"]["text"]

    msg2 = {"jsonrpc": "2.0", "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": "file:///main.py"},
                       "contentChanges": [{"text": "x = 1"}]}}
    await session._patch_request(msg2)
    assert session._doc_text == "x = 1"

    # didSave
    msg3 = {"jsonrpc": "2.0", "method": "textDocument/didSave", "params": {}}
    await session._patch_request(msg3)


async def test_lsp_session_patch_request_position(lsp_session):
    session, ws = lsp_session
    msg = {"jsonrpc": "2.0", "id": 2, "method": "textDocument/completion",
           "params": {"position": {"line": 5, "character": 3}}}
    await session._patch_request(msg)
    assert msg["params"]["position"]["line"] == 6

    msg2 = {"jsonrpc": "2.0", "id": 3, "method": "textDocument/codeAction",
            "params": {"range": {"start": {"line": 1, "character": 0},
                                 "end": {"line": 2, "character": 1}}}}
    await session._patch_request(msg2)
    assert msg2["params"]["range"]["start"]["line"] == 2


async def test_lsp_session_patch_request_configuration(lsp_session):
    session, ws = lsp_session
    msg = {"jsonrpc": "2.0", "id": 4, "method": "workspace/didChangeConfiguration",
           "params": {"settings": {}}}
    await session._patch_request(msg)
    assert "python" in msg["params"]["settings"]


async def test_lsp_session_server_requests(lsp_session):
    session, ws = lsp_session
    resp = session._handle_server_request({"jsonrpc": "2.0", "id": "a", "method": "window/workDoneProgress/create"})
    assert resp["result"] is None
    resp2 = session._handle_server_request({"jsonrpc": "2.0", "id": "b", "method": "workspace/configuration",
                                            "params": {"items": [{"section": "python"}, {"section": "nope"}]}})
    assert resp2["result"][0]["pythonPath"]
    assert resp2["result"][1] is None
    # 无 id
    assert session._handle_server_request({"method": "workspace/configuration"}) == {}


async def test_lsp_session_apply_change(lsp_session):
    session, ws = lsp_session
    session._doc_text = "line0\nline1\nline2"
    session._apply_change({"text": "REPLACED", "range": {
        "start": {"line": 1, "character": 2}, "end": {"line": 2, "character": 3}}})
    assert session._doc_text == "line0\nliREPLACEDe2"
    session._apply_change({"text": "whole"})
    assert session._doc_text == "whole"
    # 越界范围 -> 收敛到最后一行
    session._doc_text = "a\nb"
    session._apply_change({"text": "X", "range": {
        "start": {"line": 99, "character": 99}, "end": {"line": 100, "character": 99}}})
    assert session._doc_text == "a\nbX"


def test_lsp_session_doc_text(lsp_session):
    session, ws = lsp_session
    session._set_doc_text("body")
    assert session._injected_text() == (
        "from xvfb_env import page, context, browser, save_page, save_content, "
        "limit_items, get_login_ticket, set_login_ticket, page_login, "
        "capture_login_state, restore_login_state\nbody")


async def test_lsp_session_sync_doc(lsp_session):
    session, ws = lsp_session
    session._doc_text = "abc"
    await session._sync_doc_to_disk()
    assert session._doc_path.read_text(encoding="utf-8").endswith("abc")


async def test_lsp_session_shift_functions(lsp_session):
    session, ws = lsp_session
    pos = {"line": 2, "character": 0}
    session._shift_pos(pos, -1)
    assert pos["line"] == 1
    session._shift_pos({"line": "x"}, 1)
    session._shift_pos(None, 1)
    rng = {"start": {"line": 1, "character": 0}, "end": {"line": 3, "character": 1}}
    session._shift_range(rng, -1)
    assert rng["start"]["line"] == 0
    session._shift_range(None, -1)
    loc = {"range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 1}}}
    session._shift_location(loc, -1)
    assert loc["range"]["start"]["line"] == 1
    session._shift_location(None, -1)
    session._shift_location("x", -1)


async def test_lsp_session_patch_completion(lsp_session):
    session, ws = lsp_session
    result = {"items": [{"textEdit": {"range": {"start": {"line": 2, "character": 0},
                                                 "end": {"line": 2, "character": 1}}},
                         "additionalTextEdits": [{"range": {"start": {"line": 3, "character": 0},
                                                            "end": {"line": 3, "character": 1}}}]}]}
    session._patch_completion(result)
    assert result["items"][0]["textEdit"]["range"]["start"]["line"] == 1
    # list / dict 其它形态
    session._patch_completion([{"textEdit": None}])
    session._patch_completion({"textEdit": {"range": {"start": {"line": 5, "character": 0},
                                                      "end": {"line": 5, "character": 1}}}})
    session._patch_completion("not-a-list")


async def test_lsp_session_patch_response_pending(lsp_session):
    session, ws = lsp_session
    session._pending["10"] = "textDocument/hover"
    msg = {"id": 10, "result": {"range": {"start": {"line": 5, "character": 0},
                                          "end": {"line": 5, "character": 1}}}}
    await session._patch_response(msg)
    assert msg["result"]["range"]["start"]["line"] == 4
    # 无 id/result
    await session._patch_response({"id": "zz"})
    await session._patch_response({"method": "x"})
    # 未知 req_method
    session._pending["11"] = "unknown/method"
    await session._patch_response({"id": 11, "result": {"x": 1}})


async def test_lsp_session_patch_response_locations(lsp_session):
    session, ws = lsp_session
    for method in ("textDocument/definition", "textDocument/typeDefinition",
                   "textDocument/declaration", "textDocument/references"):
        session._pending[str(20)] = method
        msg = {"id": 20, "result": [{"range": {"start": {"line": 3, "character": 0},
                                               "end": {"line": 3, "character": 1}}}]}
        await session._patch_response(msg)
        assert msg["result"][0]["range"]["start"]["line"] == 2

    session._pending["21"] = "textDocument/documentHighlight"
    msg = {"id": 21, "result": [{"range": {"start": {"line": 1, "character": 0},
                                           "end": {"line": 1, "character": 1}}}]}
    await session._patch_response(msg)
    assert msg["result"][0]["range"]["start"]["line"] == 0

    session._pending["22"] = "textDocument/documentSymbol"
    msg = {"id": 22, "result": [{"range": {"start": {"line": 4, "character": 0},
                                           "end": {"line": 4, "character": 1}},
                                 "selectionRange": {"start": {"line": 4, "character": 0},
                                                    "end": {"line": 4, "character": 1}},
                                 "children": []}]}
    await session._patch_response(msg)
    assert msg["result"][0]["range"]["start"]["line"] == 3

    session._pending["23"] = "textDocument/rename"
    msg = {"id": 23, "result": {"changes": {"file:///a": [
        {"range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 1}}}]},
        "documentChanges": [
            {"textEdits": [{"range": {"start": {"line": 3, "character": 0},
                                      "end": {"line": 3, "character": 1}}}]},
            {"edits": [{"range": {"start": {"line": 4, "character": 0},
                                  "end": {"line": 4, "character": 1}}}]},
            {"textDocument": {}},
        ]}}
    await session._patch_response(msg)
    assert msg["result"]["changes"]["file:///a"][0]["range"]["start"]["line"] == 1
    assert msg["result"]["documentChanges"][0]["textEdits"][0]["range"]["start"]["line"] == 2
    assert msg["result"]["documentChanges"][1]["edits"][0]["range"]["start"]["line"] == 3

    session._pending["24"] = "textDocument/codeAction"
    msg = {"id": 24, "result": [{"edit": {"changes": {}}}]}
    await session._patch_response(msg)

    session._pending["25"] = "callHierarchy/incomingCalls"
    msg = {"id": 25, "result": [{"range": {"start": {"line": 1, "character": 0},
                                           "end": {"line": 1, "character": 1}},
                                 "selectionRange": {"start": {"line": 2, "character": 0},
                                                    "end": {"line": 2, "character": 1}}}]}
    await session._patch_response(msg)
    assert msg["result"][0]["range"]["start"]["line"] == 0

    # completionItem/resolve
    session._pending["26"] = "completionItem/resolve"
    msg = {"id": 26, "result": {"textEdit": {"range": {"start": {"line": 1, "character": 0},
                                                       "end": {"line": 1, "character": 1}}}}}
    await session._patch_response(msg)
    assert msg["result"]["textEdit"]["range"]["start"]["line"] == 0


async def test_lsp_session_patch_diagnostics(lsp_session):
    session, ws = lsp_session
    msg = {"method": "textDocument/publishDiagnostics",
           "params": {"diagnostics": [
               {"range": {"start": {"line": 0, "character": 0},
                          "end": {"line": 0, "character": 1}}, "message": "x"},
               {"range": {"start": {"line": 1, "character": 0},
                          "end": {"line": 1, "character": 1}},
                "message": "Undefined name 'page'"},
               {"range": {"start": {"line": 2, "character": 0},
                          "end": {"line": 2, "character": 1}}, "message": "real error"},
           ]}}
    await session._patch_response(msg)
    diags = msg["params"]["diagnostics"]
    assert len(diags) == 1
    assert diags[0]["range"]["start"]["line"] == 1


async def test_lsp_session_on_lsp_message(lsp_session):
    session, ws = lsp_session
    # workspace/configuration server request -> 回包给 pyright, 不转发 ws
    await session._on_lsp_message({"id": "cfg", "method": "workspace/configuration",
                                   "params": {"items": [{"section": "python"}]}})
    # publishDiagnostics -> 转发 ws
    await session._on_lsp_message({"method": "textDocument/publishDiagnostics",
                                   "params": {"diagnostics": []}})
    # 普通结果转发
    await session._on_lsp_message({"id": 99, "result": {"a": 1}})
    assert any("a" in s for s in ws.sent)


async def test_lsp_session_pump(lsp_session):
    session, ws = lsp_session
    ws.queue(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {}}))
    ws.queue("bad json{{")
    ws.end()
    await session.pump()
    # 会话已停止
    assert session._proc is None


async def test_lsp_session_on_lsp_message_exception_handled(lsp_session):
    session, ws = lsp_session
    # ws.send_text 抛错 -> 静默
    async def boom(text):
        raise RuntimeError("ws closed")

    ws.send_text = boom
    await session._on_lsp_message({"id": 1, "result": {"x": 1}})
    assert session._proc is not None


async def test_lsp_session_stop_idempotent(lsp_session):
    session, ws = lsp_session
    await session.stop()
    await session.stop()
