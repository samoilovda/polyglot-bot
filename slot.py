"""Слоты публикаций (по Ташкенту) — GitHub cron сильно запаздывает, поэтому время решает код."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TASHKENT = timezone(timedelta(hours=5))
SLOT_HOURS = (10, 16, 22)
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")


def current_slot(now=None):
    """Последний наступивший слот, например '2026-10-10T16'."""
    now = (now or datetime.now(timezone.utc)).astimezone(TASHKENT)
    past = [h for h in SLOT_HOURS if h <= now.hour]
    if past:
        return f"{now:%Y-%m-%d}T{max(past):02d}"
    return f"{now - timedelta(days=1):%Y-%m-%d}T{max(SLOT_HOURS):02d}"


def is_due(last_slot, now=None):
    return last_slot != current_slot(now)


def main():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            last = json.load(f).get("last_slot")
    except FileNotFoundError:
        last = None
    due = os.environ.get("GITHUB_EVENT_NAME") != "schedule" or is_due(last)
    print(f"current={current_slot()} last={last} due={due}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"due={'true' if due else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
