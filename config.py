"""
config.py — Central configuration for ChatPSA.

All environment-driven constants live here. Every other module imports from
this file so that nothing needs to import from app.py (which would be
circular) and configuration is never duplicated.
"""

import logging
import os

# ── App identity ──────────────────────────────────────────────────────────────

APP_NAME = os.environ.get("APP_NAME", "ChatPSA")

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CW_DB_PATH", os.path.join(APP_DIR, "cw_data.db"))

# Agent memories live in their own file so they are never locked by the
# sync containers that write to cw_data.db.
MEMORIES_DB_PATH = os.path.join(os.path.dirname(DB_PATH), "agent_memories.db")

# ── Query limits ──────────────────────────────────────────────────────────────

MAX_ROWS = 50
MAX_CONVERSATION_TURNS = 10

# Safety cap: approximate token budget for conversation history.
# When history exceeds this, oldest messages are dropped before the API call.
MAX_HISTORY_TOKENS = 150_000
MAX_QUERY_PINS = 10

# ── ConnectWise / Helpdesk settings ───────────────────────────────────────────

# Name of the primary helpdesk board in ConnectWise.
HELPDESK_BOARD = os.environ.get("HELPDESK_BOARD", "Help Desk")

# Company name(s) to exclude from anomaly/trend queries (comma-separated).
# Typically set to the MSP's own company name so internal tickets don't skew
# trends.
TRENDS_EXCLUDE_COMPANIES = [
    c.strip()
    for c in os.environ.get("TRENDS_EXCLUDE_COMPANIES", "").split(",")
    if c.strip()
]

# Validate config values used in SQL interpolation (insights.py Postgres queries).
# These come from env vars and are interpolated into f-strings, so reject anything
# that could break out of a SQL string literal.
import re as _re
_SQL_SAFE = _re.compile(r"^[a-zA-Z0-9 &'()./_-]+$")
# Priority names that count as "high priority" for the stale-ticket insight.
# If TRENDS_HIGH_PRIORITIES is set, it's used as-is (comma-separated).
# Otherwise, any priority containing "Critical" or "High" (case-insensitive) matches.
_raw_priorities = os.environ.get("TRENDS_HIGH_PRIORITIES", "")
TRENDS_HIGH_PRIORITIES = [
    p.strip()
    for p in _raw_priorities.split(",")
    if p.strip()
]

if not _SQL_SAFE.match(HELPDESK_BOARD):
    raise RuntimeError(f"HELPDESK_BOARD contains unsafe characters: {HELPDESK_BOARD!r}")
for _company in TRENDS_EXCLUDE_COMPANIES:
    if not _SQL_SAFE.match(_company):
        raise RuntimeError(f"TRENDS_EXCLUDE_COMPANIES contains unsafe value: {_company!r}")
for _prio in TRENDS_HIGH_PRIORITIES:
    if not _SQL_SAFE.match(_prio):
        raise RuntimeError(f"TRENDS_HIGH_PRIORITIES contains unsafe value: {_prio!r}")

# ── Claude model ──────────────────────────────────────────────────────────────

# Single source of truth. Override with CLAUDE_MODEL env var.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4096"))

# ── Azure AD / Entra ID ───────────────────────────────────────────────────────

AZURE_CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET")
AZURE_TENANT_ID     = os.environ.get("AZURE_TENANT_ID")
AZURE_ENABLED       = all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID])
AZURE_AUTHORITY     = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}" if AZURE_ENABLED else None
AZURE_SCOPES        = ["User.Read"] if AZURE_ENABLED else []

# ── Azure AD runtime checks ──────────────────────────────────────────────────
# Dynamic functions that check DB-managed settings (updated via Admin UI)
# with env var fallback.  These allow credential rotation without restarting.

def is_azure_enabled():
    """True if Azure AD authentication is fully configured.

    Checks DB-managed settings first (updated via Admin UI), falling back
    to env vars.  This allows credentials to be updated at runtime.
    """
    from settings import get_setting
    client_id = get_setting("azure_client_id") or ""
    client_secret = get_setting("azure_client_secret") or ""
    tenant_id = get_setting("azure_tenant_id") or ""
    return bool(client_id and client_secret and tenant_id)


def get_azure_credentials():
    """Return (client_id, client_secret, tenant_id) from DB-first settings.

    Falls back to env vars for each value if the DB row is empty.
    Used by auth.py and secret_expiry.py instead of static imports.
    """
    from settings import get_setting
    return (
        get_setting("azure_client_id") or "",
        get_setting("azure_client_secret") or "",
        get_setting("azure_tenant_id") or "",
    )


def get_azure_credentials_with_fallback():
    """Return (client_id, client_secret, previous_secret, tenant_id).

    Includes the previous client secret for rotation fallback.
    """
    from settings import get_setting
    return (
        get_setting("azure_client_id") or "",
        get_setting("azure_client_secret") or "",
        get_setting("azure_client_secret_previous") or "",
        get_setting("azure_tenant_id") or "",
    )


# ── CIPP / M365 integration ───────────────────────────────────────────────────
# Static flags (for sync scripts that import config directly at startup)
CIPP_ENABLED = all([
    os.environ.get("CIPP_CLIENT_ID"),
    os.environ.get("CIPP_CLIENT_SECRET"),
    os.environ.get("CIPP_API_URL"),
])

# ── Duo Security ─────────────────────────────────────────────────────────────
DUO_ENABLED = all([
    os.environ.get("DUO_IKEY"),
    os.environ.get("DUO_SKEY"),
    os.environ.get("DUO_HOST"),
])

# ── Runtime service checks ───────────────────────────────────────────────────
# These functions check both env vars AND DB-managed settings, so credentials
# added via the admin UI are picked up without a restart.

def is_cipp_enabled():
    """True if CIPP is configured (via env vars or admin settings)."""
    from settings import get_setting
    return all([
        get_setting("cipp_client_id") or os.environ.get("CIPP_CLIENT_ID"),
        get_setting("cipp_client_secret") or os.environ.get("CIPP_CLIENT_SECRET"),
        get_setting("cipp_api_url") or os.environ.get("CIPP_API_URL"),
    ])

def is_duo_enabled():
    """True if Duo is configured (via env vars or admin settings)."""
    from settings import get_setting
    return all([
        get_setting("duo_ikey") or os.environ.get("DUO_IKEY"),
        get_setting("duo_skey") or os.environ.get("DUO_SKEY"),
        get_setting("duo_host") or os.environ.get("DUO_HOST"),
    ])

def is_huntress_enabled():
    """True if Huntress is configured (via env vars or admin settings)."""
    from settings import get_setting
    key = (get_setting("huntress_api_key") or os.environ.get("HUNTRESS_API_KEY") or "").strip()
    secret = (get_setting("huntress_api_secret") or os.environ.get("HUNTRESS_API_SECRET") or "").strip()
    return bool(key and secret)

def is_threatlocker_enabled():
    """True if ThreatLocker is configured (via env vars or admin settings)."""
    from settings import get_setting
    key = (get_setting("threatlocker_api_key") or os.environ.get("THREATLOCKER_API_KEY") or "").strip()
    return bool(key)

# ── Timezone ──────────────────────────────────────────────────────────────────

from datetime import datetime as _dt, timezone, timedelta
from zoneinfo import ZoneInfo

# IANA timezone name (e.g. "Australia/Brisbane", "America/New_York").
# Falls back to constructing a fixed-offset zone from APP_TZ_OFFSET for
# backwards compatibility with deployments that only set the numeric offset.
APP_TZ_NAME = os.environ.get("APP_TZ_NAME", "").strip()
_APP_TZ_OFFSET_ENV = os.environ.get("APP_TZ_OFFSET", "").strip()

if APP_TZ_NAME:
    try:
        APP_TZ = ZoneInfo(APP_TZ_NAME)
    except KeyError:
        import logging as _tz_log
        _tz_log.getLogger("chatpsa.config").error(
            "Invalid APP_TZ_NAME '%s'. Use an IANA name like "
            "'Australia/Brisbane' or 'America/New_York'. "
            "See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones  "
            "Falling back to Australia/Brisbane.", APP_TZ_NAME)
        APP_TZ_NAME = "Australia/Brisbane"
        APP_TZ = ZoneInfo(APP_TZ_NAME)
elif _APP_TZ_OFFSET_ENV:
    APP_TZ_NAME = ""  # no IANA name — fixed offset mode
    try:
        APP_TZ = timezone(timedelta(hours=float(_APP_TZ_OFFSET_ENV)))
    except (ValueError, OverflowError):
        import logging as _tz_log
        _tz_log.getLogger("chatpsa.config").error(
            "Invalid APP_TZ_OFFSET '%s'. Must be a number (e.g. 10, -5, 5.5). "
            "Falling back to Australia/Brisbane.", _APP_TZ_OFFSET_ENV)
        APP_TZ_NAME = "Australia/Brisbane"
        APP_TZ = ZoneInfo(APP_TZ_NAME)
else:
    APP_TZ_NAME = "Australia/Brisbane"
    APP_TZ = ZoneInfo(APP_TZ_NAME)


def get_tz_offset():
    """Current UTC offset in hours for the configured timezone (DST-aware).

    Returns a float to handle half-hour zones (e.g. 5.5 for India, 5.75 for Nepal).
    """
    now = _dt.now(APP_TZ)
    return now.utcoffset().total_seconds() / 3600


def get_tz_offset_sql():
    """SQLite-compatible offset string, e.g. "'+10 hours'" or "'+5.5 hours'".

    Computed from the current UTC offset so it adjusts automatically for DST.
    Uses decimal hours for half-hour zones (e.g. India +5.5, Nepal +5.75).
    """
    total_seconds = _dt.now(APP_TZ).utcoffset().total_seconds()
    hours = total_seconds / 3600
    sign = "+" if hours >= 0 else ""
    if hours == int(hours):
        return f"'{sign}{int(hours)} hours'"
    return f"'{sign}{hours} hours'"


def get_tz_label():
    """Human-readable timezone label, e.g. 'AEST' or 'UTC+10'."""
    now = _dt.now(APP_TZ)
    # ZoneInfo zones have abbreviation via tzname(); fixed-offset zones don't
    tzname = now.tzname()
    if tzname and not tzname.startswith("UTC"):
        return tzname
    offset_h = get_tz_offset()
    sign = "+" if offset_h >= 0 else ""
    if offset_h == int(offset_h):
        return f"UTC{sign}{int(offset_h)}"
    return f"UTC{sign}{offset_h}"


# Legacy constant — kept for any code that reads it directly.
# Prefer get_tz_offset() for DST-aware behaviour.
APP_TZ_OFFSET = int(get_tz_offset())

# ── Logging ───────────────────────────────────────────────────────────────────
# Configured once here; every module gets its own child logger via
# logging.getLogger("chatpsa.<module>") which inherits this level/format.

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
