# Telegram news bot

A Python pipeline that turns RSS news into short Telegram channel posts, with a
human or a scheduler deciding what goes out:

```
RSS / GDELT  ->  normalize + dedupe  ->  cheap pre-filter  ->  LLM relevance filter
     ->  rewrite  ->  legal gate (code)  ->  LLM editorial score  ->  card image
     ->  approval bot (human)  or  auto editor (scheduler)  ->  Telegram channel
```

The shipped configuration is a Russian-language crime-news channel. The
default prompts in `prompts/` are written in Russian and the legal gate matches
Russian text (see [Limitations](#limitations)). Prompts, source list and
keyword lists are plain files and constants, so they can be changed.

Everything runs offline by default: `LLM_PROVIDER=mock` and
`PUBLISH_MODE=dry-run` need no API keys and make no calls to an LLM provider
or Telegram.

License: MIT (see LICENSE). No licence file is included, so all rights are
reserved until the owner decides.

## What is verified

- `python -m pytest -q`: **795 passed, 0 failed, 0 skipped** (Python 3.12.5,
  Windows). Details and the exact command are in [TESTING.md](TESTING.md).
- The suite makes no outbound network calls (checked with a socket blocker,
  see TESTING.md). LLM providers and the Telegram API are replaced by fakes in
  the tests, so the tests do not show how the code behaves against the live
  services.

## Architecture

### Data flow (`src/main.py`, one run)

1. **Ingest.** RSS feeds from the registry in `src/sources.py`
   (`src/ingest_rss.py`), optionally GDELT DOC 2.0 (`src/ingest_gdelt.py`,
   off by default). `--dry-run` reads `data/sample_news.json` instead (synthetic
   items, no network).
2. **Normalize and dedupe.** `src/normalize.py` maps every item to one
   `NewsItem` schema (URL canonicalisation, hash). `src/dedupe_l1.py` removes
   near-duplicates with MinHash/LSH (CPU only, no LLM). Items already seen in
   the last 30 days (`seen_items` table) are dropped before any paid call.
3. **Age filter.** Items older than `STORY_MAX_AGE_HOURS` by feed date are
   dropped; feeds without dates pass.
4. **Cheap pre-filter** (`src/prefilter.py`). Regex scoring of title and
   description, no network and no model. Hard rejects (war, non-news,
   non-crime) get -10; the rest are ranked with a per-source weight and a
   per-source quota (`PREFILTER_SOURCE_QUOTA`), and only the top
   `MAX_LLM_ITEMS_PER_RUN` go on. That ceiling is also capped by money:
   `MAX_RUN_COST_USD` divided by the worst-case cost of one scoring call.
5. **Relevance** (`src/relevance.py`, prompt `prompts/relevance_judge.md`).
   Cheap model scores five axes (curiosity, emotion, unusual, shareable,
   ru_fit) and sets a boolean `relevant` from a genre filter; the numeric sum
   only ranks. In mock mode a keyword scorer is used instead.
6. **Cross-source dedupe**, then the pool is ranked and cut.
7. Depending on `WORKFLOW_MODE`:
   - `legacy`: the run itself prepares up to `MAX_TOPICS_PER_RUN` candidate
     posts (steps 8 to 11) and queues them for the approval bot.
   - `digest`: the run stops after scoring and stores a short list
     (`digest_items`). The owner receives it in Telegram and picks a story;
     the paid steps 8 to 11 run only for the picked story.
   - `auto`: same pool as `digest`; `src/auto_editor.py` picks the best story
     shortly before each publication slot (see below).
8. **Case status** (`src/case_status.py`). Detects from the source text
   whether the person is suspected, charged, convicted, acquitted, etc.
   The status is passed to both the rewriter prompt and the gate.
9. **Legal gate, pre** (`src/legal_gate.py: check_pre`) on the raw item.
10. **Rewrite** (`src/rewrite.py`, prompt `prompts/rewriter_ru.md`, mid-tier
    model), then **legal gate, post** (`check_post` and `check_headline`) on
    the generated text and the card headline. A hard fail triggers a
    regeneration, up to `MAX_REGENERATE_ATTEMPTS`.
11. **Editorial score** (`src/scorer.py`, prompt
    `prompts/editorial_scorer.md`): six dimensions 0 to 10. A post needs
    `MIN_EDITORIAL_AVG`. In `digest` mode a score between
    `EDITORIAL_BORDERLINE_MIN` and `MIN_EDITORIAL_AVG` goes to the human with a
    flag. In `auto` mode the best scored variant is accepted if it clears
    `AUTO_EDITORIAL_FLOOR` (hard minimum 8.0 in `config.py`), is not a hard
    fail and has `legal_safety >= 6`. Then `src/image_card.py` draws a PNG card.
12. **Approval and publishing.**
    `src/approval_bot.py` (aiogram 3, long polling) shows candidates to the
    owner and admins with approve, edit, regenerate, reject and hold buttons.
    `src/publish_due.py` publishes scheduled posts whose slot has come.
    Both go through one function, `approval_bot._publish_post`, which
    re-runs the legal gate on the final text (also on text typed by a human),
    checks the post belongs to the configured channel, and claims the post
    with an atomic `UPDATE` so a double click or two processes cannot publish
    it twice. Sending uses the Bot API (`src/publisher.py`: `sendPhoto` with a
    caption when a card exists, otherwise `sendMessage`). Without `PUBLISH_MODE=live` nothing is sent.

### The legal gate is code, not a prompt

`src/legal_gate.py` is deterministic: lists and regular expressions in
`config.py` and the module. The prompt asks the model to follow the same rules
but the code does not trust it. It is applied three times: to the raw item,
to the generated post and headline, and again at the publish point. What it
checks:

- source URL present;
- post text is mostly Cyrillic (the checks below only work on Russian text);
- verdict wording: guilt words aimed at a named person without a qualifier
  ("according to the investigation", "convicted", etc.) block the post; the
  allowed wording depends on the detected case status;
- minors: age pattern plus a name pattern forces manual review;
- armed forces / state bodies in a negative context: flagged manual-only;
- method details (suicide methods, drug synthesis, bypassing security): hard fail;
- editorial clichés from `prompts/slop-list.txt`: hard fail for machine
  text, a warning for text typed by a human.

Text is normalised before matching (homoglyphs, zero-width characters,
hyphens and spaces inside words) so that simple evasions do not pass.
`tests/test_gate_evasion.py`, `test_legal_gate.py` and
`test_case_status_gate.py` cover this.

### Cost caps (budget guard)

Every LLM call goes through `src/llm_client.py:generate_json`, which checks
the caps **before** the network call and writes the cost of every real call
to the `cost_log` table, including the JSON retry:

| Cap | Variable | Default |
|---|---|---|
| Per run (estimated up front from worst-case call cost) | `MAX_RUN_COST_USD` | 0.05 |
| Per day (hard stop) | `DAILY_COST_LIMIT_USD` | 0.10 |
| Per month, target (warning) | `MONTHLY_COST_TARGET_USD` | 3.0 |
| Per month, absolute limit (hard stop) | `MONTHLY_COST_LIMIT_USD` | 5.0 |

Other limits on paid work: `MAX_LLM_ITEMS_PER_RUN`, `MAX_TOPICS_PER_RUN`,
`MAX_CANDIDATES_PER_DAY`, `MAX_PENDING_QUEUE`, `MAX_PREPARATION_ATTEMPTS_PER_DAY`,
and a cross-process requests-per-minute limiter (`src/rate_limit.py`,
`LLM_RPM_LIMIT`). A model that is missing from `config.LLM_PRICES_PER_1M` is
priced at the most expensive known rate rather than at zero.

### Storage (`src/storage.py`)

One SQLite file, `data/news_bot.db`, WAL mode. Tables: `raw_items`, `events`,
`posts`, `metrics`, `seen_items`, `cost_log`, `admins`, `digest_items`,
`selection_attempts`, `llm_calls`, `llm_lock`, `meta`, `crossposts`,
`integration_state`. Drafts are also written as markdown to `data/drafts/` and
card images to `data/drafts/images/`. Everything under `data/` except
`sample_news.json` is git-ignored.

### Modules

| Module | Role |
|---|---|
| `config.py` | All settings, read from environment / `.env`; keyword lists and legal-gate word lists; model price table |
| `src/main.py` | Pipeline orchestrator (`python -m src.main`) |
| `src/sources.py` | Source registry: role (discovery / verification), weight, on/off switches |
| `src/ingest_rss.py`, `src/ingest_gdelt.py` | Fetchers; one failing feed does not stop the others |
| `src/normalize.py`, `src/dedupe_l1.py` | Common item schema, MinHash/LSH dedupe |
| `src/prefilter.py` | Cheap regex pre-filter and ranking before the paid stage |
| `src/relevance.py`, `src/scorer.py`, `src/rewrite.py`, `src/digest.py` | LLM stages (each has a mock path) |
| `src/llm_client.py` | Gemini / OpenAI / Anthropic client, JSON parsing with one retry, budget guard, pricing |
| `src/rate_limit.py` | Requests-per-minute limiter shared by processes (SQLite) |
| `src/case_status.py` | Court-status detection from the source text |
| `src/legal_gate.py` | Deterministic legal and style gate |
| `src/image_card.py` | PNG card (Pillow) |
| `src/approval_bot.py` | Telegram approval UI, single publish point |
| `src/auto_editor.py`, `src/schedule.py` | Slot-based automatic editor; slot times in `SCHEDULE_TZ`, stored as UTC |
| `src/publish_due.py` | One-shot runner: publish posts whose slot has come (and fill slots in `auto` mode) |
| `src/notify.py` | Sends the digest message to the owner after a run |
| `src/roles.py` | Owner and admins stored in the database |
| `src/crosspost_due.py`, `src/threads_api.py`, `src/threads_format.py` | Optional mirror to Threads, as a separate one-shot worker |
| `src/backup_db.py` | Consistent SQLite snapshot (backup API), daily and weekly rotation |
| `src/news_schedule.py` | Reads the next run time from a systemd timer, if one exists |
| `src/env_file.py` | Locked update of one secret in `.env` (used for the Threads token refresh) |
| `src/publisher.py`, `src/storage.py` | Bot API sending and draft files; SQLite layer |

The scheduler is deliberately outside the bot: the long-running bot only
handles button presses, and the one-shot runners (`src.main`,
`src.publish_due`, `src.crosspost_due`, `src.backup_db`) are meant to be started
by cron or systemd timers. This repository does not include timer or service
unit files.

## Running

Requires Python 3.12.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # optional for a dry run
```

Dry run on the bundled sample data (no network, no keys, nothing is sent):

```bash
python -m src.main --dry-run
```

Other commands:

```bash
python -m src.main --discovery-only   # fetch + cheap filter only; no model calls, no DB writes
python -m src.main                    # real ingest; behaviour depends on .env
python -m src.approval_bot            # approval bot; needs TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID
python -m src.publish_due             # publish due scheduled posts (run from cron / a timer)
python -m src.crosspost_due           # Threads mirror worker (only if THREADS_ENABLED=true)
python -m src.backup_db               # database snapshot
python calibrate.py                   # print relevance score distribution for current feeds
python check_cost.py                  # print the sum of cost_log
```

To go live: set `LLM_PROVIDER` and the matching key, set `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_OWNER_CHAT_ID`, `TELEGRAM_CHANNEL_ID`, then `PUBLISH_MODE=live`.
Live publishing without a channel id stops with an error instead of falling
back to draft mode. Check model names and prices in `config.py` first; they go
out of date.

Bot commands (owner and admins): `/digest`, `/scheduled`, `/review`, `/queue`,
`/addadmin <id> [name]`, `/deladmin <id>` (the last two are owner-only), plus
a persistent keyboard.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

The tests use a temporary database per test, a guard that points the database
and card directory away from `data/`, and a fixture that forces
`LLM_PROVIDER=mock` whatever `.env` contains. See [TESTING.md](TESTING.md) for
the recorded result.

## Configuration

All variables are optional; defaults are in `config.py`, a full annotated
example is in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `PUBLISH_MODE` | `dry-run` | `live` sends to the channel |
| `WORKFLOW_MODE` | `legacy` | `legacy`, `digest` or `auto` |
| `LLM_PROVIDER` | `mock` | `mock`, `gemini`, `openai`, `anthropic` |
| `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | empty | Provider keys |
| `*_MODEL_CHEAP`, `*_MODEL_MID` | see `config.py` | Cheap model for relevance and scoring, mid for rewriting |
| `TELEGRAM_BOT_TOKEN` | empty | Bot token |
| `TELEGRAM_OWNER_CHAT_ID` | empty | Owner's numeric user id |
| `TELEGRAM_CHANNEL_ID` | empty (no default) | `@channel` or `-100...` |
| `TELEGRAM_ADMIN_IDS`, `TELEGRAM_ADMIN_LABELS` | empty | Extra editors |
| `DISCOVERY_ENABLED`, `SOURCES_DISABLED`, `SOURCES_ENABLED` | `true`, empty, empty | Source switches |
| `GDELT_ENABLED`, `NEWSDATA_API_KEY` | `false`, empty | Extra ingest options |
| `PREFILTER_MIN_SCORE`, `PREFILTER_SOURCE_QUOTA`, `MAX_LLM_ITEMS_PER_RUN` | `-9`, `0.4`, `40` | Pre-filter |
| `MIN_EDITORIAL_AVG`, `EDITORIAL_BORDERLINE_MIN`, `AUTO_EDITORIAL_FLOOR` | `8.6`, `8.0`, `8.0` | Quality thresholds |
| `MAX_RUN_COST_USD`, `DAILY_COST_LIMIT_USD`, `MONTHLY_COST_TARGET_USD`, `MONTHLY_COST_LIMIT_USD` | `0.05`, `0.10`, `3.0`, `5.0` | Cost caps in USD |
| `DIGEST_SIZE`, `MAX_SUCCESSFUL_SELECTIONS_PER_DAY`, `MAX_PREPARATION_ATTEMPTS_PER_DAY` | `10`, `2`, `4` | Digest workflow |
| `AUTO_MAX_POSTS_PER_DAY`, `AUTO_LEAD_MINUTES`, `AUTO_FRESHNESS_HOURS` | `3`, `25`, `24` | Auto workflow |
| `SCHEDULE_TZ`, `MORNING_SLOT`, `DAY_SLOT`, `EVENING_SLOT` | `Europe/Moscow`, `09:00`, `14:00`, `20:00` | Publication slots |
| `STORY_MAX_AGE_HOURS` | `24` | Maximum age by feed date (0 disables) |
| `THREADS_*` | disabled | Optional Threads mirror |
| `LOG_LEVEL` | `INFO` | |

## Limitations

- **Russian only, and tied to Russian law.** The legal gate, case-status
  detector, minors and name heuristics, slop list and default prompts all work
  on Russian text. Rules such as "armed forces / state bodies in a negative
  context" refer to Russian legislation. English-language posts are rejected by
  the language check. Using another language or jurisdiction means rewriting
  those parts, not just translating prompts.
- **Heuristics, not legal advice.** The gate uses word roots, regular
  expressions and sentence-level qualifier matching. It will have false
  positives (valid posts blocked) and false negatives. A human approval step
  is part of the design in `legacy` and `digest` modes; `auto` mode removes it.
- **Cost accounting counts tokens only.** `cost_log` multiplies the token
  counts reported by the provider by the price table in `config.py`. Any
  other billable item (for example search grounding, if someone adds it) is not
  counted. Prices and model IDs in the table are a snapshot and will go stale.
- **Feed availability is not guaranteed.** Some official feeds blocked
  requests from data-centre IPs when tested (listed in `src/sources.py`
  `UNREACHABLE`). The feeds' terms of use were not reviewed for this
  repository; check them before running against a real channel.
- **Single-host design.** SQLite with WAL, a database-backed rate limiter and
  one-shot runners. No multi-node deployment, no external queue.
- **No clustering across sources beyond MinHash.** Embedding-based
  clustering (the `events.centroid_embedding` column) is not implemented.
- **Scheduling is not included.** You need to run `publish_due`, `main` and
  `backup_db` from cron or systemd yourself. `src/news_schedule.py` reads
  the next run from a systemd timer named `news-bot-run.timer` and silently
  returns nothing on machines without systemd.
- **Platform notes.** The image card looks for system fonts (PT Serif,
  Georgia, Consolas on Windows; DejaVu and Liberation on Linux) and falls
  back if none is found; card appearance depends on the installed fonts.
- **Not exercised against live services in this repository.** The Threads
  mirror, Telegram publishing and the three LLM providers are covered only
  with fakes and mocks here.
- **Default timezone is `Europe/Moscow`.** Change `SCHEDULE_TZ` if needed.
- **Default prompts are Russian** and describe a calm, fact-first crime-news
  voice. They are the main thing to adapt for another channel.
- No CI configuration and no linter configuration are included.
