"""Слоты публикаций (по Ташкенту) — GitHub cron сильно запаздывает, поэтому время решает код.

Два независимых набора слотов: цитаты (SLOT_HOURS, ключ состояния last_slot) и «Words in context»
(WORDS_SLOT_HOURS, ключ last_words_slot). Если наступили оба, цитата публикуется первой, слова — следующим запуском.

Пропущенные слоты цитат догоняются: один запуск — одна цитата, не больше MAX_BACKLOG слотов назад.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TASHKENT = timezone(timedelta(hours=5))
SLOT_HOURS = (10, 16, 22)
MAX_BACKLOG = 2  # текущий слот и один пропущенный; более старые отбрасываются
STATE_PATH = os.environ.get("STATE_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")


def words_slot_hours():
    raw = os.environ.get("WORDS_SLOT_HOURS", "13")
    return tuple(sorted({int(x) for x in raw.split(",") if x.strip()})) or (13,)


def current_slot(now=None, hours=None):
    """Последний наступивший слот, например '2026-10-10T16'."""
    hours = tuple(hours or SLOT_HOURS)
    now = (now or datetime.now(timezone.utc)).astimezone(TASHKENT)
    past = [h for h in hours if h <= now.hour]
    if past:
        return f"{now:%Y-%m-%d}T{max(past):02d}"
    return f"{now - timedelta(days=1):%Y-%m-%d}T{max(hours):02d}"


def is_due(last_slot, now=None, hours=None):
    return last_slot != current_slot(now, hours)


def quote_slot(last_slot, now=None):
    """Какой слот цитат публиковать: самый старый неопубликованный из последних MAX_BACKLOG; None — всё опубликовано."""
    now = (now or datetime.now(timezone.utc)).astimezone(TASHKENT)
    days = [now - timedelta(days=d) for d in range(MAX_BACKLOG, -1, -1)]
    current = current_slot(now)
    recent = [s for day in days for h in sorted(SLOT_HOURS) if (s := f"{day:%Y-%m-%d}T{h:02d}") <= current][-MAX_BACKLOG:]
    if not last_slot:
        return current
    return next((s for s in recent if s > last_slot), None)


def decide(state, event_name, now=None, force_words=False):
    """-> (quotes_due, words_due). Не публикуем оба типа за один запуск: цитата первой."""
    if force_words:
        return False, True
    manual = event_name != "schedule"
    quotes_due = manual or quote_slot(state.get("last_slot"), now) is not None
    words_due = (not manual) and not quotes_due and is_due(state.get("last_words_slot"), now, words_slot_hours())
    return quotes_due, words_due


def main():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    force_words = os.environ.get("FORCE_WORDS", "").strip().lower() in ("1", "true", "yes")
    quotes_due, words_due = decide(state, os.environ.get("GITHUB_EVENT_NAME"), force_words=force_words)
    due = quotes_due or words_due
    print(
        f"current={current_slot()} last={state.get('last_slot')} words_current={current_slot(hours=words_slot_hours())} "
        f"last_words={state.get('last_words_slot')} quotes_due={quotes_due} words_due={words_due}"
    )
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            for k, v in (("due", due), ("quotes_due", quotes_due), ("words_due", words_due)):
                f.write(f"{k}={'true' if v else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
