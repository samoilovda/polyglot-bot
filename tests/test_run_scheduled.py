import main
import run_scheduled
import slot


def test_skips_when_nothing_due(monkeypatch):
    monkeypatch.setattr(main, "load_state", lambda: {"last_slot": "x"})
    monkeypatch.setattr(slot, "quote_slot", lambda last: None)
    monkeypatch.setattr(main, "main", lambda: 1 / 0)
    assert run_scheduled.run() == 0


def test_publishes_and_returns_exit_code(monkeypatch):
    monkeypatch.setattr(main, "load_state", lambda: {})
    monkeypatch.setattr(slot, "quote_slot", lambda last: "2026-10-10T10")
    monkeypatch.setattr(main, "main", lambda: 1)
    assert run_scheduled.run() == 1


def test_save_state_is_atomic(tmp_path):
    p = tmp_path / "s.json"
    main.save_state({"last_slot": "a"}, str(p))
    assert main.load_state(str(p))["last_slot"] == "a" and [x.name for x in tmp_path.iterdir()] == ["s.json"]
