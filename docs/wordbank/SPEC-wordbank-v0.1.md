# Wordbank — specification v0.1

> A personal, multilingual tool for capturing words, checking their pronunciation and practising them. Single user (Denis). Written for Claude Code as the starting brief.
> Working title "Wordbank"; rename freely.
> **Before coding:** read section 12 (defaults and open questions) and stop at each milestone marked **⏸ owner check**.

---

## 0. Goals and non-goals

### Goals (v1)
1. **Capture a word from anywhere** with at most two taps:
   - the Android text-selection menu ("Add to Wordbank");
   - the Android Share sheet;
   - a private Telegram bot;
   - a quick-add box in the web app on Mac and Linux.
2. **See the pronunciation immediately** when unsure: IPA first, audio second. Every IPA and audio item carries a clear label saying where it came from.
3. **Work in many languages.** English (UK), Spanish, Portuguese (Brazil) and Uzbek (Latin) are needed now. Adding a language must be configuration, not code.
4. **Practise later.** Practice shows the word with the IPA hidden, then reveals the IPA and plays the audio.
5. **Return the words in context.** polyglot-bot publishes short texts built from saved words to the existing Telegram quotes channel.
6. **Open data.** Export to CSV/JSON and an Anki-importable TSV.

### Non-goals (v1)
- Automatic pronunciation assessment.
- Scraping Oxford or other proprietary dictionaries. Oxford is a **link-out only**.
- Multi-user accounts, sharing, monetisation.
- Rebuilding a full spaced-repetition system. Keep scheduling simple and rely on the Anki export.

### Hard constraints
- Routine lookup, capture and practice must make **no LLM calls**. LLM use is limited to the batch enrichment (§6.4) and publications (§9), with a configurable daily budget.
- The **owner's personal notes never leave the server**. They do not go to LLMs, Telegram posts or logs.

---

## 1. Architecture

```
 Android app (Kotlin) ─┐
 Telegram capture bot ─┼──HTTPS──►  Server (FastAPI, Docker, ARM64)  ◄── Web app / PWA (Mac, Linux, tablet)
 Web quick-add ────────┘                │      │
                                        │      ├── Lexicon (imported Wiktionary data, read-only)
                                        │      ├── Personal data (items, notes, reviews)
                                        │      ├── Audio cache (Commons recordings + synthetic TTS)
                                        │      └── Jobs: audio fetch, LLM enrichment, backups
                                        ▲
                     polyglot-bot (GitHub Actions) ── pulls candidate words, posts to quotes channel, reports back
```

| Part | Tech | Repo |
|---|---|---|
| Server + web app | Python 3.13, FastAPI, SQLAlchemy 2 + Alembic, Pydantic v2; front-end Vite + Svelte (or Preact) PWA | new repo `wordbank` |
| Android app | Kotlin, Jetpack Compose, OkHttp/Retrofit, Room (offline queue), minSdk 26 | `wordbank/android/` |
| Telegram capture | Part of the server (webhook endpoint); a **separate bot token** from the quotes bot | `wordbank` |
| Publications | New post type inside **polyglot-bot** | existing repo `samoilovda/polyglot-bot` |

Deployment: `docker compose` on the owner's ARM64 server, behind the existing reverse proxy, with HTTPS on a public domain (owner supplies the domain).

---

## 2. Data model

Use Postgres if it already runs on the server; otherwise SQLite (WAL mode). See §12. All text is NFC-normalised. Language codes are BCP-47.

### 2.1 `languages` (seeded from a config file `languages.yaml`)
| Field | Notes |
|---|---|
| `code` | e.g. `en-GB`, `es`, `pt-BR`, `uz-Latn` |
| `name` | display name |
| `preferred_ipa_tags` | Wiktionary accent tags in priority order: en-GB → `["Received-Pronunciation","UK","British"]`; pt-BR → `["Brazil","São Paulo","Rio de Janeiro"]`; es → see §12 |
| `tts_voices` | edge-tts voices, e.g. `en-GB-RyanNeural`, `es-ES-AlvaroNeural`, `pt-BR-AntonioNeural`, `uz-UZ-SardorNeural` (as already used in polyglot-bot) |
| `detect_hints` | letters and patterns for detection (§5) |
| `enabled` | bool |

### 2.2 Lexicon (reference data, rebuilt by import, never edited by hand)
- **`lex_entries`**: `id`, `lang`, `headword`, `headword_norm` (for matching: case-folded, Uzbek apostrophes unified, see §5), `pos`, `sense_hint` (short gloss to tell homographs apart), `source` (`wiktionary`), `source_rev` (dump date), `licence` (`CC BY-SA 4.0`).
- **`lex_pron`**: `id`, `entry_id`, `ipa`, `tags` (accent tags as given), `kind` (`phonemic` if `/…/`, `phonetic` if `[…]`).
- **`lex_audio_ref`**: `id`, `entry_id`, `commons_filename`, `tags`. These are references only; the files are fetched lazily (§6.2).
- **`lex_forms`**: inflected form → entry (for example `scrutinised` → `scrutinise`), from Wiktionary form-of data where available.

### 2.3 Audio
- **`audio`**: `id`, `kind` (`reference_recording` | `synthetic`), `origin` (`wikimedia_commons` | `edge_tts` | `user_upload`), `lang`, `text_spoken`, `voice` (for synthetic), `file_path` (local), `source_url`, `author`, `licence`, `fetched_at`, `sha256`.
- An audio row is linked either to a lexicon entry or to a personal item.
- The UI shows **"Recording · Wikimedia Commons · {author} · {licence}"** or **"Synthetic voice · {voice}"**, never an unlabelled play button.

### 2.4 Personal data
- **`items`** (what the owner saved):
  - `id`, `lang`, `lang_confidence` (`auto` | `confirmed`), `text` (as captured: a word or a short phrase), `lemma`;
  - `entry_id` (nullable link to the lexicon; with several pronunciations, the one the owner picked);
  - `ipa_user` (IPA typed by the owner, e.g. imported from the Sheet);
  - `ipa_practice`, plus `ipa_practice_origin` (`llm` | `user`), `ipa_practice_model`, `ipa_practice_at`, `ipa_practice_notes`: the narrow practice transcription (aspiration and similar, §6.4);
  - `issue_tags` (set: `stress`, `vowel`, `consonant`, `silent_letter`, `aspiration`, `other`);
  - `note` (free text, **private**);
  - `context` (the sentence it came from, if known, **private** by default);
  - `captured_via` (`android_select` | `android_share` | `telegram` | `web` | `import`), `source_app` (if Android reports it);
  - `status` (`new` | `learning` | `known` | `archived`);
  - `publishable` (bool, default `true`: may be used in public posts);
  - `created_at`, `updated_at`.
- **`reviews`**: `id`, `item_id`, `at`, `result` (`again` | `good`), `mode` (`card` | `list`).
- **`publications`**: `id`, `lang`, `item_ids`, `posted_at`, `telegram_message_id`, `text_hash`. A word is not reused within N days (configurable, default 21).
- **`events`** (optional audit log): capture, edit and delete events, used for debugging the capture flows.

**Rule:** the dictionary IPA (`lex_pron`), the owner's IPA (`ipa_user`) and the practice IPA (`ipa_practice`) are **separate fields**. None overwrites another. The UI shows them side by side, each labelled.

---

## 3. Server API (JSON, `/api/v1`)

Auth: see §10. Every endpoint except `/health` and the Telegram webhook needs a token or session.

| Method & path | Purpose |
|---|---|
| `GET /lookup?q=&lang=` | Lexicon lookup. Without `lang`: run detection (§5) and return candidates per language, ranked. Response: entries with POS, all prons (preferred variety first), audio refs, and whether the owner already saved it. Must be fast (target < 100 ms p95 on the server) and make no network calls. |
| `GET /suggest?q=&lang=` | Prefix and typo suggestions (edit distance ≤ 2) for the search box. |
| `POST /items` | Create an item: `{text, lang?, context?, captured_via, source_app?, entry_id?, issue_tags?, note?}`. Idempotent on `(lang, text_norm)` within 24 h: returns the existing item and bumps it instead of duplicating. |
| `PATCH /items/{id}` / `DELETE /items/{id}` | Edit or delete. |
| `GET /items?lang=&status=&tag=&q=&due=` | Collection listing. |
| `POST /items/{id}/reviews` | Record a practice result. |
| `GET /audio/{id}` | Serve a cached file. |
| `POST /audio/resolve` | `{entry_id}` or `{item_id}` → returns a playable audio id. Order: cached recording → fetch from Commons (§6.2) → synthesise with edge-tts (§6.3). Returns the label metadata. |
| `POST /enrich/practice-ipa` | Queue LLM enrichment for given items (§6.4). |
| `POST /import/sheet` | Upload CSV (§8). Returns a review report and does **not** commit until confirmed. |
| `POST /import/sheet/{job}/commit` | Commit a reviewed import. |
| `GET /export?format=csv|json|anki` | Export. |
| `GET /publish/candidates?lang=&n=` | For polyglot-bot (§9). Publish-scope token only. |
| `POST /publish/report` | For polyglot-bot: `{item_ids, lang, telegram_message_id}`. |
| `POST /telegram/webhook/{secret}` | Telegram capture bot (§4.3). |

Keep an OpenAPI schema; the Android client is generated from it or written against it.

---

## 4. Capture channels

### 4.1 Android — text-selection menu (primary)
- An activity with an intent filter for `android.intent.action.PROCESS_TEXT`, `mimeType="text/plain"`, label **"Add to Wordbank"**. Read `Intent.EXTRA_PROCESS_TEXT`. Always treat it as read-only: never return modified text.
- The UI is a **translucent activity with a bottom sheet** over the current app. The user must not lose their place.
  1. Show the selected text, trimmed. If it is longer than 6 words, offer: "Save as phrase" or tap a single word.
  2. Show the detected language as a chip. Tapping it opens the enabled languages; the choice is remembered as a hint (§5).
  3. Call `/lookup`. Show the IPA (preferred variety; tabs if several POS or pronunciations) and a play button (`/audio/resolve`; while that loads, device TTS can be used, labelled synthetic). Show "Open in Oxford / Wiktionary" link-outs.
  4. Primary button **Save** (one tap; the sheet closes after a short confirmation). Secondary actions: issue-tag chips, a note field, "Not now".
- **Offline:** if the server is unreachable, save to a Room queue and sync with WorkManager when online. Show "Saved offline".
- **Performance:** the sheet should appear within 300 ms. Lookup results fill in when they arrive.
- **⏸ M0 spike first (§11):** a commenter on an Android tutorial reported that custom selection actions didn't work on Redmi devices. Verify on the owner's Poco F8 Pro (HyperOS) in at least Chrome, Telegram, a PDF/e-book reader and Gmail before building more. Record the results in `docs/android-compat.md`.

### 4.2 Android — Share sheet (fallback)
- `ACTION_SEND` with `text/plain`. Same bottom sheet. Long shared text: the owner picks the word(s), and the full text is stored as `context`.

### 4.3 Telegram capture bot (separate private bot)
- Webhook on the server. Respond **only** to the owner's Telegram user id; ignore everyone else silently.
- Message → item:
  - A plain word or phrase: detect the language, save, and reply with headword, IPA and source label, voice/audio, and inline buttons (`🌐 language`, `🏷 issue`, `🗑 undo`).
  - The suffix `@es`, `@pt`, `@en`, `@uz` forces the language.
  - A forwarded or long message (more than 6 words): reply with the words as inline buttons to pick from; store the full text as `context`.
- Commands: `/last` (last 10 items), `/undo`, `/lang <code>` (sets the detection hint for 30 minutes).

### 4.4 Web quick-add
- See §7.1.

---

## 5. Language detection (auto-detect + one-tap fix)

Deterministic, with no network or LLM calls:
1. **Script:** Latin, Cyrillic, Arabic and so on. Non-Latin script restricts the candidates immediately.
2. **Characters:**
   - pt: `ã õ ç â ê ô à` and the endings `-ção -ções -ão`;
   - es: `ñ ¿ ¡` and the ending `-ción`;
   - uz: `oʻ gʻ o‘ g‘ o' g'` and the digraphs `sh ch ng` with frequent `q x`.
   - **Normalise all Uzbek apostrophe variants (U+02BB, U+2018, U+2019, U+0027) to one form for matching.** Keep the original spelling for display.
3. **Lexicon hits:** an exact or form match in each enabled language's lexicon. Score by match type (lemma > form) and word frequency, if available.
4. **Tie-breaks:** an active hint (§4.3 `/lang`, or the language chosen last in the past 30 minutes), then the most-used language over the past 7 days.
5. If confidence is low, still pick the best guess, mark `lang_confidence=auto` and **show the chip prominently**.

Write unit tests with real ambiguous pairs, e.g. `real`, `final`, `animal` (es/pt/en), `casa` (es/pt), `kitob` (uz), `nação` (pt).

---

## 6. Pronunciation data pipeline

### 6.1 Lexicon import (Wiktionary via kaikki.org)
- Source: kaikki.org Wiktextract data for the **English-language Wiktionary edition**, which contains entries for all languages.
  - The raw dump is a single `raw-wiktextract-data.jsonl.gz` of about 2.9 GB, updated regularly.
  - **Stream and filter by `lang_code`** (`en`, `es`, `pt`, `uz`, plus later languages). Do not store the whole dump.
  - The per-language post-processed files are marked deprecated on kaikki.org, so don't depend on them.
- Keep entries with `sounds` containing `ipa` or audio filenames. Also keep entries without pronunciation, so lookup can still say "known word, no IPA".
- Store the licence and dump date. Show "Pronunciation from Wiktionary (CC BY-SA)" in the UI.
- Coverage reality, to show honestly in the UI: Uzbek has about 5k senses in kaikki versus about 1.8M for English. Expect thin Uzbek IPA and rely on synthetic audio plus LLM practice IPA there.
- Command: `wordbank import-lexicon --langs en,es,pt,uz --from <path or url>`. Make it re-runnable: rebuild the lexicon tables atomically and keep the `items.entry_id` links where `(lang, headword, pos, ipa)` still matches. Log any unmatched links.
- After the first import, run and report a **coverage test** against the owner's existing word list (§8): the share of words with any IPA, with preferred-variety IPA, and with a UK recording. **⏸ owner check.**

### 6.2 Reference recordings (Wikimedia Commons)
- Fetch lazily, on first play, by the filename from `lex_audio_ref`. Get the licence and author from the Commons API (`prop=imageinfo&iiprop=url|extmetadata`) and store them with the file.
- Be polite: a descriptive `User-Agent` with contact details, at most one request per second, retries with backoff, and never re-download an existing file (check by `sha256` and filename).
- Optional background job: pre-fetch recordings for all saved items.

### 6.3 Synthetic audio (fallback)
- Server-side **edge-tts**, using the language's configured voice. Cache by `(voice, text)`. Label it as synthetic.
- Note that edge-tts relies on an unofficial Microsoft endpoint (as in polyglot-bot). Wrap it behind a `TtsProvider` interface so another engine (e.g. Piper) can be added later.
- On Android, device TTS is an offline fallback, also labelled synthetic.

### 6.4 Practice IPA via LLM (occasional, batched)
- **Purpose:** a narrower "practice transcription" showing what a learner should pay attention to: aspiration (`pʰ tʰ kʰ`), dark/light l, flapping where relevant, vowel reduction, stress. It sits alongside the dictionary's phonemic IPA and never replaces it.
- **Trigger:** manual ("Enrich 20 items" in the web app), or a nightly job for items saved that day without `ipa_practice`. Daily budget cap (`LLM_DAILY_MAX_ITEMS`, default 50).
- **Provider:** OpenRouter, reusing polyglot-bot's pattern: JSON response format, `temperature` 0.2, validation and up to 2 retries. Model configurable via `OPENROUTER_MODEL`.
- **Input per item:** `lang`, variety, `text`, the dictionary IPA if any, and the POS. **Never** the note or context.
- **Output:** `{phonemic, practice, confidence (0–1), notes (≤ 20 words, English), differs_from_dictionary (bool)}`.
  - Validate that the output uses only IPA characters.
  - If `differs_from_dictionary` is true and confidence is below 0.7, flag the item for review.
- The UI shows **"Practice IPA · generated by {model} · {date}"** with an edit button. Editing sets the origin to `user`.

### 6.5 Link-outs
- Per language, configurable URL templates, e.g. Oxford Learner's, Cambridge, Wiktionary, Forvo. These are links only: no fetching, no scraping.

---

## 7. Web app (PWA; desktop-first, responsive)

### 7.1 Lookup (home screen)
- A search box focused on load, with suggestions as you type (§3 `/suggest`). "Did you mean" for typos.
- **Result:** headword, language chip, POS tabs, a large preferred-variety IPA, other variants collapsed, and play (normal and 0.75×) with the provenance label. Also: practice IPA if saved, the owner's IPA if any, link-outs.
- **Decision:** two buttons, **Fine** (clear and refocus) and **Save** (saves; then optional issue chips and a note, with no blocking modal).
- A strip of recent lookups.
- Keyboard: `/` focuses search, `Enter` saves, `Space` replays, `Esc` clears.

### 7.2 Collection
- Filter by language, status, issue tag, origin, "needs review" (§6.4) and date. Edit inline. Bulk actions: enrich, archive, mark not publishable, export.

### 7.3 Practice
- **Card mode:** word (plus context if present), say it, tap to reveal the IPA (practice IPA first if present, then dictionary IPA), play audio, then mark **Again** or **Good**. Simple scheduling: Good → interval ×2 (1, 2, 4, 8… days, capped at 60); Again → 1 day.
- **List mode** (reading aloud): rows of words with the IPA hidden per row; tap a row to reveal it and play.
- Filter practice by language and tag. Cache the practice queue and its audio for offline use (service worker).

### 7.4 Import review and settings
- Import review: see §8.
- Settings: enabled languages and varieties, voices, link-outs, LLM budget, publication defaults.

---

## 8. Import from the owner's Google Sheet ("English learning" → tab "Words")

v1 uses a **CSV uploaded by the owner** (File → Download → CSV). No Google API auth.

The known layout is irregular. Handle it robustly:
- Words and IPA sit in **column pairs**: B/C, E/F, H/I. Column A and other columns are mostly empty. Column I sometimes holds a short definition instead of IPA (e.g. "repair smth" next to `mend`), and column J occasionally holds a definition too (e.g. next to `dates`).
- Treat as IPA a cell that contains IPA characters or slashes. Otherwise store it as `note` text.
- **Normalise the IPA:** trim whitespace and tabs, add missing slashes, and keep the owner's aspiration marks as they are.
  - Store it in `ipa_user`. Where it contains `ʰ` or similar narrow marks, **also** copy it to `ipa_practice` with origin `user`.
- Likely misspellings: where a word has no lexicon match but there is one within edit distance ≤ 2, show it as a **suggestion**, never an automatic change. Examples from the sheet: `tenious`, `serrenely`, `persue`, `cruize`, `shrievelled`.
- Duplicates (e.g. `scold`, `species`, `vocal`, `pasta`, `essay`, `global`, `comment`, `encourage`, `participle`): merge them, keeping the non-empty IPA.
  - Two IPAs that differ only in notation (aspiration marks, `ɛ`/`e`, missing slashes; e.g. `parent` as `/ˈpʰɛərənt/` and `/ˈpeərənt/`) are merged: the plain form goes to `ipa_user`, the aspirated one to `ipa_practice`.
  - Two IPAs that really differ (e.g. `increase` as noun `/ˈɪnkriːs/` and verb `/ɪnˈkriːs/`) are kept as **separate items**, each linked to the matching POS entry.
- Rows with IPA but no word go to a "needs attention" list.
- All imported items: language `en-GB`, `captured_via=import`, status `learning`.
- Show a review screen: counts, suggestions to accept or reject, conflicts. Commit only after confirmation.

---

## 9. Publications in polyglot-bot ("Words in context")

**Destination: the existing quotes channel**, which is public. Therefore:
- Only items with `publishable=true` are used. Notes and contexts are **never** sent to the bot or the LLM.
- The posts reveal which words the owner is learning. The owner has accepted this; the `publishable` flag is the escape hatch.

### 9.1 Server side
- `GET /publish/candidates?lang=&n=6` returns items in status `new` or `learning`, not published in the last N days, preferring items with issue tags and recent capture. Fields: `id, text, lemma, lang, ipa (preferred dictionary IPA, or practice IPA, or user IPA, with the origin label)`.
- Authenticated with a **publish-scope token**, a GitHub secret `WORDBANK_TOKEN`, along with `WORDBANK_URL`.

### 9.2 polyglot-bot side (new module, quotes behaviour unchanged)
- New module `words_post.py`, with tests mirroring the existing style (pytest, no network, mocked `requests.post`).
- **Schedule:** a separate slot, configurable (default one per day at 13:00 Tashkent), with its own state key `last_words_slot` in `data/state.json`. Extend `slot.py` so quotes and words slots are evaluated independently. The existing `SLOT_HOURS = (10, 16, 22)` stays.
- **Language rotation** among languages with at least 4 candidates (state key `words_lang_index`).
- **LLM** (same OpenRouter pattern as `translate_quote`):
  - Write a 3–5 sentence natural text at a configurable level (default B1 for es/pt/uz, B2+ for en) that uses **4–6 of the candidate words**.
  - Mark each used word in `{{…}}`.
  - Return a Russian translation of the whole text, because the channel is multilingual and Russian is the owner's native language; this is configurable.
  - Return per-word glosses in English.
- **Validation:** every marked word must correspond to a candidate (lemma or inflected form, checked against the candidate list). The text must be 300–900 characters. Up to 2 retries, then skip the post without failing the workflow.
- **Post format** (HTML, reusing `html.escape`, `make_bold`, caption limits and `compact_for_caption`):
  ```
  📚 <b>Words in context</b> · 🇪🇸
  <blockquote>…text with <b>bold</b> words…</blockquote>
  ✨ <b>palabra</b> /paˈlaβɾa/ — word
  ✨ …
  ━━━━━━━━━━━━━━━━━━
  🇷🇺 <i>…translation…</i>
  ```
  Show the IPA beside each word, with no provenance labels in the public post (keep it clean), but use only IPA that the server marked as dictionary or user. Practice IPA from the LLM is allowed only if `WORDS_ALLOW_LLM_IPA=true`.
- **Audio:** the whole text via edge-tts in the language's voice. Optional card image using `card.py` if it adapts easily; otherwise text and voice only in v1.
- After a successful send: `POST /publish/report`, then update the state. If the report fails, log it and don't fail the post.
- `DRY_RUN` and `FORCE_CULTURE`-style overrides (`FORCE_WORDS_LANG`) for local testing.

---

## 10. Security, privacy, operations

- **Auth:**
  - Web: password with Argon2id plus a session cookie (`Secure`, `HttpOnly`, `SameSite=Lax`). Passkeys are a nice-to-have.
  - Android and the Telegram bot: long-lived API tokens with scopes (`capture`, `read`, `publish`), revocable in settings.
  - Telegram webhook: secret path plus `X-Telegram-Bot-Api-Secret-Token`, and an owner-user-id whitelist.
- Rate-limit the auth endpoints. CORS is limited to the app's own origin.
- Secrets live in a `.env` file outside git; provide a `.env.example`.
- **Backups:** a daily database dump plus the audio directory, rotated (7 daily, 4 weekly). A documented restore command.
- Logging: no notes or contexts in logs.
- **Docker:** multi-arch images (arm64 required). `docker compose up` brings up `api`, `worker` (jobs), the database if Postgres, and static web assets served by the api or the proxy.
- `GET /health` for monitoring.

---

## 11. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | **Android selection-menu spike** | Minimal app shows "Add to Wordbank" and displays the selected text in Chrome, Telegram, a PDF/e-book reader and Gmail on the Poco F8 Pro. Results recorded. **⏸ owner check.** If it fails, the Share sheet becomes primary. |
| M1 | Server skeleton | FastAPI, DB, migrations, auth, `/items` CRUD, `/health`, Docker compose on arm64, CI (lint + tests). |
| M2 | Lexicon import + lookup | English, Spanish, Portuguese and Uzbek imported; `/lookup` and `/suggest`; detection (§5) with tests; coverage report against the Sheet CSV. **⏸ owner check.** |
| M3 | Web app: lookup, quick-add, collection | §7.1, §7.2 usable on desktop and mobile browsers. |
| M4 | Android app | §4.1 and §4.2, offline queue, token auth. |
| M5 | Telegram capture bot | §4.3. |
| M6 | Audio | Commons fetch with licence metadata, edge-tts fallback, provenance labels everywhere. |
| M7 | Sheet import | §8 with review screen. **⏸ owner check** of the import report. |
| M8 | Practice | Card and list modes, offline cache. |
| M9 | LLM practice IPA | §6.4 with budget and review flags. |
| M10 | Publications | §9 in polyglot-bot; dry run for every language. **⏸ owner check** of three sample posts before enabling the schedule. |
| M11 | Export + backups | CSV, JSON and Anki TSV; backup and restore tested. |

---

## 12. Defaults chosen and open questions

Defaults Claude Code may use unless the owner says otherwise:
1. **Database:** Postgres if one already runs on the server; otherwise SQLite. *Owner to confirm which database is on the server.*
2. **Spanish variety:** `es` with Spain as the preferred IPA tag and the `es-ES` voice (as in polyglot-bot), with Latin American variants shown. *Owner to confirm the preferred variety.*
3. **Portuguese:** `pt-BR` preferred, pt-PT shown as a variant.
4. **Front-end:** Svelte + Vite. Any small, PWA-friendly framework is acceptable.
5. **Words post:** once a day at 13:00 Tashkent, 4–6 words, Russian translation line.
6. **Practice scheduling:** the simple doubling rule in §7.3. Anki export covers more serious SRS.
7. **Domain:** the owner provides the domain and reverse-proxy details (Caddy, Nginx or Traefik?).

Ask the owner before deviating from: the separation of IPA fields (§2.4), the no-LLM rule for routine use (§0), privacy of notes and context (§0, §9), and the no-scraping rule (§0, §6.5).

---

## 13. References
- Android `PROCESS_TEXT` walkthrough: https://dev.to/bigaru/providing-custom-text-selection-actions-in-android-1akc
- kaikki.org raw data: https://kaikki.org/dictionary/rawdata.html (formats documented at https://github.com/tatuylonen/wiktextract)
- Oxford Dictionaries API terms (why no caching or scraping): https://developer.oxforddictionaries.com/api-terms-and-conditions
- polyglot-bot: https://github.com/samoilovda/polyglot-bot (`main.py`, `slot.py`, `.github/workflows/daily.yml`)
