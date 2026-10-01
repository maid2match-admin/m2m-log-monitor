# m2m-log-monitor

A scheduled health check for a fleet of Heroku apps. Every hour it checks
each app for maintenance mode, crashed/down dynos, and new error/warning
lines in the logs, then posts a summary to Slack.

## Features

- Reports new error log lines (and optionally warnings) since the last run
- Alerts on crashed or down dynos
- Skips apps entirely while they're in maintenance mode
- Filters out known-noisy log lines (e.g. scanner traffic) so reports stay
  actionable
- Sends a "resolved" message when a previously-erroring app comes back clean
- Posts findings to a Slack channel via an Incoming Webhook

The list of monitored apps lives in `config.py` (`MONITORED_APPS`).

## How it works

This is a standalone script, not a server — it runs to completion and exits.
It's intended to be invoked on a schedule (e.g. Heroku Scheduler) rather than
run continuously. Each run walks every app in `MONITORED_APPS`, checks its
health and logs, and sends at most one Slack message per app.

## Requirements

- Python 3.13
- A Heroku account/API key with access to every monitored app
- A Postgres database (for tracking what's already been reported)
- A Slack Incoming Webhook

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in real values
```

Environment variables are loaded from `.env` automatically via
`python-dotenv`; `.env` is gitignored so real secrets never get committed.

| Var | Required | Purpose |
|---|---|---|
| `HEROKU_API_KEY` | yes | `heroku auth:token`, from an account with access to every app in `MONITORED_APPS` |
| `DATABASE_URL` | yes (unless `DRY_RUN`) | Postgres connection string used to track what's already been reported |
| `SLACK_WEBHOOK_URL` | yes (unless `DRY_RUN`) | Incoming Webhook URL for the target Slack channel |
| `REPORT_WARNINGS` | no | `true` to also report warning lines (default `false`) |
| `LOG_SESSION_LINES` | no | Log lines fetched per app per run (default `1500`) |
| `LOG_LOOKBACK_HOURS` | no | Only report lines from within this many hours of now (default `6`) |
| `DRY_RUN` | no | `true` to print Slack messages instead of sending, and skip the database (default `false`) |
| `DRAIN_APPS` | no | Comma-separated apps whose logs arrive via the drain receiver; the scheduled run skips their log scan (default empty) |
| `DRAIN_PASSWORD` | for the receiver | Basic-auth password in the drain URL; empty rejects every POST |
| `DRAIN_USERNAME` | no | Basic-auth username in the drain URL (default `logplex`) |
| `DRAIN_FLUSH_SECONDS` | no | How often the receiver posts buffered lines to Slack (default `30`) |
| `DRAIN_MAX_BUFFERED_LINES` | no | Most lines held per app between posts; the excess is summarised (default `200`) |
| `DRAIN_RECEIVER_URL` | if `DRAIN_APPS` is set | Base URL the scheduled run uses to reach the receiver's `GET /status` |
| `DRAIN_STALE_MINUTES` | no | Minutes an app may go without delivering a line before its drain counts as broken (default `60`) |

## Running

```bash
python main.py
```

To try it out locally without sending real Slack messages or touching the
database:

```bash
DATABASE_URL= DRY_RUN=true python main.py
```

## Deploying to Heroku

```bash
heroku create m2m-log-monitor
heroku addons:create heroku-postgresql:mini
heroku addons:create scheduler:standard
heroku config:set HEROKU_API_KEY=... SLACK_WEBHOOK_URL=...
git push heroku main
```

Then open the scheduler dashboard and add a job:

```bash
heroku addons:open scheduler
```

- Command: `python main.py`
- Frequency: every hour, at :10

## Log drain receiver

The scheduled run fetches at most 1,500 lines per app, the most Heroku's log-session
API returns. For a busy app that is a small slice of the interval: on `m2m-proxy` it
was about 30 minutes, ~8% of the then 6-hour interval and still only ~50% of an hour (measured 2026-10-01). For such apps,
`drain_receiver.py` takes a Heroku HTTPS log drain instead, so every line is
classified the moment it is written.

- Runs as the `web` process (`gunicorn`, one worker; see `Procfile`). Use a
  Basic dyno or larger: an Eco dyno sleeps, and Logplex drops lines it can't
  deliver.
- Classifies with the same `log_parser.classify()` as the scheduled run, and
  also flags `State changed from ... to crashed`.
- Buffers lines and posts one Slack message per app every
  `DRAIN_FLUSH_SECONDS`, so a burst of errors becomes one message.
- Checks Basic auth from the drain URL in constant time, ignores retried
  `Logplex-Frame-Id`s, and returns 404 for any app not in `DRAIN_APPS`.
- `GET /` is an unauthenticated health check.

Rollout for an app (`m2m-proxy` shown):

```bash
heroku config:set DRAIN_PASSWORD="$(openssl rand -hex 32)" -a m2m-log-monitor
heroku ps:scale web=1:basic -a m2m-log-monitor
heroku drains:add "https://logplex:<DRAIN_PASSWORD>@<m2m-log-monitor host>/drain/m2m-proxy" -a m2m-proxy
# once lines are arriving, stop the scheduled run scanning the same logs:
heroku config:set DRAIN_APPS=m2m-proxy DRAIN_RECEIVER_URL=https://<m2m-log-monitor host> -a m2m-log-monitor
```

`DRAIN_APPS` gates both the receiver (unknown apps get 404) and the scheduled
skip, so set it before attaching the drain if you want the first lines
accepted, at the cost of a brief overlap where both report. The scheduled run
keeps checking maintenance mode and dyno health for drain apps.

**Self-check.** The receiver reports its health at `GET /status` (same Basic
auth): when each app last delivered a line, the last Slack success and
failure, and whether the flush thread is alive. For every drain app, the
scheduled run reads `/status`. If the receiver is unreachable, rejects the
credentials, has heard nothing for `DRAIN_STALE_MINUTES`, or last failed to
post to Slack, the run posts a "log drain is unhealthy" alert and falls back
to the 1,500-line pull for that app. A broken drain therefore means partial
coverage plus an alert, never silence.

## Tests

```bash
pip install pytest
pytest
```
