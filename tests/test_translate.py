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


def _lesson_json(culture="ru", original=None):
    langs = {k: {"word": f"w-{k}", "ipa": f"/i-{k}/", "text": f"t {{{{w-{k}}}}} x"} for k in main.CULTURE_ORDER}
    langs[culture]["text"] = original or "raz dva {{tri}} chetyre pyat"
    return {"meaning_en": "three", "languages": langs}


def test_valid_response(monkeypatch):
    payload = json.dumps(_lesson_json())
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: FakeResponse(payload))
    lesson = main.translate_quote(QUOTE, "ru")
    assert lesson["meaning_en"] == "three"
    assert set(lesson["languages"]) == set(main.CULTURE_ORDER)
    assert lesson["languages"]["es"]["ipa"] == "/i-es/"


def test_markdown_wrapped_json(monkeypatch):
    payload = "```json\n" + json.dumps(_lesson_json()) + "\n```"
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert set(main.translate_quote(QUOTE, "ru")["languages"]) == set(main.CULTURE_ORDER)


def test_altered_original_falls_back_to_plain_text(monkeypatch):
    payload = json.dumps(_lesson_json(original="something {{else}} entirely"))
    monkeypatch.setattr(main.requests, "post", lambda *a, **k: FakeResponse(payload))
    assert main.translate_quote(QUOTE, "ru")["languages"]["ru"]["text"] == QUOTE["text"]


def test_missing_language_retries_then_raises(monkeypatch):
    calls = []
    bad = json.dumps({"meaning_en": "x", "languages": {"en_gb": {"word": "a", "ipa": "/a/", "text": "a"}}})

    def fake_post(*a, **k):
        calls.append(1)
        return FakeResponse(bad)

    monkeypatch.setattr(main.requests, "post", fake_post)
    with pytest.raises(RuntimeError):
        main.translate_quote(QUOTE, "ru")
    assert len(calls) == 3  # 1 попытка + 2 повтора
