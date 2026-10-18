import html
import json
import os
import random
import re
import subprocess
import sys

import requests

import card
import slot

# --- КОНФИГУРАЦИЯ ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
MODEL_NAME = os.environ.get("OPENROUTER_MODEL", "~google/gemini-flash-latest")  # алиас OpenRouter на последнюю Gemini Flash
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
SEND_AS_VOICE = os.environ.get("SEND_AS_VOICE", "true").strip().lower() in ("1", "true", "yes")
GENERATE_CARD = os.environ.get("GENERATE_CARD", "true").strip().lower() in ("1", "true", "yes")
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
def build_prompt(quote, culture):
    all_langs = "\n".join(f'- "{k}": {CULTURES[k]["lang_name"]}' for k in CULTURE_ORDER)
    source = f', from "{quote["source"]}"' if quote.get("source") else ""
    return f"""You are a literary translator of famous quotations and a language teacher.

Original ({CULTURES[culture]["lang_name"]}), by {quote["author_en"]}{source}:
\"\"\"{quote["text"]}\"\"\"

Step 1. Pick ONE of the most difficult / sophisticated words of the ORIGINAL quotation (a content word, not a function word).
Step 2. Explain its meaning in simple English (max 12 words).
Step 3. Context: In 1 concise sentence in simple English (max 15 words), explain the historical, cultural, or literary context of this quotation or author.
Step 4. Language bridge: In 1 concise sentence in simple English (max 20 words), highlight an interesting linguistic connection, etymology, cognate, or comparison between the picked word/concept and another language in the set (e.g. shared Latin roots between en/es/pt, Arabic-Uzbek ties, or false friends).
Step 5. Translate the quotation into every language except the original one, and choose in each translation the word that corresponds to the picked word.

Languages:
{all_langs}

Rules:
- Keep the meaning, tone and rhythm; translate literarily, not word by word.
- If a well-known published translation exists, prefer it.
- British English: British spelling (colour, realise).
- Brazilian Portuguese: Brazilian norm and vocabulary.
- Uzbek: Latin script, with o‘ and g‘.
- Arabic: Modern Standard Arabic, no diacritics.
- Translate only the quotation itself, without the author or quotation marks.
- In EVERY text, wrap the picked word (or its equivalent) in {{{{double curly braces}}}}. This is required for every language, including Arabic.
- For the original language, "text" is the original quotation copied EXACTLY, only with the word wrapped in braces.
- For each language give "word" (the word in its dictionary/inflected form as used in the text) and "ipa" (its IPA transcription in slashes, e.g. /ˈkɒntəmpleɪt/).

Answer with strictly valid JSON:
{{"meaning_en": "...", "context_en": "...", "bridge_en": "...", "languages": {{"<lang key>": {{"word": "...", "ipa": "/.../", "text": "... {{{{word}}}} ..."}}, ...}}}}
The "languages" object must contain exactly these keys: {json.dumps(CULTURE_ORDER)}."""


def parse_lesson(data, quote, culture):
    """Проверяет ответ модели. Возвращает {"meaning_en": str, "context_en": str, "bridge_en": str, "languages": {key: {word, ipa, text}}}."""
    meaning = str(data.get("meaning_en", "")).strip()
    if not meaning:
        raise ValueError("missing meaning_en")
    context_en = str(data.get("context_en") or data.get("context") or "").strip()
    bridge_en = str(data.get("bridge_en") or data.get("bridge") or "").strip()
    languages = data.get("languages") or {}
    result = {}
    for key in CULTURE_ORDER:
        item = languages.get(key) or {}
        text = str(item.get("text", "")).strip()
        if not text:
            raise ValueError(f"missing text for {key}")
        if key == culture and re.sub(r"\{+|\}+", "", text).strip() != quote["text"].strip():
            text = quote["text"]  # модель исказила оригинал — берём как есть, без выделения
        result[key] = {
            "word": str(item.get("word", "")).strip(),
            "ipa": str(item.get("ipa", "")).strip(),
            "text": text,
        }
    if not result[culture]["word"]:
        raise ValueError("missing word for original language")
    return {
        "meaning_en": meaning,
        "context_en": context_en,
        "bridge_en": bridge_en,
        "languages": result,
    }


def translate_quote(quote, culture, retries=2):
    """Переводит цитату, выбирает сложное слово и его эквиваленты. См. parse_lesson."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/polyglot-bot",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": build_prompt(quote, culture)}],
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
            return parse_lesson(json.loads(clean_json_response(raw)), quote, culture)
        except Exception as e:
            last_error = e
            print(f"Translation attempt {attempt + 1} failed: {e}")
    raise RuntimeError(f"Translation failed: {last_error}")


# --- ОЗВУЧКА ---
def add_audio_metadata(filename, quote, culture):
    """Вшивает ID3-теги в аудиофайл (автор, название цитаты, альбом). Ошибка не фатальна."""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, COMM, ID3NoHeaderError
        try:
            tags = ID3(filename)
        except ID3NoHeaderError:
            tags = ID3()
        title = quote.get("source") or quote["text"][:60]
        artist = quote["author"]
        if quote.get("author_en") and quote["author_en"] != quote["author"]:
            artist += f" ({quote['author_en']})"
        info = CULTURES.get(culture, {})
        album = f"Polyglot Wisdom • {info.get('title', 'Quotes')}"

        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags["TALB"] = TALB(encoding=3, text=album)
        tags["COMM"] = COMM(encoding=3, lang="eng", desc="Quote", text=quote["text"])
        tags.save(filename)
        print(f"Audio metadata added: {artist} — {title}")
    except Exception as e:
        print(f"Audio metadata error (skipped): {e}")


def generate_audio(quote_or_text, culture=None, voice=None, filename="quote.mp3"):
    """Генерирует аудиофайл с помощью бесплатного edge-tts и добавляет ID3-метаданные. Ошибка не фатальна."""
    if isinstance(quote_or_text, dict):
        text = quote_or_text["text"]
        quote = quote_or_text
    else:
        text = quote_or_text
        quote = None
        if voice is None and culture is not None and not isinstance(culture, dict):
            voice = culture
            culture = None

    clean_text = re.sub(r"\{+|\}+|\*+|<[^>]+>", "", text)
    command = ["edge-tts", "--voice", voice, "--text", clean_text, "--write-media", filename]
    try:
        subprocess.run(command, check=True, timeout=120)
        if quote and culture:
            add_audio_metadata(filename, quote, culture)
        return filename
    except Exception as e:
        print(f"Audio generation error: {e}")
        return None


def get_ffmpeg_exe():
    """Возвращает путь к ffmpeg (системный или из пакета imageio-ffmpeg)."""
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def generate_video_card(card_path, audio_path, output_path="quote_video.mp4"):
    """Склеивает статичную карточку и озвучку в MP4-ролик с помощью ffmpeg. Ошибка не фатальна."""
    if not (card_path and audio_path and os.path.exists(card_path) and os.path.exists(audio_path)):
        return None
    command = [
        get_ffmpeg_exe(), "-y",
        "-loop", "1",
        "-framerate", "2",
        "-i", card_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        subprocess.run(command, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        print(f"Video card generated: {output_path}")
        return output_path
    except Exception as e:
        print(f"Video generation error (skipped): {e}")
        return None



def visible_length(html_text):
    """Длина видимого текста в UTF-16 code units после снятия HTML-тегов (лимит Telegram)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(plain.encode("utf-16-le")) // 2


def compact_for_caption(message, limit=CAPTION_LIMIT):
    """Ужимает текст подписи до лимита Telegram (1024), чтобы медиа и текст уходили одним сообщением."""
    if visible_length(message) <= limit:
        return message
    for pattern in (
        r"\n\u200e?━+",
        r"\n\n<blockquote>.*?</blockquote>",
        r"\n<i>[^<]*?</i>",
        r"\n\u200e?📖 <i>.*?</i>",
        r"\n\u200e?🌉 <i>.*?</i>",
    ):
        message = re.sub(pattern, "", message, count=1)
        if visible_length(message) <= limit:
            return message
    return message


# --- ФОРМАТ СООБЩЕНИЯ ---
def rtl(text):
    """Оборачивает в RLM-маркеры, чтобы арабский не «ломал» соседние строки."""
    return f"‏{text}‏"


def make_bold(escaped_text):
    """{{слово}} -> <b>слово</b> (текст уже экранирован)."""
    return re.sub(r"\{+(.*?)\}+", r"<b>\1</b>", escaped_text)


def format_message(quote, culture, lesson):
    info = CULTURES[culture]
    country = country_flag(quote.get("country")) or info["flag"]
    is_ar = culture == "ar"
    langs = lesson["languages"]

    title = html.escape(info["title"])
    text = make_bold(html.escape(langs[culture]["text"]))
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

    context_en = html.escape(lesson.get("context_en") or lesson.get("context") or "").strip()
    if context_en:
        lines.append(f"📖 <i>{context_en}</i>")

    word = html.escape(langs[culture]["word"])
    ipa = html.escape(langs[culture]["ipa"])
    word_line = f"✨ <b>{word}</b>" + (f" {ipa}" if ipa else "")
    word_line = f"{word_line} — {html.escape(lesson['meaning_en'])}"
    separator = "━━━━━━━━━━━━━━━━━━"
    if is_ar:
        # арабское слово в начале строки делает абзац RTL и перемешивает IPA/перевод;
        # LRM в начале принудительно задаёт LTR-абзац
        word_line, separator = f"\u200e{word_line}", f"\u200e{separator}"
    lines += ["", word_line]

    bridge_en = html.escape(lesson.get("bridge_en") or lesson.get("bridge") or "").strip()
    if bridge_en:
        bridge_line = f"🌉 <i>{bridge_en}</i>"
        if is_ar:
            bridge_line = f"\u200e{bridge_line}"
        lines.append(bridge_line)

    lines.append(separator)

    for key in CULTURE_ORDER:
        if key == culture:
            continue
        item = langs[key]
        line = make_bold(html.escape(item["text"]))
        if key == "ar":
            line = rtl(line)
        ipa = html.escape(item["ipa"])
        prefix = f"{ipa} " if ipa else ""
        lines.append(f"{CULTURES[key]['flag']} {prefix}{line}")
    return "\n".join(lines)


# --- ОТПРАВКА ---
def send_telegram(message, audio_path=None, card_path=None, video_path=None, quote=None):
    """Отправляет пост. Возвращает True только если сообщение реально доставлено."""
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

    def call(method, **kwargs):
        response = requests.post(f"{base}/{method}", timeout=60, **kwargs)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")
        return result["result"]

    has_video = bool(video_path and os.path.exists(video_path))
    has_audio = bool(audio_path and os.path.exists(audio_path))
    has_card = bool(card_path and os.path.exists(card_path))
    caption_msg = compact_for_caption(message) if (has_video or has_card) else message
    fits_caption = visible_length(caption_msg) <= CAPTION_LIMIT

    title = quote.get("source") if quote else None
    performer = quote.get("author") if quote else None

    def send_media_audio(reply_to=None):
        if not has_audio:
            return None
        try:
            with open(audio_path, "rb") as f:
                if SEND_AS_VOICE:
                    data = {"chat_id": TG_CHAT_ID}
                    if reply_to:
                        data["reply_to_message_id"] = reply_to
                    call("sendVoice", data=data, files={"voice": f})
                    print("Telegram voice message sent.")
                else:
                    data = {"chat_id": TG_CHAT_ID}
                    if title:
                        data["title"] = title
                    if performer:
                        data["performer"] = performer
                    if reply_to:
                        data["reply_to_message_id"] = reply_to
                    call("sendAudio", data=data, files={"audio": f})
                    print("Telegram audio message sent.")
        except Exception as e:
            print(f"Telegram Audio Error: {e}")

    try:
        # Сценарий 0 (основной): есть видео-карточка (картинка + звук в одном MP4)
        if has_video:
            with open(video_path, "rb") as video_file:
                if fits_caption:
                    call(
                        "sendVideo",
                        data={
                            "chat_id": TG_CHAT_ID,
                            "caption": caption_msg,
                            "parse_mode": "HTML",
                            "width": 1080,
                            "height": 1350,
                            "supports_streaming": "true",
                        },
                        files={"video": video_file},
                    )
                    print("Telegram video card with caption sent in a single message.")
                else:
                    sent_video = call(
                        "sendVideo",
                        data={
                            "chat_id": TG_CHAT_ID,
                            "width": 1080,
                            "height": 1350,
                            "supports_streaming": "true",
                        },
                        files={"video": video_file},
                    )
                    print("Telegram video card sent.")
                    call(
                        "sendMessage",
                        json={
                            "chat_id": TG_CHAT_ID,
                            "text": message[:MESSAGE_LIMIT],
                            "parse_mode": "HTML",
                            "reply_to_message_id": sent_video["message_id"],
                        },
                    )
                    print("Telegram text message sent (reply to video).")
            return True

        # Сценарий 1 (фолбэк): есть фото-карточка без видео
        if has_card:
            with open(card_path, "rb") as photo_file:
                if fits_caption:
                    sent_root = call(
                        "sendPhoto",
                        data={"chat_id": TG_CHAT_ID, "caption": message, "parse_mode": "HTML"},
                        files={"photo": photo_file},
                    )
                    print("Telegram photo with caption sent.")
                else:
                    sent_root = call(
                        "sendPhoto",
                        data={"chat_id": TG_CHAT_ID},
                        files={"photo": photo_file},
                    )
                    print("Telegram photo sent.")
                    sent_text = call(
                        "sendMessage",
                        json={"chat_id": TG_CHAT_ID, "text": message[:MESSAGE_LIMIT], "parse_mode": "HTML", "reply_to_message_id": sent_root["message_id"]},
                    )
                    sent_root = sent_text
                    print("Telegram text message sent (reply to photo).")

            send_media_audio(reply_to=sent_root["message_id"])
            return True

        # Сценарий 2: карточки нет, но есть аудио и текст влезает в caption
        if has_audio and fits_caption:
            with open(audio_path, "rb") as f:
                if SEND_AS_VOICE:
                    call("sendVoice", data={"chat_id": TG_CHAT_ID, "caption": message, "parse_mode": "HTML"}, files={"voice": f})
                    print("Telegram voice message with caption sent.")
                else:
                    data = {"chat_id": TG_CHAT_ID, "caption": message, "parse_mode": "HTML"}
                    if title:
                        data["title"] = title
                    if performer:
                        data["performer"] = performer
                    call("sendAudio", data=data, files={"audio": f})
                    print("Telegram audio message with caption sent.")
            return True

        # Сценарий 3: обычное текстовое сообщение + аудио вдогонку
        sent = call("sendMessage", json={"chat_id": TG_CHAT_ID, "text": message[:MESSAGE_LIMIT], "parse_mode": "HTML"})
        print("Telegram text message sent.")
        send_media_audio(reply_to=sent["message_id"])
        return True

    except Exception as e:
        print(f"Telegram Error: {e}")
        return False


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
    lesson = translate_quote(quote, culture)
    message = format_message(quote, culture, lesson)

    card_path = None
    if GENERATE_CARD:
        try:
            card_path = card.generate_card(quote, culture, lesson, output_path="quote_card.png")
        except Exception as e:
            print(f"Card generation error (skipped): {e}")

    if DRY_RUN:
        print(message)
        if card_path:
            print(f"Dry run: card generated at {card_path}")
        print("Dry run: nothing sent, state unchanged.")
        return 0

    voice = VOICE_BY_COUNTRY.get(quote.get("country", ""), CULTURES[culture]["voice"])
    audio_path = generate_audio(quote, culture, voice)
    video_path = generate_video_card(card_path, audio_path)

    if not send_telegram(message, audio_path=audio_path, card_path=card_path, video_path=video_path, quote=quote):
        print("Failed to send to Telegram; state not changed.")
        return 1

    state["posted_ids"].append(quote["id"])
    state["last_slot"] = slot.current_slot()
    state["next_culture_index"] = (index + 1) % len(CULTURE_ORDER)
    save_state(state)
    print("Workflow completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())


