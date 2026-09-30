import json

import pytest

import main

QUOTE = {"text": "raz dva tri chetyre pyat", "author_en": "X", "source": "S"}


class FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _keys(culture):
    return [k for k in main.CULTURE_ORDER if k != culture]


def test_valid_response(monkeypatch):
    payload = json.dumps({k: f"t-{k}" for k in _keys("ru")})
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert main.translate_quote(QUOTE, "ru") == {k: f"t-{k}" for k in _keys("ru")}


def test_markdown_wrapped_json(monkeypatch):
    payload = "```json\n" + json.dumps({k: "t" for k in _keys("ru")}) + "\n```"
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert set(main.translate_quote(QUOTE, "ru")) == set(_keys("ru"))


def test_missing_key_retries_then_raises(monkeypatch):
    calls = []
    bad = json.dumps({"en_gb": "only one"})

    def fake_post(*a, **k):
        calls.append(1)
        return FakeResponse(bad)

    monkeypatch.setattr(main.requests, "post", fake_post)
    with pytest.raises(RuntimeError):
        main.translate_quote(QUOTE, "ru")
    assert len(calls) == 3  # 1 попытка + 2 повтора
