"""Слоты публикаций (по Ташкенту) — GitHub cron сильно запаздывает, поэтому время решает код.

Два независимых набора слотов: цитаты (SLOT_HOURS, ключ состояния last_slot) и «Words in context»
(WORDS_SLOT_HOURS, ключ last_words_slot). Если наступили оба, цитата публикуется первой, слова — следующим запуском.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TASHKENT = timezone(timedelta(hours=5))
SLOT_HOURS = (10, 16, 22)
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")


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


WORDS_REQUIRED_ENV = ("WORDS_TG_CHAT_ID", "WORDBANK_URL", "WORDBANK_TOKEN")


def words_missing_config(env=None):
    """Секреты, без которых пост «Words in context» отправить нельзя."""
    env = os.environ if env is None else env
    return [k for k in WORDS_REQUIRED_ENV if not (env.get(k) or "").strip()]


def decide(state, event_name, now=None, force_words=False):
    """-> (quotes_due, words_due). Не публикуем оба типа за один запуск: цитата первой."""
    if force_words:
        return False, True
    manual = event_name != "schedule"
    quotes_due = manual or is_due(state.get("last_slot"), now)
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
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    missing = words_missing_config()
    if words_due and missing and not dry_run:
        # Не падаем и не ставим зависимости каждый час впустую: слот слов просто пропускается до настройки секретов.
        print(f"Words in context: skipped, secrets not set: {', '.join(missing)} (quotes are not affected).")
        words_due = False
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
