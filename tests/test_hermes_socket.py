import os
import shutil
import socket
import tempfile
import threading

import pytest

import llm


def serve(reply):
    d = tempfile.mkdtemp(dir="/tmp")  # short path: AF_UNIX limit
    path = os.path.join(d, "s")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    got = []

    def run():
        c, _ = srv.accept()
        data = b""
        while chunk := c.recv(4096):
            data += chunk
        got.append(data.decode())
        c.sendall(reply)
        c.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return d, path, got, t


@pytest.mark.parametrize("reply,ok", [(b'{"a": 1}', True), (b"", False)])
def test_socket_roundtrip(monkeypatch, reply, ok):
    d, path, got, t = serve(reply)
    monkeypatch.setenv("HERMES_SOCKET", path)
    try:
        if ok:
            assert llm.HermesSocketProvider().complete_json("привет", timeout=5) == {"a": 1}
        else:
            with pytest.raises(llm.LlmError):
                llm.HermesSocketProvider().complete_json("привет", timeout=5)
        t.join(5)
        assert got == ["привет"]
    finally:
        shutil.rmtree(d)


def test_missing_socket_is_llm_error(monkeypatch):
    monkeypatch.setenv("HERMES_SOCKET", "/nonexistent/s")
    with pytest.raises(llm.LlmError):
        llm.HermesSocketProvider().complete_json("x", timeout=1)
