"""LLM-провайдеры без отдельной оплаты API (ТЗ Wordbank §3.1).

Контракт: provider.complete_json(prompt, temperature=, timeout=) -> dict.
Исключения: LlmAuthError (401/403), LlmQuotaError (402/429), LlmBadOutput (не JSON), LlmError (прочее).
В лог пишутся только имя провайдера, длительность и тип ошибки — не промпт и не ответ.
"""
import json
import os
import re
import subprocess
import time

import requests

DEFAULT_PROVIDERS = "claude_cli,gemini"
BAD_OUTPUT_RETRIES = 2
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LlmError(Exception):
    pass


class LlmAuthError(LlmError):
    pass


class LlmQuotaError(LlmError):
    pass


class LlmBadOutput(LlmError):
    pass


def clean_json_response(content):
    """Очищает ответ от markdown-оберток."""
    content = re.sub(r"^```json\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"^```\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
    return content.strip()


def _parse_json(raw):
    try:
        data = json.loads(clean_json_response(raw))
    except (ValueError, TypeError) as e:
        raise LlmBadOutput("response is not valid JSON") from e
    if not isinstance(data, dict):
        raise LlmBadOutput("response JSON is not an object")
    return data


def _check_http(response):
    status = getattr(response, "status_code", 200)
    if status in (401, 403):
        raise LlmAuthError(f"HTTP {status}")
    if status in (402, 429):
        raise LlmQuotaError(f"HTTP {status}")
    if status >= 400:
        raise LlmError(f"HTTP {status}")


def _chat_completion(url, headers, payload, timeout):
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise LlmError(f"network error: {type(e).__name__}") from e
    _check_http(response)
    try:
        raw = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LlmBadOutput("unexpected response shape") from e
    return _parse_json(raw)


class ClaudeCliProvider:
    """Claude Code CLI по подписке (CLAUDE_CODE_OAUTH_TOKEN). Флаги сверены с claude 2.1.x."""

    name = "claude_cli"
    AUTH_MARKERS = ("401", "unauthorized", "authentication", "oauth", "token", "login", "not logged in")
    QUOTA_MARKERS = ("rate limit", "usage limit", "quota", "429", "limit reached")

    def complete_json(self, prompt, *, temperature=0.3, timeout=120):  # temperature CLI не принимает
        model = os.environ.get("CLAUDE_MODEL", "haiku")
        cmd = ["claude", "-p", "--output-format", "json", "--tools", "",
               "--no-session-persistence", "--model", model]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise LlmError("claude CLI timeout") from e
        except OSError as e:
            raise LlmError(f"claude CLI not runnable: {type(e).__name__}") from e
        out = proc.stdout or ""
        wrapper = None
        try:
            wrapper = json.loads(out)
        except ValueError:
            pass
        if proc.returncode != 0 or (isinstance(wrapper, dict) and wrapper.get("is_error")):
            text = ((wrapper or {}).get("result") if isinstance(wrapper, dict) else "") or ""
            blob = f"{proc.stderr or ''} {out if wrapper is None else text}".lower()
            if any(m in blob for m in self.QUOTA_MARKERS):
                raise LlmQuotaError("claude CLI: limit")
            if any(m in blob for m in self.AUTH_MARKERS):
                raise LlmAuthError("claude CLI: auth")
            raise LlmError(f"claude CLI exit code {proc.returncode}")
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("result"), str):
            raise LlmBadOutput("claude CLI: unexpected wrapper")
        return _parse_json(wrapper["result"])


class GeminiProvider:
    name = "gemini"

    def complete_json(self, prompt, *, temperature=0.3, timeout=60):
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LlmAuthError("GEMINI_API_KEY is not set")
        payload = {
            "model": os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        return _chat_completion(GEMINI_URL, {"Authorization": f"Bearer {key}"}, payload, timeout)


class OpenRouterProvider:
    name = "openrouter"

    def complete_json(self, prompt, *, temperature=0.3, timeout=60):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise LlmAuthError("OPENROUTER_API_KEY is not set")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/polyglot-bot",
        }
        payload = {
            "model": os.environ.get("OPENROUTER_MODEL", "~google/gemini-flash-latest"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        return _chat_completion(OPENROUTER_URL, headers, payload, timeout)


class HermesProvider:
    """Send the prompt to the SSH key's forced command, using stdin."""

    name = "hermes"

    def complete_json(self, prompt, *, temperature=0.3, timeout=120):
        target = os.environ.get("HERMES_SSH_TARGET")
        if not target:
            raise LlmAuthError("HERMES_SSH_TARGET is not set")
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no"]
        if identity := os.environ.get("HERMES_SSH_IDENTITY"):
            cmd.extend(["-i", identity, "-o", "IdentitiesOnly=yes"])
        if known_hosts := os.environ.get("HERMES_SSH_KNOWN_HOSTS"):
            cmd.extend(["-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null"])
        try:
            proc = subprocess.run([*cmd, "--", target], input=prompt, capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise LlmError("Hermes SSH timeout") from e
        except OSError as e:
            raise LlmError(f"Hermes SSH not runnable: {type(e).__name__}") from e
        if proc.returncode == 255:
            raise LlmError("Hermes SSH connection failed (check host key and credentials)")
        if proc.returncode != 0:
            raise LlmError(f"Hermes generation exited with code {proc.returncode}")
        return _parse_json(proc.stdout)


PROVIDERS = {p.name: p for p in (ClaudeCliProvider, GeminiProvider, OpenRouterProvider, HermesProvider)}


def get_chain():
    names = [n.strip() for n in os.environ.get("LLM_PROVIDERS", DEFAULT_PROVIDERS).split(",") if n.strip()]
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        raise ValueError(f"unknown LLM provider(s): {', '.join(unknown)}")
    return [PROVIDERS[n]() for n in names]


def complete_json_chain(prompt, temperature=0.3, timeout=None, chain=None):
    """Пробует провайдеров по порядку. Auth/Quota — сразу к следующему; BadOutput — до 2 повторов."""
    failures = []
    for provider in chain or get_chain():
        kwargs = {"temperature": temperature}
        if timeout:
            kwargs["timeout"] = timeout
        attempts = 0
        while True:
            started = time.monotonic()
            try:
                data = provider.complete_json(prompt, **kwargs)
                print(f"LLM provider {provider.name}: ok in {time.monotonic() - started:.1f}s")
                return data
            except LlmBadOutput as e:
                attempts += 1
                print(f"LLM provider {provider.name}: bad output ({e}), attempt {attempts}")
                if attempts > BAD_OUTPUT_RETRIES:
                    failures.append((provider.name, e))
                    break
            except LlmError as e:
                print(f"LLM provider {provider.name}: {type(e).__name__}: {e}")
                failures.append((provider.name, e))
                break
    reasons = "; ".join(f"{n}: {type(e).__name__} ({e})" for n, e in failures)
    if failures and all(isinstance(e, (LlmAuthError, LlmQuotaError)) for _, e in failures):
        raise LlmError(f"all providers unavailable (auth/quota): {reasons}")
    raise LlmError(f"all providers failed: {reasons}")
