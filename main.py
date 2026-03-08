import os
import json
import requests
import html
import re
import random
import subprocess

# --- КОНФИГУРАЦИЯ ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Используем Gemini 2.0 Flash (быстрая и дешевая)
MODEL_NAME = "google/gemini-2.0-flash-001"

def clean_json_response(content):
    """Очищает ответ от markdown-оберток"""
    content = re.sub(r"^```json\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"^```\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
    return content.strip()

def get_word_data():
    """Запрашивает слово с учетом случайной буквы и детального перевода."""
    
    random_letter = random.choice("ABCDEFGHIJKLMNOPRSTUVW")
    banned_words = "catharsis, resilience, procrastination, empathy, narcissism, burnout"

    prompt = f"""
    You are the author of a channel called "Memeglish Philosophy". 
    Your tone is: Witty, slightly cynical, paradoxical, and philosophical.
    
    Task:
    1. Pick a sophisticated English word (Level B2-C2) related to psychology, modern life, or existentialism.
    
    CRITICAL CONSTRAINTS:
    - The word MUST start with the letter: "{random_letter}"
    - DO NOT use these words: {banned_words}
    
    2. Create a sentence using this word. 
       Style: The sentence MUST be an oxymoron, a paradox, or a dark joke.
       
    3. Explain the grammar rule briefly.

    4. Translate into: Russian, Spanish, Portuguese (Brazil), Arabic, and Maori.
    For EACH language, provide:
       - The translated word.
       - The IPA transcription of that word.
       - The translated sentence where the translated word is wrapped in {{double curly braces}}.
       CRITICAL: Ensure the {{ }} are present in EVERY language, especially Arabic!

    Output strictly valid JSON:
    {{
      "word": "The chosen word (starting with {random_letter})",
      "transcription": "/IPA/",
      "sentence_en": "The witty sentence using {{the word}}.",
      "grammar_rule": "Short explanation...",
      "translations": {{
        "ru": {{ "word": "...", "transcription": "[...]", "sentence": "Предложение с {{словом}}." }},
        "es": {{ "word": "...", "transcription": "[...]", "sentence": "Frase con {{palabra}}." }},
        "pt_br": {{ "word": "...", "transcription": "[...]", "sentence": "..." }},
        "ar": {{ "word": "...", "transcription": "[...]", "sentence": "..." }},
        "mi": {{ "word": "...", "transcription": "[...]", "sentence": "..." }}
      }}
    }}
    """

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/polyglot-bot"
    }

    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.0
    }

    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        raw_content = result['choices'][0]['message']['content']
        return json.loads(clean_json_response(raw_content))
    except Exception as e:
        print(f"Error fetching data: {e}")
        if 'raw_content' in locals(): print(f"Raw: {raw_content}")
        return None

def generate_audio(text, filename="daily_word.mp3"):
    """Генерирует аудиофайл с помощью бесплатного edge-tts."""
    # Очищаем текст от фигурных скобок и спецсимволов, чтобы голос их не читал
    clean_text = re.sub(r'\{+|\}+|\*+|<[^>]+>', '', text)
    
    # en-GB-RyanNeural - саркастичный британский мужской голос
    voice = "en-GB-RyanNeural" 
    
    command = ["edge-tts", "--voice", voice, "--text", clean_text, "--write-media", filename]
    
    try:
        subprocess.run(command, check=True)
        return filename
    except Exception as e:
        print(f"Audio generation error: {e}")
        return None

def send_telegram(data, audio_path=None):
    if not data: return

    def make_bold(text):
        return re.sub(r"\{+(.*?)\}+", r"<b>\1</b>", text)

    def format_line(flag, lang_key):
        lang_data = data['translations'].get(lang_key)
        if not lang_data: return ""
        
        safe_sent = html.escape(lang_data['sentence'])
        safe_trans = html.escape(lang_data.get('transcription', ''))
        formatted = make_bold(safe_sent)
        
        # МАГИЯ ДЛЯ АРАБСКОГО (RLM маркеры)
        if lang_key == 'ar':
            formatted = f"\u200F{formatted}\u200F"

        return f"{flag} <code>{safe_trans}</code> {formatted}\n"

    safe_sentence = html.escape(data['sentence_en'])
    formatted_main_sentence = make_bold(safe_sentence)
    
    safe_word = html.escape(data['word'])
    safe_transcription = html.escape(data.get('transcription', ''))
    safe_grammar = html.escape(data.get('grammar_rule', ''))

    message = (
        f"🐸 <b>Word Of The Day!</b>\n\n"
        f"✨ <b>Word:</b> {safe_word} {safe_transcription}\n"
        f"💭 {formatted_main_sentence}\n\n"
        f"🧠 <i>{safe_grammar}</i>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{format_line('🇷🇺', 'ru')}"
        f"{format_line('🇪🇸', 'es')}"
        f"{format_line('🇧🇷', 'pt_br')}"
        f"{format_line('🇸🇦', 'ar')}"
        f"{format_line('🇳🇿', 'mi')}"
    )

    # Если аудио успешно сгенерировалось — отправляем метод sendAudio
    if audio_path and os.path.exists(audio_path):
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendAudio"
        with open(audio_path, 'rb') as audio_file:
            files = {'audio': audio_file}
            # Текст поста становится подписью (caption) к аудио
            payload = {'chat_id': TG_CHAT_ID, 'caption': message, 'parse_mode': 'HTML'}
            try:
                requests.post(url, data=payload, files=files).raise_for_status()
                print("Telegram message with audio sent.")
            except Exception as e:
                print(f"Telegram Audio Error: {e}")
    else:
        # Резервный вариант: если аудио сломалось, шлем просто текст
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
        try:
            requests.post(url, json=payload).raise_for_status()
            print("Telegram text message sent (fallback).")
        except Exception as e:
            print(f"Telegram Text Error: {e}")

def send_discord(data):
    if not data: return
    
    def make_bold_md(text):
        return re.sub(r"\{+(.*?)\}+", r"**\1**", text)

    def format_line_md(flag, lang_key):
        lang_data = data['translations'].get(lang_key)
        if not lang_data: return ""
        
        sent = make_bold_md(lang_data['sentence'])
        
        # МАГИЯ ДЛЯ АРАБСКОГО (RLM маркеры)
        if lang_key == 'ar':
            sent = f"\u200F{sent}\u200F"
        
        trans = lang_data.get('transcription', '')
        return f"{flag} `[{trans}]` {sent}\n"

    sentence_md = make_bold_md(data['sentence_en'])
    
    translations_block = (
        f"{format_line_md('🇷🇺', 'ru')}"
        f"{format_line_md('🇪🇸', 'es')}"
        f"{format_line_md('🇧🇷', 'pt_br')}"
        f"{format_line_md('🇸🇦', 'ar')}"
        f"{format_line_md('🇳🇿', 'mi')}"
    )

    embed = {
        "title": f"🇬🇧 Word: {data['word']}",
        "description": sentence_md,
        "color": 3447003,
        "fields": [
            {"name": "Grammar / Nuance", "value": data['grammar_rule'], "inline": False},
            {"name": "Translations", "value": translations_block, "inline": False}
        ],
        "footer": {"text": "Daily Polyglot Bot"}
    }
    
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"username": "Polyglot Tutor", "embeds": [embed]})
        print("Discord message sent.")
    except Exception as e:
        print(f"Discord Error: {e}")

if __name__ == "__main__":
    print("Starting word generation...")
    word_data = get_word_data()
    
    if word_data:
        # Формируем текст для озвучки: "Слово. Английское предложение."
        text_to_speak = f"{word_data['word']}. {word_data['sentence_en']}"
        
        # Генерируем mp3
        print("Generating audio...")
        audio_file_path = generate_audio(text_to_speak)
        
        # Отправляем в каналы
        send_telegram(word_data, audio_path=audio_file_path)
        send_discord(word_data)
        
        print("Workflow completed successfully!")
    else:
        print("Failed to get word data.")
