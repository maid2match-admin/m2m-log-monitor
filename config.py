"""Configuration for the Heroku multi-app log monitor."""
import os

from dotenv import load_dotenv

load_dotenv()

MONITORED_APPS = [
    "lca-booking-system",
    "lca-launch27",
    "lca-proxy",
    "m2m-bookings-mcp",
    "m2m-forecast-bookings",
    "m2m-hubspot",
    "m2m-launch27",
    "m2m-log-monitor",
    "m2m-new-sales",
    "m2m-pays",
    "m2m-proxy",
    "m2m-ratings",
    "m2m-sales-data",
    "m2m-sales-report",
    "m2m-sandbox-proxy",
    "m2m-staff-retention",
    "m2m-team-details",
    "m2m-time-sheets",
    "m2m-zip2location",
    "m2mnz-bookings-mcp",
    "m2mnz-proxy",
    "m2mnz-sales-report",
    "m2mnz-team-details",
    "mlc-lead-data",
    "mlc-ratings",
]

SLACK_CHANNEL_NAME = "#m2m-system-alerts"

HEROKU_API_KEY = os.environ.get("HEROKU_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

REPORT_WARNINGS = os.environ.get("REPORT_WARNINGS", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

LOG_SESSION_LINES = int(os.environ.get("LOG_SESSION_LINES", "1500"))

LOG_LOOKBACK_HOURS = int(os.environ.get("LOG_LOOKBACK_HOURS", "6"))

DOWN_DYNO_STATES = {"crashed", "down"}

HEROKU_API_BASE = "https://api.heroku.com"
HEROKU_ACCEPT_HEADER = "application/vnd.heroku+json; version=3"

HTTP_TIMEOUT_SECONDS = 30

# --- Log drain receiver (drain_receiver.py) ---------------------------------
# Apps whose logs arrive via a Heroku HTTPS log drain instead of the 1500-line
# log-session pull. The scheduled run still checks these apps' maintenance mode
# and dyno health, but skips their log scan so lines aren't reported twice.
# Only list an app here once its drain is attached and delivering.
DRAIN_APPS = frozenset(
    name.strip()
    for name in os.environ.get("DRAIN_APPS", "").split(",")
    if name.strip()
)

# Basic-auth credentials embedded in the drain URL
# (https://DRAIN_USERNAME:DRAIN_PASSWORD@<host>/drain/<app>). An empty password
# makes the receiver reject every POST (fail closed).
DRAIN_USERNAME = os.environ.get("DRAIN_USERNAME", "logplex")
DRAIN_PASSWORD = os.environ.get("DRAIN_PASSWORD", "")

# How often buffered lines are posted to Slack, and the most lines held per app
# between posts (the excess is counted and summarised, not sent).
DRAIN_FLUSH_SECONDS = int(os.environ.get("DRAIN_FLUSH_SECONDS", "30"))
DRAIN_MAX_BUFFERED_LINES = int(os.environ.get("DRAIN_MAX_BUFFERED_LINES", "200"))

# Logplex batches are small (a few hundred lines); anything far larger is not
# a real drain delivery.
DRAIN_MAX_BODY_BYTES = 2 * 1024 * 1024
