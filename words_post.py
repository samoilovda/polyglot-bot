"""«Words in context»: пост из сохранённых слов Wordbank в ОТДЕЛЬНЫЙ канал (WORDS_TG_CHAT_ID).

Отдельная точка входа; поведение постов с цитатами (main.py) не меняется. Приватные поля Wordbank (заметки, контекст)
сюда не приходят: API отдаёт только id, text, lemma, lang, ipa, ipa_origin.
"""
import hashlib
import html
import os
import re
import sys

import requests

import llm
import main
import slot

WORDBANK_URL = os.environ.get("WORDBANK_URL", "").rstrip("/")
WORDBANK_TOKEN = os.environ.get("WORDBANK_TOKEN")
WORDS_TG_CHAT_ID = os.environ.get("WORDS_TG_CHAT_ID")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
FORCE_WORDS_LANG = os.environ.get("FORCE_WORDS_LANG", "").strip()
ALLOW_LLM_IPA = os.environ.get("WORDS_ALLOW_LLM_IPA", "").strip().lower() in ("1", "true", "yes")
TRANSLATION_LANG = os.environ.get("WORDS_TRANSLATION_LANG", "ru")

MIN_CANDIDATES = 4
MIN_USED, MAX_USED = 4, 6
MIN_CHARS, MAX_CHARS = 300, 900

# Порядок ротации = ключи языков Wordbank
LANGS = {
    "en-GB": {"flag": "🇬🇧", "name": "British English", "voice": "en-GB-RyanNeural", "level": "B2+"},
    "es": {"flag": "🇪🇸", "name": "Spanish (Spain)", "voice": "es-ES-AlvaroNeural", "level": "B1"},
    "pt-BR": {"flag": "🇧🇷", "name": "Brazilian Portuguese", "voice": "pt-BR-AntonioNeural", "level": "B1"},
    "uz-Latn": {"flag": "🇺🇿", "name": "Uzbek (Latin script, with oʻ and gʻ)", "voice": "uz-UZ-SardorNeural", "level": "B1"},
}
LANG_ORDER = list(LANGS)


def level_for(lang):
    return os.environ.get(f"WORDS_LEVEL_{lang.split('-')[0].upper()}", LANGS[lang]["level"])


# --- Wordbank API ---
def fetch_candidates(lang, n=6):
    r = requests.get(
        f"{WORDBANK_URL}/api/v1/publish/candidates", params={"lang": lang, "n": n},
        headers={"Authorization": f"Bearer {WORDBANK_TOKEN}"}, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def report(item_ids, lang, message_id, text_hash):
    try:
        r = requests.post(
            f"{WORDBANK_URL}/api/v1/publish/report",
            json={"item_ids": item_ids, "lang": lang, "telegram_message_id": message_id, "text_hash": text_hash},
            headers={"Authorization": f"Bearer {WORDBANK_TOKEN}"}, timeout=30,
        )
        r.raise_for_status()
    except Exception as e:  # отчёт не должен ронять уже отправленный пост
        print(f"Report to Wordbank failed (ignored): {e}")


def choose_language(state, fetch=fetch_candidates):
    """-> (lang, candidates) | (None, None). Ротация среди языков с ≥ MIN_CANDIDATES кандидатов."""
    if FORCE_WORDS_LANG:
        cands = fetch(FORCE_WORDS_LANG)
        return (FORCE_WORDS_LANG, cands) if len(cands) >= MIN_CANDIDATES else (None, None)
    start = state.get("words_lang_index", 0) % len(LANG_ORDER)
    for step in range(len(LANG_ORDER)):
        idx = (start + step) % len(LANG_ORDER)
        lang = LANG_ORDER[idx]
        try:
            cands = fetch(lang)
        except Exception as e:
            print(f"Candidates for {lang} unavailable: {e}")
            continue
        if len(cands) >= MIN_CANDIDATES:
            state["_words_chosen_index"] = idx
            return lang, cands
        print(f"{lang}: only {len(cands)} candidates, skipped")
    return None, None


# --- LLM ---
def build_prompt(lang, candidates):
    cfg = LANGS[lang]
    words = "\n".join(f"- {c['text']}" + (f" (lemma: {c['lemma']})" if c.get("lemma") else "") for c in candidates)
    return f"""Write a short, natural text in {cfg['name']} at CEFR level {level_for(lang)}: exactly 5 sentences, 400-600 characters long, counting characters (never fewer than {MIN_CHARS}, never more than {MAX_CHARS}). Longer sentences are fine.
Use between {MIN_USED} and {MAX_USED} of these words (inflect them correctly if the grammar needs it):
{words}
Wrap every used word exactly as it appears in the text in double curly braces, e.g. {{{{word}}}}.
Also give a translation of the whole text into {TRANSLATION_LANG} (without braces) and a short English gloss per used word.
Reply with JSON only:
{{"text": "...", "translation": "...", "words": [{{"candidate": "<exact candidate text from the list>", "form": "<form as used in the text>", "gloss": "<English gloss>"}}]}}"""


_APOS = str.maketrans({c: "\u02bb" for c in "\u2018\u2019\u0027\u02bc\u0060"})


def norm(s):
    """Сравнение слов: регистр, пробелы и все варианты узбекского апострофа (oʻ, o', o‘ …) считаются одним."""
    return re.sub(r"\s+", " ", s.strip().casefold().translate(_APOS))


def related(form, candidate):
    """Точное совпадение, лемма или простая морфология: форма начинается с основы кандидата."""
    forms = {norm(candidate["text"])} | ({norm(candidate["lemma"])} if candidate.get("lemma") else set())
    f = norm(form)
    if f in forms:
        return True
    for base in forms:
        stem = base[: max(3, len(base) - 2)]
        if len(base) >= 3 and f.startswith(stem) and len(f) <= len(base) + 5:
            return True
    return False


def validate(data, candidates):
    """Возвращает нормализованный урок или бросает ValueError."""
    if not isinstance(data, dict):
        raise ValueError("not an object")
    text, translation, words = data.get("text"), data.get("translation"), data.get("words")
    if not isinstance(text, str) or not isinstance(translation, str) or not translation.strip() or not isinstance(words, list):
        raise ValueError("missing fields")
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise ValueError(f"text length {len(text)} outside {MIN_CHARS}-{MAX_CHARS}")
    marked = [m.strip() for m in re.findall(r"\{\{(.+?)\}\}", text)]
    if not marked:
        raise ValueError("no marked words")
    by_text = {c["text"]: c for c in candidates}
    used = {}
    for w in words:
        cand = by_text.get(w.get("candidate")) if isinstance(w, dict) else None
        if cand is None:
            raise ValueError("word is not a candidate")
        f = norm(w.get("form", ""))
        # suffixes after the braces are allowed (agglutinative Uzbek: {{shahar}}ga)
        if not any(f == norm(m) or f.startswith(norm(m)) for m in marked) or not related(w["form"], cand):
            raise ValueError(f"form {w.get('form')!r} not marked or unrelated to {cand['text']!r}")
        used[cand["id"]] = {"candidate": cand, "form": w["form"], "gloss": str(w.get("gloss", "")).strip()}
    for m in marked:  # every marked word must belong to a candidate
        if not any(norm(u["form"]) == norm(m) or norm(u["form"]).startswith(norm(m)) for u in used.values()):
            raise ValueError(f"marked word {m!r} is not a candidate")
    if not MIN_USED <= len(used) <= MAX_USED:
        raise ValueError(f"{len(used)} words used, need {MIN_USED}-{MAX_USED}")
    return {"text": text, "translation": translation.strip(), "used": list(used.values())}


def generate_lesson(lang, candidates, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            data = llm.complete_json_chain(build_prompt(lang, candidates), temperature=0.4)
            return validate(data, candidates)
        except (ValueError, llm.LlmError) as e:
            last = e
            print(f"Words lesson attempt {attempt + 1} failed: {e}")
            if isinstance(e, llm.LlmError):
                break
    raise RuntimeError(f"lesson generation failed: {last}")


# --- формат ---
def format_message(lang, lesson):
    cfg = LANGS[lang]
    body = main.make_bold(html.escape(lesson["text"]))
    lines = [f"📚 <b>Words in context</b> · {cfg['flag']}", f"<blockquote>{body}</blockquote>"]
    for u in lesson["used"]:
        c = u["candidate"]
        ipa = ""
        if c.get("ipa") and (c.get("ipa_origin") in ("dictionary", "user") or (c.get("ipa_origin") == "llm" and ALLOW_LLM_IPA)):
            ipa = f" {html.escape(c['ipa'])}"
        gloss = f" — {html.escape(u['gloss'])}" if u["gloss"] else ""
        lines.append(f"✨ <b>{html.escape(c['text'])}</b>{ipa}{gloss}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"🇷🇺 <i>{html.escape(lesson['translation'])}</i>" if TRANSLATION_LANG == "ru" else f"<i>{html.escape(lesson['translation'])}</i>")
    message = "\n".join(lines)
    return message if main.visible_length(message) <= main.MESSAGE_LIMIT else main.compact_for_caption(message, main.MESSAGE_LIMIT)


# --- Telegram ---
def send(message, audio_path=None):
    """-> message_id отправленного текстового сообщения. Бросает исключение при неудаче."""
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    r = requests.post(f"{base}/sendMessage", timeout=60, json={
        "chat_id": WORDS_TG_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True})
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram error: {body}")
    mid = body["result"]["message_id"]
    if audio_path and os.path.exists(audio_path):
        try:
            with open(audio_path, "rb") as f:
                requests.post(f"{base}/sendVoice", timeout=60, data={"chat_id": WORDS_TG_CHAT_ID, "reply_to_message_id": mid}, files={"voice": f}).raise_for_status()
        except Exception as e:  # звук необязателен
            print(f"Words audio send failed (ignored): {e}")
    return mid


def run(state, now=None):
    """Одна попытка публикации. Возвращает код выхода (0 — успех или осознанный пропуск, 1 — сбой отправки)."""
    if not DRY_RUN and not WORDS_TG_CHAT_ID:
        print("WARNING: WORDS_TG_CHAT_ID is not set; words post not sent (never falls back to the quotes channel).")
        return 0
    if not WORDBANK_URL or not WORDBANK_TOKEN:
        print("WARNING: WORDBANK_URL / WORDBANK_TOKEN not set; words post skipped.")
        return 0
    words_slot = slot.current_slot(now, slot.words_slot_hours())
    lang, candidates = choose_language(state)
    chosen_index = state.pop("_words_chosen_index", None)
    if lang is None:
        print("Not enough candidates in any language; skipping this slot.")
        if not DRY_RUN:
            state["last_words_slot"] = words_slot
            main.save_state(state)
        return 0
    try:
        lesson = generate_lesson(lang, candidates)
    except RuntimeError as e:
        print(f"{e}; skipping this slot.")
        if not DRY_RUN:
            state["last_words_slot"] = words_slot
            main.save_state(state)
        return 0
    message = format_message(lang, lesson)
    if DRY_RUN:
        print(message)
        print("Dry run: nothing sent, state unchanged.")
        return 0
    audio = main.generate_audio(lesson["text"], voice=LANGS[lang]["voice"], filename="words.mp3")
    try:
        mid = send(message, audio)
    except Exception as e:
        print(f"Failed to send words post: {e}; state not changed.")
        return 1
    report([u["candidate"]["id"] for u in lesson["used"]], lang, mid, hashlib.sha256(lesson["text"].encode()).hexdigest())
    state["last_words_slot"] = words_slot
    chosen = chosen_index if chosen_index is not None else LANG_ORDER.index(lang)
    state["words_lang_index"] = (chosen + 1) % len(LANG_ORDER)
    main.save_state(state)
    print("Words post sent.")
    return 0


def main_entry():
    state = main.load_state()
    return run(state)


if __name__ == "__main__":
    sys.exit(main_entry())
