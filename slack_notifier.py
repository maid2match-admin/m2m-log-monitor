"""Slack alert delivery via an Incoming Webhook."""
import requests

import config

# Stay well under Slack's ~40,000 char message limit, leaving room for the
# code-block fencing and header text added around each chunk.
_MAX_CHUNK_CHARS = 3500


def _post_to_slack(text):
    """Send one message; return True if Slack accepted it (always True in DRY_RUN).

    A non-2xx response is printed and reported as False rather than raised, so
    one bad post never aborts a run. Network failures still raise.
    """
    if config.DRY_RUN:
        print("[DRY_RUN] Would send to Slack:\n" + text)
        return True
    response = requests.post(
        config.SLACK_WEBHOOK_URL,
        json={
            "text": text,
            "username": "M2M Log Monitor",
            "icon_emoji": ":rotating_light:",
        },
        timeout=config.HTTP_TIMEOUT_SECONDS,
    )
    if not response.ok:
        print(
            f"Failed to post to Slack ({response.status_code}): "
            f"{response.text[:500]}"
        )
    return response.ok


def _chunk_lines(raw_lines, max_chars=_MAX_CHUNK_CHARS):
    """Group raw log line strings into chunks that fit under max_chars each."""
    chunks = []
    current = []
    current_len = 0
    for raw in raw_lines:
        line_len = len(raw) + 1
        if current and current_len + line_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(raw)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks or [[]]


def send_error_report(app_name, errors, warnings):
    """Send one batched Slack report for an app's newly found error/warning lines."""
    header = f"*{app_name}*: {len(errors)} error line(s)"
    if warnings:
        header += f", {len(warnings)} warning line(s)"

    all_lines = [("ERROR", line) for line in errors] + [
        ("WARN", line) for line in warnings
    ]
    raw_lines = [f"[{tag}] {line.raw}" for tag, line in all_lines]
    chunks = _chunk_lines(raw_lines)

    total_parts = len(chunks)
    delivered = True
    for index, chunk in enumerate(chunks, start=1):
        part_suffix = f" (part {index}/{total_parts})" if total_parts > 1 else ""
        body = "\n".join(chunk)
        text = f"{header}{part_suffix}\n```\n{body}\n```"
        delivered = _post_to_slack(text) and delivered
    return delivered


def send_dyno_down(app_name, down_dynos):
    """Alert that one or more dynos for an app are crashed/down."""
    lines = [f"- {dyno.get('name', '?')}: {dyno.get('state', '?')}" for dyno in down_dynos]
    text = f"*{app_name}*: dyno(s) reported down:\n" + "\n".join(lines)
    _post_to_slack(text)


def send_resolved(app_name):
    """Alert that an app's previously reported errors have cleared."""
    text = f"*{app_name}*: no new errors since the last check (previously reported errors have cleared)."
    _post_to_slack(text)


def send_drain_overflow(app_name, dropped):
    """Note that the drain buffer filled and further lines were not sent."""
    text = (
        f"*{app_name}*: {dropped} more error/warning line(s) arrived via the log "
        "drain in this interval but were not sent (buffer limit). Check "
        f"`heroku logs -a {app_name}` for the full picture."
    )
    return _post_to_slack(text)


def send_drain_problem(app_name, problems):
    """Alert that the log drain for an app is unhealthy (scheduled run fell back to polling)."""
    lines = "\n".join(f"- {problem}" for problem in problems)
    text = (
        f"*{app_name}*: log drain is unhealthy, so this run fell back to the "
        f"1500-line log pull (partial coverage):\n{lines}"
    )
    return _post_to_slack(text)


def send_check_failure(app_name, error):
    """Alert that the monitor itself failed to check an app."""
    text = f"*{app_name}*: log monitor could not check this app: {error}"
    _post_to_slack(text)
