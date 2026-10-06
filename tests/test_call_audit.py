import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time

import pytest

import config
import drain_receiver
import log_parser
import main
import reported_lines
from test_drain_receiver import basic, frame, post, syslog, _pulled_app

EVENT = json.dumps({"level":"WARNING","event_type":"unpaginated_call",
                    "message":"Unpaginated call to /v1/staff/bookings/range",
                    "extra":{"ip_address":"192.0.2.1","user_agent":"test"}})


def audit_line():
    return log_parser.parse_log_text("2099-01-01T00:00:00+00:00 app[web.1]: "+EVENT)[0]


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setattr(config,"DRAIN_APPS",frozenset({"m2m-proxy"}))
    monkeypatch.setattr(config,"DRAIN_USERNAME","logplex")
    monkeypatch.setattr(config,"DRAIN_PASSWORD","s3cret")
    monkeypatch.setattr(config,"REPORT_WARNINGS",False)
    state = drain_receiver.DrainState()
    monkeypatch.setattr(state,"ensure_flusher",lambda:None)
    monkeypatch.setattr(drain_receiver,"STATE",state)
    return state


def test_targeted_archive_independent_of_slack(receiver,monkeypatch):
    stored=[]
    monkeypatch.setattr(reported_lines,"store_call_audit",lambda app,lines: stored.extend(lines) or len(lines))
    monkeypatch.setattr(drain_receiver.slack_notifier,"send_error_report",lambda *_:pytest.fail("no Slack warning"))
    body=frame(syslog("app","web.1",EVENT))
    assert post(body,auth=basic(),frame_id="one") == "204 No Content"
    assert post(body,auth=basic(),frame_id="one") == "204 No Content"
    receiver.flush()
    assert len(stored)==1 and receiver.last_audit_ok_at is not None


@pytest.mark.parametrize("result",[0,None])
def test_incomplete_storage_never_acks_or_commits_frame(receiver,monkeypatch,result):
    monkeypatch.setattr(reported_lines,"store_call_audit",lambda *_:result)
    body=frame(syslog("app","web.1",EVENT))
    assert post(body,auth=basic(),frame_id="retry") == "503 Service Unavailable"
    assert "retry" not in receiver._seen_frames
    monkeypatch.setattr(reported_lines,"store_call_audit",lambda app,lines:len(lines))
    assert post(body,auth=basic(),frame_id="retry") == "204 No Content"


def test_database_failure_allows_frame_retry(receiver,monkeypatch):
    def failed(*_): raise RuntimeError("unavailable")
    monkeypatch.setattr(reported_lines,"store_call_audit",failed)
    body=frame(syslog("app","web.1",EVENT))
    assert post(body,auth=basic(),frame_id="retry") == "503 Service Unavailable"
    assert receiver.last_audit_failure_at is not None and "retry" not in receiver._seen_frames
    monkeypatch.setattr(reported_lines,"store_call_audit",lambda app,lines:len(lines))
    assert post(body,auth=basic(),frame_id="retry") == "204 No Content"


def test_missing_database_fails_closed(receiver,monkeypatch):
    monkeypatch.setattr(config,"DRY_RUN",False)
    assert post(frame(syslog("app","web.1",EVENT)),auth=basic(),frame_id="retry") == "503 Service Unavailable"
    assert "retry" not in receiver._seen_frames


def test_concurrent_retry_persists_once(receiver,monkeypatch):
    stored=[]
    def slow(app,lines):
        time.sleep(0.02)
        stored.extend(lines)
        return len(lines)
    monkeypatch.setattr(reported_lines,"store_call_audit",slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:receiver.accept_frame("same","m2m-proxy",[audit_line()]),range(2)))
    assert sorted(results)==[False,True] and len(stored)==1


def test_scheduled_audit_failure_does_not_advance_watermark(monkeypatch):
    monkeypatch.setattr(config,"DRAIN_APPS",frozenset())
    monkeypatch.setattr(config,"DATABASE_URL","postgres://fake")
    _pulled_app(monkeypatch,"2099-01-01T00:00:00+00:00 app[web.1]: "+EVENT)
    moved=[]
    monkeypatch.setattr(main.state_store,"set_last_state",lambda *args:moved.append(args))
    monkeypatch.setattr(reported_lines,"store_call_audit",lambda *_:0)
    with pytest.raises(RuntimeError,match="incomplete"):
        main.check_app("m2m-sandbox-proxy")
    assert moved==[]


def test_plaintext_info_and_other_warning_are_not_audit_events():
    other=log_parser.parse_log_text('2099-01-01T00:00:00Z app[web.1]: {"level":"WARNING","event_type":"performance"}\n2099-01-01T00:00:00Z app[web.1]: INFO Unpaginated call\n')
    assert log_parser.unpaginated_call_warnings(other)==[]


def test_audit_storage_has_connection_and_statement_deadlines(monkeypatch):
    monkeypatch.setattr(config,"DATABASE_URL","postgres://fake")
    monkeypatch.setattr(config,"DRY_RUN",False)
    observed={}
    class Cursor:
        rowcount=1
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def executemany(self,*_): pass
        def execute(self,*_): pass
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def cursor(self): return Cursor()
        def commit(self): observed["committed"]=True
    def connect(url,**kwargs):
        observed.update(kwargs)
        return Connection()
    monkeypatch.setattr(reported_lines.psycopg,"connect",connect)
    assert reported_lines.store_call_audit("m2m-proxy",[audit_line()])==1
    assert observed=={"connect_timeout":5,"options":"-c statement_timeout=3000 -c lock_timeout=2000","committed":True}
