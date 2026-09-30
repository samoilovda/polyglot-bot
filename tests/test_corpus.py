import json
import os

import pytest

import main

MIN_QUOTES = 4  # uz пока короткий; целевой размер — 30 (см. предупреждение ниже)
TARGET_QUOTES = 30


def _load_all():
    return {c: main.load_quotes(c) for c in main.CULTURE_ORDER}


@pytest.mark.parametrize("culture", main.CULTURE_ORDER)
def test_corpus_file_is_valid(culture):
    quotes = main.load_quotes(culture)
    assert len(quotes) >= MIN_QUOTES
    for q in quotes:
        for field in ("id", "country", "text", "author", "author_en", "source"):
            assert str(q.get(field, "")).strip(), f"{q.get('id')}: empty {field}"
        assert q["id"].startswith(culture + "-")
        assert main.count_words(q["text"]) >= main.MIN_WORDS, q["id"]
        assert len(q["text"]) <= 300, q["id"]
        assert main.country_flag(q["country"]), f"{q['id']}: bad country {q['country']}"


def test_ids_unique_globally():
    ids = [q["id"] for qs in _load_all().values() for q in qs]
    assert len(ids) == len(set(ids))


def test_corpus_size_warning():
    small = {c: len(qs) for c, qs in _load_all().items() if len(qs) < TARGET_QUOTES}
    if small:
        import warnings
        warnings.warn(f"Corpus below target of {TARGET_QUOTES}: {small}")


def test_state_file_is_valid():
    with open(main.STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    assert 0 <= state["next_culture_index"] < len(main.CULTURE_ORDER)
    assert isinstance(state["posted_ids"], list)
