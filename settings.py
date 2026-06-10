"""settings.py — DB-managed application settings with env override.

Provides get_setting() and set_setting() for runtime-configurable values.

Precedence:
  1. If ENV_OVERRIDE=true in the environment, env vars always win.
  2. Otherwise, the app_settings DB table is the source of truth.
  3. If the DB row is missing (fresh deploy, table not created yet),
     falls back to the env var / hardcoded default silently.

The in-process cache has a 30-second TTL so admin changes propagate
without requiring a restart, while avoiding a DB hit on every request.
"""
import hashlib
import logging
import os
import sqlite3
import time

from config import DB_PATH

log = logging.getLogger("chatpsa.settings")

# ── ENV_OVERRIDE mode ────────────────────────────────────────────────────────
# When true, all configurable settings are read from env vars and the DB
# table is ignored entirely.  Useful for deployments managed via Docker
# env files or CI/CD pipelines.
ENV_OVERRIDE = os.environ.get("ENV_OVERRIDE", "").lower() in ("true", "1", "yes")

# ── In-process cache ─────────────────────────────────────────────────────────
_cache: dict = {}       # key -> value (string)
_cache_ts: float = 0    # monotonic timestamp of last refresh
_CACHE_TTL = 30         # seconds


# Setting metadata — imported from db.py to keep the registry in one place.
from db import CONFIGURABLE_SETTINGS


def _mask_identifier(identifier: str) -> str:
    """Return a deterministic, non-reversible short hash of an identifier.

    Useful for correlating log entries without exposing PII (e.g. email).
    """
    if not identifier:
        return "unknown"
    return hashlib.sha256(identifier.encode()).hexdigest()[:12]


def _refresh_cache():
    """Reload all settings from the DB into the process cache."""
    global _cache, _cache_ts
    try:
        if not os.path.exists(DB_PATH):
            _cache_ts = time.monotonic()
            return
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
            _cache = {r["key"]: r["value"] for r in rows}
        finally:
            conn.close()
    except Exception as e:
        log.debug("settings cache refresh failed (will use env fallback): %s", e)
    _cache_ts = time.monotonic()


def invalidate_cache():
    """Force the next get_setting() call to re-read from the DB."""
    global _cache_ts
    _cache_ts = 0


def get_setting(key: str, default: str | None = None) -> str:
    """Read a single setting value.

    Args:
        key: The setting key (e.g. "app_name", "helpdesk_board").
        default: Fallback if the key is not found anywhere. If None, uses
                 the hardcoded default from CONFIGURABLE_SETTINGS.

    Returns:
        The setting value as a string.
    """
    meta = CONFIGURABLE_SETTINGS.get(key, {})
    env_var = meta.get("env", "")
    hardcoded_default = meta.get("default", "")

    # Resolve the fallback: caller's default > env var > hardcoded default
    if default is None:
        default = os.environ.get(env_var, hardcoded_default) if env_var else hardcoded_default

    # ENV_OVERRIDE mode: use env var if it's actually set in the environment.
    # If the var is missing from the env, fall through to the DB cache —
    # seed_app_settings() preserves DB values for missing env vars, so
    # reading them here keeps the two paths consistent.
    if ENV_OVERRIDE and env_var:
        env_val = os.environ.get(env_var)
        if env_val is not None:
            return env_val
        # env var not in environment — fall through to DB

    # Normal mode: check DB cache
    if (time.monotonic() - _cache_ts) > _CACHE_TTL:
        _refresh_cache()

    if key in _cache:
        return _cache[key]

    # DB miss — fall back to env var / default
    return os.environ.get(env_var, default) if env_var else default


def get_setting_int(key: str, default: int = 0) -> int:
    """Convenience: read a setting and parse as int."""
    try:
        return int(get_setting(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_setting_list(key: str) -> list[str]:
    """Convenience: read a comma-separated setting as a list of stripped strings."""
    raw = get_setting(key, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


SECRET_MASK = "••••••••"


def set_setting(key: str, value: str, updated_by: str = ""):
    """Write a setting to the DB and invalidate the cache.

    Only works when ENV_OVERRIDE is not active.
    Rejects keys not registered in CONFIGURABLE_SETTINGS.
    Skips writes when a secret field is submitted unchanged (masked value).
    """
    if ENV_OVERRIDE:
        log.warning("set_setting(%s) ignored — ENV_OVERRIDE is active", key)
        return False

    if key not in CONFIGURABLE_SETTINGS:
        log.warning("set_setting: unknown key %r rejected", key)
        return False

    # Don't overwrite a secret with its own mask — user didn't change it
    meta = CONFIGURABLE_SETTINGS[key]
    if meta.get("type") == "secret" and value == SECRET_MASK:
        return True

    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=5000")

            # When rotating the Azure client secret, save the old value as
            # a fallback so auth keeps working during the transition window.
            if key == "azure_client_secret" and value != SECRET_MASK:
                old_row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (key,)
                ).fetchone()
                if old_row and old_row[0] and old_row[0] != value:
                    conn.execute("""
                        INSERT INTO app_settings (key, value, updated_at, updated_by)
                        VALUES (?, ?, datetime('now'), ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at,
                            updated_by = excluded.updated_by
                    """, ("azure_client_secret_previous", old_row[0], updated_by))

            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at, updated_by)
                VALUES (?, ?, datetime('now'), ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
            """, (key, value, updated_by))
            conn.commit()
        invalidate_cache()
        # Don't log secret values
        display_val = "••••••••" if meta.get("type") == "secret" else value[:100]
        log.info("set_setting: %s = %s (by %s)", key, display_val, _mask_identifier(updated_by))
        return True
    except Exception as e:
        log.warning("set_setting error: %s", e)
        return False



def get_all_settings() -> list[dict]:
    """Return all configurable settings with their current values and metadata.

    Used by the admin settings UI.  Secret values are masked in the response
    so credentials are never sent to the browser.  Relative help_url paths
    are resolved against docs_base_url so forks get links to their own docs.

    The azure_secret_expiry field is auto-populated from the Graph API when
    Application.Read.All is granted and no manual date has been saved.
    """
    # Resolve relative help_url paths against the docs base URL
    docs_base = (get_setting("docs_base_url", "") or "").rstrip("/")

    result = []
    for key, meta in CONFIGURABLE_SETTINGS.items():
        is_secret = meta.get("type") == "secret"
        raw_value = get_setting(key)

        # Auto-populate azure_secret_expiry from Graph when DB is empty
        auto_detected = False
        if key == "azure_secret_expiry" and not raw_value:
            try:
                from secret_expiry import get_active_expiry_date
                graph_date = get_active_expiry_date()
                if graph_date:
                    raw_value = graph_date
                    auto_detected = True
            except Exception:
                pass  # Graph unavailable — field stays empty

        # Resolve help_url: absolute URLs pass through, relative paths
        # get prefixed with docs_base_url
        help_url = meta.get("help_url", "")
        if help_url and not help_url.startswith("http"):
            help_url = f"{docs_base}/{help_url}" if docs_base else ""

        entry = {
            "key": key,
            "label": meta["label"],
            "description": meta.get("description", ""),
            "type": meta.get("type", "text"),
            "group": meta.get("group", ""),
            "value": SECRET_MASK if (is_secret and raw_value) else raw_value,
            "has_value": bool(raw_value) if is_secret else None,
            "env_var": meta.get("env", ""),
            "default": meta.get("default", ""),
            "validate": meta.get("validate"),
            "help_url": help_url,
        }
        if auto_detected:
            entry["auto_detected"] = True
        result.append(entry)
    return result
