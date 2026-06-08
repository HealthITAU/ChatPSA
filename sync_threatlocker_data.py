#!/usr/bin/env python3
"""
ThreatLocker → SQLite Sync Engine

Pulls computer inventory from the ThreatLocker portal API and stores it in the
local SQLite database.  Uses a parent-level API key with childOrganizations=True
to pull all managed endpoints in one pass.

The org-level cross-reference to ConnectWise companies is handled by the
customer_map table — customer_map.threatlocker_org_id links ThreatLocker orgs
to CW company IDs.  This syncer only pulls the ThreatLocker-specific data.

Flow:
  1. POST /Computer/ComputerGetByAllParameters — paginated, all child orgs
  2. Group by organizationId → derive org list and per-org computer records
  3. Upsert threatlocker_computers (per-org replacement)

Auth: Raw API key in the Authorization header (no Bearer/Basic prefix).
API:  POST endpoints at https://portalapi.d.threatlocker.com/portalapi/

Usage:
    python sync_threatlocker_data.py                         # Full sync
    python sync_threatlocker_data.py --db /data/cw_data.db   # Custom DB path

Environment / Admin Settings:
    THREATLOCKER_API_KEY — ThreatLocker portal API key (parent-level)

    Credentials can also be set via the Admin → Settings UI.  The syncer
    checks DB-managed settings first, then falls back to env vars.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Logging — consistent with other syncers
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync.threatlocker")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get(
    "CW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cw_data.db"),
)

TL_BASE = "https://portalapi.d.threatlocker.com/portalapi"

# Batch commit size — release the write lock periodically so the Flask web app
# can slip in its small writes between batches.
BATCH_SIZE = 50


# ── Credential loading ───────────────────────────────────────────────────────

def _load_credentials() -> str:
    """Return the ThreatLocker API key from DB settings or env vars.

    Checks DB-managed settings first (set via Admin UI) then falls back to
    environment variables.
    """
    api_key = os.environ.get("THREATLOCKER_API_KEY", "")

    try:
        from settings import get_setting
        api_key = (get_setting("threatlocker_api_key") or api_key).strip()
    except Exception as e:
        log.debug("Could not load ThreatLocker credentials from DB settings, using env vars: %s", e)

    return api_key


# ── ThreatLocker API helpers ─────────────────────────────────────────────────

def tl_api(path: str, api_key: str, body: dict,
           retries: int = 3, backoff: int = 10,
           timeout: int = 120) -> list | dict:
    """POST to a ThreatLocker portal API endpoint with retry logic.

    ThreatLocker returns a flat JSON array for list endpoints (no wrapper),
    or an error object with StatusCode/Message on failure.
    An empty response (no body) is treated as an empty list.
    """
    url = f"{TL_BASE}{path}"
    data = json.dumps(body).encode()

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Authorization", api_key)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    return []
                result = json.loads(raw)

                # Check for error response
                if isinstance(result, dict) and "StatusCode" in result:
                    raise RuntimeError(
                        f"ThreatLocker API error {result.get('StatusCode')}: "
                        f"{result.get('Message', 'unknown')}"
                    )
                return result

        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()
            except Exception:
                pass
            log.warning("HTTP %s from ThreatLocker %s (attempt %d/%d): %s",
                        e.code, path, attempt, retries, body_text[:300])

            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", backoff * (2 ** (attempt - 1))))
                log.info("Rate limited. Retrying in %ds...", retry_after)
                time.sleep(retry_after)
                continue
            elif e.code >= 500:
                if attempt < retries:
                    wait = backoff * (2 ** (attempt - 1))
                    log.info("Server error, retrying in %ds...", wait)
                    time.sleep(wait)
                    continue
            raise

        except urllib.error.URLError as e:
            log.warning("Network error on %s (attempt %d/%d): %s",
                        path, attempt, retries, e.reason)
            if attempt < retries:
                wait = backoff * (2 ** (attempt - 1))
                time.sleep(wait)
                continue
            raise

    raise RuntimeError(f"ThreatLocker API call failed after {retries} attempts: {path}")


def fetch_all_computers(api_key: str) -> list:
    """Paginate through ComputerGetByAllParameters.  Returns all computers."""
    all_computers = []
    page = 1
    page_size = 500
    total_rows = None

    while True:
        batch = tl_api(
            "/Computer/ComputerGetByAllParameters",
            api_key,
            {
                "pageNumber": page,
                "pageSize": page_size,
                "childOrganizations": True,
                "showDeleted": False,
                "orderBy": "computername",
                "isAscending": True,
            },
        )

        if not batch:
            break

        all_computers.extend(batch)

        # totalRows is embedded in each record
        if total_rows is None and batch:
            total_rows = batch[0].get("totalRows", 0)
            log.info("  Total computers reported by API: %d", total_rows)

        if len(all_computers) >= (total_rows or 0):
            break

        page += 1

    return all_computers


def fetch_organizations(api_key: str) -> list | None:
    """Fetch child organizations via the dedicated orgs endpoint.

    Uses OrganizationGetChildOrganizationsByParameters which returns
    a flat list of org objects with id, name, computerCount, etc.
    Falls back to deriving orgs from computers if the endpoint fails
    (e.g. older ThreatLocker portal versions).
    """
    try:
        result = tl_api(
            "/Organization/OrganizationGetChildOrganizationsByParameters",
            api_key,
            {"pageNumber": 1, "pageSize": 5000, "includeAllChildren": True},
            retries=2, timeout=30,
        )
        if isinstance(result, list):
            orgs = []
            for o in result:
                orgs.append({
                    "id": str(o.get("id", o.get("organizationId", ""))).strip(),
                    "name": o.get("name", o.get("organizationName", "")),
                    "count": o.get("computerCount", 0),
                })
            log.info("Fetched %d organizations via dedicated API endpoint", len(orgs))
            return orgs
    except Exception as e:
        log.warning("OrganizationGetChildOrganizationsByParameters failed (%s), "
                    "will derive orgs from computers instead", e)
    return None


# ── Database schema ──────────────────────────────────────────────────────────

THREATLOCKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS threatlocker_organizations (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    computer_count  INTEGER DEFAULT 0,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS threatlocker_computers (
    computer_id                     TEXT PRIMARY KEY,
    organization_id                 TEXT NOT NULL,
    organization_name               TEXT,
    computer_name                   TEXT,
    hostname                        TEXT,
    operating_system                TEXT,
    os_type                         TEXT,
    computer_group                  TEXT,
    computer_group_id               TEXT,
    mode                            TEXT,
    is_lock_down_mode               INTEGER DEFAULT 0,
    is_isolation_mode               INTEGER DEFAULT 0,
    is_tamper_protection_disabled   INTEGER DEFAULT 0,
    driver_status                   TEXT,
    threatlocker_version            TEXT,
    service_version                 TEXT,
    last_checkin                    TEXT,
    last_checkin_ip                 TEXT,
    date_created                    TEXT,
    deny_count_one_day              INTEGER DEFAULT 0,
    deny_count_three_days           INTEGER DEFAULT 0,
    deny_count_seven_days           INTEGER DEFAULT 0,
    is_deleted                      INTEGER DEFAULT 0,
    is_isolated                     INTEGER DEFAULT 0,
    is_locked_out                   INTEGER DEFAULT 0,
    raw_json                        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tl_computers_org ON threatlocker_computers(organization_id);
CREATE INDEX IF NOT EXISTS idx_tl_computers_name ON threatlocker_computers(computer_name);
CREATE INDEX IF NOT EXISTS idx_tl_computers_mode ON threatlocker_computers(mode);
"""


def init_threatlocker_tables(conn: sqlite3.Connection):
    """Create ThreatLocker tables if they don't exist."""
    conn.executescript(THREATLOCKER_SCHEMA)
    conn.commit()


# ── Batch commit helper ─────────────────────────────────────────────────────

def _batch_commit(conn, counter):
    """Commit if counter has reached BATCH_SIZE.  Returns 0 (reset counter)."""
    if counter >= BATCH_SIZE:
        conn.commit()
        time.sleep(0.05)  # Yield lock for other writers
        return 0
    return counter


# ── Extract / normalize ─────────────────────────────────────────────────────

def extract_computer(c: dict) -> tuple:
    """Normalize a ThreatLocker computer record to a DB row tuple."""
    return (
        c.get("computerId", ""),
        c.get("organizationId", ""),
        c.get("organization"),
        c.get("computerName"),
        c.get("hostname"),
        (c.get("operatingSystem") or "").strip() or None,
        c.get("osType"),
        c.get("group"),
        c.get("computerGroupId"),
        c.get("mode"),
        int(bool(c.get("isLockDownMode"))),
        int(bool(c.get("isIsolationMode"))),
        int(bool(c.get("isTamperProtectionDisabled"))),
        c.get("driverStatusString"),
        c.get("threatLockerVersion"),
        c.get("serviceVersion"),
        c.get("lastCheckin"),
        c.get("lastCheckinIPAddress"),
        c.get("dateCreated"),
        c.get("denyCountOneDay", 0),
        c.get("denyCountThreeDays", 0),
        c.get("denyCountSevenDays", 0),
        int(bool(c.get("isDeleted"))),
        int(bool(c.get("isIsolated"))),
        int(bool(c.get("isLockedOut"))),
        json.dumps(c),
    )


# ── Sync functions ──────────────────────────────────────────────────────────

def sync_organizations(conn: sqlite3.Connection, computers: list,
                       api_key: str | None = None) -> list[dict]:
    """Upsert ThreatLocker organizations.

    Tries the dedicated OrganizationGetChildOrganizationsByParameters
    endpoint first (accurate names and counts). Falls back to deriving
    orgs from computer data if the endpoint is unavailable.
    Returns a list of org dicts: [{id, name, count}, ...].
    """
    org_list = None
    from_api = False
    if api_key:
        org_list = fetch_organizations(api_key)

    if org_list:
        orgs = {o["id"]: o for o in org_list if o["id"]}
        from_api = True
    else:
        orgs = {}

    # Always supplement with orgs derived from computer records —
    # the dedicated API may only return parent-level orgs, missing
    # child orgs that appear in computer data.
    # Reset counts so we derive accurate totals from actual computers.
    for o in orgs.values():
        o["count"] = 0
    for c in computers:
        raw = c.get("organizationId")
        if raw is None:
            continue
        oid = str(raw).strip()
        if not oid:
            continue
        if oid not in orgs:
            orgs[oid] = {"id": oid, "name": c.get("organization", ""), "count": 0}
            from_api = False  # Mixed source
        orgs[oid]["count"] = orgs[oid].get("count", 0) + 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    batch = 0

    # Full replacement
    conn.execute("DELETE FROM threatlocker_organizations")
    for o in orgs.values():
        try:
            conn.execute(
                "INSERT INTO threatlocker_organizations VALUES (?,?,?,?)",
                (o["id"], o["name"], o["count"], now),
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            log.warning("Skipped org %s: %s", o["id"], e)
    conn.commit()

    log.info("Upserted %d organizations (%s)", len(orgs),
             "from API" if from_api else "supplemented from computer data")
    return list(orgs.values())


def sync_computers(conn: sqlite3.Connection, api_key: str,
                   computers: list | None = None) -> tuple[int, int]:
    """Upsert computers by org (per-org replacement).

    If *computers* is provided, uses them directly; otherwise fetches from API.
    Returns (total_computers, org_count).
    """
    if computers is None:
        log.info("Fetching computers from ThreatLocker...")
        computers = fetch_all_computers(api_key)
        log.info("Fetched %d computer(s)", len(computers))

    # Group by organizationId for per-org replacement
    orgs: dict[str, list] = {}
    for c in computers:
        raw = c.get("organizationId")
        org_id = str(raw).strip() if raw is not None else ""
        if not org_id:
            continue
        orgs.setdefault(org_id, []).append(c)

    total = 0
    batch = 0

    # Purge rows for orgs that no longer appear in the API response
    current_org_ids = set(orgs.keys())
    if current_org_ids:
        placeholders = ",".join("?" for _ in current_org_ids)
        conn.execute(
            f"DELETE FROM threatlocker_computers WHERE organization_id NOT IN ({placeholders})",
            list(current_org_ids),
        )
    else:
        conn.execute("DELETE FROM threatlocker_computers")
    conn.commit()
    time.sleep(0.05)  # Yield lock for other writers

    for org_id, org_computers in orgs.items():
        org_name = org_computers[0].get("organization", org_id) if org_computers else org_id

        try:
            # Per-org replacement: clear existing, insert fresh
            conn.execute(
                "DELETE FROM threatlocker_computers WHERE organization_id = ?",
                (org_id,),
            )

            for c in org_computers:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO threatlocker_computers VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        extract_computer(c),
                    )
                    total += 1
                    batch += 1
                    batch = _batch_commit(conn, batch)
                except Exception as e:
                    log.warning("Skipped computer %s in %s: %s",
                                c.get("computerId"), org_name, e)

            conn.commit()  # Commit per-org to release write lock for other syncers
            time.sleep(0.05)  # Yield lock for other writers

            if len(org_computers) > 20:
                log.info("  %s: %d computers", org_name, len(org_computers))

        except Exception as e:
            log.error("Error syncing computers for %s: %s", org_name, e)
            continue
    log.info("Synced %d computers across %d org(s)", total, len(orgs))
    return total, len(orgs)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync ThreatLocker endpoint data to SQLite")
    parser.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    api_key = _load_credentials()
    if not api_key:
        log.error("THREATLOCKER_API_KEY is not configured — "
                  "set it via Admin → Settings or .env and restart this container.")
        sys.exit(10)

    start = datetime.now()
    log.info("ThreatLocker -> SQLite sync starting at %s", start.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Database: %s", args.db)

    # Lock file — same pattern as other syncers
    lock_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), ".sync_running_threatlocker")
    try:
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        lock_file = None

    try:
        conn = sqlite3.connect(args.db, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        init_threatlocker_tables(conn)

        # Per-entity status tracking
        from sync_state import init_sync_state, TrackedSync
        init_sync_state(conn)

        counts = {}

        # Step 1: Computers — fetch inside TrackedSync so API errors are recorded
        with TrackedSync(conn, "threatlocker", "threatlocker_computers") as t:
            log.info("Fetching computers from ThreatLocker...")
            all_computers = fetch_all_computers(api_key)
            log.info("Fetched %d computer(s)", len(all_computers))
            total, _org_count = sync_computers(conn, api_key, all_computers)
            t.record_count = total
            counts["threatlocker_computers"] = total

        # Step 2: Organizations (via API, falls back to deriving from computers)
        with TrackedSync(conn, "threatlocker", "threatlocker_organizations") as t:
            org_list = sync_organizations(conn, all_computers, api_key=api_key)
            t.record_count = len(org_list)
            counts["threatlocker_organizations"] = len(org_list)

        # Write to sync_log for backwards compat with existing UI
        elapsed = (datetime.now() - start).total_seconds()
        conn.execute(
            "INSERT INTO sync_log (synced_at, tables_synced, record_counts, duration_seconds) VALUES (?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ",".join(counts.keys()),
                json.dumps(counts),
                elapsed,
            ),
        )
        conn.commit()
        conn.close()

        log.info("ThreatLocker sync complete in %.1fs", elapsed)
        for table, count in counts.items():
            log.info("  %s: %d records", table, count)

    finally:
        if lock_file and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass


if __name__ == "__main__":
    main()
