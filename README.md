# Quotes Bot

Telegram-бот: 3 раза в день публикует цитату великого человека (с автором и источником)
и переводы на 5 других языков. Культуры чередуются по кругу:

🇬🇧 Британия и Ирландия → 🇪🇸 Испания и Латинская Америка → 🇧🇷 Бразилия → 🇺🇿 Узбекистан → 🇸🇦 арабский мир → 🇷🇺 Россия

Флаг в заголовке поста — страна автора (🇬🇧, 🏴 Шотландия/Уэльс, 🇮🇪, 🇪🇸, 🇲🇽, 🇦🇷, …).
Переводы: британский английский, испанский, бразильский португальский, узбекский (латиница),
арабский (MSA), русский.

## Как устроено
- `data/quotes/<культура>.json` — корпус цитат (оригинал, автор, страна, источник). Переводы и языковые пояснения генерирует LLM.
- `card.py` — генерация графической карточки цитаты (Pillow, 1080×1350, без вызовов ИИ / 0 токенов).
- `main.py` — обогащение цитаты (сложное слово, IPA, исторический контекст `📖` и мост языков `🌉`), озвучка через `edge-tts` с ID3-метаданными (`mutagen`), склейка карточки и звука в единый видео-пост через `ffmpeg` (`sendVideo`, либо фолбэк на `sendPhoto` + `sendVoice`).
- `data/state.json` — очередь культур и уже опубликованные цитаты; коммитится workflow после каждой успешной публикации.
- `.github/workflows/daily.yml` — расписание (10:00, 16:00, 22:00 по Ташкенту; cron каждый час, слот выбирает `slot.py`), тесты, запуск, коммит состояния.

## Переменные окружения
- `TG_BOT_TOKEN`, `TG_CHAT_ID` — основные секреты.
- `LLM_PROVIDERS` задаёт порядок провайдеров. В GitHub Actions по умолчанию используется `hermes`, локально — `claude_cli,gemini`. Для `claude_cli` используются `CLAUDE_CODE_OAUTH_TOKEN` и `CLAUDE_MODEL`, для `gemini` — `GEMINI_API_KEY` и `GEMINI_MODEL`, для `openrouter` — `OPENROUTER_API_KEY` и `OPENROUTER_MODEL`. При ошибке авторизации или квоты цепочка сразу переходит к следующему провайдеру. При невалидном JSON допускаются два повтора.
- `SEND_AS_VOICE` — отправлять аудио как нативное голосовое сообщение (`true` по умолчанию) или как аудиофайл (`false`).
- `GENERATE_CARD` — создавать графическую карточку цитаты (`true` по умолчанию).
- `DRY_RUN` — тестовый запуск без отправки (`1` / `true`).
Также нужно: Settings → Actions → General → Workflow permissions → *Read and write*.

## Hermes на сервере

Hermes получает промпт по SSH и возвращает JSON. Бот проверяет структуру ответа, формирует пост, публикует его и сохраняет состояние ротации.

1. Установите `tools/hermes_generate.py` на хосте Hermes. В `authorized_keys` учётной записи Hermes добавьте **отдельный** ключ для бота с `restrict` и принудительной командой. Подставьте абсолютные пути к Python Hermes, скрипту и каталогу установки, а также провайдер и модель этого экземпляра.

   ```text
   restrict,command="/usr/bin/timeout --kill-after=5s 110s /path/to/python -I /path/to/hermes_generate.py /path/to/hermes-agent PROVIDER MODEL" ssh-ed25519 PUBLIC_KEY polyglot-hermes
   ```

   Обработчик запускает Hermes без инструментов, памяти и файлов контекста. Промпт передаётся через stdin, а Telegram-токен остаётся в GitHub Actions.
2. В секреты репозитория добавьте `HERMES_SSH_TARGET` (`user@host`), `HERMES_SSH_KEY` (закрытая часть выделенного ключа) и `HERMES_SSH_HOST_KEY` (проверенная строка `known_hosts` для хоста).
3. Запустите `Quotes Bot` с `dry_run=true`. В журнале должна появиться строка `LLM provider hermes: ok`. Пост и состояние ротации при пробном запуске не отправляются.

## Локальный запуск
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
LLM_PROVIDERS=gemini GEMINI_API_KEY=... DRY_RUN=1 FORCE_CULTURE=uz python main.py   # печатает пост и генерирует тестовую карточку
```

## Как добавить цитаты
Добавьте объект в нужный `data/quotes/*.json`: `id` (`<культура>-<автор>-NNN`, уникальный), `country`
(ISO-код: `GB`, `IE`, `GB-SCT`, `GB-WLS`, `ES`, `MX`, …), `text` (≥ 5 слов, ≤ 300 символов),
`author`, `author_en`, `source`. Тесты проверят формат.

## «Words in context» (words_post.py)
Отдельный пост из слов, сохранённых в Wordbank, в **отдельный** канал (`WORDS_TG_CHAT_ID`; в канал цитат не отправляется никогда).
- Секреты: `WORDBANK_URL`, `WORDBANK_TOKEN` (токен со scope `publish`), `WORDS_TG_CHAT_ID`; LLM — те же, что у цитат.
- Слот: `WORDS_SLOT_HOURS` (по умолчанию `13` по Ташкенту), ключи состояния `last_words_slot`, `words_lang_index`. Если наступили оба слота, цитата публикуется первой, слова — следующим часовым запуском.
- Переменные: `WORDS_LEVEL_EN/ES/PT/UZ`, `WORDS_ALLOW_LLM_IPA`, `FORCE_WORDS_LANG` (для DRY_RUN), `WORDS_TRANSLATION_LANG`.
- Ручной запуск: workflow_dispatch с `words=true` (и `dry_run=true` для образца поста).
- При нехватке кандидатов (< 4) или неудаче генерации слот пропускается с кодом выхода 0.
