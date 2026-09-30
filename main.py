import html
import json
import os
import random
import re
import subprocess
import sys

import requests

# --- КОНФИГУРАЦИЯ ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
MODEL_NAME = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# Для отладки: принудительно выбрать культуру (ключ из CULTURE_ORDER)
FORCE_CULTURE = os.environ.get("FORCE_CULTURE", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUOTES_DIR = os.path.join(BASE_DIR, "data", "quotes")
STATE_PATH = os.path.join(BASE_DIR, "data", "state.json")

MIN_WORDS = 5
CAPTION_LIMIT = 1024  # лимит подписи к аудио в Telegram
MESSAGE_LIMIT = 4096  # лимит обычного сообщения в Telegram

# Порядок ротации культур (= ключи языков)
CULTURE_ORDER = ["en_gb", "es", "pt_br", "uz", "ar", "ru"]

CULTURES = {
    "en_gb": {"flag": "🇬🇧", "lang_name": "British English", "title": "British wisdom", "voice": "en-GB-RyanNeural"},
    "es": {"flag": "🇪🇸", "lang_name": "Spanish (Spain and Latin America)", "title": "Sabiduría hispana", "voice": "es-ES-AlvaroNeural"},
    "pt_br": {"flag": "🇧🇷", "lang_name": "Brazilian Portuguese", "title": "Sabedoria brasileira", "voice": "pt-BR-AntonioNeural"},
    "uz": {"flag": "🇺🇿", "lang_name": "Uzbek (Latin script, with o‘ and g‘)", "title": "O‘zbek donoligi", "voice": "uz-UZ-SardorNeural"},
    "ar": {"flag": "🇸🇦", "lang_name": "Modern Standard Arabic", "title": "حكمة عربية", "voice": "ar-SA-HamedNeural"},
    "ru": {"flag": "🇷🇺", "lang_name": "Russian", "title": "Русская мудрость", "voice": "ru-RU-DmitryNeural"},
}

# Голоса озвучки для отдельных стран (если нет — берётся голос культуры)
VOICE_BY_COUNTRY = {
    "IE": "en-IE-ConnorNeural",
    "MX": "es-MX-JorgeNeural",
    "AR": "es-AR-TomasNeural",
    "CO": "es-CO-GonzaloNeural",
    "CL": "es-CL-LorenzoNeural",
}


# --- УТИЛИТЫ ---
def country_flag(code):
    """Код страны -> эмодзи-флаг. 'ES' -> 🇪🇸, 'GB-SCT' -> 🏴 Шотландия, 'GB-WLS' -> 🏴 Уэльс."""
    code = (code or "").upper()
    if re.fullmatch(r"[A-Z]{2}", code):
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)
    if re.fullmatch(r"[A-Z]{2}-[A-Z]{3}", code):
        tags = "".join(chr(0xE0000 + ord(c.lower())) for c in code.replace("-", ""))
        return "\U0001F3F4" + tags + chr(0xE007F)
    return ""


def count_words(text):
    return len(text.split())


def clean_json_response(content):
    """Очищает ответ от markdown-оберток"""
    content = re.sub(r"^```json\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"^```\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
    return content.strip()


# --- СОСТОЯНИЕ И КОРПУС ---
def default_state():
    return {"next_culture_index": 0, "posted_ids": []}


def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return default_state()
    state.setdefault("next_culture_index", 0)
    state.setdefault("posted_ids", [])
    return state


def save_state(state, path=STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_quotes(culture, quotes_dir=QUOTES_DIR):
    with open(os.path.join(quotes_dir, f"{culture}.json"), encoding="utf-8") as f:
        return json.load(f)


def pick_quote(culture, state, quotes_dir=QUOTES_DIR):
    """Случайная неопубликованная цитата культуры. Когда все использованы — круг начинается заново."""
    quotes = [q for q in load_quotes(culture, quotes_dir) if count_words(q["text"]) >= MIN_WORDS]
    if not quotes:
        raise RuntimeError(f"No valid quotes for culture {culture}")
    posted = set(state["posted_ids"])
    fresh = [q for q in quotes if q["id"] not in posted]
    if not fresh:
        print(f"WARNING: all quotes of '{culture}' were used, starting a new cycle.")
        ids = {q["id"] for q in quotes}
        state["posted_ids"] = [i for i in state["posted_ids"] if i not in ids]
        fresh = quotes
    return random.choice(fresh)


# --- ПЕРЕВОД ---
def translate_quote(quote, culture, retries=2):
    """Переводит цитату на все языки, кроме языка оригинала. Возвращает {lang_key: text}."""
    targets = [k for k in CULTURE_ORDER if k != culture]
    lang_list = "\n".join(f'- "{k}": {CULTURES[k]["lang_name"]}' for k in targets)
    prompt = f"""You are a literary translator of famous quotations.

Original ({CULTURES[culture]["lang_name"]}), by {quote["author_en"]}{f', from "{quote["source"]}"' if quote.get("source") else ""}:
\"\"\"{quote["text"]}\"\"\"

Translate the quotation into each of these languages:
{lang_list}

Rules:
- Keep the meaning, tone and rhythm; translate literarily, not word by word.
- If a well-known published translation exists, prefer it.
- British English: British spelling (colour, realise).
- Brazilian Portuguese: Brazilian norm and vocabulary.
- Uzbek: Latin script, with o‘ and g‘.
- Arabic: Modern Standard Arabic, no diacritics.
- Output only the translation of the quotation itself, without the author or quotation marks.

Answer with strictly valid JSON, keys exactly: {json.dumps(targets)}.
Example shape: {{"{targets[0]}": "...", "{targets[1]}": "..."}}"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/polyglot-bot",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=60,
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            data = json.loads(clean_json_response(raw))
            missing = [k for k in targets if not str(data.get(k, "")).strip()]
            if missing:
                raise ValueError(f"missing translations: {missing}")
            return {k: str(data[k]).strip() for k in targets}
        except Exception as e:
            last_error = e
            print(f"Translation attempt {attempt + 1} failed: {e}")
    raise RuntimeError(f"Translation failed: {last_error}")


# --- ОЗВУЧКА ---
def generate_audio(text, voice, filename="quote.mp3"):
    """Генерирует аудиофайл с помощью бесплатного edge-tts. Ошибка не фатальна."""
    clean_text = re.sub(r"\{+|\}+|\*+|<[^>]+>", "", text)
    command = ["edge-tts", "--voice", voice, "--text", clean_text, "--write-media", filename]
    try:
        subprocess.run(command, check=True, timeout=120)
        return filename
    except Exception as e:
        print(f"Audio generation error: {e}")
        return None


# --- ФОРМАТ СООБЩЕНИЯ ---
def rtl(text):
    """Оборачивает в RLM-маркеры, чтобы арабский не «ломал» соседние строки."""
    return f"‏{text}‏"


def format_message(quote, culture, translations):
    info = CULTURES[culture]
    country = country_flag(quote.get("country")) or info["flag"]
    is_ar = culture == "ar"

    title = html.escape(info["title"])
    text = html.escape(quote["text"])
    author = html.escape(quote["author"])
    author_en = html.escape(quote.get("author_en", ""))
    source = html.escape(quote.get("source", ""))

    author_line = f"— <b>{author}</b>"
    if author_en and author_en != author:
        author_line += f" ({author_en})"

    head = f"{country} <b>{title}</b>"
    body = f"<blockquote>{text}</blockquote>"
    if is_ar:
        head, body, author_line = rtl(head), f"<blockquote>{rtl(text)}</blockquote>", rtl(author_line)

    lines = [head, "", body, author_line]
    if source:
        lines.append(f"<i>{source}</i>")
    lines.append("━━━━━━━━━━━━━━━━━━")

    for key in CULTURE_ORDER:
        if key == culture:
            continue
        line = html.escape(translations[key])
        if key == "ar":
            line = rtl(line)
        lines.append(f"{CULTURES[key]['flag']} {line}")
    return "\n".join(lines)


# --- ОТПРАВКА ---
def send_telegram(message, audio_path=None):
    """Отправляет пост. Возвращает True только если сообщение реально доставлено."""
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

    def call(method, **kwargs):
        response = requests.post(f"{base}/{method}", timeout=60, **kwargs)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")
        return result["result"]

    has_audio = bool(audio_path and os.path.exists(audio_path))

    try:
        if has_audio and len(message) <= CAPTION_LIMIT:
            with open(audio_path, "rb") as audio_file:
                call("sendAudio", data={"chat_id": TG_CHAT_ID, "caption": message, "parse_mode": "HTML"},
                     files={"audio": audio_file})
            print("Telegram message with audio sent.")
            return True

        sent = call("sendMessage", json={"chat_id": TG_CHAT_ID, "text": message[:MESSAGE_LIMIT], "parse_mode": "HTML"})
        print("Telegram text message sent.")
    except Exception as e:
        print(f"Telegram Error: {e}")
        return False

    if has_audio:  # аудио вдогонку; его сбой не отменяет уже опубликованный пост
        try:
            with open(audio_path, "rb") as audio_file:
                call("sendAudio", data={"chat_id": TG_CHAT_ID, "reply_to_message_id": sent["message_id"]},
                     files={"audio": audio_file})
            print("Telegram audio sent.")
        except Exception as e:
            print(f"Telegram Audio Error (post already published): {e}")
    return True


# --- ТОЧКА ВХОДА ---
def main():
    state = load_state()
    index = state["next_culture_index"] % len(CULTURE_ORDER)
    culture = (FORCE_CULTURE if DRY_RUN and FORCE_CULTURE else CULTURE_ORDER[index])
    if culture not in CULTURES:
        print(f"Unknown culture: {culture}")
        return 1
    print(f"Culture: {culture} (dry run: {DRY_RUN})")

    quote = pick_quote(culture, state)
    translations = translate_quote(quote, culture)
    message = format_message(quote, culture, translations)

    if DRY_RUN:
        print(message)
        print("Dry run: nothing sent, state unchanged.")
        return 0

    voice = VOICE_BY_COUNTRY.get(quote.get("country", ""), CULTURES[culture]["voice"])
    audio_path = generate_audio(quote["text"], voice)

    if not send_telegram(message, audio_path):
        print("Failed to send to Telegram; state not changed.")
        return 1

    state["posted_ids"].append(quote["id"])
    state["next_culture_index"] = (index + 1) % len(CULTURE_ORDER)
    save_state(state)
    print("Workflow completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
