"""Запуск по таймеру на сервере: если слот цитаты наступил, опубликовать одну цитату."""
import sys

import main
import slot


def run():
    state = main.load_state()
    due = slot.quote_slot(state.get("last_slot"))
    if due is None:
        print(f"nothing due, last={state.get('last_slot')}")
        return 0
    print(f"due={due} last={state.get('last_slot')}")
    return main.main()


if __name__ == "__main__":
    sys.exit(run())
