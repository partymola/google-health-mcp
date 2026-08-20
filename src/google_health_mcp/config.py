"""Configuration paths and constants for the Google Health MCP server."""

import os
from pathlib import Path

# Default config and data paths (XDG-compatible; override via environment variables)
_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "google-health-mcp"
_DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "google-health-mcp" / "google_health.db"

# Config and data paths (overridable via environment variables)
CONFIG_DIR = Path(os.environ.get("GOOGLE_HEALTH_MCP_CONFIG_DIR", _DEFAULT_CONFIG_DIR))
DB_PATH = Path(os.environ.get("GOOGLE_HEALTH_MCP_DB_PATH", _DEFAULT_DB_PATH))

# Offline / cache-only mode: when truthy, the server needs no credentials and
# makes no live API calls - it serves the local SQLite cache only. Useful for
# multi-host setups (one host syncs, others read the shared cache), CI, and
# privacy. See the "Offline / cache-only mode" section in the README.
OFFLINE_TRUTHY_VALUES = ("1", "true", "yes", "on")
# Only meaningful to `doctor`, which warns about a value in neither set - those
# are typos that silently leave offline mode off. Anything here reads as "off"
# deliberately and must not be reported as a mistake.
OFFLINE_FALSY_VALUES = ("0", "false", "no", "off")
OFFLINE_MODE = (
    os.environ.get("GOOGLE_HEALTH_MCP_OFFLINE", "").strip().lower() in OFFLINE_TRUTHY_VALUES
)

# Google Health API. The client file is Google's downloaded Desktop-client
# JSON, read as-is rather than reshaped, so a user can drop it in unedited.
GOOGLE_CLIENT_PATH = CONFIG_DIR / "google_client.json"
GOOGLE_TOKENS_PATH = CONFIG_DIR / "google_tokens.json"
GOOGLE_API_BASE = "https://health.googleapis.com/v4"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Read-only, and never a writeonly scope.
#
# A grant does not gain scopes on refresh, so a scope left out costs every
# user a second consent. That is not licence to ask for everything: a scope
# with no reader does not belong here, and exercise GPS (location) is the
# case in point - it is a location history, and it stays out.
#
# The scope list published in the developer docs, and the one in the API's own
# discovery document, are both incomplete - several readonly scopes selectable
# in the Cloud console appear in neither. Check the console, not either page.
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly "
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly "
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly "
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly "
    "https://www.googleapis.com/auth/googlehealth.ecg.readonly "
    "https://www.googleapis.com/auth/googlehealth.irn.readonly "
    "https://www.googleapis.com/auth/googlehealth.settings.readonly"
)
GOOGLE_CALLBACK_PORT = 8081
GOOGLE_REDIRECT_URI = f"http://localhost:{GOOGLE_CALLBACK_PORT}"

# An access token lasts an hour, which is what `expires_in` reports when the
# response omits it.
GOOGLE_TOKEN_LIFETIME = 3600

# Every type the cache holds. Read by the CLI's `--types` help and by
# `doctor`'s freshness check, and asserted equal to `GOOGLE_SYNC_HANDLERS` by
# a seam test - a type here with no handler is a table nothing can ever fill.
#
# It does NOT drive the sync: both `all` expansions read the handler map, so
# the order here is the order the help text prints, not the order a sync runs.
# Trends and compare validate against `_TREND_FNS`, which is deliberately
# narrower - ECG readings and rhythm alerts are episodes with no daily series.
CACHED_DATA_TYPES = (
    "heart_rate",
    "activity",
    "exercises",
    "sleep",
    "weight",
    "spo2",
    "hrv",
    "azm",
    "breathing_rate",
    "skin_temperature",
    "core_temperature",
    "cardio_fitness",
    "food_log",
    "ecg",
    "irn",
)
