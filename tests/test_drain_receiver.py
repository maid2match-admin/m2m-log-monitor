import base64
import io

import pytest

import config
import drain_receiver
import main
import slack_notifier


def frame(message):
    data = message.encode("utf-8")
    return str(len(data)).encode() + b" " + data


def syslog(source, dyno, message, ts="2026-10-01T05:06:49.011209+00:00"):
    return f"<190>1 {ts} host {source} {dyno} - {message}"


JSON_ERROR = '{"timestamp": "2026-10-01T05:06:49.011209", "level": "ERROR", "message": "Unhandled exception", "app": "m2m-proxy"}'
JSON_WARNING_WITH_ERROR_WORD = '{"level": "WARNING", "message": "Launch27 API 422: x", "data": {"error": "bad"}}'
JSON_INFO = '{"level": "INFO", "message": "Retrieved 5 teams"}'
ROUTER_OK = 'at=info method=GET path="/v1/staff/teams" host=m2m-proxy.herokuapp.com status=200 bytes=10'
ROUTER_H12 = 'at=error code=H12 desc="Request timeout" method=GET path="/v1/staff/bookings" status=503'
R14 = "Error R14 (Memory quota exceeded)"
CRASH = "State changed from up to crashed"


@pytest.fixture(autouse=True)
def drain_config(monkeypatch):
    monkeypatch.setattr(config, "DRAIN_APPS", frozenset({"m2m-proxy"}))
    monkeypatch.setattr(config, "DRAIN_USERNAME", "logplex")
    monkeypatch.setattr(config, "DRAIN_PASSWORD", "s3cret")
    monkeypatch.setattr(config, "REPORT_WARNINGS", False)
    monkeypatch.setattr(config, "DRAIN_MAX_BUFFERED_LINES", 200)
    state = drain_receiver.DrainState()
    monkeypatch.setattr(drain_receiver, "STATE", state)
    monkeypatch.setattr(state, "ensure_flusher", lambda: None)
    return state


@pytest.fixture
def slack(monkeypatch):
    sent = []
    monkeypatch.setattr(slack_notifier, "_post_to_slack", sent.append)
    return sent


def basic(user="logplex", password="s3cret"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def post(body, path="/drain/m2m-proxy", auth=None, frame_id=None, method="POST"):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    if frame_id is not None:
        environ["HTTP_LOGPLEX_FRAME_ID"] = frame_id
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    drain_receiver.app(environ, start_response)
    return captured["status"]


# --- framing and conversion ---------------------------------------------------

def test_parse_frames_splits_octet_counted_messages():
    body = frame(syslog("app", "web.1", "one")) + frame(syslog("heroku", "router", "two"))
    messages = drain_receiver.parse_frames(body)
    assert len(messages) == 2
    assert messages[1].endswith("two")


def test_parse_frames_counts_bytes_not_characters():
    body = frame(syslog("app", "web.1", "café ✓")) + frame(syslog("app", "web.1", "next"))
    messages = drain_receiver.parse_frames(body)
    assert messages[0].endswith("café ✓")
    assert messages[1].endswith("next")


def test_parse_frames_keeps_good_frames_before_a_truncated_one():
    body = frame(syslog("app", "web.1", "ok")) + b"500 <190>1 truncated"
    assert len(drain_receiver.parse_frames(body)) == 1


def test_to_log_line_matches_heroku_logs_format():
    line = drain_receiver.to_log_line(syslog("heroku", "router", ROUTER_OK) + "\n")
    assert line == f"2026-10-01T05:06:49.011209+00:00 heroku[router]: {ROUTER_OK}"


def test_to_log_line_rejects_non_syslog():
    assert drain_receiver.to_log_line("not syslog at all") is None


# --- auth and routing ---------------------------------------------------------

def test_health_check_needs_no_auth():
    assert post(b"", path="/", method="GET") == "200 OK"


def test_rejects_missing_and_wrong_credentials():
    body = frame(syslog("app", "web.1", JSON_ERROR))
    assert post(body).startswith("401")
    assert post(body, auth=basic(password="wrong")).startswith("401")
    assert post(body, auth="Basic !!!notbase64").startswith("401")


def test_fails_closed_when_no_password_configured(monkeypatch):
    monkeypatch.setattr(config, "DRAIN_PASSWORD", "")
    assert post(b"", auth=basic(password="")).startswith("401")


def test_unknown_app_is_404_even_with_valid_auth():
    assert post(b"", path="/drain/some-other-app", auth=basic()).startswith("404")


def test_oversized_body_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "DRAIN_MAX_BODY_BYTES", 10)
    assert post(b"x" * 11, auth=basic()).startswith("413")


# --- classification and flushing ----------------------------------------------

def test_reports_errors_router_h_codes_r14_and_crashes_but_not_routine_lines(drain_config, slack):
    body = b"".join(
        frame(syslog(src, dyno, msg))
        for src, dyno, msg in [
            ("app", "web.1", JSON_ERROR),
            ("app", "web.1", JSON_WARNING_WITH_ERROR_WORD),
            ("app", "web.1", JSON_INFO),
            ("heroku", "router", ROUTER_OK),
            ("heroku", "router", ROUTER_H12),
            ("heroku", "web.1", R14),
            ("heroku", "web.1", CRASH),
        ]
    )
    assert post(body, auth=basic()) == "204 No Content"
    drain_config.flush()

    assert len(slack) == 1
    text = slack[0]
    assert text.startswith("*m2m-proxy*: 4 error line(s)")
    for expected in ("Unhandled exception", "code=H12", "R14", "to crashed"):
        assert expected in text
    for unexpected in ("Launch27 API 422", "Retrieved 5 teams", "status=200"):
        assert unexpected not in text


def test_warnings_reported_when_enabled(monkeypatch, drain_config, slack):
    monkeypatch.setattr(config, "REPORT_WARNINGS", True)
    post(frame(syslog("app", "web.1", JSON_WARNING_WITH_ERROR_WORD)), auth=basic())
    drain_config.flush()
    assert "0 error line(s), 1 warning line(s)" in slack[0]


def test_bursts_are_batched_into_one_post(drain_config, slack):
    for _ in range(5):
        post(frame(syslog("app", "web.1", JSON_ERROR)), auth=basic())
    drain_config.flush()
    assert len(slack) == 1
    assert "5 error line(s)" in slack[0]


def test_quiet_interval_posts_nothing(drain_config, slack):
    post(frame(syslog("heroku", "router", ROUTER_OK)), auth=basic())
    drain_config.flush()
    assert slack == []


def test_buffer_limit_summarises_overflow(monkeypatch, drain_config, slack):
    monkeypatch.setattr(config, "DRAIN_MAX_BUFFERED_LINES", 3)
    body = b"".join(frame(syslog("app", "web.1", JSON_ERROR)) for _ in range(5))
    post(body, auth=basic())
    drain_config.flush()
    assert "3 error line(s)" in slack[0]
    assert "2 more error/warning line(s)" in slack[1]


def test_retried_frame_is_not_counted_twice(drain_config, slack):
    body = frame(syslog("app", "web.1", JSON_ERROR))
    post(body, auth=basic(), frame_id="frame-1")
    post(body, auth=basic(), frame_id="frame-1")
    drain_config.flush()
    assert "1 error line(s)" in slack[0]


def test_flush_survives_slack_failure_and_reports_it(monkeypatch, drain_config, capsys):
    def boom(_text):
        raise RuntimeError("slack down")

    monkeypatch.setattr(slack_notifier, "_post_to_slack", boom)
    post(frame(syslog("app", "web.1", JSON_ERROR)), auth=basic())
    drain_config.flush()
    assert "ERROR drain flush for m2m-proxy" in capsys.readouterr().out


def test_routine_status_output_cannot_trip_the_scheduled_scan(drain_config, slack, capsys):
    import log_parser

    post(frame(syslog("app", "web.1", JSON_ERROR)), auth=basic())
    drain_config.flush()
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("drain flush:")
    line = log_parser.parse_log_text(f"2026-10-01T05:06:49+00:00 app[web.1]: {printed}")
    assert log_parser.classify(line, include_warnings=True) == ([], [])


# --- scheduled run hand-off ---------------------------------------------------

def test_scheduled_run_skips_log_scan_for_drain_apps(monkeypatch):
    monkeypatch.setattr(main.heroku_client, "get_maintenance_mode", lambda app: False)
    monkeypatch.setattr(main.heroku_client, "get_dynos", lambda app: [])

    def no_log_session(app):
        raise AssertionError("log session must not be opened for a drain app")

    monkeypatch.setattr(main.heroku_client, "create_log_session", no_log_session)
    assert main.check_app("m2m-proxy") == "ok (logs via drain, dynos_down=0)"
