#!/usr/bin/env python3
"""
Duo Security (MSP Edition) → SQLite Sync

Pulls user data from all Duo child accounts via the Accounts API and stores
it in the local SQLite database as `duo_users`.  Each child account's users
are linked to a CW Manage company via `customer_map.duo_account_id`.

MSP flow:
  1. List child accounts via POST /accounts/v1/account/list
  2. For each child, query /admin/v1/users using the parent's Accounts API
     credentials with the child's account_id — sent to the child's api_hostname.
     No per-child Admin API applications are needed.
  3. Store users in `duo_users` keyed by (user_id, duo_account_id)

Usage:
    python sync_duo_data.py                        # Sync all child accounts
    python sync_duo_data.py --db /data/cw_data.db
    python sync_duo_data.py --dry-run              # Fetch and print, no writes

Environment / Admin Settings:
    DUO_IKEY          Accounts API integration key (MSP parent)
    DUO_SKEY          Accounts API secret key
    DUO_HOST          API hostname (e.g. api-XXXXXXXX.duosecurity.com)
    CW_DB_PATH        Override the default database path

    Credentials can also be set via the Admin → Settings UI.  The syncer
    checks DB-managed settings first, then falls back to env vars.
"""

import argparse
import base64
import email.utils
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import logging
log = logging.getLogger("sync.duo")


# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = os.environ.get(
    "CW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cw_data.db"),
)


# ── Credential loading ───────────────────────────────────────────────────────

def _load_credentials() -> tuple:
    """Return (ikey, skey, host) from DB settings or env vars.

    Checks DB-managed settings first (set via Admin UI) then falls back to
    environment variables.
    """
    ikey = os.environ.get("DUO_IKEY", "").strip()
    skey = os.environ.get("DUO_SKEY", "").strip()
    host = os.environ.get("DUO_HOST", "").strip()

    try:
        from settings import get_setting
        ikey = (get_setting("duo_ikey") or ikey).strip()
        skey = (get_setting("duo_skey") or skey).strip()
        host = (get_setting("duo_host") or host).strip()
    except Exception as e:
        log.debug("Could not load Duo credentials from DB settings, using env vars: %s", e)

    return ikey, skey, host


# ── Database ───────────────────────────────────────────────────────────────────

SCHEMA_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS duo_accounts (
    account_id      TEXT PRIMARY KEY,
    name            TEXT,
    api_hostname    TEXT,
    synced_at       TEXT
);
"""

SCHEMA_USERS = """
CREATE TABLE IF NOT EXISTS duo_users (
    user_id          TEXT NOT NULL,
    duo_account_id   TEXT NOT NULL,
    username         TEXT,
    email            TEXT,
    realname         TEXT,
    status           TEXT,
    is_enrolled      INTEGER,
    last_login       TEXT,
    created          TEXT,
    phones_count     INTEGER DEFAULT 0,
    tokens_count     INTEGER DEFAULT 0,
    groups           TEXT,
    notes            TEXT,
    raw_json         TEXT,
    PRIMARY KEY (user_id, duo_account_id)
);
"""


def init_db(conn):
    """Create the duo_users table if it doesn't exist."""
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA_ACCOUNTS)
    conn.execute(SCHEMA_USERS)
    conn.commit()


def _ts_to_iso(ts):
    """Convert a Unix timestamp (int or None) to ISO string."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_users(conn, users, duo_account_id):
    """Insert or replace all users for a given Duo account."""
    data = []
    for u in users:
        groups = ", ".join(g.get("name", "") for g in (u.get("groups") or []))
        data.append((
            u.get("user_id"),
            duo_account_id,
            u.get("username"),
            u.get("email"),
            u.get("realname"),
            u.get("status"),
            1 if u.get("is_enrolled") else 0,
            _ts_to_iso(u.get("last_login")),
            _ts_to_iso(u.get("created")),
            len(u.get("phones") or []),
            len(u.get("tokens") or []),
            groups or None,
            u.get("notes"),
            json.dumps(u),
        ))

    # Clear existing users for this account before re-inserting so that
    # deleted Duo users are removed from the local DB.
    conn.execute("DELETE FROM duo_users WHERE duo_account_id = ?", (duo_account_id,))

    conn.executemany(
        """INSERT INTO duo_users
           (user_id, duo_account_id, username, email, realname, status,
            is_enrolled, last_login, created, phones_count, tokens_count,
            groups, notes, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        data,
    )
    return len(data)


# ── Duo API helpers ───────────────────────────────────────────────────────────

def _duo_api_call(method, host, path, params, ikey, skey, timeout=30):
    """Make an HMAC-SHA1 signed request to the Duo API.

    `host` is the hostname used for both the signature and the request URL.
    For Accounts API calls this is the parent's api_hostname.
    For Admin API calls on a child account this is the child's api_hostname,
    but the ikey/skey are still the parent Accounts API credentials.
    """
    now = email.utils.formatdate()

    # Build canonical params string (sorted, URL-encoded)
    sorted_params = urllib.parse.urlencode(sorted(params.items()))

    # Build string to sign — host must match the request destination
    canon = "\n".join([now, method.upper(), host.lower(), path, sorted_params])
    sig = hmac.new(skey.encode(), canon.encode(), hashlib.sha1).hexdigest()
    auth = base64.b64encode(f"{ikey}:{sig}".encode()).decode()

    if method.upper() == "GET":
        url = f"https://{host}{path}"
        if sorted_params:
            url += f"?{sorted_params}"
        req = urllib.request.Request(url)
    else:
        url = f"https://{host}{path}"
        req = urllib.request.Request(url, data=sorted_params.encode())
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

    req.add_header("Date", now)
    req.add_header("Authorization", f"Basic {auth}")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def duo_api(method, host, path, params, ikey, skey, retries=3, backoff=10):
    """API call wrapper with exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            return _duo_api_call(method, host, path, params, ikey, skey)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            print(f"  HTTP {e.code} from Duo (attempt {attempt}/{retries}): {body}",
                  file=sys.stderr)
            if e.code == 429 or e.code >= 500:
                if attempt < retries:
                    wait = backoff * (2 ** (attempt - 1))
                    print(f"  Retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
            raise
        except urllib.error.URLError as e:
            print(f"  Network error (attempt {attempt}/{retries}): {e.reason}",
                  file=sys.stderr)
            if attempt < retries:
                wait = backoff * (2 ** (attempt - 1))
                print(f"  Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise

    raise RuntimeError(f"Duo API call failed after {retries} attempts")


# ── Accounts API (MSP parent) ────────────────────────────────────────────────

def list_child_accounts(host, ikey, skey):
    """List all child accounts under this MSP parent."""
    resp = duo_api("POST", host, "/accounts/v1/account/list", {}, ikey, skey)
    if resp.get("stat") != "OK":
        raise RuntimeError(f"Failed to list Duo accounts: {resp}")
    return resp.get("response", [])


# ── Fetch users (per child via parent creds) ────────────────────────────────

def fetch_all_users(child_host, account_id, parent_ikey, parent_skey):
    """Paginate through /admin/v1/users for a child account.

    Uses the parent's Accounts API credentials (ikey/skey) but signs and
    sends the request to the child's api_hostname with the child's
    account_id as a parameter.  This is the documented "Using Accounts API
    with Admin API" flow — no per-child Admin API application required.
    """
    users = []
    offset = 0
    limit = 300

    while True:
        params = {
            "account_id": account_id,
            "limit": str(limit),
            "offset": str(offset),
        }
        resp = duo_api("GET", child_host, "/admin/v1/users",
                       params, parent_ikey, parent_skey)

        if resp.get("stat") != "OK":
            raise RuntimeError(f"Duo API error: {resp.get('message', 'unknown')}")

        batch = resp.get("response", [])
        users.extend(batch)

        metadata = resp.get("metadata", {})
        if "next_offset" not in metadata:
            break
        offset = metadata["next_offset"]

    return users


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Duo MSP user data into SQLite")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print without writing")
    args = parser.parse_args()

    ikey, skey, host = _load_credentials()

    if not all([ikey, skey, host]):
        print("DUO_IKEY, DUO_SKEY, and DUO_HOST are not configured.")
        print("Set them via Admin → Settings or .env and restart this container.")
        sys.exit(10)

    print(f"Syncing Duo MSP users → {args.db}")
    print(f"  Accounts API host: {host}")

    # Write a lock file so the web app can show a "syncing" indicator.
    # Placed next to the DB on the shared Docker volume (/data/).
    lock_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), ".sync_running_duo")
    try:
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        lock_file = None  # Non-fatal — indicator just won't show

    start = datetime.now()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)

        # Step 1: List all child accounts
        print("  Listing child accounts...")
        children = list_child_accounts(host, ikey, skey)
        print(f"  Found {len(children)} child account(s)")

        if args.dry_run:
            for c in children:
                print(f"    {c.get('account_id')}  {c.get('name')}  ({c.get('api_hostname')})")
            print("  (dry-run — no DB writes)")
            return

        # Per-entity sync status tracking
        from sync_state import init_sync_state, TrackedSync
        init_sync_state(conn)

        total_users = 0
        errors = []

        with TrackedSync(conn, "duo", "duo_users") as t:
            for child in children:
                account_id = child["account_id"]
                account_name = child.get("name", account_id)
                child_host = child["api_hostname"]

                # Upsert account metadata so mappings UI can show names
                conn.execute(
                    "INSERT OR REPLACE INTO duo_accounts (account_id, name, api_hostname, synced_at) VALUES (?, ?, ?, ?)",
                    (account_id, account_name, child_host, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                )

                try:
                    # Step 2: Fetch users using parent creds + child hostname
                    print(f"  Syncing {account_name} ({account_id})...")
                    users = fetch_all_users(child_host, account_id, ikey, skey)
                    print(f"    Got {len(users)} users")

                    # Step 3: Upsert into duo_users
                    count = upsert_users(conn, users, account_id)
                    total_users += count
                    conn.commit()

                except Exception as e:
                    msg = f"{account_name}: {e}"
                    print(f"  ERROR syncing {msg}", file=sys.stderr)
                    errors.append(msg)
                    continue  # Don't let one child failure abort the whole sync

            t.record_count = total_users

        # Track duo_accounts as its own entity so the Sync Status UI shows a green dot
        with TrackedSync(conn, "duo", "duo_accounts") as t:
            t.record_count = len(children) - len(errors)

        elapsed = (datetime.now() - start).total_seconds()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO sync_log (synced_at, tables_synced, record_counts, duration_seconds) VALUES (?, ?, ?, ?)",
            (now, "duo_users", str(total_users), elapsed),
        )
        conn.commit()

    finally:
        conn.close()
        # Always remove the lock file so the indicator clears even on failure
        if lock_file and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass

    print(f"Duo sync complete. {total_users} users across {len(children)} accounts.")
    print(f"Duo sync complete. {total_users} users across {len(children)} accounts.")
    if errors:
        print(f"  {len(errors)} account(s) had errors:", file=sys.stderr)
        for e in errors:
            print(f"    {e}", file=sys.stderr)
        sys.exit(2)  # Partial failure


if __name__ == "__main__":
    main()
