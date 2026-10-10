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
