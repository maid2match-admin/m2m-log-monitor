from datetime import datetime, timezone

import pytest

import config
import log_parser
import reported_lines


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith(("CREATE TABLE", "CREATE INDEX")):
            self.db["ddl"] += 1
            return
        if normalized.startswith("DELETE FROM reported_lines"):
            self.db["pruned_with"].append(params)
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def executemany(self, sql, rows):
        assert " ".join(sql.split()).startswith("INSERT INTO reported_lines")
        self.db["rows"].extend(rows)


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db["commits"] += 1


@pytest.fixture
def db(monkeypatch):
    state = {"rows": [], "ddl": 0, "pruned_with": [], "commits": 0}
    monkeypatch.setattr(config, "DATABASE_URL", "postgres://fake")
    monkeypatch.setattr(config, "REPORTED_LINES_RETENTION_DAYS", 30)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(reported_lines, "_schema_ready", False)
    monkeypatch.setattr(reported_lines, "_connect", lambda: FakeConnection(state))
    return state


def lines(text):
    return log_parser.parse_log_text(text)


ERR = "2026-10-04T06:29:30.565920+00:00 heroku[web.1]: State changed from up to crashed"
WARN = '2026-10-04T06:29:31+00:00 app[web.1]: {"level": "WARNING", "message": "slow"}'


def test_store_inserts_errors_and_warnings_with_raw_line_and_timestamp(db):
    assert reported_lines.store("m2m-proxy", lines(ERR), lines(WARN)) == 2
    assert db["rows"] == [
        ("m2m-proxy", "error", datetime(2026, 10, 4, 6, 29, 30, 565920, tzinfo=timezone.utc), ERR),
        ("m2m-proxy", "warning", datetime(2026, 10, 4, 6, 29, 31, tzinfo=timezone.utc), WARN),
    ]
    assert db["commits"] == 1


def test_store_prunes_past_retention_on_every_write(db):
    reported_lines.store("m2m-proxy", lines(ERR), [])
    reported_lines.store("m2m-proxy", lines(ERR), [])
    assert db["pruned_with"] == [(30,), (30,)]


def test_schema_is_created_once_per_process(db):
    reported_lines.store("m2m-proxy", lines(ERR), [])
    ddl_after_first = db["ddl"]
    reported_lines.store("m2m-proxy", lines(ERR), [])
    assert ddl_after_first == 3
    assert db["ddl"] == 3


def test_nothing_to_store_opens_no_connection(monkeypatch):
    def no_connect():
        raise AssertionError("must not connect with nothing to store")

    monkeypatch.setattr(reported_lines, "_connect", no_connect)
    assert reported_lines.store("m2m-proxy", [], []) == 0


def test_dry_run_stores_nothing(db, monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    assert reported_lines.store("m2m-proxy", lines(ERR), lines(WARN)) == 0
    assert db["rows"] == [] and db["commits"] == 0
