# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone Python script (not a long-running server) that Heroku Scheduler
invokes every 6 hours. Each run walks the fleet of apps in
`config.MONITORED_APPS`, checks each one's maintenance mode / dyno health /
recent logs, and posts findings to Slack. There is no web framework, queue,
or persistent process — `main.py` runs to completion and exits. The one
exception is the optional `web` process, `drain_receiver.py` (see "Log drain
receiver" below).

## Commands

```bash
# setup
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# tests
pytest
pytest tests/test_log_parser.py
pytest tests/test_log_parser.py::test_classify_finds_errors_and_ignores_warnings_when_disabled

# local dry run against real Heroku apps (no Slack sends, prints instead)
DRY_RUN=true python main.py
```

Config is read from environment variables, loaded from a local `.env` via
`python-dotenv` if present (see `.env.example`). `DATABASE_URL` unset skips
the Postgres watermark store entirely — useful for a dry run that shouldn't
mutate the production `seen_state` table (the same table the real Scheduler
job writes to).

## Architecture

Five modules, each a thin, independently-testable layer; `main.py` wires
them together with no business logic of its own beyond the per-app loop:

- **`config.py`** — all environment/env-derived values (`MONITORED_APPS`,
  API keys, feature flags). Every other module imports this rather than
  reading `os.environ` directly.
- **`heroku_client.py`** — Heroku Platform API v3 wrapper (`_get`/`_post`
  helpers, `HerokuApiError`). Maintenance mode is a field on the app
  resource itself (`GET /apps/{app}`), not a separate endpoint.
- **`log_parser.py`** — pure functions, no I/O. Parses Logplex's
  `source[dyno]: message` text format into `LogLine` records, classifies
  error/warning lines, and computes the "newest line"
  watermark. `LogLine.hash` (sha256 of the raw line) plus timestamp is what
  makes de-duplication exact even when two lines share a timestamp.
  Severity is decided in this order by `classify()`:
  - **Structured JSON wins.** Upstream apps (m2m-proxy et al.) emit JSON log
    bodies whose top-level `"level"` field is authoritative. When
    `parse_json_level()` extracts a string `level` from the line body,
    routing is decided by that level alone (`ERROR`/`CRITICAL` -> errors,
    `WARNING`/`WARN` -> warnings, `DEBUG`/`INFO` -> dropped) and the keyword
    regexes are never consulted. This is deliberate: these lines routinely
    carry the word "Error" in their `message` or a nested `data.error`, so
    substring-matching would misroute WARNING lines to the alert channel.
  - Only **non-JSON** lines (Heroku platform lines, uvicorn plaintext,
    tracebacks) fall back to keyword regex (`ERROR_RE`/`WARNING_RE`).

  Two categories of line are also dropped:
  - Lines matching `NOISE_RE` (checked first, before JSON parsing) — known-noisy substrings that aren't
    actionable, e.g. internet port-scanners hitting Heroku Postgres addons
    (`no pg_hba.conf entry for host`, `unsupported frontend protocol`, `no
    PostgreSQL user name specified in startup packet`, a failed login as
    the literal user `"postgres"`), vulnerability scanners probing web
    dynos for a CMS that was never installed (`/wp-`), and google-auth's
    benign Regional Access Boundary token-refresh probe (swallowed
    internally; see
    [googleapis/google-cloud-python#17515](https://github.com/googleapis/google-cloud-python/issues/17515)).
    Add new substrings to `NOISE_PATTERNS` as new noise sources turn up.
  - Lines where `has_benign_explicit_level()` is true — Heroku router
    (`at=info`) and uvicorn access-log (`INFO:` prefix) **non-JSON** lines
    carry their own authoritative severity marker, which overrides incidental
    `ERROR_RE`/`WARNING_RE` keyword hits elsewhere in the line (e.g. a
    scanner requesting `/error.php` matches `\berror\b` in the URL despite
    the line being pure access-log noise). Genuine router errors
    (`at=error`, e.g. H12 timeouts) and uvicorn's own `ERROR:`-level lines
    are unaffected.
- **`state_store.py`** — Postgres-backed watermark persistence (one row per
  app: last-seen timestamp + hash + whether that run had errors, used to
  detect "back to clean" for the resolved-notification). `psycopg.connect`
  is the only thing tests mock (see `tests/test_state_store.py`'s
  `FakeConnection`/`FakeCursor`) — there's no ORM.
- **`slack_notifier.py`** — all outbound Slack messages go through
  `_post_to_slack`, which is the single `DRY_RUN` gate (prints instead of
  POSTing) and sets an explicit `username`/`icon_emoji` on every payload so
  messages display consistently regardless of which Slack app the
  `SLACK_WEBHOOK_URL` was originally created under. Long error/warning
  reports are chunked to stay under Slack's message size limit
  (`_MAX_CHUNK_CHARS`).

### Per-app flow (`main.check_app`)

1. Maintenance mode check → skip app entirely if enabled.
2. Dyno state check → alert (but don't skip) if any dyno is `crashed`/`down`.
3. Fetch the last watermark (+ `had_errors` flag) from `state_store`
   (skipped if `DATABASE_URL` is unset — the app then re-reports the whole
   log buffer every run, with no resolved-notification).
4. Open a Logplex log session and fetch text, parse it, filter to lines
   after the watermark, then drop anything older than
   `config.LOG_LOOKBACK_HOURS` (default 6h) so a missing/reset watermark
   can't dredge up days-old errors — classify what's left into
   errors/warnings.
5. Send one batched Slack report if there's anything new; if there's
   nothing new but the previous run had errors, send a resolved message
   instead. Then advance the watermark to the newest line's
   (timestamp, hash, had_errors).

A failure anywhere in this flow for one app (`HerokuApiError` or anything
else) is caught in `main.main()`, reported via `send_check_failure`, and
does not stop the run for remaining apps.

**Known limitation:** Heroku's log-session API returns a rolling buffer, not
a true time-range query. A very high-volume dyno can produce enough output
to roll past 6 hours between runs, and those lines are silently missed —
accepted tradeoff for most apps. When an app outgrows it (`m2m-proxy`:
1,500 lines ≈ 30 min), move that app to the drain receiver instead of
changing the scheduled run.

### Log drain receiver (`drain_receiver.py`)

A WSGI app that gunicorn serves as the `web` process, with **one worker**: the
per-app buffers, frame-ID de-duplication and flush thread are all
in-process, so more workers would split buffers and double-count retried
frames. The flush thread starts lazily on the first request so it runs in
the forked worker, not the gunicorn master.

- Logplex POSTs octet-counted RFC 5424 frames (`parse_frames`).
  `to_log_line` rewrites each one into the exact `<ts> source[dyno]: msg`
  shape `heroku logs` prints, so `log_parser` is reused unchanged and Slack
  reports look the same as scheduled ones.
- `classify_lines` = `log_parser.classify()` plus `heroku[...]` crash state
  changes. The scheduled run catches crashes via the dyno API instead.
- `config.DRAIN_APPS` is both the receiver's allowlist (404 otherwise) and
  the set the scheduled run skips the log scan for (`main.check_app`).
- **This app is itself in `MONITORED_APPS`**, and the scheduled run
  keyword-matches its logs. So the receiver never prints drained content,
  and its routine status line (`drain flush: ...`) avoids the words
  error/warning. Real failures are printed with an `ERROR` prefix
  deliberately, so the scheduled run reports them.
  `test_routine_status_output_cannot_trip_the_scheduled_scan` guards this.
  Keep it in mind for any new `print` in the receiver.
