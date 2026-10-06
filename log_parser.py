"""Parsing and classification of Heroku Logplex log output.

A Heroku log session returns plain text, one record per line, in the same
format `heroku logs` prints, e.g.:

    2024-05-01T12:00:00.123456+00:00 app[web.1]: Something happened
    2024-05-01T12:00:00.654321+00:00 heroku[web.1]: State changed from up to crashed
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

LINE_RE = re.compile(
    r"^(?P<timestamp>\S+)\s+"
    r"(?P<source>[^\[\s]+)\[(?P<dyno>[^\]]+)\]:\s*"
    r"(?P<message>.*)$"
)

ERROR_RE = re.compile(
    r"\b(error|err|exception|traceback|critical|fatal)\b", re.IGNORECASE
)
WARNING_RE = re.compile(r"\b(warn|warning)\b", re.IGNORECASE)

# Known-noisy lines that match ERROR_RE/WARNING_RE but aren't worth reporting,
# e.g. internet port-scanners probing Heroku Postgres addons, and vulnerability
# scanners probing web dynos for CMS installs that were never there.
NOISE_PATTERNS = (
    "no pg_hba.conf entry for host",
    # Malformed/non-Postgres startup packets: a real app always speaks valid
    # Postgres protocol, so these can only be raw connection scanners.
    "unsupported frontend protocol",
    "no PostgreSQL user name specified in startup packet",
    # Heroku Postgres addon users are randomly generated, never "postgres",
    # so a failed login as that literal username is a scanner, not the app.
    'password authentication failed for user "postgres"',
    # WordPress/CMS vulnerability probes hitting web dynos that never ran
    # WordPress; the requested filename can vary (e.g. one containing the
    # substring "exception", which is why this was slipping past as an error).
    "/wp-",
    # google-auth's best-effort Regional Access Boundary (trust boundary)
    # lookup on service-account token refresh; failure is swallowed by the
    # library and the credential refresh/API call still succeeds regardless
    # (https://github.com/googleapis/google-cloud-python/issues/17515).
    "Regional Access Boundary HTTP request failed after retries",
)
NOISE_RE = re.compile("|".join(re.escape(p) for p in NOISE_PATTERNS), re.IGNORECASE)

# Heroku router and uvicorn access-log lines carry their own explicit severity
# marker, which is authoritative over incidental ERROR_RE/WARNING_RE keyword
# hits elsewhere in the line (e.g. a scanner requesting "/error.php" or
# "/wp-includes/registration-exception.php" is still just an INFO-level
# access-log entry). The router only uses at=error/at=warning for its own
# routing problems (timeouts, no web dynos, etc.), never merely because the
# upstream app returned a non-2xx status; uvicorn's access logger is always
# INFO regardless of response status, with real exceptions logged separately.
ROUTER_INFO_RE = re.compile(r"(?:^|\s)at=info(?:\s|$)")
ACCESS_LOG_INFO_RE = re.compile(r"^INFO:\s")

# Upstream apps (m2m-proxy, m2m-sandbox-proxy, ...) emit structured JSON logs
# whose top-level "level" field is the authoritative severity. When a line body
# is valid JSON with a string "level", that level decides routing and the
# keyword heuristics below are never consulted — otherwise the word "Error"
# appearing in a WARNING line's `message` (or a nested `data.error`) would
# misroute it to the alert channel. See classify().
JSON_ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})
JSON_WARNING_LEVELS = frozenset({"WARNING", "WARN"})


@dataclass(frozen=True)
class LogLine:
    timestamp: datetime | None
    source: str
    dyno: str
    message: str
    raw: str

    @property
    def hash(self):
        return hashlib.sha256(self.raw.encode("utf-8", errors="replace")).hexdigest()


def _parse_timestamp(raw_timestamp):
    try:
        text = raw_timestamp
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def parse_log_text(raw_text):
    """Parse raw Logplex text into a list of LogLine records, in file order.

    Lines that don't match the expected `source[dyno]: message` shape are
    skipped (e.g. blank lines, partial frames) rather than raising.
    """
    lines = []
    for raw_line in raw_text.splitlines():
        if not raw_line.strip():
            continue
        match = LINE_RE.match(raw_line)
        if not match:
            continue
        lines.append(
            LogLine(
                timestamp=_parse_timestamp(match.group("timestamp")),
                source=match.group("source"),
                dyno=match.group("dyno"),
                message=match.group("message"),
                raw=raw_line,
            )
        )
    return lines


def is_error_line(line: LogLine) -> bool:
    return bool(ERROR_RE.search(line.message))


def is_warning_line(line: LogLine) -> bool:
    return bool(WARNING_RE.search(line.message))


def is_noise_line(line: LogLine) -> bool:
    return bool(NOISE_RE.search(line.message))


def has_benign_explicit_level(line: LogLine) -> bool:
    return bool(ROUTER_INFO_RE.search(line.message) or ACCESS_LOG_INFO_RE.match(line.message))


def parse_json_level(line: LogLine) -> str | None:
    """Return the upper-cased top-level "level" of a JSON log body, else None.

    Upstream apps emit structured JSON (`{"level": "WARNING", "message": ...}`)
    where `level` is the authoritative severity. `LINE_RE` has already stripped
    Heroku's `<ts> app[dyno]:` prefix, so `line.message` is the JSON payload —
    but locate the first `{` defensively in case of leading text, and use
    `raw_decode` so trailing text after the object doesn't break parsing.

    Returns None when the body isn't a JSON object with a string `"level"`
    (i.e. Heroku platform lines, uvicorn plaintext, tracebacks) so the caller
    falls back to the keyword heuristics.
    """
    start = line.message.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(line.message[start:])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    level = obj.get("level")
    if not isinstance(level, str):
        return None
    return level.strip().upper()


def classify(lines, include_warnings):
    """Split lines into (errors, warnings) preserving order.

    Lines matching `NOISE_RE` (known-noisy but not actionable, e.g. Postgres
    port-scan rejections) are dropped first. For the rest, structured JSON
    logs win: if a line body is valid JSON with a string `"level"`, that level
    alone decides routing (ERROR/CRITICAL -> errors, WARNING/WARN -> warnings,
    DEBUG/INFO -> dropped) and the keyword heuristics are never consulted — so
    a WARNING line whose `message` (or nested `data.error`) contains the word
    "Error" can never be misrouted to the error bucket.

    Only non-JSON lines fall back to the keyword heuristics: a benign explicit
    level (Heroku router `at=info`, uvicorn access-log `INFO:`) is dropped, and
    the remainder are matched against `ERROR_RE`/`WARNING_RE`, regardless of
    incidental keyword hits elsewhere in the line (e.g. a scanner-requested
    path containing "error"). A line matching both error and warning patterns
    is counted only as an error. `warnings` is always `[]` when
    `include_warnings` is False.
    """
    errors = []
    warnings = []
    for line in lines:
        if is_noise_line(line):
            continue
        level = parse_json_level(line)
        if level is not None:
            if level in JSON_ERROR_LEVELS:
                errors.append(line)
            elif include_warnings and level in JSON_WARNING_LEVELS:
                warnings.append(line)
            continue
        if has_benign_explicit_level(line):
            continue
        if is_error_line(line):
            errors.append(line)
        elif include_warnings and is_warning_line(line):
            warnings.append(line)
    return errors, warnings


def unpaginated_call_warnings(lines):
    """Select structured proxy audit events independently of Slack routing."""
    selected = []
    for line in lines:
        if parse_json_level(line) not in JSON_WARNING_LEVELS:
            continue
        start = line.message.find("{")
        try:
            value, _ = json.JSONDecoder().raw_decode(line.message[start:])
        except ValueError:
            continue
        if value.get("event_type") == "unpaginated_call":
            selected.append(line)
    return selected


def newest_line(lines):
    """Return the line with the latest timestamp, breaking ties by list order.

    Falls back to the last line in the list if none have a parseable
    timestamp (so the watermark still advances).
    """
    timestamped = [line for line in lines if line.timestamp is not None]
    if not timestamped:
        return lines[-1] if lines else None
    return max(timestamped, key=lambda line: line.timestamp)


def filter_since(lines, cutoff):
    """Return only lines at or after `cutoff`; lines with no parseable timestamp are kept.

    Bounds how far back a report can reach after a watermark reset or a
    first-ever run, so results aren't dominated by log lines from days
    earlier still sitting in Heroku's rolling log buffer.
    """
    return [line for line in lines if line.timestamp is None or line.timestamp >= cutoff]


def lines_after(lines, after_timestamp, after_hash):
    """Return lines newer than the given watermark.

    `after_timestamp` may be None (no prior watermark -> return all lines).
    Lines with no parseable timestamp are always included, since they can't
    be reliably compared against the watermark.
    """
    if after_timestamp is None:
        return list(lines)
    kept = []
    for line in lines:
        if line.timestamp is None:
            kept.append(line)
            continue
        if line.timestamp > after_timestamp:
            kept.append(line)
        elif line.timestamp == after_timestamp and line.hash != after_hash:
            kept.append(line)
    return kept
