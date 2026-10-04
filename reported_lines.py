"""Postgres archive of the log lines reported to Slack.

Heroku keeps only a short rolling log buffer (~40 minutes for m2m-proxy), and
Slack messages are chunked and hard to search, so every error/warning line
that gets reported is also kept here for REPORTED_LINES_RETENTION_DAYS. Both
the drain receiver and the scheduled run write to it.

Read it locally, never via `heroku run`: a one-off dyno's output goes into
this app's own logs, where the scheduled run would report the lines again.

    heroku pg:psql -a m2m-log-monitor -c "
        SELECT logged_at, severity, line FROM reported_lines
        WHERE app_name = 'm2m-proxy'
          AND logged_at BETWEEN '2026-10-04 06:20Z' AND '2026-10-04 06:40Z'
        ORDER BY logged_at"
"""
import psycopg

import config

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reported_lines (
    id BIGSERIAL PRIMARY KEY,
    app_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    logged_at TIMESTAMPTZ,
    line TEXT NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_CREATE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS reported_lines_app_logged_at ON reported_lines (app_name, logged_at)",
    "CREATE INDEX IF NOT EXISTS reported_lines_stored_at ON reported_lines (stored_at)",
)

_INSERT_SQL = """
INSERT INTO reported_lines (app_name, severity, logged_at, line)
VALUES (%s, %s, %s, %s)
"""

_PRUNE_SQL = """
DELETE FROM reported_lines
WHERE stored_at < now() - make_interval(days => %s)
"""

_schema_ready = False


def _connect():
    return psycopg.connect(config.DATABASE_URL)


def store(app_name, errors, warnings):
    """Insert the reported lines and prune anything past retention. Returns rows stored.

    A no-op under DRY_RUN: nothing was really sent to Slack, and a local dry
    run's DATABASE_URL is usually the production database.
    """
    global _schema_ready
    if config.DRY_RUN:
        return 0
    rows = [(app_name, "error", line.timestamp, line.raw) for line in errors]
    rows += [(app_name, "warning", line.timestamp, line.raw) for line in warnings]
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            if not _schema_ready:
                cur.execute(_CREATE_TABLE_SQL)
                for sql in _CREATE_INDEXES_SQL:
                    cur.execute(sql)
            cur.executemany(_INSERT_SQL, rows)
            cur.execute(_PRUNE_SQL, (config.REPORTED_LINES_RETENTION_DAYS,))
        conn.commit()
    _schema_ready = True
    return len(rows)
