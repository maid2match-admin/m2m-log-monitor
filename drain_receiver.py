"""Heroku HTTPS log drain receiver.

The scheduled run (main.py) pulls at most 1500 log lines per app each hour,
which is only ~30 minutes of m2m-proxy's output. Apps listed in DRAIN_APPS
instead stream every log line here as Logplex POSTs it, so nothing is missed:

    heroku drains:add \\
        "https://$DRAIN_USERNAME:$DRAIN_PASSWORD@<receiver-host>/drain/m2m-proxy" \\
        -a m2m-proxy

Each POST is parsed into the same `<ts> source[dyno]: message` lines that
`heroku logs` prints, classified by log_parser.classify() exactly like the
scheduled run, and buffered. A background thread posts the buffer to Slack
every DRAIN_FLUSH_SECONDS, so a burst of errors becomes one message, not one
per line.

Served by gunicorn with a single worker (see Procfile): the buffer and the
flush thread live in that one process.

Note on this process's own output: m2m-log-monitor is itself in
MONITORED_APPS, and the scheduled run keyword-matches its logs. Normal status
lines below deliberately avoid the words "error"/"warning", and drained log
content is never printed, so routine operation can't trigger an alert on this
app. Real receiver failures are printed with an "ERROR" prefix on purpose, so
the scheduled run does surface them.
"""
import atexit
import base64
import binascii
import json
import re
import secrets
import threading
import time
from collections import OrderedDict

import config
import log_parser
import reported_lines
import slack_notifier

# RFC 5424 header as Heroku sends it: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME
# PROCID MSGID MSG, e.g. "<190>1 2026-10-01T05:06:49.011209+00:00 host app
# web.1 - message". APP-NAME is "app" or "heroku"; PROCID is the dyno
# ("web.1") or "router".
_SYSLOG_RE = re.compile(
    r"^<\d{1,3}>\d+ (?P<timestamp>\S+) \S+ (?P<source>\S+) (?P<dyno>\S+) \S+ ?(?P<message>.*)$",
    re.DOTALL,
)

# Heroku reports a crash only as a state change, which carries none of the
# keywords classify() looks for. The scheduled run catches crashed dynos via
# the dyno API; the drain sees the event the moment it happens.
_CRASH_RE = re.compile(r"State changed from \S+ to crashed")

_SEEN_FRAME_IDS_MAX = 1000


def parse_frames(body: bytes) -> list[str]:
    """Split a Logplex octet-counted body into syslog messages.

    The body is a run of `<length> <message>` frames with no separator, where
    length is the byte count of message. A malformed frame stops parsing; the
    messages already read are kept.
    """
    messages = []
    pos = 0
    end = len(body)
    while pos < end:
        space = body.find(b" ", pos)
        if space == -1:
            break
        length_text = body[pos:space]
        if not length_text.isdigit():
            break
        length = int(length_text)
        start = space + 1
        if start + length > end:
            break
        messages.append(body[start:start + length].decode("utf-8", errors="replace"))
        pos = start + length
    return messages


def to_log_line(message: str) -> str | None:
    """Convert one syslog message to `heroku logs` format, or None if unparseable."""
    match = _SYSLOG_RE.match(message.rstrip("\n"))
    if not match:
        return None
    text = match.group("message").replace("\n", " ")
    return f"{match.group('timestamp')} {match.group('source')}[{match.group('dyno')}]: {text}"


def classify_lines(lines):
    """Same routing as the scheduled run, plus dyno-crash state changes as errors."""
    errors, warnings = log_parser.classify(lines, config.REPORT_WARNINGS)
    flagged = set(map(id, errors)) | set(map(id, warnings))
    for line in lines:
        if id(line) not in flagged and line.source == "heroku" and _CRASH_RE.search(line.message):
            errors.append(line)
    return errors, warnings


class _AppBuffer:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.dropped = 0

    def add(self, errors, warnings, limit):
        for bucket, new in ((self.errors, errors), (self.warnings, warnings)):
            room = max(0, limit - len(self.errors) - len(self.warnings))
            bucket.extend(new[:room])
            self.dropped += max(0, len(new) - room)

    def is_empty(self):
        return not (self.errors or self.warnings or self.dropped)


class DrainState:
    """Per-process buffers, frame de-duplication, and the flush thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buffers: dict[str, _AppBuffer] = {}
        self._seen_frames: OrderedDict[str, None] = OrderedDict()
        self._flusher = None
        # Health, read by the scheduled run via GET /status. Epoch seconds.
        self.started_at = time.time()
        self.last_frame_at: dict[str, float] = {}
        self.last_slack_ok_at = None
        self.last_slack_failure_at = None
        self.last_slack_failure = None

    def record_frame(self, app_name):
        with self._lock:
            self.last_frame_at[app_name] = time.time()

    def _record_slack(self, delivered, failure=None):
        with self._lock:
            if delivered:
                self.last_slack_ok_at = time.time()
            else:
                self.last_slack_failure_at = time.time()
                self.last_slack_failure = failure

    def status(self):
        with self._lock:
            return {
                "started_at": self.started_at,
                "now": time.time(),
                "last_frame_at": dict(self.last_frame_at),
                "last_slack_ok_at": self.last_slack_ok_at,
                "last_slack_failure_at": self.last_slack_failure_at,
                "last_slack_failure": self.last_slack_failure,
                "flusher_running": self._flusher is not None and self._flusher.is_alive(),
            }

    def is_duplicate_frame(self, frame_id):
        """True if this Logplex frame was already accepted (Logplex retries on failure)."""
        if not frame_id:
            return False
        with self._lock:
            if frame_id in self._seen_frames:
                return True
            self._seen_frames[frame_id] = None
            if len(self._seen_frames) > _SEEN_FRAME_IDS_MAX:
                self._seen_frames.popitem(last=False)
            return False

    def add(self, app_name, errors, warnings):
        if not (errors or warnings):
            return
        with self._lock:
            buffer = self._buffers.setdefault(app_name, _AppBuffer())
            buffer.add(errors, warnings, config.DRAIN_MAX_BUFFERED_LINES)

    def flush(self):
        """Post every non-empty buffer to Slack and reset it."""
        with self._lock:
            pending = {name: buf for name, buf in self._buffers.items() if not buf.is_empty()}
            self._buffers = {}
        for app_name, buf in pending.items():
            # Archive before posting, so the lines are kept even if Slack is down.
            self._archive(app_name, buf)
            try:
                delivered = True
                if buf.errors or buf.warnings:
                    delivered = slack_notifier.send_error_report(app_name, buf.errors, buf.warnings)
                if buf.dropped:
                    delivered = slack_notifier.send_drain_overflow(app_name, buf.dropped) and delivered
            except Exception as exc:  # noqa: BLE001 - keep flushing other apps
                self._record_slack(False, type(exc).__name__)
                print(f"ERROR drain flush for {app_name} could not post to Slack: {type(exc).__name__}")
                continue
            self._record_slack(delivered, None if delivered else "non-2xx response")
            if not delivered:
                print(f"ERROR drain flush for {app_name}: Slack rejected the post")
                continue
            print(
                f"drain flush: app={app_name} sent={len(buf.errors)}+{len(buf.warnings)} "
                f"over_limit={buf.dropped}"
            )

    @staticmethod
    def _archive(app_name, buf):
        if not config.DATABASE_URL or not (buf.errors or buf.warnings):
            return
        try:
            reported_lines.store(app_name, buf.errors, buf.warnings)
        except Exception as exc:  # noqa: BLE001 - storage must never block the Slack post
            print(f"ERROR drain flush for {app_name} could not store reported lines: {type(exc).__name__}")

    def ensure_flusher(self):
        """Start the flush thread once, lazily, so it runs in the gunicorn worker (post-fork)."""
        with self._lock:
            if self._flusher is not None:
                return
            self._flusher = threading.Thread(target=self._flush_loop, name="drain-flusher", daemon=True)
            self._flusher.start()

    def _flush_loop(self):
        while True:
            time.sleep(config.DRAIN_FLUSH_SECONDS)
            self.flush()


STATE = DrainState()
# Best effort: post what's buffered when the worker shuts down (e.g. daily dyno
# restart), instead of losing up to DRAIN_FLUSH_SECONDS of lines.
atexit.register(STATE.flush)


def is_authorized(header_value):
    """Constant-time check of the Basic credentials Logplex sends from the drain URL."""
    if not config.DRAIN_PASSWORD or not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, sep, password = decoded.partition(":")
    if not sep:
        return False
    expected = f"{config.DRAIN_USERNAME}:{config.DRAIN_PASSWORD}"
    return secrets.compare_digest(f"{username}:{password}".encode(), expected.encode())


def _respond(start_response, status, body=b"", headers=None, content_type="text/plain"):
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body)))] + (headers or []))
    return [body]


_UNAUTHORIZED = ("401 Unauthorized", b"", [("WWW-Authenticate", 'Basic realm="drain"')])


def app(environ, start_response):
    """WSGI entry point: GET / for health, POST /drain/<app> for Logplex."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "GET" and path in ("/", "/health"):
        return _respond(start_response, "200 OK", b"ok")

    if method == "GET" and path == "/status":
        if not is_authorized(environ.get("HTTP_AUTHORIZATION")):
            return _respond(start_response, *_UNAUTHORIZED)
        STATE.ensure_flusher()
        body = json.dumps(STATE.status()).encode()
        return _respond(start_response, "200 OK", body, content_type="application/json")

    if not path.startswith("/drain/"):
        return _respond(start_response, "404 Not Found")
    app_name = path[len("/drain/"):]
    if app_name not in config.DRAIN_APPS:
        return _respond(start_response, "404 Not Found")
    if method != "POST":
        return _respond(start_response, "405 Method Not Allowed", headers=[("Allow", "POST")])
    if not is_authorized(environ.get("HTTP_AUTHORIZATION")):
        return _respond(start_response, *_UNAUTHORIZED)

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > config.DRAIN_MAX_BODY_BYTES:
        return _respond(start_response, "413 Payload Too Large")

    STATE.ensure_flusher()
    STATE.record_frame(app_name)
    if STATE.is_duplicate_frame(environ.get("HTTP_LOGPLEX_FRAME_ID")):
        return _respond(start_response, "204 No Content")

    body = environ["wsgi.input"].read(length) if length else b""
    raw_lines = [line for line in map(to_log_line, parse_frames(body)) if line]
    lines = log_parser.parse_log_text("\n".join(raw_lines))
    errors, warnings = classify_lines(lines)
    STATE.add(app_name, errors, warnings)
    return _respond(start_response, "204 No Content")
