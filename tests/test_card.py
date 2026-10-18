import os
from PIL import Image

import card
import main


def test_generate_card_creates_image(tmp_path):
    quote = {
        "id": "en_gb-test-001",
        "country": "GB",
        "text": "All the world's a stage, and all the men and women merely players.",
        "author": "William Shakespeare",
        "author_en": "William Shakespeare",
        "source": "As You Like It, 1599",
    }
    lesson = {
        "meaning_en": "only or merely",
        "languages": {
            "en_gb": {"word": "merely", "ipa": "/ˈmɪəli/", "text": "All the world's a stage {{merely}}"},
        },
    }
    out_file = str(tmp_path / "card.png")
    result = card.generate_card(quote, "en_gb", lesson, output_path=out_file)

    assert result == out_file
    assert os.path.exists(out_file)

    with Image.open(out_file) as im:
        assert im.size == (1080, 1350)
        assert im.format == "PNG"


def test_generate_card_arabic(tmp_path):
    quote = {
        "id": "ar-test-001",
        "country": "EG",
        "text": "إذا غامَرْتَ في شَرَفٍ مَرُومِ فَلا تَقنَعْ بما دُونَ النّجومِ",
        "author": "المتنبي",
        "author_en": "Al-Mutanabbi",
        "source": "ديوان المتنبي",
    }
    out_file = str(tmp_path / "card_ar.png")
    result = card.generate_card(quote, "ar", output_path=out_file)
    assert os.path.exists(result)


def test_add_audio_metadata(tmp_path):
    from mutagen.id3 import ID3
    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 500)

    quote = {
        "text": "To be or not to be.",
        "author": "William Shakespeare",
        "source": "Hamlet",
    }
    main.add_audio_metadata(str(mp3_file), quote, "en_gb")

    tags = ID3(str(mp3_file))
    assert str(tags.get("TIT2")) == "Hamlet"
    assert str(tags.get("TPE1")) == "William Shakespeare"
    assert "Polyglot Wisdom" in str(tags.get("TALB"))


def test_send_telegram_voice_and_card(monkeypatch, tmp_path):
    calls = []

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "result": {"message_id": 42}}

    def fake_post(url, *args, **kwargs):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, kwargs))
        return FakeResp()

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main, "TG_BOT_TOKEN", "dummy_token")
    monkeypatch.setattr(main, "TG_CHAT_ID", "12345")
    monkeypatch.setattr(main, "SEND_AS_VOICE", True)

    card_file = tmp_path / "c.png"
    card_file.write_bytes(b"dummy_png")
    audio_file = tmp_path / "a.mp3"
    audio_file.write_bytes(b"dummy_mp3")

    ok = main.send_telegram("Hello post", audio_path=str(audio_file), card_path=str(card_file))
    assert ok is True
    methods = [c[0] for c in calls]
    assert "sendPhoto" in methods
    assert "sendVoice" in methods


def test_send_telegram_video_single_message(monkeypatch, tmp_path):
    calls = []

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "result": {"message_id": 99}}

    def fake_post(url, *args, **kwargs):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, kwargs))
        return FakeResp()

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main, "TG_BOT_TOKEN", "dummy_token")
    monkeypatch.setattr(main, "TG_CHAT_ID", "12345")

    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"dummy_mp4")
    card_file = tmp_path / "c.png"
    card_file.write_bytes(b"dummy_png")
    audio_file = tmp_path / "a.mp3"
    audio_file.write_bytes(b"dummy_mp3")

    ok = main.send_telegram(
        "<b>Hello</b> post",
        audio_path=str(audio_file),
        card_path=str(card_file),
        video_path=str(video_file),
    )
    assert ok is True
    assert len(calls) == 1
    assert calls[0][0] == "sendVideo"
    assert calls[0][1]["data"]["caption"] == "<b>Hello</b> post"


def test_visible_length():
    assert main.visible_length("<b>Tom &amp; Jerry</b>") == len("Tom & Jerry")


def test_compact_for_caption():
    msg = (
        "🇬🇧 <b>British wisdom</b>\n\n"
        "<blockquote>A long quote here that is already on the card</blockquote>\n"
        "— <b>Author</b>\n"
        "📖 <i>Context line</i>\n\n"
        "✨ <b>word</b> — meaning\n"
        "🌉 <i>Bridge line</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🇷🇺 перевод"
    )
    full_len = main.visible_length(msg)
    compacted = main.compact_for_caption(msg, limit=full_len - 25)
    assert "<blockquote>" not in compacted
    assert "📖" in compacted
    assert "🌉" in compacted
    assert main.visible_length(compacted) <= full_len - 25



def test_generate_video_card_real_ffmpeg(tmp_path):
    import subprocess
    quote = {
        "id": "en_gb-test-001",
        "country": "GB",
        "text": "All the world's a stage, and all the men and women merely players.",
        "author": "William Shakespeare",
        "author_en": "William Shakespeare",
        "source": "As You Like It, 1599",
    }
    c_path = str(tmp_path / "card.png")
    a_path = str(tmp_path / "audio.mp3")
    v_path = str(tmp_path / "video.mp4")

    card.generate_card(quote, "en_gb", output_path=c_path)
    ffmpeg_exe = main.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg_exe, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1", "-q:a", "9", a_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    out = main.generate_video_card(c_path, a_path, output_path=v_path)
    assert out == v_path
    assert os.path.exists(v_path)
    assert os.path.getsize(v_path) > 1000
