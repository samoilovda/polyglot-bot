import json
import os
from datetime import datetime, timezone

import jsonschema
import pytest

import llm
import main
import slot
import words_post as wp

SCHEMA = json.load(open(os.path.join(os.path.dirname(__file__), "publish-candidates.schema.json")))


def cand(i, text, lemma=None, ipa="/x/", origin="dictionary", lang="es"):
    return {"id": i, "text": text, "lemma": lemma, "lang": lang, "ipa": ipa, "ipa_origin": origin}


CANDS = [cand(1, "palabra"), cand(2, "casa", ipa="/ˈkasa/"), cand(3, "correr", ipa=None, origin=None), cand(4, "libro", ipa="/l/", origin="llm"), cand(5, "agua"), cand(6, "tiempo")]
TEXT = (
    "Ayer, en mi {{casa}}, encontré un {{libro}} muy antiguo sobre cada {{palabra}} que usamos. "
    "Después de leer un rato, salí a {{correr}} por el parque para despejar la mente. "
    "Con un vaso de {{agua}} fría en la mano, pensé que el {{tiempo}} pasa muy rápido cuando uno disfruta aprendiendo "
    "cosas nuevas todos los días, y que merece la pena anotar lo aprendido."
)


def good():
    return {
        "text": TEXT, "translation": "Вчера в моём доме я нашёл очень старую книгу.",
        "words": [{"candidate": c["text"], "form": c["text"], "gloss": f"gloss-{c['text']}"} for c in CANDS],
    }


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(wp, "WORDBANK_URL", "https://wb.example")
    monkeypatch.setattr(wp, "WORDBANK_TOKEN", "tok")
    monkeypatch.setattr(wp, "WORDS_TG_CHAT_ID", "-100words")
    monkeypatch.setattr(wp, "TG_BOT_TOKEN", "bot")
    monkeypatch.setattr(wp, "DRY_RUN", False)
    monkeypatch.setattr(wp, "FORCE_WORDS_LANG", "")
    monkeypatch.setattr(main, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(main, "save_state", lambda s, path=None: saved.append(json.loads(json.dumps(s))))
    saved.clear()


saved = []


def utc(d, h):
    return datetime(2026, 10, d, h, 0, tzinfo=timezone.utc)


# ---- slots ----
def test_words_slot_independent_from_quotes():
    assert slot.current_slot(utc(10, 9), (13,)) == "2026-10-10T13"  # 14:00 Tashkent
    assert slot.current_slot(utc(10, 7), (13,)) == "2026-10-09T13"  # 12:00 Tashkent: before the slot
    assert slot.SLOT_HOURS == (10, 16, 22)


def test_quotes_first_then_words_next_run():
    state = {"last_slot": "2026-10-10T10", "last_words_slot": "2026-10-09T13"}
    assert slot.decide(state, "schedule", utc(10, 8)) == (False, True)  # words slot reached, quotes up to date
    state["last_slot"] = "2026-10-09T22"
    assert slot.decide(state, "schedule", utc(10, 8)) == (True, False)  # both due -> quotes only
    assert slot.decide(state, "workflow_dispatch", utc(10, 8)) == (True, False)
    assert slot.decide(state, "workflow_dispatch", utc(10, 8), force_words=True) == (False, True)
    assert slot.decide({"last_slot": "2026-10-10T10", "last_words_slot": "2026-10-10T13"}, "schedule", utc(10, 8)) == (False, False)


# ---- contract / privacy ----
def test_fixture_matches_wordbank_schema():
    jsonschema.validate(CANDS, SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([{**CANDS[0], "note": "secret"}], SCHEMA)


def test_prompt_contains_only_candidate_words():
    p = wp.build_prompt("es", CANDS)
    assert "palabra" in p and "B1" in p and "/ˈkasa/" not in p


# ---- validation ----
def test_valid_lesson():
    lesson = wp.validate(good(), CANDS)
    assert len(lesson["used"]) == 6


def test_inflected_form_accepted():
    d = good()
    d["text"] = d["text"].replace("{{correr}}", "{{corriendo}}").replace("salí a", "salí")
    d["words"][2]["form"] = "corriendo"
    assert wp.validate(d, CANDS)["used"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(text=d["text"].replace("{{agua}}", "{{fuego}}")),  # marked word not a candidate
    lambda d: d.update(text="{{casa}} corta"),  # too short
    lambda d: d.update(text=d["text"] * 3),  # too long
    lambda d: d.update(words=d["words"][:3]),  # marked words without declared candidates
    lambda d: d["words"][0].update(candidate="inventada"),
    lambda d: d.update(translation=""),
    lambda d: d["words"][0].update(form="otra"),
])
def test_invalid_lessons_rejected(mutate):
    d = good()
    mutate(d)
    with pytest.raises(ValueError):
        wp.validate(d, CANDS)


def test_too_few_words_used():
    d = good()
    d["words"] = d["words"][:3]
    d["text"] = " ".join(["Ayer en {{casa}}, un {{libro}} y una {{palabra}}."] * 8)
    with pytest.raises(ValueError):
        wp.validate(d, CANDS)


# ---- language rotation ----
def test_rotation_skips_languages_with_few_candidates():
    data = {"en-GB": [cand(1, "a")], "es": CANDS, "pt-BR": CANDS, "uz-Latn": CANDS}
    state = {"words_lang_index": 0}
    assert wp.choose_language(state, fetch=lambda lang: data[lang])[0] == "es"
    assert state["_words_chosen_index"] == 1


def test_force_lang(monkeypatch):
    monkeypatch.setattr(wp, "FORCE_WORDS_LANG", "pt-BR")
    assert wp.choose_language({}, fetch=lambda lang: CANDS)[0] == "pt-BR"


# ---- format ----
def test_format_message_ipa_policy():
    lesson = wp.validate(good(), CANDS)
    msg = wp.format_message("es", lesson)
    assert msg.startswith("📚 <b>Words in context</b> · 🇪🇸") and "<blockquote>" in msg and "<b>casa</b>" in msg
    assert "/ˈkasa/" in msg and "🇷🇺 <i>" in msg and "gloss-casa" in msg
    assert "/l/" not in msg  # LLM IPA is not allowed by default
    assert "Synthetic" not in msg and "dictionary" not in msg  # no provenance labels in the public post


def test_llm_ipa_opt_in(monkeypatch):
    monkeypatch.setattr(wp, "ALLOW_LLM_IPA", True)
    assert "/l/" in wp.format_message("es", wp.validate(good(), CANDS))


# ---- run ----
class Resp:
    def __init__(self, body=None):
        self.body = body or {"ok": True, "result": {"message_id": 77}}

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


@pytest.fixture
def net(monkeypatch):
    calls = {"post": [], "get": []}
    monkeypatch.setattr(wp.requests, "get", lambda url, **k: (calls["get"].append((url, k)), Resp(CANDS))[1])
    monkeypatch.setattr(wp.requests, "post", lambda url, **k: (calls["post"].append((url, k)), Resp())[1])
    monkeypatch.setattr(main, "generate_audio", lambda *a, **k: None)
    monkeypatch.setattr(llm, "complete_json_chain", lambda prompt, temperature=0.3, **k: good())
    return calls


def test_run_posts_to_words_channel_reports_and_updates_state(net):
    state = {"words_lang_index": 1}
    assert wp.run(state, utc(10, 8)) == 0
    send = next(k for url, k in net["post"] if url.endswith("/sendMessage"))
    assert send["json"]["chat_id"] == "-100words"
    report = next(k for url, k in net["post"] if url.endswith("/publish/report"))
    assert report["json"]["item_ids"] == [1, 2, 3, 4, 5, 6] and report["json"]["telegram_message_id"] == 77
    assert saved[-1]["last_words_slot"] == "2026-10-10T13" and saved[-1]["words_lang_index"] == 2
    assert "_words_chosen_index" not in saved[-1]


def test_no_channel_configured_never_falls_back_to_quotes_channel(net, monkeypatch):
    monkeypatch.setattr(wp, "WORDS_TG_CHAT_ID", None)
    monkeypatch.setattr(main, "TG_CHAT_ID", "-100quotes")
    assert wp.run({}) == 0
    assert not net["post"] and not saved


def test_dry_run_sends_nothing_and_keeps_state(net, monkeypatch, capsys):
    monkeypatch.setattr(wp, "DRY_RUN", True)
    assert wp.run({}) == 0
    assert not net["post"] and not saved and "Words in context" in capsys.readouterr().out


def test_lesson_failure_skips_slot_with_exit_zero(net, monkeypatch):
    monkeypatch.setattr(llm, "complete_json_chain", lambda *a, **k: {"text": "short"})
    assert wp.run({}, utc(10, 8)) == 0
    assert not any(url.endswith("/sendMessage") for url, _ in net["post"])
    assert saved[-1]["last_words_slot"] == "2026-10-10T13"  # do not hammer the LLM every hour


def test_llm_outage_skips_with_exit_zero(net, monkeypatch):
    def boom(*a, **k):
        raise llm.LlmError("all providers unavailable")

    monkeypatch.setattr(llm, "complete_json_chain", boom)
    assert wp.run({}, utc(10, 8)) == 0


def test_report_failure_does_not_fail_post(net, monkeypatch):
    real = wp.requests.post

    def post(url, **k):
        if url.endswith("/publish/report"):
            raise RuntimeError("wordbank down")
        return real(url, **k)

    monkeypatch.setattr(wp.requests, "post", post)
    assert wp.run({}, utc(10, 8)) == 0 and saved[-1]["last_words_slot"] == "2026-10-10T13"


def test_telegram_failure_returns_1_and_keeps_state(net, monkeypatch):
    monkeypatch.setattr(wp.requests, "post", lambda url, **k: Resp({"ok": False}))
    assert wp.run({}, utc(10, 8)) == 1 and not saved


def test_not_enough_candidates_skips(net, monkeypatch):
    monkeypatch.setattr(wp.requests, "get", lambda url, **k: Resp(CANDS[:2]))
    assert wp.run({}, utc(10, 8)) == 0 and saved[-1]["last_words_slot"] == "2026-10-10T13"
