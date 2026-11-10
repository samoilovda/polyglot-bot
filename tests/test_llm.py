import json
import subprocess

import pytest

import llm


class Resp:
    def __init__(self, status=200, content="{}"):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def cli_result(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def test_cli_success_unwraps_result(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw["input"]
        return cli_result(json.dumps({"is_error": False, "result": '```json\n{"a": 1}\n```'}))

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDE_MODEL", "sonnet")
    assert llm.ClaudeCliProvider().complete_json("PROMPT") == {"a": 1}
    assert seen["input"] == "PROMPT" and "sonnet" in seen["cmd"] and "-p" in seen["cmd"]


def test_cli_timeout(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(llm.LlmError):
        llm.ClaudeCliProvider().complete_json("p")


def test_cli_auth_error(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: cli_result("", "Error: 401 authentication_error", 1))
    with pytest.raises(llm.LlmAuthError):
        llm.ClaudeCliProvider().complete_json("p")


def test_cli_quota_error(monkeypatch):
    out = json.dumps({"is_error": True, "result": "Claude usage limit reached"})
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: cli_result(out, "", 1))
    with pytest.raises(llm.LlmQuotaError):
        llm.ClaudeCliProvider().complete_json("p")


def test_cli_bad_output(monkeypatch):
    out = json.dumps({"is_error": False, "result": "not json"})
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: cli_result(out))
    with pytest.raises(llm.LlmBadOutput):
        llm.ClaudeCliProvider().complete_json("p")


def test_gemini_ok(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Resp(200, '{"x": 2}'))
    assert llm.GeminiProvider().complete_json("p") == {"x": 2}


def test_gemini_429_is_quota(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Resp(429))
    with pytest.raises(llm.LlmQuotaError):
        llm.GeminiProvider().complete_json("p")


def test_gemini_without_key_is_auth(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(llm.LlmAuthError):
        llm.GeminiProvider().complete_json("p")


def test_hermes_sends_prompt_on_stdin_to_forced_command(monkeypatch):
    monkeypatch.setenv("HERMES_SSH_TARGET", "hermes@stelve")
    monkeypatch.setenv("HERMES_SSH_IDENTITY", "/tmp/hermes_key")
    monkeypatch.setenv("HERMES_SSH_KNOWN_HOSTS", "/tmp/hermes_hosts")

    def fake_run(cmd, **kw):
        assert cmd[-2:] == ["--", "hermes@stelve"]
        assert "StrictHostKeyChecking=yes" in cmd
        assert "/tmp/hermes_key" in cmd
        assert "UserKnownHostsFile=/tmp/hermes_hosts" in cmd
        assert kw["input"] == "цитата\n$(ignored)"
        return cli_result('{"answer":"перевод"}')

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert llm.HermesProvider().complete_json("цитата\n$(ignored)") == {"answer": "перевод"}


def test_hermes_ssh_failure_does_not_log_remote_output(monkeypatch):
    monkeypatch.setenv("HERMES_SSH_TARGET", "hermes@stelve")
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: cli_result("prompt", "remote stderr", 255))
    with pytest.raises(llm.LlmError, match=r"^Hermes SSH connection failed \(check host key and credentials\)$"):
        llm.HermesProvider().complete_json("prompt")


class Stub:
    def __init__(self, name, results):
        self.name, self.results, self.calls = name, list(results), 0

    def complete_json(self, prompt, **kw):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_chain_quota_falls_through_without_retry():
    a, b = Stub("a", [llm.LlmQuotaError("q")]), Stub("b", [{"ok": 1}])
    assert llm.complete_json_chain("p", chain=[a, b]) == {"ok": 1}
    assert a.calls == 1


def test_chain_bad_output_retries_twice_then_next():
    a = Stub("a", [llm.LlmBadOutput("x")] * 3)
    b = Stub("b", [{"ok": 1}])
    assert llm.complete_json_chain("p", chain=[a, b]) == {"ok": 1}
    assert a.calls == 3


def test_chain_all_unavailable_message():
    a, b = Stub("a", [llm.LlmAuthError("tok")]), Stub("b", [llm.LlmQuotaError("q")])
    with pytest.raises(llm.LlmError, match="auth/quota"):
        llm.complete_json_chain("p", chain=[a, b])


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDERS", "nope")
    with pytest.raises(ValueError):
        llm.get_chain()
