"""Scheduled-run check that the log drain receiver is actually working.

The receiver's own log lines are a poor alarm: this app is busy enough that
the scheduled run's 1500-line pull only covers part of each hour. So
the receiver reports its health at GET /status, and main.check_app() asks it
directly for every app in DRAIN_APPS. Any problem means the drain can't be
trusted for this run: main alerts and falls back to the log-session pull, so a
dead receiver degrades to partial coverage instead of none.
"""
from datetime import datetime, timezone

import requests

import config


def fetch_status():
    """Return (status_dict, None) or (None, problem_text)."""
    if not config.DRAIN_RECEIVER_URL:
        return None, "DRAIN_RECEIVER_URL is not set, so the receiver can't be checked"
    try:
        response = requests.get(
            config.DRAIN_RECEIVER_URL.rstrip("/") + "/status",
            auth=(config.DRAIN_USERNAME, config.DRAIN_PASSWORD),
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return None, f"receiver unreachable ({type(exc).__name__})"
    if response.status_code == 401:
        return None, "receiver rejected the monitor's credentials (DRAIN_PASSWORD mismatch?)"
    if not response.ok:
        return None, f"receiver /status returned HTTP {response.status_code}"
    try:
        return response.json(), None
    except ValueError:
        return None, "receiver /status did not return JSON"


def _minutes(seconds):
    return int(seconds // 60)


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def evaluate(status, app_name, stale_minutes):
    """Return a list of problems with the drain for app_name (empty = healthy).

    Ages are measured against the receiver's own clock (`now`) to avoid skew.
    """
    problems = []
    now = status["now"]

    if not status.get("flusher_running"):
        problems.append("receiver's Slack flush thread is not running")

    last_frame = status.get("last_frame_at", {}).get(app_name)
    reference = last_frame if last_frame is not None else status["started_at"]
    age = now - reference
    if age > stale_minutes * 60:
        if last_frame is None:
            problems.append(
                f"no log lines received since the receiver started {_minutes(age)} min ago "
                "(drain detached or URL/password wrong?)"
            )
        else:
            problems.append(f"no log lines received for {_minutes(age)} min (last at {_utc(last_frame)})")

    failed_at = status.get("last_slack_failure_at")
    ok_at = status.get("last_slack_ok_at")
    if failed_at is not None and (ok_at is None or failed_at > ok_at):
        reason = status.get("last_slack_failure") or "unknown"
        problems.append(f"receiver's most recent Slack post failed at {_utc(failed_at)} ({reason})")

    audit_failed = status.get("last_audit_failure_at")
    audit_ok = status.get("last_audit_ok_at")
    if audit_failed is not None and (audit_ok is None or audit_failed > audit_ok):
        problems.append(f"receiver's most recent call audit storage failed at {_utc(audit_failed)}")

    return problems


def check(app_name):
    """Return the list of drain problems for app_name, fetching receiver status."""
    status, problem = fetch_status()
    if problem:
        return [problem]
    return evaluate(status, app_name, config.DRAIN_STALE_MINUTES)
