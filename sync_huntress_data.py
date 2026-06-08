#!/usr/bin/env python3
"""
Huntress EDR → SQLite Sync Engine

Pulls organizations, agents (endpoint coverage), and incident reports from the
Huntress API and stores them in the local SQLite database.

The org-level cross-reference to ConnectWise companies is handled by the
customer_map table — customer_map.huntress_org_id links Huntress orgs to CW
company IDs.  This syncer only pulls the Huntress-specific data.

Flow:
  1. GET /v1/organizations          — list all orgs
  2. For each org: GET /v1/agents   — per-org replacement
  3. For each org: GET /v1/incident_reports — per-org replacement

Auth: HTTP Basic — HUNTRESS_API_KEY as username, HUNTRESS_API_SECRET as password.
Docs: https://api.huntress.io/docs

Usage:
    python sync_huntress_data.py                    # Full sync
    python sync_huntress_data.py --db /data/cw_data.db  # Custom DB path

Environment / Admin Settings:
    HUNTRESS_API_KEY     — Huntress API key (used as Basic auth username)
    HUNTRESS_API_SECRET  — Huntress API secret (used as Basic auth password)

    Credentials can also be set via the Admin → Settings UI.  The syncer
    checks DB-managed settings first, then falls back to env vars.
"""

import argparse
import base64
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
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
log = logging.getLogger("sync.huntress")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get(
    "CW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cw_data.db"),
)

HUNTRESS_BASE = "https://api.huntress.io/v1"

# Batch commit size — release the write lock periodically so the Flask web app
# can slip in its small writes between batches.
BATCH_SIZE = 50


# ── Credential loading ───────────────────────────────────────────────────────

def _load_credentials() -> tuple[str, str]:
    """Return (api_key, api_secret) from DB settings or env vars.

    Checks DB-managed settings first (set via Admin UI) then falls back to
    environment variables.
    """
    api_key = os.environ.get("HUNTRESS_API_KEY", "")
    api_secret = os.environ.get("HUNTRESS_API_SECRET", "")

    try:
        from settings import get_setting
        api_key = (get_setting("huntress_api_key") or api_key).strip()
        api_secret = (get_setting("huntress_api_secret") or api_secret).strip()
    except Exception:
        # settings module may not be available if run standalone outside the
        # app directory — fall through to env vars only.
        pass

    return api_key, api_secret


# ── Huntress API helpers ─────────────────────────────────────────────────────

def _build_auth_header(api_key: str, api_secret: str) -> str:
    """Build HTTP Basic auth header value."""
    creds = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    return f"Basic {creds}"


def huntress_api(path: str, auth_header: str, params: dict | None = None,
                 retries: int = 3, backoff: int = 10) -> dict:
    """Call the Huntress API with retry logic."""
    url = f"{HUNTRESS_BASE}{path}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("Authorization", auth_header)
            req.add_header("Accept", "application/json")

            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            log.warning("HTTP %s from Huntress %s (attempt %d/%d): %s",
                        e.code, path, attempt, retries, body[:300])

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

    raise RuntimeError(f"Huntress API call failed after {retries} attempts: {path}")


def fetch_paginated(path: str, auth_header: str, result_key: str,
                    extra_params: dict | None = None) -> list:
    """Paginate through a Huntress endpoint.  Returns all items."""
    items: list = []
    page = 1
    per_page = 500

    while True:
        params = {"page": page, "limit": per_page}
        if extra_params:
            params.update(extra_params)

        resp = huntress_api(path, auth_header, params=params)
        batch = resp.get(result_key, [])
        items.extend(batch)

        pagination = resp.get("pagination", {})
        total_pages = pagination.get("total_pages", 1)
        current_page = pagination.get("current_page", page)

        if current_page >= total_pages:
            break
        page += 1

    return items


# ── Database schema ──────────────────────────────────────────────────────────

HUNTRESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS huntress_organizations (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    agent_count     INTEGER DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS huntress_agents (
    id              TEXT PRIMARY KEY,
    huntress_org_id TEXT NOT NULL,
    hostname        TEXT,
    ip_address      TEXT,
    external_ip     TEXT,
    os_type         TEXT,
    os_name         TEXT,
    platform        TEXT,
    arch            TEXT,
    agent_version   TEXT,
    status          TEXT,
    last_seen_at    TEXT,
    installed_at    TEXT,
    tags            TEXT,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS huntress_incidents (
    id              TEXT PRIMARY KEY,
    huntress_org_id TEXT NOT NULL,
    agent_id        TEXT,
    severity        TEXT,
    status          TEXT,
    category        TEXT,
    title           TEXT,
    summary         TEXT,
    indicator       TEXT,
    sent_at         TEXT,
    closed_at       TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_huntress_agents_org ON huntress_agents(huntress_org_id);
CREATE INDEX IF NOT EXISTS idx_huntress_agents_status ON huntress_agents(status);
CREATE INDEX IF NOT EXISTS idx_huntress_incidents_org ON huntress_incidents(huntress_org_id);
CREATE INDEX IF NOT EXISTS idx_huntress_incidents_status ON huntress_incidents(status);
CREATE INDEX IF NOT EXISTS idx_huntress_incidents_severity ON huntress_incidents(severity);
"""


def init_huntress_tables(conn: sqlite3.Connection):
    """Create Huntress tables if they don't exist."""
    conn.executescript(HUNTRESS_SCHEMA)
    conn.commit()


# ── Batch commit helper ─────────────────────────────────────────────────────

def _batch_commit(conn, counter):
    """Commit if counter has reached BATCH_SIZE.  Returns 0 (reset counter)."""
    if counter >= BATCH_SIZE:
        conn.commit()
        return 0
    return counter


# ── Extract / normalize functions ────────────────────────────────────────────

def extract_org(o: dict) -> tuple:
    """Normalize a Huntress organization to a DB row tuple."""
    return (
        str(o.get("id", "")),
        o.get("name"),
        o.get("agents_count") or o.get("agent_count", 0),
        o.get("created_at"),
        o.get("updated_at"),
        json.dumps(o),
    )


def extract_agent(a: dict, org_id: str) -> tuple:
    """Normalize a Huntress agent to a DB row tuple."""
    tags = a.get("tags") or []
    if isinstance(tags, list):
        tags = ", ".join(str(t) for t in tags)

    # Derive online/offline status from last_callback_at (within 30 min = online)
    status = None
    last_callback = a.get("last_callback_at")
    if last_callback:
        try:
            cb = datetime.fromisoformat(last_callback.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - cb).total_seconds() / 60
            status = "online" if age_min < 30 else "offline"
        except Exception:
            status = "unknown"

    return (
        str(a.get("id", "")),
        org_id,
        a.get("hostname"),
        a.get("ipv4_address"),
        a.get("external_ip"),
        a.get("platform"),           # "windows", "macos", "linux"
        a.get("os"),                  # "Windows 11 Enterprise"
        a.get("platform"),
        a.get("arch"),
        a.get("version"),
        status,
        a.get("last_callback_at") or a.get("last_survey_at"),
        a.get("created_at"),
        tags or None,
        json.dumps(a),
    )


def extract_incident(i: dict, org_id: str) -> tuple:
    """Normalize a Huntress incident report to a DB row tuple."""
    return (
        str(i.get("id", "")),
        org_id,
        str(i["agent_id"]) if i.get("agent_id") else None,
        i.get("severity"),
        i.get("status"),
        i.get("category") or i.get("type"),
        i.get("title"),
        i.get("summary") or i.get("description"),
        i.get("indicator"),
        i.get("sent_at"),
        i.get("closed_at"),
        i.get("created_at"),
        i.get("updated_at"),
        json.dumps(i),
    )


# ── Sync functions ───────────────────────────────────────────────────────────

def sync_organizations(conn: sqlite3.Connection, auth_header: str) -> list[dict]:
    """Fetch and upsert all Huntress organizations.  Returns the raw org list."""
    log.info("Fetching organizations...")
    orgs = fetch_paginated("/organizations", auth_header, "organizations")
    log.info("Found %d organization(s)", len(orgs))

    batch = 0
    for o in orgs:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO huntress_organizations VALUES (?,?,?,?,?,?)",
                extract_org(o),
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            log.warning("Skipped org %s: %s", o.get("id"), e)
    conn.commit()

    log.info("Upserted %d organizations", len(orgs))
    return orgs


def sync_agents(conn: sqlite3.Connection, auth_header: str, orgs: list[dict]) -> int:
    """Fetch agents per org (full replacement).  Returns total agent count."""
    total = 0
    batch = 0

    for org in orgs:
        org_id = str(org.get("id", ""))
        org_name = org.get("name", org_id)

        try:
            agents = fetch_paginated(
                "/agents", auth_header, "agents",
                extra_params={"organization_id": org_id},
            )

            # Per-org replacement: clear existing, insert fresh
            conn.execute(
                "DELETE FROM huntress_agents WHERE huntress_org_id = ?",
                (org_id,),
            )

            for a in agents:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO huntress_agents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        extract_agent(a, org_id),
                    )
                    total += 1
                    batch += 1
                    batch = _batch_commit(conn, batch)
                except Exception as e:
                    log.warning("Skipped agent %s in %s: %s", a.get("id"), org_name, e)

            if len(agents) > 20:
                log.info("  %s: %d agents", org_name, len(agents))

        except Exception as e:
            log.error("Error syncing agents for %s: %s", org_name, e)
            continue

    conn.commit()
    log.info("Synced %d agents across %d orgs", total, len(orgs))
    return total


def sync_incidents(conn: sqlite3.Connection, auth_header: str, orgs: list[dict]) -> int:
    """Fetch incident reports per org (full replacement).  Returns total count."""
    total = 0
    batch = 0

    for org in orgs:
        org_id = str(org.get("id", ""))
        org_name = org.get("name", org_id)

        try:
            incidents = fetch_paginated(
                "/incident_reports", auth_header, "incident_reports",
                extra_params={"organization_id": org_id},
            )

            # Per-org replacement
            conn.execute(
                "DELETE FROM huntress_incidents WHERE huntress_org_id = ?",
                (org_id,),
            )

            for i in incidents:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO huntress_incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        extract_incident(i, org_id),
                    )
                    total += 1
                    batch += 1
                    batch = _batch_commit(conn, batch)
                except Exception as e:
                    log.warning("Skipped incident %s in %s: %s", i.get("id"), org_name, e)

        except Exception as e:
            log.error("Error syncing incidents for %s: %s", org_name, e)
            continue

    conn.commit()
    log.info("Synced %d incident reports", total)
    return total


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Huntress EDR data to SQLite")
    parser.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    api_key, api_secret = _load_credentials()
    if not api_key or not api_secret:
        log.error("HUNTRESS_API_KEY and HUNTRESS_API_SECRET are not configured — "
                  "set them via Admin → Settings or .env and restart this container.")
        sys.exit(10)

    auth_header = _build_auth_header(api_key, api_secret)

    start = datetime.now()
    log.info("Huntress -> SQLite sync starting at %s", start.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Database: %s", args.db)

    # Lock file — same pattern as sync_cw_data.py
    lock_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), ".sync_running_huntress")
    try:
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        lock_file = None

    try:
        conn = sqlite3.connect(args.db, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        init_huntress_tables(conn)

        # Per-entity status tracking
        from sync_state import init_sync_state, TrackedSync
        init_sync_state(conn)

        counts = {}

        # Step 1: Organizations
        with TrackedSync(conn, "huntress", "huntress_organizations") as t:
            orgs = sync_organizations(conn, auth_header)
            t.record_count = len(orgs)
            counts["huntress_organizations"] = len(orgs)

        # Step 2: Agents (depends on orgs list)
        with TrackedSync(conn, "huntress", "huntress_agents") as t:
            n = sync_agents(conn, auth_header, orgs)
            t.record_count = n
            counts["huntress_agents"] = n

        # Step 3: Incident reports (depends on orgs list)
        with TrackedSync(conn, "huntress", "huntress_incidents") as t:
            n = sync_incidents(conn, auth_header, orgs)
            t.record_count = n
            counts["huntress_incidents"] = n

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

        log.info("Huntress sync complete in %.1fs", elapsed)
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
