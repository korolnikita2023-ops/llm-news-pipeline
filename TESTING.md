# Testing

## Result

| | |
|---|---|
| Command | `python -m pytest -q` (from the repository root) |
| Python | 3.12.5 (Windows 11) |
| Dependencies | `pip install -r requirements.txt` into a fresh virtualenv outside the repo (aiogram 3.31.0, httpx 0.28.1, feedparser 6.0.14, datasketch 2.0.0, Pillow 12.3.0, python-dotenv 1.2.4, google-genai 2.28.0, openai 3.24.0, anthropic 1.11.0, pytest 9.1.1; `tzdata` was added because Windows has no system time-zone database) |
| Collected | 795 tests in 29 test modules (`tests/test_*.py`) |
| **Outcome** | **795 passed, 0 failed, 0 skipped, 0 xfailed** |
| Duration | about 35 to 46 seconds |

The same result was obtained three times: plain run, a run with an outbound
network blocker, and a run with a hostile environment (see below).

## Network

The tests must pass offline. This was checked with a small pytest plugin
(kept outside the repository) that raises on any `connect()` to a
non-loopback address and on any DNS lookup of a non-local name, and counts
the attempts. Result: 795 passed, **0 blocked network attempts**. The LLM
providers and the Telegram / Threads HTTP APIs are replaced by fakes or
monkeypatched in the tests, so nothing here exercises the real services.

## Isolation from local configuration

`tests/conftest.py` forces `LLM_PROVIDER=mock` and removes provider API keys
for every test, points the database and card image directory at temporary
paths, and pins `WORKFLOW_MODE=legacy`. As a check, the full suite was also run
with `LLM_PROVIDER=gemini GEMINI_API_KEY=<fake> PUBLISH_MODE=live` set in the
environment: 795 passed, 0 network attempts.

## Changes made to tests when this repository was prepared

The tests are the original ones, with these changes:

- Private identifiers in fixtures were replaced by synthetic values
  (Telegram chat ids, user ids, admin names and handles, a bot-token-shaped
  string, a database file name). The assertions are unchanged in meaning.
- Two tests in `tests/test_digest_and_schedule.py`
  (`test_translation_is_one_call_for_the_whole_list`,
  `test_translation_keeps_untranslated_rows`) only passed on a machine whose
  `.env` selected a real LLM provider; with no `.env` they took the mock path
  and failed. They now build their config with a fake provider and key through
  the new `_llm_cfg()` helper. The LLM call itself is still monkeypatched.
- `tests/conftest.py` got the `_pin_llm_provider` autouse fixture described above.
- No test was deleted or weakened.

Before these two test changes and the `.env.example` file were in place, a
run of the unmodified copy gave 791 passed, 4 failed: two tests that read
`.env.example` (`test_floor_default_matches_the_example_file`,
`test_prefilter_threshold_matches_the_example_file`) and the two translation
tests above.

Two tests read `.env.example` to check that it matches the defaults in
`config.py` (`AUTO_EDITORIAL_FLOOR`, `PREFILTER_MIN_SCORE`); keep that file in
sync with `config.py`.

## Static checks

The project has no linter or type-checker configuration, and none was added.
For information only: `ruff check --select F,E9` (pyflakes errors and syntax
errors) reports no problems. Ruff with its default rule set reports hundreds of
style findings, mostly import ordering; they were not addressed.

## Smoke run

`python -m src.main --dry-run` (mock LLM, bundled sample data, no network)
completes with exit code 0 and prints a run summary. In that run the mock
pipeline processed 14 of 15 sample items after the cheap filter, 2 reached the
rewrite stage, and both were rejected (one by the legal gate for a missing
source URL, one by the editorial score), so no candidate was queued. That is a
valid outcome for the mock path, not an error.
