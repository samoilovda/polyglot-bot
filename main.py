import os
import json
import requests
import html
import re

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
    """Запрашивает у LLM слово и переводы в стиле Memeglish."""

    prompt = """
    You are the author of a channel called "Memeglish Philosophy". 
    Your tone is: Witty, slightly cynical, paradoxical, and philosophical (but in a funny way).
    
    1. Pick a sophisticated English word (Level B2-C2) related to psychology, modern life, existentialism, or human stupidity.
    2. Create a sentence using this word. 
       CRITICAL STYLE INSTRUCTION: The sentence MUST be an oxymoron, a paradox, a dark joke, or a witty observation. 
       It should sound like a quote from Oscar Wilde if he lived in 2024 and posted memes.
       Example: "Laziness is just the habit of resting before you get tired."
    3. Explain the grammar rule or a linguistic nuance briefly.
    4. Translate the sentence into: Russian, Spanish, Portuguese (Brazil), Turkish, Arabic, and Maori.
    
    Output strictly valid JSON:
    {
      "word": "The chosen word",
      "transcription": "/IPA/",
      "sentence_en": "The witty/philosophical sentence using {{the word}}.",
      "grammar_rule": "Short explanation...",
      "translations": {
        "ru": "...",
        "es": "...",
        "pt_br": "...",
        "tr": "...",
        "ar": "...",
        "mi": "..."
      }
    }
    """

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/polyglot-bot"
    }

    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.1  # Повышаем температуру для большего креатива и юмора
    }

    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        raw_content = result['choices'][0]['message']['content']
        return json.loads(clean_json_response(raw_content))
    except Exception as e:
        print(f"Error: {e}")
        if 'raw_content' in locals(): print(f"Raw: {raw_content}")
        return None
      
def send_telegram(data):
    if not data: return
    # Экранируем HTML
    safe_sentence = html.escape(data['sentence_en']).replace("{{", "<b>").replace("}}", "</b>")
    safe_word = html.escape(data['word'])
    
    message = (
        f"🐸 <b>Memeglish Philosophy</b>\n"
        f"✨ <b>Word:</b> {safe_word} {html.escape(data.get('transcription', ''))}\n\n"
        f"💭 {formatted_sentence}\n\n"
        f"🧠 <i>{html.escape(data['grammar_rule'])}</i>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🇷🇺 {html.escape(data['translations']['ru'])}\n"
        f"🇪🇸 {html.escape(data['translations']['es'])}\n"
        f"🇧🇷 {html.escape(data['translations']['pt_br'])}\n"
        f"🇹🇷 {html.escape(data['translations']['tr'])}\n"
        f"🇸🇦 {html.escape(data['translations']['ar'])}\n"
        f"🇳🇿 {html.escape(data['translations']['mi'])}"
    )

    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
    requests.post(url, json=payload)

def send_discord(data):
    if not data: return
    # Markdown для Discord
    sentence_md = data['sentence_en'].replace("{{", "**").replace("}}", "**")
    
    embed = {
        "title": f"🇬🇧 Word: {data['word']}",
        "description": sentence_md,
        "color": 3447003, # Синий цвет
        "fields": [
            {"name": "Grammar / Nuance", "value": data['grammar_rule'], "inline": False},
            {"name": "Translations", "value": (
                f"🇷🇺 {data['translations']['ru']}\n"
                f"🇪🇸 {data['translations']['es']}\n"
                f"🇧🇷 {data['translations']['pt_br']}\n"
                f"🇹🇷 {data['translations']['tr']}\n"
                f"🇸🇦 {data['translations']['ar']}\n"
                f"🇳🇿 {data['translations']['mi']}"
            ), "inline": False}
        ],
        "footer": {"text": "Daily Polyglot Bot"}
    }
    
    requests.post(DISCORD_WEBHOOK_URL, json={"username": "Polyglot Tutor", "embeds": [embed]})

if __name__ == "__main__":
    data = get_word_data()
    if data:
        send_telegram(data)
        send_discord(data)
        print("Done!")
