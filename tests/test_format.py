import main

LESSON = {
    "meaning_en": "a meaning",
    "languages": {k: {"word": f"w-{k}", "ipa": f"/i-{k}/", "text": f"tr-{k} {{{{w-{k}}}}}"} for k in main.CULTURE_ORDER},
}
TR = LESSON


def _quote(**kw):
    q = {"id": "x", "country": "GB", "text": "Tom & Jerry <b>five words here</b>", "author": "A. Author",
         "author_en": "A. Author", "source": "Book"}
    q.update(kw)
    return q


def test_html_is_escaped():
    lesson = {"meaning_en": "m", "languages": {k: dict(v) for k, v in LESSON["languages"].items()}}
    lesson["languages"]["en_gb"]["text"] = "Tom & Jerry <b>five words here</b>"
    msg = main.format_message(_quote(), "en_gb", lesson)
    assert "Tom &amp; Jerry &lt;b&gt;" in msg
    assert "<b>five" not in msg


def test_original_language_not_duplicated():
    msg = main.format_message(_quote(), "en_gb", TR)
    assert "🇬🇧 <code>" not in msg  # нет строки перевода на язык оригинала
    for key in main.CULTURE_ORDER[1:]:
        assert f"tr-{key}" in msg


def test_arabic_lines_have_rlm():
    msg = main.format_message(_quote(), "ru", TR)
    assert "‏tr-ar <b>w-ar</b>‏" in msg


def test_arabic_original_wrapped():
    msg = main.format_message(_quote(country="EG", text="نص عربي من خمس كلمات"), "ar", TR)
    assert "<blockquote>‏" in msg


def test_arabic_word_line_forced_ltr():
    msg = main.format_message(_quote(country="EG", text="نص عربي من خمس كلمات"), "ar", TR)
    assert "\u200e✨ <b>w-ar</b>" in msg
    assert "\u200e━" in msg


def test_country_flags():
    assert main.country_flag("ES") == "🇪🇸"
    assert main.country_flag("IE") == "🇮🇪"
    assert main.country_flag("GB-SCT").startswith("\U0001F3F4")
    assert main.country_flag("GB-WLS").endswith(chr(0xE007F))
    assert main.country_flag("") == ""


def test_header_uses_quote_country():
    msg = main.format_message(_quote(country="MX"), "es", TR)
    assert msg.startswith("🇲🇽")


def test_author_en_shown_only_if_different():
    msg = main.format_message(_quote(author="Лев", author_en="Leo"), "ru", TR)
    assert "<b>Лев</b> (Leo)" in msg
    assert "(A. Author)" not in main.format_message(_quote(), "en_gb", TR)


def test_word_bold_with_ipa_and_meaning():
    msg = main.format_message(_quote(), "en_gb", LESSON)
    assert "✨ <b>w-en_gb</b> <code>/i-en_gb/</code> — a meaning" in msg
    assert "<blockquote>tr-en_gb <b>w-en_gb</b></blockquote>" in msg
    assert "🇪🇸 <code>/i-es/</code> tr-es <b>w-es</b>" in msg


def test_missing_ipa_is_omitted():
    lesson = {"meaning_en": "m", "languages": {k: dict(v, ipa="") for k, v in LESSON["languages"].items()}}
    msg = main.format_message(_quote(), "en_gb", lesson)
    assert "<code>" not in msg
