from datetime import datetime, timezone

import slot


def utc(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=timezone.utc)


def test_current_slot_boundaries():
    assert slot.current_slot(utc(10, 5, 0)) == "2026-10-10T10"
    assert slot.current_slot(utc(10, 11, 0)) == "2026-10-10T16"
    assert slot.current_slot(utc(10, 17, 0)) == "2026-10-10T22"


def test_before_first_slot_uses_previous_evening():
    assert slot.current_slot(utc(10, 4, 59)) == "2026-10-09T22"  # 09:59 Ташкент
    assert slot.current_slot(utc(10, 19, 30)) == "2026-10-10T22"  # 00:30 Ташкент 11-го


def test_is_due():
    assert slot.is_due(None, utc(10, 5))
    assert not slot.is_due("2026-10-10T10", utc(10, 8))
    assert slot.is_due("2026-10-10T10", utc(10, 11))


def test_words_missing_config():
    assert slot.words_missing_config({}) == ["WORDS_TG_CHAT_ID", "WORDBANK_URL", "WORDBANK_TOKEN"]
    assert slot.words_missing_config({"WORDS_TG_CHAT_ID": "", "WORDBANK_URL": "u", "WORDBANK_TOKEN": "t"}) == ["WORDS_TG_CHAT_ID"]
    assert slot.words_missing_config({"WORDS_TG_CHAT_ID": "-1", "WORDBANK_URL": "u", "WORDBANK_TOKEN": "t"}) == []


def test_words_slot_skipped_without_channel(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state.json"
    state.write_text('{"last_slot": "x"}')
    out = tmp_path / "out"
    monkeypatch.setattr(slot, "STATE_PATH", str(state))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("FORCE_WORDS", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    for k in slot.WORDS_REQUIRED_ENV:
        monkeypatch.setenv(k, "")
    assert slot.main() == 0
    assert "words_due=false" in out.read_text() and "due=false" in out.read_text()
    assert "Words in context: skipped, secrets not set: WORDS_TG_CHAT_ID" in capsys.readouterr().out
