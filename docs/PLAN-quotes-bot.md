# План переработки: Polyglot Bot → «Цитаты великих людей»

> Документ — техническое задание для ИИ-агента. Выполнять этапы по порядку,
> после каждого этапа — коммит. Ветка: рабочая ветка задачи (не `main`).

## 0. Контекст: что есть сейчас

| Файл | Что делает |
|---|---|
| `main.py` | `get_word_data()` — просит LLM (OpenRouter, `google/gemini-2.0-flash-001`) придумать английское слово + фразу-парадокс + переводы на ru/es/pt_br/ar/mi; `generate_audio()` — озвучка через `edge-tts` (`en-GB-RyanNeural`); `send_telegram()` — пост с аудио (caption) или текстом; `send_discord()` — embed в Discord. |
| `.github/workflows/daily.yml` | Cron `0 6 * * *` (1 раз в день), Python 3.9, секреты `OPENROUTER_API_KEY`, `TG_BOT_TOKEN`, `TG_CHAT_ID`, `DISCORD_WEBHOOK_URL`. |
| `requirements.txt` | `requests`, `edge-tts`. |

Состояния между запусками нет — каждый запуск независим.

## 1. Целевое поведение

1. Бот запускается **3 раза в день** (GitHub Actions cron).
2. Каждый запуск публикует в **Telegram** одну **цитату великого человека**:
   - с **указанием автора**;
   - длиной **не менее 5 слов** (в оригинале);
   - **культуры чередуются по кругу**: 🇬🇧 британская → 🇪🇸 испанская → 🇧🇷 бразильская → 🇺🇿 узбекская → 🇸🇦 арабская → 🇷🇺 русская → снова 🇬🇧 …
     (полный цикл = 6 постов = 2 дня);
   - цитата даётся **в оригинале** на языке своей культуры + **переводы на остальные 5 языков**:
     британский английский (en-GB), испанский (es-ES), бразильский португальский (pt-BR),
     узбекский (uz, **латиница**), арабский (литературный, MSA), русский (ru).
3. **Discord отключён** (ни вызова, ни секрета в workflow).
4. Цитаты **не повторяются**, пока не исчерпан корпус.

### Соответствие «культура → язык оригинала → примеры авторов»

| Ключ | Флаг | Язык оригинала | Примеры авторов (ориентир, не исчерпывающий) |
|---|---|---|---|
| `en_gb` | 🇬🇧 | British English | Shakespeare, Churchill, Samuel Johnson, Jane Austen, Orwell, C. S. Lewis, Darwin |
| `es` | 🇪🇸 | Español (España) | Cervantes, Unamuno, Ortega y Gasset, Antonio Machado, García Lorca, Calderón |
| `pt_br` | 🇧🇷 | Português (Brasil) | Machado de Assis, Clarice Lispector, Paulo Freire, Carlos Drummond de Andrade, Jorge Amado |
| `uz` | 🇺🇿 | O‘zbek (lotin) | Alisher Navoiy, Zahiriddin Bobur, Abdulla Qodiriy, Cho‘lpon, Abdulla Oripov, Erkin Vohidov |
| `ar` | 🇸🇦 | العربية الفصحى | المتنبي، ابن خلدون، جبران خليل جبران، نجيب محفوظ، محمود درويش، طه حسين |
| `ru` | 🇷🇺 | Русский | Толстой, Достоевский, Чехов, Пушкин, Ахматова, Лихачёв |

Правила принадлежности к культуре: автор писал на этом языке и связан с этой страной/регионом.
Для `en_gb` — только британские авторы (не американцы; ирландцев вроде Уайльда/Шоу не брать — спорно).
Для `es` — авторы Испании (латиноамериканских не брать, чтобы «испанская культура» не размывалась;
при желании позже расширить до всего испаноязычного мира).
Для `uz` классиков, писавших на чагатайском (Навои, Бабур), приводить в общепринятом **современном узбекском** написании.

## 2. Ключевые архитектурные решения

### 2.1. Цитаты берутся из проверенного корпуса, а не придумываются LLM на лету

LLM регулярно выдумывает цитаты и приписывает их не тем людям — особенно для узбекской
и арабской культур. Поэтому:

- В репозитории хранится **корпус** `data/quotes/<culture>.json` — цитаты в оригинале с автором и источником.
- LLM используется **только для перевода** на остальные 5 языков.
- Корпус один раз генерируется агентом (черновик) и **проверяется человеком** перед запуском.

Объём: при 3 постах в день каждая культура выходит раз в 2 дня → **60 цитат на культуру ≈ 4 месяца**
без повторов. Стартовый минимум — 30 на культуру (2 месяца), дальше пополнять.

Формат записи:

```json
{
  "id": "ru-tolstoy-001",
  "text": "Все счастливые семьи похожи друг на друга, каждая несчастливая семья несчастлива по-своему.",
  "author": "Лев Толстой",
  "author_en": "Leo Tolstoy",
  "source": "«Анна Каренина», 1877"
}
```

### 2.2. Состояние ротации хранится в репозитории

Файл `data/state.json`:

```json
{
  "next_culture_index": 0,
  "posted_ids": ["ru-tolstoy-001", "..."]
}
```

- Порядок культур — константа `CULTURE_ORDER = ["en_gb", "es", "pt_br", "uz", "ar", "ru"]`.
- После **успешной** отправки в Telegram: `next_culture_index = (i + 1) % 6`, id цитаты добавляется в `posted_ids`.
- Если отправка не удалась — состояние **не меняется** (следующий запуск повторит ту же культуру).
- Workflow коммитит `data/state.json` обратно в репозиторий (`[skip ci]` в сообщении коммита).
- Побочный плюс: регулярные коммиты не дают GitHub **автоматически отключить** cron-workflow
  после 60 дней неактивности репозитория (вероятная причина того, что бот «остановился»).

Выбор цитаты: случайная из `quotes/<culture>.json`, чей `id` не в `posted_ids`.
Если все цитаты культуры использованы — удалить id этой культуры из `posted_ids`
(начать круг заново) и написать предупреждение в лог.

### 2.3. Discord — отключить полностью

Удалить `send_discord()` и её вызов, убрать `DISCORD_WEBHOOK_URL` из workflow.
Код остаётся в истории git; вернуть при необходимости отдельной задачей.

## 3. Этапы работы

### Этап 1. Корпус цитат

1. Создать `data/quotes/{en_gb,es,pt_br,uz,ar,ru}.json` по формату из 2.1.
2. Требования к каждой цитате:
   - ≥ 5 слов в `text` (проверяется тестом, см. этап 5);
   - широко известная, с атрибуцией, которую можно подтвердить; поле `source` заполнено
     (произведение/речь/письмо, год); сомнительные и «интернет-цитаты» — не включать;
   - без политически острых и религиозно-спорных высказываний;
   - длина `text` ≤ 300 символов (иначе пост не влезет в лимиты Telegram, см. 3.4);
   - уникальный `id` вида `<culture>-<author-slug>-<NNN>`.
3. Минимум 30 цитат на культуру.
4. **Остановиться и попросить владельца проверить корпус** перед этапом 6 (запуск).

### Этап 2. Переписать `main.py`

Удалить: `get_word_data()`, `send_discord()`, логику `{{…}}` → `<b>`, Maori (`mi`).

Добавить (можно в одном файле, функции маленькие и тестируемые):

```text
CULTURES = {
  "en_gb": {"flag": "🇬🇧", "lang_name": "British English",      "title": "British wisdom",       "voice": "en-GB-RyanNeural"},
  "es":    {"flag": "🇪🇸", "lang_name": "Spanish (Spain)",      "title": "Sabiduría española",   "voice": "es-ES-AlvaroNeural"},
  "pt_br": {"flag": "🇧🇷", "lang_name": "Brazilian Portuguese", "title": "Sabedoria brasileira", "voice": "pt-BR-AntonioNeural"},
  "uz":    {"flag": "🇺🇿", "lang_name": "Uzbek (Latin script)", "title": "O‘zbek donoligi",      "voice": "uz-UZ-SardorNeural"},
  "ar":    {"flag": "🇸🇦", "lang_name": "Modern Standard Arabic","title": "حكمة عربية",          "voice": "ar-SA-HamedNeural"},
  "ru":    {"flag": "🇷🇺", "lang_name": "Russian",              "title": "Русская мудрость",     "voice": "ru-RU-DmitryNeural"},
}
CULTURE_ORDER = ["en_gb", "es", "pt_br", "uz", "ar", "ru"]
```

Функции:

- `load_state()` / `save_state(state)` — чтение/запись `data/state.json` (если файла нет — дефолт).
- `load_quotes(culture)` — чтение корпуса.
- `pick_quote(culture, state)` — выбор неиспользованной цитаты (логика из 2.2).
- `count_words(text)` — подсчёт слов по пробелам; `pick_quote` пропускает цитаты < 5 слов.
- `translate_quote(quote, culture)` — один запрос в OpenRouter:
  - переводит `text` на все языки из `CULTURES`, **кроме** языка оригинала;
  - промпт: «литературный перевод, сохранить смысл и стиль; если есть устоявшийся
    опубликованный перевод — использовать его; en-GB — британская орфография
    (colour, realise); pt-BR — бразильская норма; uz — латиница с o‘/g‘;
    ar — фусха; ответ строго JSON `{"<lang_key>": "<translation>", ...}`»;
  - `temperature` ≈ 0.3 (нужна точность, а не креативность);
  - по возможности `response_format: {"type": "json_object"}`; оставить `clean_json_response()` как страховку;
  - `timeout=60` у `requests.post`;
  - валидация: все 5 ключей есть, строки непустые; при ошибке — до 2 повторов, затем исключение.
- `generate_audio(text, voice, filename)` — оставить существующую, добавить параметр `voice`;
  озвучивать **оригинал** цитаты голосом её культуры.
- `format_message(quote, culture, translations)` — HTML для Telegram (см. 3.4).
- `send_telegram(message, audio_path)` — см. 3.4.
- `main()`:
  1. `state = load_state()`; `culture = CULTURE_ORDER[state["next_culture_index"]]`;
  2. `quote = pick_quote(...)`; `translations = translate_quote(...)`;
  3. `audio = generate_audio(...)` (ошибка аудио не фатальна);
  4. если `DRY_RUN=1` — напечатать сообщение и выйти **без** отправки и без изменения state;
  5. `send_telegram(...)`; только при успехе — обновить и сохранить state;
  6. при любой фатальной ошибке — `sys.exit(1)`, чтобы запуск в Actions стал красным
     (сейчас ошибки только печатаются и job «зелёный»).

Конфигурация через env: `OPENROUTER_API_KEY`, `TG_BOT_TOKEN`, `TG_CHAT_ID`,
`OPENROUTER_MODEL` (по умолчанию — текущая модель; проверить, что она ещё доступна
в OpenRouter, иначе выбрать актуальную модель с хорошим качеством для узбекского и арабского),
`DRY_RUN`.

### Этап 3. Формат поста в Telegram

```html
🇷🇺 <b>Русская мудрость</b>

<blockquote>Все счастливые семьи похожи друг на друга, каждая несчастливая семья несчастлива по-своему.</blockquote>
— <b>Лев Толстой</b> (Leo Tolstoy)
<i>«Анна Каренина», 1877</i>
━━━━━━━━━━━━━━━━━━
🇬🇧 All happy families are alike; each unhappy family is unhappy in its own way.
🇪🇸 Todas las familias felices se parecen…
🇧🇷 Todas as famílias felizes se parecem…
🇺🇿 Barcha baxtli oilalar bir-biriga o‘xshaydi…
🇸🇦 ‏كل العائلات السعيدة متشابهة…‏
```

Требования:
- весь пользовательский текст — через `html.escape()`;
- арабские строки оборачивать в RLM `‏…‏` (как сейчас);
- если оригинал арабский — заголовок и цитату тоже оборачивать в RLM;
- порядок строк переводов — по `CULTURE_ORDER`, пропуская язык оригинала.

### Этап 4. Отправка и лимиты Telegram

- Подпись к аудио (`caption`) ограничена **1024 символами**, обычное сообщение — **4096**.
  Шесть языков легко превышают 1024.
- Алгоритм `send_telegram`:
  1. если аудио есть и `len(message) <= 1024` → `sendAudio` с caption;
  2. иначе → `sendMessage` с текстом, затем (если аудио есть) `sendAudio` без caption
     с `reply_to_message_id` на отправленное сообщение;
  3. проверять `raise_for_status()` и `response.json()["ok"]`; `timeout=30`;
  4. возвращать `True/False` — от этого зависит сохранение state.
- Длину считать по тексту без HTML-тегов (Telegram считает видимые символы), но
  для простоты допустимо считать с тегами — это только строже.

### Этап 5. Тесты

Добавить `tests/` (pytest, без сетевых вызовов; `pytest` — в `requirements-dev.txt`):

- `test_corpus.py`: для каждого файла корпуса — валидный JSON, все поля есть,
  `id` уникальны глобально, ≥ 5 слов, ≤ 300 символов, id начинается с ключа культуры.
- `test_rotation.py`: индекс идёт 0→5→0; неудачная отправка не меняет state;
  `pick_quote` не возвращает уже опубликованные; при исчерпании — сброс по культуре.
- `test_format.py`: HTML экранируется (`<`, `&` в цитате), язык оригинала не дублируется
  в переводах, арабские строки содержат RLM.
- `translate_quote` тестировать с замоканным `requests.post` (валидный ответ, JSON в ```-обёртке,
  отсутствующий ключ → повтор → исключение).

### Этап 6. Workflow (`.github/workflows/daily.yml`)

- Переименовать в `Quotes Bot` (файл можно оставить `daily.yml` или переименовать в `quotes.yml`).
- Расписание — 3 раза в день, **не ровно в :00** (в начале часа GitHub сильно задерживает cron):

  ```yaml
  schedule:
    - cron: '7 6 * * *'    # 09:07 МСК
    - cron: '7 11 * * *'   # 14:07 МСК
    - cron: '7 17 * * *'   # 20:07 МСК
  workflow_dispatch:
    inputs:
      dry_run:
        description: 'Only print, do not post'
        type: boolean
        default: false
  ```
  (время — предложение; владелец может поменять.)
- `permissions: contents: write` (для коммита state).
- `concurrency: { group: quotes-bot, cancel-in-progress: false }` — чтобы два запуска
  не взяли одну культуру.
- `actions/checkout@v4`, `actions/setup-python@v5`, `python-version: '3.12'` (3.9 снят с поддержки).
- Шаг `pytest` перед запуском бота (корпус сломан → не публиковать).
- env: `OPENROUTER_API_KEY`, `TG_BOT_TOKEN`, `TG_CHAT_ID`, `DRY_RUN: ${{ inputs.dry_run }}`;
  **убрать** `DISCORD_WEBHOOK_URL`.
- Последний шаг — коммит состояния, только если файл изменился:

  ```yaml
  - name: Commit state
    if: success()
    run: |
      git config user.name "quotes-bot"
      git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
      git add data/state.json
      git diff --cached --quiet || git commit -m "chore: update quote rotation state [skip ci]"
      git push
  ```

### Этап 7. Уборка

- `requirements.txt`: `requests`, `edge-tts` (закрепить версии).
- Добавить `.gitignore` (`*.mp3`, `__pycache__/`, `.pytest_cache/`).
- `README.md`: что делает бот, секреты, как добавить цитаты, как запустить `DRY_RUN=1 python main.py` локально.
- Удалить секрет `DISCORD_WEBHOOK_URL` в настройках репозитория — **делает владелец вручную**.

## 4. Критерии готовности

- [ ] `pytest` зелёный локально и в Actions.
- [ ] `DRY_RUN=1 python main.py` печатает корректный пост для каждой из 6 культур
      (прогнать 6 раз, подставляя индекс) — без отправки и без изменения state.
- [ ] Ручной запуск workflow (`workflow_dispatch`, dry_run=false) публикует пост в Telegram,
      а в репозитории появляется коммит с обновлённым `data/state.json`.
- [ ] Следующий запуск берёт следующую культуру.
- [ ] В коде и workflow нет упоминаний Discord.
- [ ] Владелец проверил корпус цитат.

## 5. Действия владельца (агент не может сделать сам)

1. Проверить корпус цитат (атрибуцию и формулировки оригиналов).
2. Убедиться, что секреты `OPENROUTER_API_KEY`, `TG_BOT_TOKEN`, `TG_CHAT_ID` актуальны
   (токены могли истечь за время простоя), удалить `DISCORD_WEBHOOK_URL`.
3. Во вкладке **Actions** репозитория проверить, что workflow **включён**
   (GitHub отключает cron после 60 дней без активности — кнопка «Enable workflow»).
4. Settings → Actions → General → Workflow permissions: разрешить **Read and write**
   (нужно для коммита `state.json`), если это не разрешено через `permissions:` в workflow.
