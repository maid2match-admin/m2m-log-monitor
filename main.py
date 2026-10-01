"""Heroku multi-app log monitor.

Run every 6 hours by Heroku Scheduler. For each monitored app:
  1. Skip entirely if the app is in maintenance mode.
  2. Alert if any dyno is crashed/down.
  3. Fetch recent logs, report new error lines (and warning lines if
     REPORT_WARNINGS is enabled), and advance the per-app watermark so the
     same lines aren't re-reported next run.

A failure checking one app is caught and reported without stopping the run
for the remaining apps.
"""
import sys
from datetime import datetime, timedelta, timezone

import config
import drain_health
import heroku_client
import log_parser
import slack_notifier
import state_store
from heroku_client import HerokuApiError


def check_app(app_name):
    """Return a short status string describing what happened for this app."""
    if heroku_client.get_maintenance_mode(app_name):
        return "skipped (maintenance mode)"

    dynos = heroku_client.get_dynos(app_name)
    down_dynos = [d for d in dynos if d.get("state") in config.DOWN_DYNO_STATES]
    if down_dynos:
        slack_notifier.send_dyno_down(app_name, down_dynos)

    if app_name in config.DRAIN_APPS:
        # Logs stream to drain_receiver.py; scanning them here too would
        # report the same lines twice — unless the drain is broken, in which
        # case alert and fall through to the pull so coverage isn't zero.
        drain_problems = drain_health.check(app_name)
        if not drain_problems:
            return "ok (logs via drain, dynos_down=%d)" % len(down_dynos)
        slack_notifier.send_drain_problem(app_name, drain_problems)

    has_state_store = bool(config.DATABASE_URL)
    last_ts, last_hash, had_errors_before = (
        state_store.get_last_state(app_name)
        if has_state_store
        else (None, None, False)
    )

    logplex_url = heroku_client.create_log_session(app_name)
    raw_text = heroku_client.fetch_log_text(logplex_url)
    lines = log_parser.parse_log_text(raw_text)

    if not lines:
        return "ok (no log lines returned, dynos_down=%d)" % len(down_dynos)

    new_lines = log_parser.lines_after(lines, last_ts, last_hash)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.LOG_LOOKBACK_HOURS)
    new_lines = log_parser.filter_since(new_lines, cutoff)
    errors, warnings = log_parser.classify(new_lines, config.REPORT_WARNINGS)

    if errors or warnings:
        slack_notifier.send_error_report(app_name, errors, warnings)
    elif has_state_store and had_errors_before:
        slack_notifier.send_resolved(app_name)

    if has_state_store:
        newest = log_parser.newest_line(lines)
        if newest is not None:
            state_store.set_last_state(
                app_name, newest.timestamp, newest.hash, bool(errors)
            )

    return (
        f"ok (errors={len(errors)}, warnings={len(warnings)}, "
        f"dynos_down={len(down_dynos)})"
    )


def main():
    missing = [
        name
        for name, value in (
            ("HEROKU_API_KEY", config.HEROKU_API_KEY),
            ("DATABASE_URL", config.DATABASE_URL),
            ("SLACK_WEBHOOK_URL", config.SLACK_WEBHOOK_URL),
        )
        if not value
    ]
    if missing and not config.DRY_RUN:
        print(f"Missing required config vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if config.DATABASE_URL:
        state_store.ensure_schema()

    checked = 0
    skipped = 0
    failed = 0

    for app_name in config.MONITORED_APPS:
        try:
            result = check_app(app_name)
        except HerokuApiError as exc:
            failed += 1
            print(f"{app_name}: FAILED - {exc}")
            slack_notifier.send_check_failure(app_name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - keep the run going per-app
            failed += 1
            print(f"{app_name}: FAILED (unexpected) - {exc}")
            slack_notifier.send_check_failure(app_name, exc)
            continue

        checked += 1
        if "skipped" in result:
            skipped += 1
        print(f"{app_name}: {result}")

    print(
        f"Run complete: {checked} checked, {skipped} skipped, "
        f"{failed} failed, {len(config.MONITORED_APPS)} total"
    )


if __name__ == "__main__":
    main()
