"""secret_expiry.py — Azure AD client-secret expiry tracking.

Two modes (automatic fallback):

  1. **Auto-detection** — uses Microsoft Graph API to query the app
     registration and read passwordCredential expiry dates.  Requires
     Application.Read.All (application permission, admin-consented).
     When multiple secrets exist, the hint field is matched against
     AZURE_CLIENT_SECRET to identify the active credential.

  2. **Manual date** — admin enters the expiry date in Admin → Settings.
     No extra Azure permissions needed.

Precedence: if Graph returns a matching credential, its date is used
and auto-populated into the settings UI.  If Graph fails (403, no
permission, network error), the manual date is used as a fallback.
Results are cached in-process (default 6 hours).
"""

import logging
import re
import time
from datetime import datetime, timezone

log = logging.getLogger("chatpsa.secret_expiry")

# ── In-process cache ────────────────────────────────────────────────────────
_cache: list | None = None
_cache_ts: float = 0
_CACHE_TTL = 6 * 3600  # 6 hours


def _get_graph_token(client_id: str, client_secret: str, tenant_id: str) -> tuple[str | None, str | None]:
    """Acquire a Graph API token via MSAL client-credentials flow.

    Returns (token, auth_error):
      - (token, None)  — success
      - (None, None)   — network/transient error (couldn't reach Azure)
      - (None, message) — authentication failed (bad credentials)
    """
    try:
        import msal
        app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" in result:
            return result["access_token"], None

        error_code = result.get("error", "")
        error_desc = result.get("error_description", "")
        log.warning("Graph token acquisition failed: %s — %s", error_code, error_desc)

        # Distinguish auth failures (bad secret) from other errors
        auth_errors = ("invalid_client", "unauthorized_client")
        auth_codes = ("AADSTS7000215", "AADSTS700016", "AADSTS90002")
        if error_code in auth_errors or any(c in error_desc for c in auth_codes):
            return None, f"Authentication failed ({error_code})"

        return None, None
    except Exception as e:
        log.warning("Graph token acquisition error: %s", e)
        return None, None


def _query_app_credentials(token: str, client_id: str) -> list[dict] | None:
    """Query Graph API for an app registration's password credentials.

    Returns a list of credential dicts, or None on failure / 403.
    """
    import requests
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/applications",
            params={
                "$filter": f"appId eq '{client_id}'",
                "$select": "displayName,passwordCredentials",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 403:
            log.info("Graph API returned 403 — Application.Read.All permission "
                     "likely not granted. Falling back to manual expiry dates.")
            return None
        resp.raise_for_status()
        data = resp.json()
        apps = data.get("value", [])
        if not apps:
            log.debug("No app registration found for appId=%s", client_id)
            return None

        app_reg = apps[0]
        app_name = app_reg.get("displayName", "Unknown")
        creds = []
        for pc in app_reg.get("passwordCredentials", []):
            end_str = pc.get("endDateTime")
            if not end_str:
                continue
            # Graph returns ISO 8601 with Z suffix, sometimes with
            # 7-digit fractional seconds that Python < 3.11 can't parse.
            clean = end_str.replace("Z", "+00:00")
            clean = re.sub(r"(\.\d{6})\d+", r"\1", clean)
            end_dt = datetime.fromisoformat(clean)
            creds.append({
                "app_name": app_name,
                "display_name": pc.get("displayName") or "Unnamed secret",
                "key_id": pc.get("keyId", ""),
                "end_date": end_dt,
                "hint": pc.get("hint", ""),
            })
        return creds
    except Exception as e:
        log.warning("Graph API query failed for appId=%s: %s", client_id, e)
        return None


def _match_active_credential(creds: list[dict], client_secret: str) -> dict | None:
    """Find the credential whose hint matches the secret currently in use.

    Azure's passwordCredential.hint contains the last few characters of the
    secret value.  We match against the tail of AZURE_CLIENT_SECRET.

    Returns the matching credential, or None if no match is found.
    """
    if not client_secret or not creds:
        return None
    for cred in creds:
        hint = cred.get("hint", "")
        if hint and client_secret.endswith(hint):
            return cred
    return None


def _manual_entry() -> dict | None:
    """Build an expiry result from a manual date in Admin → Settings."""
    from settings import get_setting
    date_str = (get_setting("azure_secret_expiry", "") or "").strip()
    if not date_str:
        return None
    try:
        end_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("Invalid manual expiry date for azure_secret_expiry: %s", date_str)
        return None
    now = datetime.now(timezone.utc)
    days_left = (end_dt - now).days
    return {
        "app_name": "Azure AD (auth)",
        "secret_name": "Client secret (manual)",
        "expires": date_str,
        "days_left": days_left,
        "status": _classify(days_left),
        "source": "manual",
    }


def check_secret_expiry(force_refresh: bool = False) -> list[dict]:
    """Return a list of Azure secrets with their expiry status.

    Each item has:
      app_name      — display name of the Azure app registration
      secret_name   — display name of the specific secret
      expires       — ISO date string (YYYY-MM-DD), or "N/A" for auth failures
      days_left     — integer (negative = already expired)
      status        — "ok" | "warning" | "critical" | "expired" | "auth_failed"
      source        — "graph" | "manual" | "validation"
      error         — (only when status is "auth_failed") human-readable message

    Returns [] if Azure is not configured.
    """
    global _cache, _cache_ts

    if not force_refresh and _cache is not None:
        if (time.monotonic() - _cache_ts) < _CACHE_TTL:
            return _cache

    from config import is_azure_enabled, get_azure_credentials

    if not is_azure_enabled():
        _cache = []
        _cache_ts = time.monotonic()
        return _cache

    results = []
    now = datetime.now(timezone.utc)
    auth_from_graph = False

    # ── Try Graph API auto-detection first ───────────────────────────────
    client_id, client_secret, tenant_id = get_azure_credentials()

    token, auth_error = _get_graph_token(client_id, client_secret, tenant_id)

    # Surface credential authentication failures so the banner can warn admins
    if auth_error:
        results.append({
            "app_name": "Azure AD (auth)",
            "secret_name": "Client secret",
            "expires": "N/A",
            "days_left": -1,  # sentinel — not a real expiry; status is set explicitly
            "status": "auth_failed",
            "source": "validation",
            "error": auth_error,
        })

    if token:
        auth_creds = _query_app_credentials(token, client_id)
        if auth_creds:
            # Try to identify the active secret by matching the hint
            active = _match_active_credential(auth_creds, client_secret)
            if active:
                # Only report the secret currently in use
                auth_from_graph = True
                days_left = (active["end_date"] - now).days
                results.append({
                    "app_name": active["app_name"],
                    "secret_name": active["display_name"],
                    "expires": active["end_date"].strftime("%Y-%m-%d"),
                    "days_left": days_left,
                    "status": _classify(days_left),
                    "source": "graph",
                })
            else:
                # No hint match — report the earliest-expiring credential
                # so the admin gets the most conservative warning
                auth_from_graph = True
                auth_creds.sort(key=lambda c: c["end_date"])
                earliest = auth_creds[0]
                days_left = (earliest["end_date"] - now).days
                results.append({
                    "app_name": earliest["app_name"],
                    "secret_name": earliest["display_name"],
                    "expires": earliest["end_date"].strftime("%Y-%m-%d"),
                    "days_left": days_left,
                    "status": _classify(days_left),
                    "source": "graph",
                })

    # ── Fall back to manual date if Graph didn't cover it ────────────────
    if not auth_from_graph:
        manual = _manual_entry()
        if manual:
            results.append(manual)

    _cache = results
    _cache_ts = time.monotonic()
    sources = set(r["source"] for r in results)
    log.info("Secret expiry check: %d credential(s) found (sources: %s)",
             len(results), ", ".join(sources) if sources else "none")
    return results


def get_active_expiry_date() -> str | None:
    """Return the expiry date (YYYY-MM-DD) of the active auth secret.

    Used by get_all_settings() to auto-populate the expiry date field
    when Graph detection is available.  Returns None if no Graph data.
    """
    secrets = check_secret_expiry()
    for s in secrets:
        if s["source"] == "graph":
            return s["expires"]
    return None


def _classify(days_left: int) -> str:
    """Classify expiry urgency."""
    if days_left < 0:
        return "expired"
    if days_left <= 7:
        return "critical"
    if days_left <= 30:
        return "warning"
    return "ok"


def get_expiry_warnings() -> list[dict]:
    """Return only secrets that need attention (warning, critical, or expired)."""
    return [s for s in check_secret_expiry() if s["status"] != "ok"]
