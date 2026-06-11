#!/usr/bin/env python3
"""sync_cipp_data.py — Pull Microsoft 365 data from CIPP into the local SQLite DB.

Syncs into the *same* database as sync_cw_data.py so Claude can join across
ConnectWise and CIPP data (e.g. "clients with open CW tickets AND MFA not enforced").

Usage
-----
    python sync_cipp_data.py                        # uses CW_DB_PATH env var
    python sync_cipp_data.py --db /data/cw_data.db  # explicit path
    python sync_cipp_data.py --policies             # also sync M365 policies (slow)

Setup
-----
CIPP uses Azure AD app-registration credentials for API access.  You need:

    1. In your Azure AD (MSP tenant), create or identify the CIPP app registration.
    2. Under "API permissions" grant the CIPP application permission and consent.
    3. Save the credentials via Admin → Settings UI (preferred) or set as env vars.

See https://docs.cipp.app/user-documentation/cipp/settings/backend for details.

Environment / Admin Settings:
    CIPP_API_URL        Base URL of your CIPP API
    CIPP_TOKEN_URL      Full OAuth2 token endpoint URL
    CIPP_TENANT_ID      Azure tenant ID (used if CIPP_TOKEN_URL not set)
    CIPP_CLIENT_ID      Azure app-registration client ID
    CIPP_CLIENT_SECRET  Azure app-registration client secret
    CW_DB_PATH          Path to the shared SQLite database (default: /data/cw_data.db)

    Credentials can also be set via the Admin → Settings UI.  The syncer
    checks DB-managed settings first, then falls back to env vars.

Optional
--------
    CIPP_SCOPE          OAuth2 scope for the token request.
                        Defaults to "{CIPP_CLIENT_ID}/.default" if not set.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cipp_tenants (
    tenant_id       TEXT PRIMARY KEY,
    display_name    TEXT,
    default_domain  TEXT,
    customer_id     TEXT,
    relationship_id TEXT,
    synced_at       TEXT
);

CREATE TABLE IF NOT EXISTS cipp_licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    sku_id          TEXT,
    sku_name        TEXT,
    active_units    INTEGER,
    consumed_units  INTEGER,
    available_units INTEGER,
    synced_at       TEXT,
    UNIQUE(tenant_id, sku_id)
);

CREATE TABLE IF NOT EXISTS cipp_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT,
    tenant_name TEXT,
    type        TEXT,
    message     TEXT,
    severity    TEXT,
    url         TEXT,
    raised_at   TEXT,
    synced_at   TEXT,
    raw_json    TEXT
);


CREATE TABLE IF NOT EXISTS cipp_policies (
    tenant_id    TEXT NOT NULL,
    policy_type  TEXT NOT NULL,
    policy_id    TEXT NOT NULL,
    policy_name  TEXT,
    state        TEXT,
    raw_json     TEXT,
    content_hash TEXT,
    synced_at    TEXT,
    PRIMARY KEY (tenant_id, policy_type, policy_id)
);

CREATE TABLE IF NOT EXISTS cipp_policy_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL,
    policy_type  TEXT NOT NULL,
    policy_id    TEXT NOT NULL,
    policy_name  TEXT,
    state        TEXT,
    raw_json     TEXT,
    content_hash TEXT,
    snapshot_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cipp_licenses_tenant ON cipp_licenses(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cipp_alerts_tenant   ON cipp_alerts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cipp_policies_tenant  ON cipp_policies(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cipp_policies_type    ON cipp_policies(policy_type);
CREATE INDEX IF NOT EXISTS idx_cipp_snapshots_tenant ON cipp_policy_snapshots(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cipp_snapshots_type   ON cipp_policy_snapshots(policy_type);
"""

# ── Config ────────────────────────────────────────────────────────────────────

def _get(key, env_name):
    """Return a setting from DB (Admin UI) first, then env var fallback."""
    val = os.environ.get(env_name, "")
    try:
        from settings import get_setting
        val = (get_setting(key) or val).strip()
    except Exception:
        if val:
            val = val.strip()
    return val


def load_config():
    """Load CIPP credentials from DB settings (Admin UI) or environment variables.

    Checks DB-managed settings first (set via Admin → Settings), then falls
    back to environment variables.

    Token URL resolution order:
      1. cipp_token_url setting / CIPP_TOKEN_URL env var
      2. cipp_tenant_id setting / CIPP_TENANT_ID env var → construct Azure AD endpoint

    Scope resolution order:
      1. CIPP_SCOPE env var  (no Admin UI setting for scope)
      2. Auto-derive as  "{client_id}/.default" (Azure AD client credentials default)
    """
    api_url       = _get("cipp_api_url",       "CIPP_API_URL")
    client_id     = _get("cipp_client_id",     "CIPP_CLIENT_ID")
    client_secret = _get("cipp_client_secret", "CIPP_CLIENT_SECRET")
    token_url     = _get("cipp_token_url",     "CIPP_TOKEN_URL")
    tenant_id     = _get("cipp_tenant_id",     "CIPP_TENANT_ID")

    required_names = []
    if not api_url:
        required_names.append("CIPP_API_URL")
    if not client_id:
        required_names.append("CIPP_CLIENT_ID")
    if not client_secret:
        required_names.append("CIPP_CLIENT_SECRET")
    if not token_url and not tenant_id:
        required_names.append("CIPP_TOKEN_URL (or CIPP_TENANT_ID)")

    if required_names:
        print(f"ERROR: Missing required settings: {', '.join(required_names)}", file=sys.stderr)
        print("Set them via Admin → Settings or .env and restart this container.", file=sys.stderr)
        sys.exit(10)

    if not token_url:
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    scope = os.environ.get("CIPP_SCOPE") or f"{client_id}/.default"

    return {
        "api_url":       api_url.rstrip("/"),
        "token_url":     token_url,
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         scope,
    }

# ── Auth ──────────────────────────────────────────────────────────────────────

_token_cache = {"token": None, "expires_at": 0}

def get_token(config):
    """Return a valid Bearer token, refreshing if within 60 s of expiry."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    resp = requests.post(config["token_url"], data={
        "client_id":     config["client_id"],
        "client_secret": config["client_secret"],
        "scope":         config["scope"],
        "grant_type":    "client_credentials",
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["token"]

# ── API helpers ───────────────────────────────────────────────────────────────

def api_get(config, endpoint, params=None, retries=3):
    """GET {api_url}/{endpoint} with auth and retry.

    CIPP returns all results in a single response and does not support
    $top/$skip pagination — passing those params causes it to return the
    same full result set on every page, creating an infinite loop.
    """
    token = get_token(config)
    url = f"{config['api_url']}/{endpoint.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=120)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"{endpoint} failed after {retries} attempts: {e}") from e
            time.sleep(2 ** attempt)

    data = r.json()

    # CIPP returns either a list directly or {"Results": [...]}
    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        inner = data.get("Results") or data.get("results")
        if isinstance(inner, list):
            return inner
        # Single-object response or unexpected structure — wrap if non-empty
        return [data] if data else []
    return []

# ── Policy endpoint registry ──────────────────────────────────────────────────
#
# Each entry declares:
#   policy_type  — discriminator stored in cipp_policies.policy_type
#   endpoint     — CIPP API endpoint name
#   id_fields    — candidate field names for the policy ID (tried in order)
#   name_fields  — candidate field names for the policy display name
#   state_fields — candidate field names for an enabled/state flag (optional)
#   singleton    — True if the endpoint returns a single object (not a list)

POLICY_ENDPOINTS = [
    # ── Conditional Access ─────────────────────────────────────
    dict(policy_type="ca_policy",
         endpoint="ListConditionalAccessPolicies",
         id_fields=["id"], name_fields=["displayName"], state_fields=["state"]),
    dict(policy_type="named_location",
         endpoint="ListNamedLocations",
         id_fields=["id"], name_fields=["displayName"], state_fields=[]),
    # ── Intune / MEM ───────────────────────────────────────────
    dict(policy_type="intune_policy",
         endpoint="ListIntunePolicy",
         id_fields=["id"], name_fields=["name", "displayName"], state_fields=[]),
    dict(policy_type="compliance_policy",
         endpoint="ListCompliancePolicies",
         id_fields=["id"], name_fields=["displayName", "name"], state_fields=[]),
    dict(policy_type="app_protection",
         endpoint="ListAppProtectionPolicies",
         id_fields=["id"], name_fields=["displayName", "name"], state_fields=[]),
    dict(policy_type="assignment_filter",
         endpoint="ListAssignmentFilters",
         id_fields=["id"], name_fields=["displayName"], state_fields=[]),
    dict(policy_type="autopilot_config",
         endpoint="ListAutopilotconfig",
         id_fields=["id"], name_fields=["displayName", "name"], state_fields=[]),
    dict(policy_type="intune_script",
         endpoint="ListIntuneScript",
         id_fields=["id"], name_fields=["displayName", "name"], state_fields=[]),
    # ── Exchange / Email Security ──────────────────────────────
    dict(policy_type="transport_rule",
         endpoint="ListTransportRules",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["State"]),
    dict(policy_type="exch_connector",
         endpoint="ListExchangeConnectors",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["Enabled"]),
    dict(policy_type="spam_filter",
         endpoint="ListSpamfilter",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["IsDefault"]),
    dict(policy_type="antiphishing",
         endpoint="ListAntiPhishingFilters",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["Enabled"]),
    dict(policy_type="malware_filter",
         endpoint="ListMalwareFilters",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["IsDefault"]),
    dict(policy_type="safe_attachments",
         endpoint="ListSafeAttachmentsFilters",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["Enable"]),
    dict(policy_type="connection_filter",
         endpoint="ListConnectionFilter",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=[]),
    dict(policy_type="safe_links",
         endpoint="ListSafeLinksPolicy",
         id_fields=["Guid", "Identity"], name_fields=["Name"], state_fields=["IsEnabled"]),
    # ── Tenant / Security ──────────────────────────────────────
    dict(policy_type="defender_state",
         endpoint="ListDefenderState",
         id_fields=[], name_fields=[], state_fields=[], singleton=True),
    dict(policy_type="sharepoint_settings",
         endpoint="ListSharepointSettings",
         id_fields=[], name_fields=[], state_fields=[], singleton=True),
    dict(policy_type="oauth_app",
         endpoint="ListOAuthApps",
         id_fields=["id", "appId"], name_fields=["displayName", "name"], state_fields=[]),
]


# ── Policy extraction helpers ────────────────────────────────────────────────

def _first(obj, fields):
    """Return the first non-empty value found among the given field names."""
    for f in fields:
        v = obj.get(f)
        if v is not None and v != "":
            return str(v)
    return None


def compute_hash(obj):
    """Stable SHA-256 hash of a dict (sorted keys, no whitespace variation)."""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def extract_policy_record(raw, ep, tenant_id, now_iso):
    """Build a cipp_policies row from a raw CIPP policy object."""
    is_singleton = ep.get("singleton", False)
    policy_id   = "singleton" if is_singleton else _first(raw, ep["id_fields"])
    policy_name = None if is_singleton else _first(raw, ep["name_fields"])
    state       = None if is_singleton else _first(raw, ep["state_fields"])

    if not policy_id:
        return None

    return {
        "tenant_id":   tenant_id,
        "policy_type": ep["policy_type"],
        "policy_id":   policy_id,
        "policy_name": policy_name,
        "state":       state,
        "raw_json":    json.dumps(raw),
        "content_hash": compute_hash(raw),
        "synced_at":   now_iso,
    }


# ── Policy sync logic ────────────────────────────────────────────────────────

def load_known_hashes(conn):
    """Load (tenant_id, policy_type, policy_id) → content_hash from cipp_policies."""
    try:
        rows = conn.execute(
            "SELECT tenant_id, policy_type, policy_id, content_hash FROM cipp_policies"
        ).fetchall()
        return {(r[0], r[1], r[2]): r[3] for r in rows}
    except Exception:
        return {}


def sync_one_type(config, ep, tenants, known_hashes, now_iso, conn):
    """Sync all policies of one type across all tenants.

    Returns (policies, snapshots, stats) where:
        policies  — all current-state rows for upsert to cipp_policies
        snapshots — changed/new rows only for insert to cipp_policy_snapshots
        stats     — dict with tenants/policies/changed/errors counts
    """
    ptype = ep["policy_type"]
    is_singleton = ep.get("singleton", False)

    policies  = []
    snapshots = []
    stats = {"tenants": 0, "policies": 0, "changed": 0, "errors": 0}
    successful_tids = set()

    for t in tenants:
        tid = t.get("customerId") or t.get("tenantId") or t.get("id")
        tname = t.get("displayName") or t.get("customerName") or tid
        if not tid:
            continue

        try:
            raw_items = api_get(config, ep["endpoint"], {"TenantFilter": tid})
        except Exception as e:
            print(f"    Warning: {ep['endpoint']} failed for {tname}: {e}", flush=True)
            stats["errors"] += 1
            continue

        if not raw_items:
            stats["tenants"] += 1
            successful_tids.add(tid)
            continue

        # Singletons return a single dict — wrap for uniform handling
        if is_singleton and isinstance(raw_items, list) and len(raw_items) == 1:
            items = raw_items
        elif is_singleton and isinstance(raw_items, dict):
            items = [raw_items]
        else:
            items = raw_items if isinstance(raw_items, list) else [raw_items]

        for raw in items:
            if not isinstance(raw, dict):
                continue

            row = extract_policy_record(raw, ep, tid, now_iso)
            if not row:
                continue

            policies.append(row)
            stats["policies"] += 1

            # Write snapshot only if the content has changed
            key = (tid, ptype, row["policy_id"])
            if known_hashes.get(key) != row["content_hash"]:
                snapshots.append(row)
                stats["changed"] += 1

        stats["tenants"] += 1
        successful_tids.add(tid)

    return policies, snapshots, stats, successful_tids


def sync_policies(config, conn):
    """Sync all 19 M365 policy types across all tenants.

    Uses hash-based drift detection to only write snapshots when content changes.
    Returns total policy count.
    """
    print("Syncing CIPP policies (19 endpoint types)...", flush=True)

    # Load current hashes once to detect changes
    known_hashes = load_known_hashes(conn)
    print(f"  Loaded {len(known_hashes)} existing policy hashes", flush=True)

    # Fetch tenant list from already-synced cipp_tenants
    tenants_raw = conn.execute(
        "SELECT tenant_id, display_name, default_domain FROM cipp_tenants"
    ).fetchall()
    # Convert to dicts matching the format sync_one_type expects
    tenants = [{"customerId": r[0], "displayName": r[1]} for r in tenants_raw]
    print(f"  {len(tenants)} tenants", flush=True)

    now_iso = datetime.now(timezone.utc).isoformat()
    total_policies  = 0
    total_snapshots = 0
    total_errors    = 0

    for ep in POLICY_ENDPOINTS:
        ptype = ep["policy_type"]
        print(f"  {ptype}...", flush=True)

        policies, snapshots, stats, successful_tids = sync_one_type(
            config, ep, tenants, known_hashes, now_iso, conn
        )

        # Upsert current-state policies
        for p in policies:
            conn.execute(
                "INSERT OR REPLACE INTO cipp_policies "
                "(tenant_id, policy_type, policy_id, policy_name, state, raw_json, content_hash, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (p["tenant_id"], p["policy_type"], p["policy_id"],
                 p["policy_name"], p["state"], p["raw_json"],
                 p["content_hash"], p["synced_at"]),
            )

        # Prune stale policies — delete rows for this policy_type that are
        # no longer returned by CIPP (per-tenant replacement).
        current_ids_by_tenant: dict[str, list[str]] = {}
        for p in policies:
            current_ids_by_tenant.setdefault(p["tenant_id"], []).append(p["policy_id"])

        # Only prune policies for tenants that sync_one_type confirmed
        # as successful. Failed tenants keep their existing data.
        for tid in successful_tids:
            current_ids = current_ids_by_tenant.get(tid, [])
            if current_ids:
                placeholders = ",".join("?" for _ in current_ids)
                conn.execute(
                    f"DELETE FROM cipp_policies WHERE tenant_id = ? AND policy_type = ? "
                    f"AND policy_id NOT IN ({placeholders})",
                    [tid, ptype] + current_ids,
                )
            else:
                conn.execute(
                    "DELETE FROM cipp_policies WHERE tenant_id = ? AND policy_type = ?",
                    (tid, ptype),
                )

        # Insert snapshots for changed policies only
        for s in snapshots:
            conn.execute(
                "INSERT INTO cipp_policy_snapshots "
                "(tenant_id, policy_type, policy_id, policy_name, state, raw_json, content_hash, snapshot_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (s["tenant_id"], s["policy_type"], s["policy_id"],
                 s["policy_name"], s["state"], s["raw_json"],
                 s["content_hash"], now_iso),
            )

        conn.commit()

        change_note = f", {stats['changed']} changed" if stats["changed"] else ""
        error_note  = f", {stats['errors']} errors" if stats["errors"] else ""
        print(f"    {stats['tenants']} tenants, {stats['policies']} policies{change_note}{error_note}", flush=True)

        total_policies  += stats["policies"]
        total_snapshots += stats["changed"]
        total_errors    += stats["errors"]

    # Retention cleanup — prune snapshots older than 365 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    pruned = conn.execute(
        "DELETE FROM cipp_policy_snapshots WHERE snapshot_at < ?", (cutoff,)
    ).rowcount
    if pruned:
        print(f"  Pruned {pruned} snapshot(s) older than 365 days", flush=True)
    conn.commit()

    print(f"  Policy sync: {total_policies} current, {total_snapshots} new snapshots", flush=True)
    if total_errors:
        print(f"  {total_errors} endpoint errors (see warnings above)", flush=True)

    return total_policies


# ── Sync functions ────────────────────────────────────────────────────────────

def sync_tenants(config, conn):
    """Sync the list of managed M365 tenants."""
    print("Syncing CIPP tenants...")
    tenants = api_get(config, "ListTenants")
    print(f"  Fetched {len(tenants)} tenants")

    now = datetime.now(timezone.utc).isoformat()
    # Upsert tenants (no DELETE — avoids empty-table window during sync)
    fetched_ids = set()
    batch = 0
    for t in tenants:
        tid = t.get("customerId") or t.get("tenantId") or t.get("id")
        fetched_ids.add(tid)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO cipp_tenants VALUES (?,?,?,?,?,?)",
                (
                    tid,
                    t.get("displayName") or t.get("customerName"),
                    t.get("defaultDomainName") or t.get("defaultDomain"),
                    t.get("customerId"),
                    t.get("relationshipId"),
                    now,
                )
            )
            batch += 1
            if batch >= 50:
                conn.commit()
                time.sleep(0.05)  # Yield write lock for web app
                batch = 0
        except Exception as e:
            print(f"  Warning: Skipped tenant {t.get('customerId')}: {e}", file=sys.stderr)
    # Remove tenants no longer returned by the API
    if fetched_ids:
        placeholders = ",".join("?" for _ in fetched_ids)
        pruned = conn.execute(
            f"DELETE FROM cipp_tenants WHERE tenant_id NOT IN ({placeholders})",
            tuple(fetched_ids)
        ).rowcount
        if pruned:
            print(f"  Pruned {pruned} stale tenant(s)")
    conn.commit()
    return len(tenants)


def sync_licenses(config, conn):
    """Sync M365 license (SKU) summary across all tenants."""
    print("Syncing CIPP licenses...")
    tenants = conn.execute("SELECT tenant_id, display_name FROM cipp_tenants").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    total = 0

    # Upsert licenses (no DELETE — avoids empty-table window during sync)
    skipped = 0
    batch = 0
    for tenant in tenants:
        tid = tenant[0]
        tname = tenant[1] or tid
        try:
            licenses = api_get(config, "ListLicenses", {"TenantFilter": tid})
        except Exception as e:
            print(f"  Warning: Skipped licenses for {tname}: {e}", file=sys.stderr)
            skipped += 1
            continue
        for lic in licenses:
            try:
                # CIPP field names (confirmed via API probe):
                #   CountUsed      → consumed users (string)
                #   TotalLicenses  → total/active licenses (string)
                #   CountAvailable / availableUnits → remaining (int)
                #   License        → human-readable product name
                consumed = int(lic.get("CountUsed") or 0)
                active   = int(lic.get("TotalLicenses") or 0)
                available = lic.get("CountAvailable") if lic.get("CountAvailable") is not None \
                            else lic.get("availableUnits", 0)
                conn.execute(
                    "INSERT OR REPLACE INTO cipp_licenses "
                    "(tenant_id, sku_id, sku_name, active_units, consumed_units, available_units, synced_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        tid,
                        lic.get("skuId") or lic.get("SkuId"),
                        lic.get("License") or lic.get("skuPartNumber") or lic.get("SkuPartNumber"),
                        active,
                        consumed,
                        available,
                        now,
                    )
                )
                total += 1
                batch += 1
                if batch >= 50:
                    conn.commit()
                    time.sleep(0.05)  # Yield write lock for web app
                    batch = 0
            except Exception as e:
                print(f"  Warning: Skipped license for tenant {tid}: {e}", file=sys.stderr)

    # Prune licenses only for tenants that were successfully synced —
    # if a tenant was skipped (API error), its existing licenses are still valid.
    if skipped:
        # Build set of successfully synced tenant IDs
        skipped_tids = set()
        for tenant in tenants:
            tid = tenant[0]
            # A tenant was skipped if it has no licenses updated this run
            row = conn.execute(
                "SELECT 1 FROM cipp_licenses WHERE tenant_id = ? AND synced_at = ?",
                (tid, now)
            ).fetchone()
            if not row:
                skipped_tids.add(tid)
        if skipped_tids:
            placeholders = ",".join("?" for _ in skipped_tids)
            pruned = conn.execute(
                f"DELETE FROM cipp_licenses WHERE synced_at < ? AND tenant_id NOT IN ({placeholders})",
                [now] + list(skipped_tids),
            ).rowcount
        else:
            pruned = conn.execute(
                "DELETE FROM cipp_licenses WHERE synced_at < ?", (now,)
            ).rowcount
    else:
        pruned = conn.execute(
            "DELETE FROM cipp_licenses WHERE synced_at < ?", (now,)
        ).rowcount
    if pruned:
        print(f"  Pruned {pruned} stale license(s)")
    conn.commit()
    skip_note = f", {skipped} tenant(s) skipped" if skipped else ""
    print(f"  Stored {total} license entries across {len(tenants) - skipped}/{len(tenants)} tenants{skip_note}")
    return total


def sync_alerts(config, conn):
    """Sync active CIPP alerts per tenant using the ExecAlertsList endpoint.

    Loops through all tenants (using their default_domain as the tenantFilter)
    and aggregates alerts into cipp_alerts. Skips tenants that error.
    """
    print("Syncing CIPP alerts...")
    tenants = conn.execute(
        "SELECT tenant_id, display_name, default_domain FROM cipp_tenants"
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    # Alerts don't have stable IDs, so we still need to clear and repopulate.
    # But we do it inside a single short transaction rather than leaving the
    # table empty for the entire duration of the API calls.
    # Step 1: Collect all alerts in memory first (no DB writes during API calls).
    all_alerts = []
    total = 0
    skipped = 0

    for tenant in tenants:
        tid, tname, domain = tenant[0], tenant[1] or tenant[0], tenant[2]
        if not domain:
            continue
        try:
            alerts = api_get(config, "ExecAlertsList", {"tenantFilter": domain})
        except Exception as e:
            print(f"  Warning: Skipped alerts for {tname}: {e}", file=sys.stderr)
            skipped += 1
            continue

        for a in alerts:
            # ExecAlertsList returns plain strings; other endpoints return dicts
            if isinstance(a, str):
                msg, alert_type, severity, url, raised_at = a, None, None, None, None
            else:
                msg       = a.get("message") or a.get("Message") or a.get("text")
                alert_type = a.get("type") or a.get("Type")
                severity  = a.get("severity") or a.get("Severity")
                url       = a.get("url") or a.get("URL")
                raised_at = a.get("raisedAt") or a.get("timestamp")
            all_alerts.append((tid, tname, alert_type, msg, severity, url, raised_at, now, json.dumps(a)))

    # Step 2: Delete and replace only for tenants that were successfully fetched.
    # Tenants that errored keep their existing alerts intact.
    successful_tids = set(row[0] for row in all_alerts)  # tenant_ids in collected alerts
    if skipped and successful_tids:
        # Only delete alerts for tenants we successfully fetched
        placeholders = ",".join("?" for _ in successful_tids)
        conn.execute(
            f"DELETE FROM cipp_alerts WHERE tenant_id IN ({placeholders})",
            list(successful_tids),
        )
    elif not skipped:
        # All tenants succeeded — safe to clear everything
        conn.execute("DELETE FROM cipp_alerts")
    # If all tenants failed (skipped == len(tenants)), don't delete anything
    batch = 0
    for row in all_alerts:
        try:
            conn.execute(
                "INSERT INTO cipp_alerts "
                "(tenant_id, tenant_name, type, message, severity, url, raised_at, synced_at, raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                row
            )
            total += 1
            batch += 1
            if batch >= 50:
                conn.commit()
                time.sleep(0.05)  # Yield write lock for web app
                batch = 0
        except Exception as e:
            print(f"  Warning: Skipped alert for {row[1]}: {e}", file=sys.stderr)

    conn.commit()
    skip_note = f", {skipped} tenants skipped" if skipped else ""
    print(f"  Stored {total} alerts across {len(tenants) - skipped} tenants{skip_note}")
    return total

# ── Database ──────────────────────────────────────────────────────────────────

def get_db(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in SCHEMA.strip().split(";"):
        s = statement.strip()
        if s:
            conn.execute(s)
    conn.commit()
    return conn

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync CIPP M365 data to SQLite")
    parser.add_argument(
        "--db",
        default=os.environ.get("CW_DB_PATH", "/data/cw_data.db"),
        help="Path to the SQLite database (default: $CW_DB_PATH or /data/cw_data.db)",
    )
    parser.add_argument(
        "--policies",
        action="store_true",
        help="Also sync M365 policy configurations (19 endpoint types, slower)",
    )
    args = parser.parse_args()

    config = load_config()

    # Lock file — prevents overlapping syncs and signals the UI sync indicator.
    # Uses a CIPP-specific lock file so the UI can show per-source sync state.
    lock_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), ".sync_running_cipp")
    try:
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        lock_file = None

    try:
        print(f"Connecting to database: {args.db}")
        conn = get_db(args.db)

        # Per-entity sync status tracking (shared across all sync scripts).
        from sync_state import init_sync_state, TrackedSync
        init_sync_state(conn)

        start = datetime.now()
        counts = {}

        def _run(entity, fn):
            with TrackedSync(conn, "cipp", entity) as t:
                n = fn()
                t.record_count = n
                counts[entity] = n

        _run("cipp_tenants",     lambda: sync_tenants(config, conn))
        _run("cipp_licenses",    lambda: sync_licenses(config, conn))
        _run("cipp_alerts",      lambda: sync_alerts(config, conn))


        if args.policies:
            _run("cipp_policies", lambda: sync_policies(config, conn))

        elapsed = (datetime.now() - start).total_seconds()
        print(f"\nCIPP sync complete in {elapsed:.1f}s")
        for table, count in counts.items():
            print(f"  {table}: {count} records")

        # Log the sync so /api/status can report last sync time and duration.
        tables_synced = ",".join(counts.keys())
        conn.execute(
            "INSERT INTO sync_log (synced_at, tables_synced, record_counts, duration_seconds) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), tables_synced, json.dumps(counts), elapsed),
        )
        conn.commit()

        conn.close()

    finally:
        if lock_file and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass


if __name__ == "__main__":
    main()
