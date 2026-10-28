import logging
import os

import pytest

import chat_assistant as ca


ALLOWED_CHAT = -1001234567890
CHANNEL_CHAT = -1009999999999
OTHER_CHAT = -1005555555555
BOT_ID = 777000111
BOT_USERNAME = "polyglotatelbot"


class FakeTelegramClient:
    def __init__(self, voice_bytes=b"OggS-fake-voice-data"):
        self.sent_messages = []
        self.downloaded_files = []
        self.created_temp_files = []
        self.voice_bytes = voice_bytes
        self.fail_download = False

    def send_text_reply(self, chat_id, text, reply_to_message_id=None):
        chunks = ca.split_telegram_message(text)
        results = []
        for idx, chunk in enumerate(chunks, start=1):
            msg = {
                "message_id": 9000 + len(self.sent_messages) + idx,
                "chat": {"id": chat_id},
                "text": chunk,
                "reply_to_message_id": reply_to_message_id,
            }
            self.sent_messages.append(msg)
            results.append(msg)
        return results

    def download_voice_to_tempfile(self, file_id, max_bytes=ca.DEFAULT_MAX_VOICE_SIZE_BYTES):
        if self.fail_download:
            raise ca.ServiceUnavailableError("Download failed")
        if len(self.voice_bytes) > max_bytes:
            raise ca.AudioLimitExceededError("Файл голосового сообщения слишком большой.")
        import tempfile
        fd, path = tempfile.mkstemp(prefix="test_voice_", suffix=".ogg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(self.voice_bytes)
        self.downloaded_files.append(file_id)
        self.created_temp_files.append(path)
        return path


class FakeArgosClient:
    def __init__(self):
        self.calls = []
        self.should_fail = False

    def translate(self, text, source_lang, target_lang):
        if self.should_fail:
            raise ca.ServiceUnavailableError("Argos timeout")
        self.calls.append((text, source_lang, target_lang))
        return f"[{source_lang}->{target_lang}] {text}"

    def translate_to_pair(self, text, source_lang):
        targets = ca.TARGET_LANGS_FOR_SOURCE[source_lang]
        return {t: self.translate(text, source_lang, t) for t in targets}


class FakeWhisperClient:
    def __init__(self, transcript="Hola amigos, ¿cómo están hoy?"):
        self.transcript = transcript
        self.calls = []
        self.should_fail = False
        self.file_existed_during_call = []

    def transcribe(self, audio_path):
        self.calls.append(audio_path)
        self.file_existed_during_call.append(os.path.exists(audio_path))
        if self.should_fail:
            raise ca.ServiceUnavailableError("Whisper unavailable")
        return self.transcript


def make_assistant(**config_overrides):
    cfg_kwargs = {
        "bot_token": "123456:SECRET_TEST_TOKEN_VALUE",
        "bot_username": BOT_USERNAME,
        "allowed_chat_ids": {ALLOWED_CHAT},
        "channel_chat_id": str(CHANNEL_CHAT),
    }
    cfg_kwargs.update(config_overrides)
    config = ca.AssistantConfig(**cfg_kwargs)
    tg = FakeTelegramClient()
    argos = FakeArgosClient()
    whisper = FakeWhisperClient()
    assistant = ca.StudyChatAssistant(
        config=config,
        tg_client=tg,
        argos_client=argos,
        whisper_client=whisper,
        bot_id=BOT_ID,
    )
    return assistant, tg, argos, whisper


def make_update(
    update_id=1,
    chat_id=ALLOWED_CHAT,
    message_id=100,
    user_id=42,
    is_bot=False,
    text=None,
    voice=None,
    reply_to_message=None,
):
    msg = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "User"},
    }
    if text is not None:
        msg["text"] = text
    if voice is not None:
        msg["voice"] = voice
    if reply_to_message is not None:
        msg["reply_to_message"] = reply_to_message
    return {"update_id": update_id, "message": msg}


# --- 1. МАРШРУТИЗАЦИЯ И ИЗОЛЯЦИЯ ЧАТОВ ---
def test_ignores_messages_outside_allowed_chat():
    assistant, tg, argos, whisper = make_assistant()
    upd = make_update(chat_id=OTHER_CHAT, text=f"@{BOT_USERNAME} Hola amigos")
    assert assistant.handle_update(upd) is None
    assert tg.sent_messages == []
    assert argos.calls == []
    assert whisper.calls == []


def test_channel_chat_is_never_allowed_even_if_configured():
    assistant, tg, argos, _ = make_assistant(
        allowed_chat_ids={ALLOWED_CHAT, CHANNEL_CHAT},
        channel_chat_id=str(CHANNEL_CHAT),
    )
    assert not assistant.is_allowed_chat(CHANNEL_CHAT)
    upd = make_update(chat_id=CHANNEL_CHAT, text=f"@{BOT_USERNAME} Hola amigos")
    assert assistant.handle_update(upd) is None
    assert tg.sent_messages == []
    assert argos.calls == []


def test_ignores_regular_chat_messages_without_mention_or_command():
    assistant, tg, argos, _ = make_assistant()
    upd = make_update(text="Просто общаемся в чате на испанском: ¡Buenos días!")
    assert assistant.handle_update(upd) is None
    assert tg.sent_messages == []
    assert argos.calls == []


def test_ignores_messages_from_bots():
    assistant, tg, argos, whisper = make_assistant()
    upd = make_update(is_bot=True, voice={"file_id": "v1", "duration": 5, "file_size": 1000})
    assert assistant.handle_update(upd) is None
    assert tg.sent_messages == []
    assert whisper.calls == []
    assert argos.calls == []


def test_reply_to_bot_message_ignored_without_explicit_command():
    assistant, tg, argos, _ = make_assistant()
    bot_msg = {
        "message_id": 50,
        "from": {"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME},
        "text": "🎙 Расшифровка:\nHola amigos, ¿cómo están?",
    }
    # Обычный ответ на сообщение бота (без команды)
    upd1 = make_update(update_id=1, text="Спасибо большое!", reply_to_message=bot_msg)
    assert assistant.handle_update(upd1) is None

    # Пустое упоминание в ответ на сообщение самого бота без команды "переведи" / "/translate"
    upd2 = make_update(update_id=2, text=f"@{BOT_USERNAME}", reply_to_message=bot_msg)
    assert assistant.handle_update(upd2) is None
    assert tg.sent_messages == []
    assert argos.calls == []


def test_reply_to_bot_message_translates_when_explicit_command_given():
    assistant, tg, argos, _ = make_assistant()
    bot_msg = {
        "message_id": 50,
        "from": {"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME},
        "text": "🎙 Расшифровка:\nHola amigos, ¿cómo están?",
    }
    upd = make_update(
        update_id=3,
        text=f"@{BOT_USERNAME} переведи",
        reply_to_message=bot_msg,
    )
    status = assistant.handle_update(upd)
    assert status == "translated_es"
    assert len(tg.sent_messages) == 1
    assert tg.sent_messages[0]["reply_to_message_id"] == 50
    assert "[es->ru] Hola amigos, ¿cómo están?" in tg.sent_messages[0]["text"]
    assert "[es->en] Hola amigos, ¿cómo están?" in tg.sent_messages[0]["text"]


# --- 2. ШЕСТЬ НАПРАВЛЕНИЙ ПЕРЕВОДА (RU<->EN, RU<->ES, EN<->ES) ---
def test_six_translation_directions_via_mention():
    assistant, tg, argos, _ = make_assistant()

    # 1 & 2: RU -> EN, RU -> ES
    res_ru = assistant.handle_update(
        make_update(update_id=10, message_id=101, text=f"@{BOT_USERNAME} Привет, мы учим испанский язык каждый день.")
    )
    assert res_ru == "translated_ru"
    msg_ru = tg.sent_messages[-1]["text"]
    assert "🇬🇧 Английский:" in msg_ru
    assert "🇪🇸 Испанский:" in msg_ru
    assert "[ru->en] Привет, мы учим испанский язык каждый день." in msg_ru
    assert "[ru->es] Привет, мы учим испанский язык каждый день." in msg_ru

    # 3 & 4: EN -> RU, EN -> ES
    res_en = assistant.handle_update(
        make_update(update_id=11, message_id=102, text=f"@{BOT_USERNAME} Hello everyone, how are you doing today?")
    )
    assert res_en == "translated_en"
    msg_en = tg.sent_messages[-1]["text"]
    assert "🇷🇺 Русский:" in msg_en
    assert "🇪🇸 Испанский:" in msg_en
    assert "[en->ru] Hello everyone, how are you doing today?" in msg_en
    assert "[en->es] Hello everyone, how are you doing today?" in msg_en

    # 5 & 6: ES -> RU, ES -> EN
    res_es = assistant.handle_update(
        make_update(update_id=12, message_id=103, text=f"@{BOT_USERNAME} ¡Hola amigos! Necesito practicar español hoy.")
    )
    assert res_es == "translated_es"
    msg_es = tg.sent_messages[-1]["text"]
    assert "🇷🇺 Русский:" in msg_es
    assert "🇬🇧 Английский:" in msg_es
    assert "[es->ru] ¡Hola amigos! Necesito practicar español hoy." in msg_es
    assert "[es->en] ¡Hola amigos! Necesito practicar español hoy." in msg_es

    directions = {(src, tgt) for _, src, tgt in argos.calls}
    assert directions == {
        ("ru", "en"),
        ("ru", "es"),
        ("en", "ru"),
        ("en", "es"),
        ("es", "ru"),
        ("es", "en"),
    }


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_argos_http_client_all_six_directions_in_one_request_per_source():
    recorded = []

    class DummySession:
        def post(self, url, json=None, headers=None, timeout=None):
            recorded.append((url, json, headers, timeout))
            out = {t: f"out({json['source']}->{t}):{json['text']}" for t in json["targets"]}
            return _Resp(payload={"source": json["source"], "translations": out})

    client = ca.ArgosClient("http://127.0.0.1:8770", timeout=12.0, api_key="k1", session=DummySession())
    directions = set()
    for src in ("ru", "en", "es"):
        pair = client.translate_to_pair("sample", src)
        assert set(pair.keys()) == set(ca.TARGET_LANGS_FOR_SOURCE[src])
        directions |= {(src, t) for t in pair}
    assert len(directions) == 6
    assert len(recorded) == 3
    assert all(r[0] == "http://127.0.0.1:8770/v1/translate" for r in recorded)
    assert all(r[2] == {"Authorization": "Bearer k1"} for r in recorded)
    assert recorded[0][1] == {"text": "sample", "source": "ru", "targets": ["en", "es"]}
    assert client.translate("hola", "es", "ru") == "out(es->ru):hola"


def test_argos_client_retries_once_on_busy_then_fails_cleanly():
    responses = [
        _Resp(429, {"error": {"code": "busy"}}, {"Retry-After": "5"}),
        _Resp(200, {"translations": {"en": "Hi", "es": "Hola"}}),
    ]
    slept = []

    class DummySession:
        def post(self, url, json=None, headers=None, timeout=None):
            return responses.pop(0)

    client = ca.ArgosClient("http://x:8770", session=DummySession(), sleep=slept.append)
    assert client.translate_to_pair("Привет", "ru") == {"en": "Hi", "es": "Hola"}
    assert slept == [5.0]

    responses[:] = [_Resp(503, {}, {"Retry-After": "60"}), _Resp(503, {}, {"Retry-After": "60"})]
    with pytest.raises(ca.ServiceUnavailableError):
        client.translate_to_pair("Привет", "ru")
    assert slept[-1] == ca.ArgosClient.MAX_RETRY_AFTER_SEC

    responses[:] = [_Resp(413, {"error": {"code": "text_too_long"}})]
    with pytest.raises(ca.ServiceUnavailableError):
        client.translate_to_pair("Привет", "ru")


def test_whisper_client_sends_bearer_token_and_language(tmp_path):
    recorded = {}

    class DummySession:
        def post(self, url, params=None, data=None, files=None, headers=None, timeout=None):
            recorded.update(url=url, data=data, headers=headers, field=list(files)[0])
            return _Resp(payload={"text": " Hola "})

    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS")
    client = ca.WhisperClient(
        "http://172.31.0.1:8765/v1/audio/transcriptions",
        api_key="stt-key",
        language="es",
        session=DummySession(),
    )
    assert client.transcribe(str(audio)) == "Hola"
    assert recorded["field"] == "file"
    assert recorded["headers"] == {"Authorization": "Bearer stt-key"}
    assert recorded["data"] == {"response_format": "json", "language": "es"}


def test_english_with_capital_i_is_detected_as_english():
    for text in ("I am late, sorry", "I think I can help you with the homework", "Can I go?"):
        res = ca.detect_language(text)
        assert (res.status, res.lang) == ("ok", "en"), text


def test_executor_dispatch_returns_queued_and_releases_slot():
    from concurrent.futures import ThreadPoolExecutor

    assistant, tg, argos, _ = make_assistant(max_concurrent_tasks=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        status = assistant.handle_update(
            make_update(update_id=900, text="/translate es Hola amigos"), executor=pool
        )
    assert status == "queued"
    assert argos.calls and tg.sent_messages
    # слот семафора освобождён после выполнения задачи в пуле
    assert assistant.handle_update(make_update(update_id=901, text="/translate es Gracias")) == "translated_es"


# --- 3. КОМАНДА /translate, ОТВЕТЫ НА СООБЩЕНИЯ И ЯВНОЕ УКАЗАНИЕ ЯЗЫКА ---
def test_translate_command_with_text_and_explicit_lang():
    assistant, tg, argos, _ = make_assistant()

    # Неоднозначное короткое слово-когнат "hotel" с явным указанием языка es
    status = assistant.handle_update(
        make_update(update_id=20, text="/translate es hotel")
    )
    assert status == "translated_es"
    assert ("hotel", "es", "ru") in argos.calls
    assert ("hotel", "es", "en") in argos.calls

    # Формат /translate lang=en doctor
    status2 = assistant.handle_update(
        make_update(update_id=21, text="/translate lang=en doctor")
    )
    assert status2 == "translated_en"
    assert ("doctor", "en", "ru") in argos.calls
    assert ("doctor", "en", "es") in argos.calls


def test_spanish_sentences_starting_with_es_or_en_not_truncated():
    assistant, tg, argos, _ = make_assistant()

    status1 = assistant.handle_update(
        make_update(update_id=25, text="/translate Es una buena idea para nosotros")
    )
    assert status1 == "translated_es"
    assert argos.calls[-1][0] == "Es una buena idea para nosotros"

    status2 = assistant.handle_update(
        make_update(update_id=26, text="/translate En la casa hay un perro grande")
    )
    assert status2 == "translated_es"
    assert argos.calls[-1][0] == "En la casa hay un perro grande"


def test_reply_translation_for_text_message_with_explicit_lang():
    assistant, tg, argos, _ = make_assistant()
    original_msg = {
        "message_id": 77,
        "from": {"id": 99, "is_bot": False},
        "text": "hotel",
    }
    upd = make_update(
        update_id=30,
        message_id=78,
        text=f"@{BOT_USERNAME} переведи с испанского",
        reply_to_message=original_msg,
    )
    status = assistant.handle_update(upd)
    assert status == "translated_es"
    assert tg.sent_messages[-1]["reply_to_message_id"] == 77
    assert ("hotel", "es", "ru") in argos.calls


# --- 4. НЕОДНОЗНАЧНЫЙ И НЕПОДДЕРЖИВАЕМЫЙ ЯЗЫК ---
def test_ambiguous_language_prompts_user_to_specify_explicitly():
    assistant, tg, argos, _ = make_assistant()
    status = assistant.handle_update(
        make_update(update_id=40, text=f"@{BOT_USERNAME} hotel")
    )
    assert status == "ambiguous_language"
    assert argos.calls == []
    reply = tg.sent_messages[-1]["text"]
    assert "укажите исходный язык явно" in reply.lower()


def test_unsupported_languages_rejected_cleanly():
    assistant, tg, argos, _ = make_assistant()

    # Французский
    s1 = assistant.handle_update(
        make_update(update_id=41, text="/translate Bonjour, comment allez-vous aujourd'hui ?")
    )
    assert s1 == "unsupported_language"

    # Арабский
    s2 = assistant.handle_update(
        make_update(update_id=42, text=f"@{BOT_USERNAME} مرحبا كيف حالك اليوم")
    )
    assert s2 == "unsupported_language"

    # Явно указанный неподдерживаемый язык
    s3 = assistant.handle_update(
        make_update(update_id=43, text="/translate fr Bonjour")
    )
    assert s3 == "unsupported_language"
    assert argos.calls == []


# --- 5. ОБРАБОТКА ГОЛОСОВЫХ И ПОВТОРНОЕ ИСПОЛЬЗОВАНИЕ РАСШИФРОВКИ ---
def test_voice_message_transcribed_and_temp_file_deleted():
    assistant, tg, argos, whisper = make_assistant()
    voice_msg = {"file_id": "voice_abc", "duration": 12, "file_size": 24000}
    upd = make_update(update_id=50, message_id=200, voice=voice_msg)

    status = assistant.handle_update(upd)
    assert status == "voice_transcribed"
    assert len(whisper.calls) == 1
    assert whisper.file_existed_during_call == [True]
    # Временный аудиофайл должен быть удалён после обработки
    for tmp in tg.created_temp_files:
        assert not os.path.exists(tmp)

    # Расшифровка опубликована ответом на исходное голосовое, без перевода
    assert argos.calls == []
    assert len(tg.sent_messages) == 1
    assert tg.sent_messages[0]["reply_to_message_id"] == 200
    assert "Hola amigos, ¿cómo están hoy?" in tg.sent_messages[0]["text"]


def test_reply_to_voice_uses_saved_transcription_without_re_calling_whisper():
    assistant, tg, argos, whisper = make_assistant()
    voice_data = {"file_id": "voice_cached", "duration": 8, "file_size": 15000}

    # Шаг 1: Приходит голосовое -> расшифровывается и кэшируется
    assistant.handle_update(make_update(update_id=60, message_id=300, voice=voice_data))
    assert len(whisper.calls) == 1

    # Шаг 2: Участник отвечает «@polyglotatelbot переведи» на это голосовое
    reply_upd = make_update(
        update_id=61,
        message_id=301,
        text=f"@{BOT_USERNAME} переведи",
        reply_to_message={
            "message_id": 300,
            "from": {"id": 42, "is_bot": False},
            "voice": voice_data,
        },
    )
    status = assistant.handle_update(reply_upd)
    assert status == "translated_es"
    # Whisper повторно НЕ вызывался!
    assert len(whisper.calls) == 1
    assert len(argos.calls) == 2
    assert tg.sent_messages[-1]["reply_to_message_id"] == 300
    assert "[es->ru] Hola amigos, ¿cómo están hoy?" in tg.sent_messages[-1]["text"]


def test_voice_duration_and_size_limits_enforced():
    assistant, tg, _, whisper = make_assistant(
        max_voice_duration_sec=60,
        max_voice_size_bytes=50_000,
    )
    # Превышена длительность
    s1 = assistant.handle_update(
        make_update(update_id=70, message_id=400, voice={"file_id": "v_long", "duration": 120, "file_size": 10_000})
    )
    assert s1 == "voice_limit_exceeded"
    assert whisper.calls == []

    # Превышен размер файла
    s2 = assistant.handle_update(
        make_update(update_id=71, message_id=401, voice={"file_id": "v_big", "duration": 10, "file_size": 100_000})
    )
    assert s2 == "voice_limit_exceeded"
    assert whisper.calls == []


# --- 6. ПОВТОРНЫЕ ОБНОВЛЕНИЯ, ОШИБКИ СЕРВИСОВ, ЛИМИТЫ И БЕЗОПАСНОСТЬ ЛОГОВ ---
def test_duplicate_updates_processed_only_once():
    assistant, tg, argos, _ = make_assistant()
    upd = make_update(update_id=888, message_id=500, text=f"@{BOT_USERNAME} Hola amigos")

    first = assistant.handle_update(upd)
    second = assistant.handle_update(upd)

    assert first == "translated_es"
    assert second == "duplicate"
    assert len(tg.sent_messages) == 1
    assert len(argos.calls) == 2


def test_whisper_and_argos_service_errors_handled_gracefully_and_temp_cleaned():
    assistant, tg, argos, whisper = make_assistant()

    # Ошибка Whisper
    whisper.should_fail = True
    s_voice = assistant.handle_update(
        make_update(update_id=90, message_id=600, voice={"file_id": "v_err", "duration": 5, "file_size": 1000})
    )
    assert s_voice == "whisper_error"
    assert "временно недоступен" in tg.sent_messages[-1]["text"]
    for tmp in tg.created_temp_files:
        assert not os.path.exists(tmp)

    # Ошибка Argos
    argos.should_fail = True
    s_tr = assistant.handle_update(
        make_update(update_id=91, message_id=601, text=f"@{BOT_USERNAME} Hola amigos")
    )
    assert s_tr == "argos_error"
    assert "временно недоступен" in tg.sent_messages[-1]["text"]


def test_long_telegram_message_splitting():
    long_text = "\n\n".join(f"Абзац номер {i}: " + ("слово " * 80) for i in range(20))
    assert len(long_text) > ca.TELEGRAM_MESSAGE_LIMIT
    chunks = ca.split_telegram_message(long_text)
    assert len(chunks) >= 2
    assert all(len(c) <= ca.TELEGRAM_MESSAGE_LIMIT for c in chunks)


def test_rate_limiter_blocks_flood_from_single_user():
    assistant, tg, argos, _ = make_assistant(rate_limit_requests=2, rate_limit_window_sec=60.0)
    assert assistant.handle_update(make_update(update_id=101, user_id=55, text="/translate es Hola")) == "translated_es"
    assert assistant.handle_update(make_update(update_id=102, user_id=55, text="/translate es Adiós")) == "translated_es"

    # 3-й и 4-й запросы блокируются, предупреждение отправляется только один раз за окно
    assert assistant.handle_update(make_update(update_id=103, user_id=55, text="/translate es Gracias")) == "rate_limited"
    assert assistant.handle_update(make_update(update_id=104, user_id=55, text="/translate es Por favor")) == "rate_limited"
    assert len(tg.sent_messages) == 3
    assert "Слишком много запросов" in tg.sent_messages[-1]["text"]


def test_logs_never_contain_message_content_or_secret_token(caplog):
    assistant, _, argos, _ = make_assistant()
    argos.should_fail = True
    secret_phrase = "СЕКРЕТНЫЙ_ТЕКСТ_ПЕРЕПИСКИ_УЧАСТНИКА"

    with caplog.at_level(logging.INFO):
        assistant.handle_update(
            make_update(update_id=201, message_id=700, text=f"@{BOT_USERNAME} {secret_phrase}")
        )

    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_phrase not in full_log
    assert "SECRET_TEST_TOKEN_VALUE" not in full_log
