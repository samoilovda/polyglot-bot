import json

import main


def _corpus(tmp_path, n=3):
    d = tmp_path / "quotes"
    d.mkdir()
    quotes = [{"id": f"ru-x-{i}", "text": "раз два три четыре пять", "author": "A", "author_en": "A",
               "country": "RU", "source": "s"} for i in range(n)]
    (d / "ru.json").write_text(json.dumps(quotes, ensure_ascii=False), encoding="utf-8")
    return str(d)


def test_pick_skips_posted(tmp_path):
    d = _corpus(tmp_path)
    state = {"next_culture_index": 0, "posted_ids": ["ru-x-0", "ru-x-1"]}
    for _ in range(10):
        assert main.pick_quote("ru", state, d)["id"] == "ru-x-2"


def test_pick_resets_when_exhausted(tmp_path):
    d = _corpus(tmp_path)
    state = {"next_culture_index": 0, "posted_ids": ["ru-x-0", "ru-x-1", "ru-x-2", "other-1"]}
    q = main.pick_quote("ru", state, d)
    assert q["id"].startswith("ru-x-")
    assert state["posted_ids"] == ["other-1"]


def test_pick_ignores_short_quotes(tmp_path):
    d = tmp_path / "quotes"
    d.mkdir()
    (d / "ru.json").write_text(json.dumps([
        {"id": "ru-a-1", "text": "коротко тут"},
        {"id": "ru-a-2", "text": "раз два три четыре пять"},
    ], ensure_ascii=False), encoding="utf-8")
    assert main.pick_quote("ru", main.default_state(), str(d))["id"] == "ru-a-2"


def _run_main(monkeypatch, tmp_path, send_ok):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"next_culture_index": 5, "posted_ids": []}))
    monkeypatch.setattr(main, "STATE_PATH", str(state_path))
    monkeypatch.setattr(main, "DRY_RUN", False)
    monkeypatch.setattr(main, "translate_quote",
                        lambda q, c: {k: "t" for k in main.CULTURE_ORDER if k != c})
    monkeypatch.setattr(main, "generate_audio", lambda *a, **k: None)
    monkeypatch.setattr(main, "send_telegram", lambda *a, **k: send_ok)
    # main.load_state использует значение по умолчанию, вычисленное при импорте — подменяем явно
    real_load, real_save = main.load_state, main.save_state
    monkeypatch.setattr(main, "load_state", lambda path=str(state_path): real_load(path))
    monkeypatch.setattr(main, "save_state", lambda s, path=str(state_path): real_save(s, path))
    code = main.main()
    return code, json.loads(state_path.read_text())


def test_success_advances_and_wraps(monkeypatch, tmp_path):
    code, state = _run_main(monkeypatch, tmp_path, True)
    assert code == 0
    assert state["next_culture_index"] == 0  # 5 -> 0
    assert len(state["posted_ids"]) == 1


def test_failed_send_keeps_state(monkeypatch, tmp_path):
    code, state = _run_main(monkeypatch, tmp_path, False)
    assert code == 1
    assert state == {"next_culture_index": 5, "posted_ids": []}
