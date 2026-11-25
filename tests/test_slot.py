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


def test_quote_slot_catches_up_one_missed_slot():
    # пропущен слот 22: утренний запуск до 10:00 публикует его, следующий — обычный
    assert slot.quote_slot("2026-10-09T16", utc(10, 4)) == "2026-10-09T22"
    # пропущен слот 10: запуск после 16:00 публикует сначала его, потом текущий
    assert slot.quote_slot("2026-10-09T22", utc(10, 13)) == "2026-10-10T10"
    assert slot.quote_slot("2026-10-10T10", utc(10, 14)) == "2026-10-10T16"
    assert slot.quote_slot("2026-10-10T16", utc(10, 15)) is None


def test_quote_slot_drops_old_backlog_and_handles_empty_state():
    assert slot.quote_slot("2026-10-01T10", utc(10, 13)) == "2026-10-10T10"
    assert slot.quote_slot(None, utc(10, 13)) == "2026-10-10T16"


def test_quote_slot_never_goes_back():
    assert slot.quote_slot("2026-10-10T16", utc(10, 8)) is None


def test_decide_uses_backlog():
    assert slot.decide({"last_slot": "2026-10-09T16"}, "schedule", utc(10, 4))[0]
    assert not slot.decide({"last_slot": "2026-10-09T22"}, "schedule", utc(10, 4))[0]
