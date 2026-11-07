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


# --- выбор цепочки при отсутствующих ключах и переход к openrouter ---
KEY_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture
def no_keys(monkeypatch, tmp_path):
    """Окружение как в Actions без секретов: переменные пустые, claude CLI установлен, но не залогинен."""
    for var in KEY_VARS:
        monkeypatch.setenv(var, "")  # Actions передаёт незаданный секрет пустой строкой
    for var in ("LLM_PROVIDERS", "OPENROUTER_MODEL", "OPENROUTER_FREE_MODELS"):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: pytest.fail("claude CLI must not be called"))
    return monkeypatch


def test_default_chain_ends_with_openrouter():
    assert llm.DEFAULT_PROVIDERS.split(",") == ["claude_cli", "gemini", "openrouter"]


def test_chain_skips_providers_without_keys(no_keys, capsys):
    no_keys.setenv("OPENROUTER_API_KEY", "or-key")
    chain = llm.get_chain()
    assert [p.name for p in chain] == ["openrouter"]
    out = capsys.readouterr().out
    assert "claude_cli: skipped (CLAUDE_CODE_OAUTH_TOKEN is not set)" in out
    assert "gemini: skipped (GEMINI_API_KEY is not set)" in out


def test_chain_keeps_configured_providers_in_order(no_keys):
    no_keys.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    no_keys.setenv("GEMINI_API_KEY", "g")
    no_keys.setenv("OPENROUTER_API_KEY", "or-key")
    assert [p.name for p in llm.get_chain()] == ["claude_cli", "gemini", "openrouter"]


def test_claude_cli_skipped_when_not_installed(no_keys):
    no_keys.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    no_keys.setattr(llm.shutil, "which", lambda name: None)
    assert llm.ClaudeCliProvider().unavailable_reason() == "claude CLI is not installed"


def test_claude_cli_local_login_counts(no_keys, tmp_path):
    (tmp_path / ".credentials.json").write_text("{}")
    assert llm.ClaudeCliProvider().unavailable_reason() is None


def test_explicit_chain_respected(no_keys):
    no_keys.setenv("LLM_PROVIDERS", "gemini")
    no_keys.setenv("OPENROUTER_API_KEY", "or-key")
    with pytest.raises(llm.LlmAuthError, match="no LLM provider configured"):
        llm.get_chain()


def test_no_keys_at_all_is_clear_error(no_keys):
    with pytest.raises(llm.LlmAuthError, match="claude_cli: CLAUDE_CODE_OAUTH_TOKEN.*gemini: GEMINI_API_KEY.*openrouter"):
        llm.complete_json_chain("p")


def test_chain_falls_through_to_openrouter(no_keys, capsys):
    no_keys.setenv("OPENROUTER_API_KEY", "or-key")
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw["json"]["model"]))
        return Resp(200, '{"ok": 1}')

    no_keys.setattr(llm.requests, "post", fake_post)
    assert llm.complete_json_chain("p") == {"ok": 1}
    assert calls == [(llm.OPENROUTER_URL, llm.OPENROUTER_DEFAULT_MODEL)]
    assert "LLM provider openrouter: ok" in capsys.readouterr().out


def test_openrouter_402_falls_back_to_free_model(no_keys):
    no_keys.setenv("OPENROUTER_API_KEY", "or-key")
    no_keys.setenv("OPENROUTER_MODEL", "paid/model")
    no_keys.setenv("OPENROUTER_FREE_MODELS", "gone/model:free,free/model:free")
    replies = {"paid/model": Resp(402), "gone/model:free": Resp(404), "free/model:free": Resp(200, '{"ok": 2}')}
    seen = []

    def fake_post(url, **kw):
        seen.append(kw["json"]["model"])
        return replies[kw["json"]["model"]]

    no_keys.setattr(llm.requests, "post", fake_post)
    assert llm.complete_json_chain("p") == {"ok": 2}
    assert seen == ["paid/model", "gone/model:free", "free/model:free"]


def test_openrouter_all_models_out_of_credits_is_quota(no_keys):
    no_keys.setenv("OPENROUTER_API_KEY", "or-key")
    no_keys.setenv("OPENROUTER_FREE_MODELS", "free/model:free")
    no_keys.setattr(llm.requests, "post", lambda *a, **k: Resp(402))
    with pytest.raises(llm.LlmQuotaError):
        llm.OpenRouterProvider().complete_json("p")


def test_openrouter_auth_error_does_not_try_other_models(no_keys):
    no_keys.setenv("OPENROUTER_API_KEY", "bad")
    seen = []
    no_keys.setattr(llm.requests, "post", lambda *a, **k: seen.append(1) or Resp(401))
    with pytest.raises(llm.LlmAuthError):
        llm.OpenRouterProvider().complete_json("p")
    assert len(seen) == 1


def test_openrouter_free_models_can_be_disabled(no_keys):
    no_keys.setenv("OPENROUTER_FREE_MODELS", "none")
    assert llm.OpenRouterProvider.models() == [llm.OPENROUTER_DEFAULT_MODEL]
