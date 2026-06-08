#!/usr/bin/env python3
"""
ConnectWise Manage → SQLite Sync Engine

Pulls tickets, time entries, agreements, companies, and members from
ConnectWise Manage's REST API and stores them in a local SQLite database.

Usage:
    python sync_cw_data.py                  # Full sync
    python sync_cw_data.py --recent         # Only last 30 days of tickets/time
    python sync_cw_data.py --recent --days 7  # Only last 7 days

Reads CW credentials from ~/.config/connectwise/cw_config.json
(same config as the existing cw_api.py skill).
"""

import argparse
import base64
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone


# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cw_data.db")
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/connectwise/cw_config.json")


def load_config():
    config_path = os.environ.get("CW_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    # Fallback to env vars
    keys = {
        "CW_SITE": "site", "CW_COMPANY_ID": "company_id",
        "CW_PUBLIC_KEY": "public_key", "CW_PRIVATE_KEY": "private_key",
        "CW_CLIENT_ID": "client_id",
    }
    config = {}
    for env_key, conf_key in keys.items():
        val = os.environ.get(env_key)
        if not val:
            print(f"Error: Missing {env_key}. Set CW_CONFIG_PATH or env vars.", file=sys.stderr)
            sys.exit(1)
        config[conf_key] = val
    return config


# ── API helpers ───────────────────────────────────────────────────────────────

def build_headers(config):
    creds = f"{config['company_id']}+{config['public_key']}:{config['private_key']}"
    encoded = base64.b64encode(creds.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "clientId": config["client_id"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_base_url(config):
    site = config["site"].rstrip("/")
    if not site.startswith("http"):
        site = f"https://{site}"
    return f"{site}/v4_6_release/apis/3.0"


# Per-endpoint page-size overrides for endpoints where CW's server struggles
# to assemble large pages within its own internal timeout. service/tickets is
# the heaviest endpoint and routinely 500s with "A timeout has occurred" at
# pageSize=1000; halving it dramatically cuts the per-page response time.
ENDPOINT_PAGE_SIZE = {
    "service/tickets": 250,
    "time/entries":    250,
}


def _date_chunks(start_iso, end_iso, days=90):
    """Yield (chunk_start, chunk_end) ISO strings spanning [start_iso, end_iso).

    Used to split a long date-range query into smaller windows so that CW's
    server has bounded work per request — both fewer rows to assemble and
    less data to sort when an orderBy is applied. Each chunk is `days`
    long; the final chunk is truncated to end_iso.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cur = datetime.strptime(start_iso, fmt).replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_iso,   fmt).replace(tzinfo=timezone.utc)
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur.strftime(fmt), nxt.strftime(fmt)
        cur = nxt


def api_get(config, endpoint, params=None, page_size=1000, max_pages=1000):
    """GET with auto-pagination. Returns list or dict.

    Resilience model:
      * Per-endpoint page-size overrides for known-heavy endpoints.
      * Up to 4 retries per page with exponential backoff (10/20/40/80s).
      * On retry exhaustion for a single page, log loudly and SKIP the page
        rather than abandoning the whole sync — losing at most page_size
        records instead of every page beyond the failure point.
    """
    headers = build_headers(config)
    base_url = get_base_url(config)
    url = f"{base_url}/{endpoint.lstrip('/')}"
    ctx = ssl.create_default_context()
    all_results = []
    page = 1

    # Apply per-endpoint page-size override (caller's value wins if smaller).
    endpoint_key = endpoint.strip("/").lower()
    capped = ENDPOINT_PAGE_SIZE.get(endpoint_key)
    if capped and capped < page_size:
        page_size = capped

    max_retries = 4
    while page <= max_pages:
        qp = dict(params or {})
        qp["pageSize"] = str(page_size)
        qp["page"] = str(page)
        full_url = f"{url}?{urllib.parse.urlencode(qp)}"

        page_succeeded = False
        page_should_skip = False

        for attempt in range(1, max_retries + 1):
            req = urllib.request.Request(full_url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                    data = json.loads(resp.read().decode())
                    if isinstance(data, list):
                        if not data:
                            return all_results
                        all_results.extend(data)
                        if len(data) < page_size:
                            return all_results
                        page += 1
                    else:
                        return data
                page_succeeded = True
                break  # Success — exit retry loop
            except urllib.error.HTTPError as e:
                # MUST be before URLError (HTTPError is a subclass of URLError)
                body = e.read().decode() if e.fp else ""
                if e.code >= 500 and attempt < max_retries:
                    backoff = 10 * (2 ** (attempt - 1))  # 10, 20, 40, 80s
                    print(f"  Server error {e.code} on {endpoint} page {page} (attempt {attempt}/{max_retries}, sleeping {backoff}s): {body[:200]}", file=sys.stderr)
                    time.sleep(backoff)
                elif e.code >= 500:
                    # Retries exhausted on a 5xx — skip this page and continue.
                    print(f"  Server error {e.code} on {endpoint} page {page} after {max_retries} attempts — SKIPPING this page and continuing.", file=sys.stderr)
                    page_should_skip = True
                    break
                else:
                    # 4xx or other non-retryable — return what we have.
                    print(f"  API Error {e.code} on {endpoint}: {body[:200]}", file=sys.stderr)
                    return all_results
            except (TimeoutError, urllib.error.URLError) as e:
                reason = str(e.reason) if hasattr(e, 'reason') else str(e)
                if attempt < max_retries:
                    backoff = 10 * (2 ** (attempt - 1))
                    print(f"  Timeout/connection error on {endpoint} page {page} (attempt {attempt}/{max_retries}, sleeping {backoff}s): {reason}", file=sys.stderr)
                    time.sleep(backoff)
                else:
                    print(f"  Timeout on {endpoint} page {page} after {max_retries} attempts — SKIPPING this page and continuing.", file=sys.stderr)
                    page_should_skip = True
                    break

        # If the page was skipped after exhausting retries, advance and continue
        # rather than abandoning the entire sync.
        if not page_succeeded and page_should_skip:
            page += 1
            continue

    return all_results


# ── Database schema ───────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT NOT NULL,
    tables_synced TEXT,
    record_counts TEXT,
    duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    status_id INTEGER,
    status_name TEXT,
    board_id INTEGER,
    board_name TEXT,
    company_id INTEGER,
    company_name TEXT,
    contact_name TEXT,
    priority_id INTEGER,
    priority_name TEXT,
    severity TEXT,
    impact TEXT,
    type_name TEXT,
    sub_type_name TEXT,
    item_name TEXT,
    source_name TEXT,
    resources TEXT,
    assigned_to TEXT,
    date_entered TEXT,
    date_resolved TEXT,
    date_closed TEXT,
    required_date TEXT,
    estimated_hours REAL,
    actual_hours REAL,
    budget_hours REAL,
    has_child_ticket INTEGER,
    parent_ticket_id INTEGER,
    agreement_id INTEGER,
    agreement_name TEXT,
    site_name TEXT,
    address_line1 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS time_entries (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER,
    ticket_summary TEXT,
    company_id INTEGER,
    company_name TEXT,
    member_id INTEGER,
    member_identifier TEXT,
    member_name TEXT,
    time_start TEXT,
    time_end TEXT,
    actual_hours REAL,
    billable_option TEXT,
    charge_to_type TEXT,
    charge_to_id INTEGER,
    agreement_id INTEGER,
    agreement_name TEXT,
    work_type_name TEXT,
    work_role_name TEXT,
    notes TEXT,
    internal_notes TEXT,
    status TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS agreements (
    id INTEGER PRIMARY KEY,
    name TEXT,
    company_id INTEGER,
    company_name TEXT,
    type_id INTEGER,
    type_name TEXT,
    start_date TEXT,
    end_date TEXT,
    cancelled_flag INTEGER,
    bill_amount REAL,
    bill_cycle TEXT,
    bill_terms TEXT,
    billing_start_date TEXT,
    work_order TEXT,
    internal_notes TEXT,
    application_units TEXT,
    application_limit REAL,
    application_cycle TEXT,
    period_type TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS agreement_additions (
    id INTEGER PRIMARY KEY,
    agreement_id INTEGER,
    product_id INTEGER,
    product_identifier TEXT,
    description TEXT,
    quantity REAL,
    less_included REAL,
    unit_price REAL,
    unit_cost REAL,
    bill_customer TEXT,
    effective_date TEXT,
    cancelled_date TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    identifier TEXT,
    status_name TEXT,
    type_name TEXT,
    phone_number TEXT,
    website TEXT,
    territory_name TEXT,
    market_name TEXT,
    address_line1 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    country TEXT,
    default_contact_name TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    identifier TEXT,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT,
    title TEXT,
    email TEXT,
    inactive_flag INTEGER,
    work_type_name TEXT,
    work_role_name TEXT,
    default_location_name TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT,
    company_id INTEGER,
    company_name TEXT,
    title TEXT,
    email TEXT,
    phone TEXT,
    phone_extension TEXT,
    type_name TEXT,
    relationship TEXT,
    inactive_flag INTEGER,
    default_billing_flag INTEGER,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS configurations (
    id INTEGER PRIMARY KEY,
    name TEXT,
    type_name TEXT,
    status_name TEXT,
    company_id INTEGER,
    company_name TEXT,
    contact_name TEXT,
    site_name TEXT,
    serial_number TEXT,
    model_number TEXT,
    tag_number TEXT,
    manufacturer_name TEXT,
    installed_date TEXT,
    warranty_expiration TEXT,
    last_login_name TEXT,
    ip_address TEXT,
    os_type TEXT,
    os_info TEXT,
    notes TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT,
    company_id INTEGER,
    company_name TEXT,
    status_name TEXT,
    manager_name TEXT,
    board_name TEXT,
    estimated_start TEXT,
    estimated_end TEXT,
    actual_start TEXT,
    actual_end TEXT,
    actual_hours REAL,
    budget_hours REAL,
    scheduled_hours REAL,
    billing_method TEXT,
    billing_amount REAL,
    description TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    invoice_number TEXT,
    company_id INTEGER,
    company_name TEXT,
    status_name TEXT,
    type TEXT,
    date TEXT,
    due_date TEXT,
    total REAL,
    balance REAL,
    agreement_name TEXT,
    ticket_id INTEGER,
    project_id INTEGER,
    billing_type TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS catalog_items (
    id INTEGER PRIMARY KEY,
    identifier TEXT,
    description TEXT,
    category_name TEXT,
    subcategory_name TEXT,
    type_name TEXT,
    product_class TEXT,
    unit_of_measure TEXT,
    price REAL,
    cost REAL,
    manufacturer_name TEXT,
    vendor_name TEXT,
    inactive_flag INTEGER,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS ticket_notes (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL,
    text TEXT,
    detail_description_flag INTEGER DEFAULT 0,
    internal_analysis_flag INTEGER DEFAULT 0,
    resolution_flag INTEGER DEFAULT 0,
    member_name TEXT,
    contact_name TEXT,
    date_created TEXT,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_company ON tickets(company_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status_name);
CREATE INDEX IF NOT EXISTS idx_tickets_date ON tickets(date_entered);
CREATE INDEX IF NOT EXISTS idx_tickets_board ON tickets(board_name);
CREATE INDEX IF NOT EXISTS idx_notes_ticket ON ticket_notes(ticket_id);
CREATE INDEX IF NOT EXISTS idx_notes_date ON ticket_notes(date_created);
CREATE INDEX IF NOT EXISTS idx_time_ticket ON time_entries(ticket_id);
CREATE INDEX IF NOT EXISTS idx_time_member ON time_entries(member_identifier);
CREATE INDEX IF NOT EXISTS idx_time_date ON time_entries(time_start);
CREATE INDEX IF NOT EXISTS idx_time_agreement ON time_entries(agreement_id);
CREATE INDEX IF NOT EXISTS idx_agreements_company ON agreements(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_configs_company ON configurations(company_id);
CREATE INDEX IF NOT EXISTS idx_configs_type ON configurations(type_name);
CREATE INDEX IF NOT EXISTS idx_projects_company ON projects(company_id);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status_name);
CREATE INDEX IF NOT EXISTS idx_invoices_company ON invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date);
CREATE INDEX IF NOT EXISTS idx_catalog_category ON catalog_items(category_name);
"""


def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")  # Wait up to 30s if DB is locked
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Batch commit helper ──────────────────────────────────────────────────────
# Releasing the write lock every BATCH_SIZE rows lets the Flask web app (and
# other containers) slip in their small writes between batches, dramatically
# reducing "database is locked" errors under concurrent access.
BATCH_SIZE = 50


def _batch_commit(conn, counter):
    """Commit if counter has reached BATCH_SIZE. Returns 0 (reset counter).

    The brief sleep after commit gives other writers (web app, other sync
    containers) a guaranteed window to acquire the write lock. Without it
    the next INSERT reacquires the lock in microseconds — too fast for
    SQLite's busy handler to react.
    """
    if counter >= BATCH_SIZE:
        conn.commit()
        time.sleep(0.05)  # 50ms yield — imperceptible but enough for other writers
        return 0
    return counter


# ── Data extraction helpers ───────────────────────────────────────────────────

def safe_get(d, *keys, default=None):
    """Safely navigate nested dicts."""
    current = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current


def extract_ticket(t):
    return (
        t.get("id"),
        t.get("summary"),
        safe_get(t, "status", "id"),
        safe_get(t, "status", "name"),
        safe_get(t, "board", "id"),
        safe_get(t, "board", "name"),
        safe_get(t, "company", "id"),
        safe_get(t, "company", "name"),
        safe_get(t, "contact", "name"),
        safe_get(t, "priority", "id"),
        safe_get(t, "priority", "name"),
        safe_get(t, "severity"),
        safe_get(t, "impact"),
        safe_get(t, "type", "name"),
        safe_get(t, "subType", "name"),
        safe_get(t, "item", "name"),
        safe_get(t, "source", "name"),
        t.get("resources"),
        safe_get(t, "owner", "name") or t.get("resources"),
        safe_get(t, "_info", "dateEntered"),
        t.get("dateResolved"),
        t.get("closedDate"),
        t.get("requiredDate"),
        t.get("estimatedExpenseAmount"),  # estimated hours
        t.get("actualHours"),
        t.get("budgetHours"),
        1 if t.get("hasChildTicket") else 0,
        safe_get(t, "parentTicketId"),
        safe_get(t, "agreement", "id"),
        safe_get(t, "agreement", "name"),
        safe_get(t, "site", "name"),
        safe_get(t, "addressLine1"),
        safe_get(t, "city"),
        safe_get(t, "stateIdentifier"),
        safe_get(t, "zip"),
        json.dumps(t),
    )


def extract_time_entry(te):
    return (
        te.get("id"),
        safe_get(te, "ticket", "id"),
        safe_get(te, "ticket", "summary"),
        safe_get(te, "company", "id"),
        safe_get(te, "company", "name"),
        safe_get(te, "member", "id"),
        safe_get(te, "member", "identifier"),
        safe_get(te, "member", "name"),
        te.get("timeStart"),
        te.get("timeEnd"),
        te.get("actualHours"),
        te.get("billableOption"),
        safe_get(te, "chargeToType"),
        safe_get(te, "chargeToId"),
        safe_get(te, "agreement", "id"),
        safe_get(te, "agreement", "name"),
        safe_get(te, "workType", "name"),
        safe_get(te, "workRole", "name"),
        te.get("notes"),
        te.get("internalNotes"),
        te.get("status"),
        json.dumps(te),
    )


def extract_agreement(a):
    return (
        a.get("id"),
        a.get("name"),
        safe_get(a, "company", "id"),
        safe_get(a, "company", "name"),
        safe_get(a, "type", "id"),
        safe_get(a, "type", "name"),
        a.get("startDate"),
        a.get("endDate"),
        1 if a.get("cancelledFlag") else 0,
        a.get("billAmount"),
        safe_get(a, "billCycleId") or a.get("billCycle"),
        safe_get(a, "billTermsId") or a.get("billTerms"),
        a.get("billingStartDate"),
        a.get("workOrder"),
        a.get("internalNotes"),
        a.get("applicationUnits"),
        a.get("applicationLimit"),
        a.get("applicationCycle"),
        a.get("periodType"),
        json.dumps(a),
    )


def extract_agreement_addition(aa, agreement_id):
    return (
        aa.get("id"),
        agreement_id,
        safe_get(aa, "product", "id"),
        safe_get(aa, "product", "identifier"),
        aa.get("description"),
        aa.get("quantity"),
        aa.get("lessIncluded"),
        aa.get("unitPrice"),
        aa.get("unitCost"),
        aa.get("billCustomer"),
        aa.get("effectiveDate"),
        aa.get("cancelledDate"),
        json.dumps(aa),
    )


def extract_company(c):
    return (
        c.get("id"),
        c.get("name"),
        c.get("identifier"),
        safe_get(c, "status", "name"),
        safe_get(c, "type", "name") or safe_get(c, "types", 0, "name") if isinstance(c.get("types"), list) and c["types"] else safe_get(c, "type", "name"),
        c.get("phoneNumber"),
        c.get("website"),
        safe_get(c, "territory", "name"),
        safe_get(c, "market", "name"),
        c.get("addressLine1"),
        c.get("city"),
        c.get("state"),
        c.get("zip"),
        safe_get(c, "country", "name"),
        safe_get(c, "defaultContact", "name"),
        json.dumps(c),
    )


def extract_member(m):
    return (
        m.get("id"),
        m.get("identifier"),
        m.get("firstName"),
        m.get("lastName"),
        f"{m.get('firstName', '')} {m.get('lastName', '')}".strip(),
        m.get("title"),
        m.get("email") or safe_get(m, "officeEmail"),
        1 if m.get("inactiveFlag") else 0,
        safe_get(m, "workType", "name"),
        safe_get(m, "workRole", "name"),
        safe_get(m, "defaultLocation", "name"),
        json.dumps(m),
    )


def extract_contact(c):
    return (
        c.get("id"),
        c.get("firstName"),
        c.get("lastName"),
        f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
        safe_get(c, "company", "id"),
        safe_get(c, "company", "name"),
        c.get("title"),
        safe_get(c, "communicationItems", 0, "value") if isinstance(c.get("communicationItems"), list) and c["communicationItems"] else None,
        c.get("phoneNumber") or c.get("phone"),
        c.get("phoneExtension"),
        safe_get(c, "type", "name"),
        safe_get(c, "relationship", "name") if isinstance(c.get("relationship"), dict) else c.get("relationship"),
        1 if c.get("inactiveFlag") else 0,
        1 if c.get("defaultBillingFlag") else 0,
        json.dumps(c),
    )


def extract_configuration(cfg):
    return (
        cfg.get("id"),
        cfg.get("name"),
        safe_get(cfg, "type", "name"),
        safe_get(cfg, "status", "name"),
        safe_get(cfg, "company", "id"),
        safe_get(cfg, "company", "name"),
        safe_get(cfg, "contact", "name"),
        safe_get(cfg, "site", "name"),
        cfg.get("serialNumber"),
        cfg.get("modelNumber"),
        cfg.get("tagNumber"),
        safe_get(cfg, "manufacturer", "name"),
        cfg.get("installedDate"),
        cfg.get("warrantyExpirationDate"),
        cfg.get("lastLoginName"),
        cfg.get("ipAddress"),
        cfg.get("osType"),
        cfg.get("osInfo"),
        cfg.get("notes"),
        json.dumps(cfg),
    )


def extract_project(p):
    return (
        p.get("id"),
        p.get("name"),
        safe_get(p, "company", "id"),
        safe_get(p, "company", "name"),
        safe_get(p, "status", "name"),
        safe_get(p, "manager", "name"),
        safe_get(p, "board", "name"),
        p.get("estimatedStart"),
        p.get("estimatedEnd"),
        p.get("actualStart"),
        p.get("actualEnd"),
        p.get("actualHours"),
        p.get("budgetHours"),
        p.get("scheduledHours"),
        p.get("billingMethod"),
        p.get("billingAmount"),
        p.get("description"),
        json.dumps(p),
    )


def extract_invoice(inv):
    return (
        inv.get("id"),
        inv.get("invoiceNumber"),
        safe_get(inv, "company", "id"),
        safe_get(inv, "company", "name"),
        safe_get(inv, "status", "name") or inv.get("status"),
        inv.get("type"),
        inv.get("date"),
        inv.get("dueDate"),
        inv.get("total"),
        inv.get("balance"),
        safe_get(inv, "agreement", "name"),
        safe_get(inv, "ticket", "id"),
        safe_get(inv, "project", "id"),
        inv.get("billingType"),
        json.dumps(inv),
    )


def extract_catalog_item(ci):
    return (
        ci.get("id"),
        ci.get("identifier"),
        ci.get("description"),
        safe_get(ci, "category", "name"),
        safe_get(ci, "subcategory", "name"),
        safe_get(ci, "type", "name"),
        ci.get("productClass"),
        safe_get(ci, "unitOfMeasure", "name") if isinstance(ci.get("unitOfMeasure"), dict) else ci.get("unitOfMeasure"),
        ci.get("price"),
        ci.get("cost"),
        safe_get(ci, "manufacturer", "name"),
        safe_get(ci, "vendor", "name"),
        1 if ci.get("inactiveFlag") else 0,
        json.dumps(ci),
    )


# ── Sync functions ────────────────────────────────────────────────────────────

def sync_tickets(config, conn, since_date=None):
    print("Syncing tickets...")

    cutoff = since_date or (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%dT00:00:00Z")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if since_date:
        # Incremental sync — filter on lastUpdated, NOT dateEntered. This is
        # critical: a ticket created months ago that gets a status change,
        # note, or moves to closed today must be picked up by the hourly
        # sync. Using dateEntered would silently miss those updates and let
        # the agent answer with stale data until the next full sync.
        print(f"  Fetching tickets updated since {cutoff}...")
        all_tickets = api_get(config, "service/tickets", {
            "conditions": f"lastUpdated>=[{cutoff}]",
            "orderBy": "lastUpdated desc",
        })
        print(f"    Got {len(all_tickets)} tickets")
    else:
        # Full sync — split the 2-year window into 90-day chunks. CW's server
        # times out when asked to sort/return very large result sets in one
        # go ("A timeout has occurred"); chunking bounds per-request work to
        # roughly one quarter of tickets, which it can handle reliably.
        # Anchored on dateEntered so the 2-year window is stable across runs.
        all_tickets = []
        chunk_count = 0
        for chunk_start, chunk_end in _date_chunks(cutoff, now_iso, days=90):
            chunk_count += 1
            print(f"  Fetching tickets {chunk_start[:10]} → {chunk_end[:10]} (chunk {chunk_count})...")
            chunk = api_get(config, "service/tickets", {
                "conditions": f"dateEntered>=[{chunk_start}] and dateEntered<[{chunk_end}]",
                "orderBy": "dateEntered desc",
            })
            print(f"    Got {len(chunk)} tickets in this chunk")
            all_tickets.extend(chunk)
        print(f"  Total: {len(all_tickets)} tickets across {chunk_count} chunks")

    # Upsert fetched tickets (no DELETE — avoids empty-table window)
    batch = 0
    for t in all_tickets:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_ticket(t)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped ticket {t.get('id')}: {e}", file=sys.stderr)

    # On full sync, prune any tickets older than the 2-year window
    if not since_date:
        pruned = conn.execute("DELETE FROM tickets WHERE date_entered < ?", (cutoff,)).rowcount
        if pruned:
            print(f"    Pruned {pruned} tickets older than cutoff")

    # Backfill any tickets where date fields are NULL but raw_json has the values.
    conn.execute("""
        UPDATE tickets SET
            date_entered  = json_extract(raw_json, '$._info.dateEntered'),
            date_closed   = json_extract(raw_json, '$.closedDate'),
            required_date = json_extract(raw_json, '$.requiredDate')
        WHERE (date_entered IS NULL OR date_closed IS NULL OR required_date IS NULL)
          AND raw_json IS NOT NULL
    """)

    conn.commit()
    print(f"  Stored {len(all_tickets)} tickets")
    return len(all_tickets)


def sync_time_entries(config, conn, since_date=None):
    print("Syncing time entries...")
    cutoff = since_date or (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%dT00:00:00Z")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if since_date:
        # Incremental — filter on lastUpdated so we pick up edits to entries
        # logged before the cutoff (technicians often back-fill or correct
        # time after the fact). Using timeStart would miss those edits.
        entries = api_get(config, "time/entries",
                          {"conditions": f"lastUpdated>=[{cutoff}]", "orderBy": "lastUpdated desc"})
        print(f"  Fetched {len(entries)} time entries")
    else:
        # Full sync — chunk by 90-day windows on timeStart (the natural
        # anchor for the 2-year window) to keep each request bounded.
        entries = []
        chunk_count = 0
        for chunk_start, chunk_end in _date_chunks(cutoff, now_iso, days=90):
            chunk_count += 1
            print(f"  Fetching time entries {chunk_start[:10]} → {chunk_end[:10]} (chunk {chunk_count})...")
            chunk = api_get(config, "time/entries", {
                "conditions": f"timeStart>=[{chunk_start}] and timeStart<[{chunk_end}]",
                "orderBy": "timeStart desc",
            })
            print(f"    Got {len(chunk)} entries in this chunk")
            entries.extend(chunk)
        print(f"  Total: {len(entries)} time entries across {chunk_count} chunks")

    # Upsert fetched entries (no DELETE — avoids empty-table window)
    batch = 0
    for te in entries:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO time_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_time_entry(te)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped time entry {te.get('id')}: {e}", file=sys.stderr)

    # On full sync, prune entries older than the 2-year window
    if not since_date:
        pruned = conn.execute("DELETE FROM time_entries WHERE time_start < ?", (cutoff,)).rowcount
        if pruned:
            print(f"    Pruned {pruned} time entries older than cutoff")

    conn.commit()
    return len(entries)


def sync_agreements(config, conn):
    print("Syncing agreements (all, including cancelled/expired)...")
    agreements = api_get(config, "finance/agreements")
    print(f"  Fetched {len(agreements)} agreements")

    fetched_ids = set()
    batch = 0
    for a in agreements:
        fetched_ids.add(a.get("id"))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO agreements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_agreement(a)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped agreement {a.get('id')}: {e}", file=sys.stderr)
            continue

        # Fetch additions for each agreement
        aid = a.get("id")
        if aid:
            additions = api_get(config, f"finance/agreements/{aid}/additions")
            for aa in additions:
                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO agreement_additions
                           (id, agreement_id, product_id, product_identifier,
                            description, quantity, less_included, unit_price,
                            unit_cost, bill_customer, effective_date,
                            cancelled_date, raw_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        extract_agreement_addition(aa, aid)
                    )
                    batch += 1
                    batch = _batch_commit(conn, batch)
                except Exception as e:
                    print(f"  Warning: Skipped addition {aa.get('id')}: {e}", file=sys.stderr)

    conn.commit()
    return len(agreements)


def sync_companies(config, conn):
    print("Syncing companies...")
    companies = api_get(config, "company/companies", {
        "conditions": "status/name='Active'",
        "fields": "id,name,identifier,status,type,types,phoneNumber,website,territory,market,addressLine1,city,state,zip,country,defaultContact"
    })
    print(f"  Fetched {len(companies)} companies")

    batch = 0
    for c in companies:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_company(c)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped company {c.get('id')}: {e}", file=sys.stderr)
    conn.commit()
    return len(companies)


def sync_members(config, conn):
    print("Syncing members...")
    members = api_get(config, "system/members", {"conditions": "inactiveFlag=false"})
    print(f"  Fetched {len(members)} active members")

    batch = 0
    for m in members:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO members VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_member(m)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped member {m.get('id')}: {e}", file=sys.stderr)
    conn.commit()
    return len(members)


def sync_contacts(config, conn):
    print("Syncing contacts...")
    contacts = api_get(config, "company/contacts", {
        "conditions": "inactiveFlag=false",
    })
    print(f"  Fetched {len(contacts)} contacts")

    batch = 0
    for c in contacts:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_contact(c)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped contact {c.get('id')}: {e}", file=sys.stderr)
    conn.commit()
    return len(contacts)


def sync_configurations(config, conn):
    print("Syncing configurations (assets)...")
    configs = api_get(config, "company/configurations", {
        "conditions": "activeFlag=true",
    })
    print(f"  Fetched {len(configs)} configurations")

    batch = 0
    for cfg in configs:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO configurations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_configuration(cfg)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped config {cfg.get('id')}: {e}", file=sys.stderr)
    conn.commit()
    return len(configs)


def sync_projects(config, conn):
    print("Syncing projects...")
    projects = api_get(config, "project/projects", {
        "conditions": "closedFlag=false",
        "orderBy": "id desc",
    })
    print(f"  Fetched {len(projects)} active projects")

    batch = 0
    for p in projects:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_project(p)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped project {p.get('id')}: {e}", file=sys.stderr)
    conn.commit()
    return len(projects)


def sync_invoices(config, conn, since_date=None):
    print("Syncing invoices...")
    cutoff = since_date or (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%dT00:00:00Z")
    conditions = f"date>=[{cutoff}]"

    invoices = api_get(config, "finance/invoices", {
        "conditions": conditions,
        "orderBy": "date desc",
    })
    print(f"  Fetched {len(invoices)} invoices")

    batch = 0
    for inv in invoices:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_invoice(inv)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped invoice {inv.get('id')}: {e}", file=sys.stderr)

    # On full sync, prune invoices older than the 2-year window
    if not since_date:
        conn.execute("DELETE FROM invoices WHERE date < ?", (cutoff,))

    conn.commit()
    return len(invoices)


def extract_ticket_note(n, ticket_id):
    return (
        n.get("id"),
        ticket_id,
        n.get("text"),
        1 if n.get("detailDescriptionFlag") else 0,
        1 if n.get("internalAnalysisFlag") else 0,
        1 if n.get("resolutionFlag") else 0,
        safe_get(n, "member", "name"),
        n.get("contactName"),
        n.get("dateCreated"),
        json.dumps(n),
    )


def _notes_synced_recently(conn, hours=12):
    """Return True if ticket_notes were synced within the last `hours` hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        row = conn.execute("""
            SELECT synced_at FROM sync_log
            WHERE synced_at >= ? AND tables_synced LIKE '%ticket_notes%'
            ORDER BY id DESC LIMIT 1
        """, (cutoff,)).fetchone()
        return row is not None
    except Exception:
        return False


def sync_ticket_notes(config, conn):
    """Sync notes for recent/open tickets only.

    Only fetches notes for tickets created in the last 90 days to keep
    the API call count manageable (1 call per ticket). Notes older than
    6 months are pruned. Skipped if notes were synced within 12 hours.
    """
    if _notes_synced_recently(conn, hours=12):
        count = conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0]
        print(f"  Skipping notes sync — last run was less than 12 hours ago ({count} notes currently stored)")
        return count, False  # (count, actually_synced)

    print("Syncing ticket notes (for tickets from last 90 days)...")

    notes_cutoff_dt = datetime.now(timezone.utc) - timedelta(days=180)
    notes_cutoff_db = notes_cutoff_dt.strftime("%Y-%m-%d")

    # Prune notes older than 6 months
    deleted = conn.execute(
        "DELETE FROM ticket_notes WHERE date_created < ?", (notes_cutoff_db,)
    ).rowcount
    conn.commit()
    if deleted:
        print(f"  Purged {deleted} notes older than 6 months")

    # Only fetch notes for tickets from the last 90 days (not the full 2 years)
    ticket_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    ticket_ids = [row[0] for row in conn.execute(
        "SELECT id FROM tickets WHERE date_entered >= ?", (ticket_cutoff,)
    ).fetchall()]
    print(f"  Fetching notes for {len(ticket_ids)} recent tickets...")

    total = 0
    batch = 0
    for ticket_id in ticket_ids:
        notes = api_get(
            config,
            f"service/tickets/{ticket_id}/notes",
            {"orderBy": "dateCreated asc"},
        )
        for n in notes:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO ticket_notes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    extract_ticket_note(n, ticket_id),
                )
                total += 1
                batch += 1
                batch = _batch_commit(conn, batch)
            except Exception as e:
                print(f"  Warning: Skipped note {n.get('id')} on ticket {ticket_id}: {e}", file=sys.stderr)

    conn.commit()
    print(f"  Stored {total} notes across {len(ticket_ids)} tickets")
    return total, True  # (count, actually_synced)


def sync_catalog(config, conn):
    print("Syncing catalog items...")
    items = api_get(config, "procurement/catalog", {
        "conditions": "inactiveFlag=false",
    })
    print(f"  Fetched {len(items)} catalog items")

    batch = 0
    for ci in items:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO catalog_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                extract_catalog_item(ci)
            )
            batch += 1
            batch = _batch_commit(conn, batch)
        except Exception as e:
            print(f"  Warning: Skipped catalog item {ci.get('id')}: {e}", file=sys.stderr)
    conn.commit()
    return len(items)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync ConnectWise Manage data to SQLite")
    parser.add_argument("--recent", action="store_true", help="Only sync recent data (default: 30 days)")
    parser.add_argument("--days", type=int, default=30, help="Number of days for --recent (default: 30)")
    parser.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    start = datetime.now()
    print(f"ConnectWise → SQLite sync starting at {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {args.db}")

    # Write a lock file so the web app can show a "syncing" indicator.
    # Placed next to the DB on the shared Docker volume (/data/).
    lock_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), ".sync_running_cw")
    try:
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        lock_file = None  # Non-fatal — indicator just won't show

    try:
        config = load_config()
        conn = init_db(args.db)

        # Per-entity status tracking for /api/sync/status visibility.
        from sync_state import init_sync_state, TrackedSync
        init_sync_state(conn)

        since_date = None
        if args.recent:
            since_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT00:00:00Z")
            print(f"Recent mode: syncing data from last {args.days} days")

        counts = {}

        def _run(entity, fn):
            with TrackedSync(conn, "cw", entity) as t:
                n = fn()
                t.record_count = n
                counts[entity] = n

        _run("tickets",        lambda: sync_tickets(config, conn, since_date))
        _run("time_entries",   lambda: sync_time_entries(config, conn, since_date))
        _run("agreements",     lambda: sync_agreements(config, conn))
        _run("companies",      lambda: sync_companies(config, conn))
        _run("members",        lambda: sync_members(config, conn))
        _run("contacts",       lambda: sync_contacts(config, conn))
        _run("configurations", lambda: sync_configurations(config, conn))
        _run("projects",       lambda: sync_projects(config, conn))
        _run("invoices",       lambda: sync_invoices(config, conn, since_date))
        _run("catalog_items",  lambda: sync_catalog(config, conn))

        # ticket_notes returns a tuple — handle separately so we still get tracking.
        from sync_state import record_started, record_finished, record_failed
        record_started(conn, "cw", "ticket_notes")
        try:
            notes_count, notes_actually_synced = sync_ticket_notes(config, conn)
            counts["ticket_notes"] = notes_count
            record_finished(conn, "cw", "ticket_notes", record_count=notes_count)
        except Exception as e:
            record_failed(conn, "cw", "ticket_notes", error=e)
            raise

        elapsed = (datetime.now() - start).total_seconds()

        # Only include ticket_notes in tables_synced when notes were actually fetched
        # (not when the 12-hour cooldown caused them to be skipped). This lets
        # /api/status find the real last-notes-sync time separately from the main sync.
        tables_synced_list = list(counts.keys())
        if not notes_actually_synced:
            tables_synced_list = [t for t in tables_synced_list if t != "ticket_notes"]

        # Log the sync
        conn.execute(
            "INSERT INTO sync_log (synced_at, tables_synced, record_counts, duration_seconds) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), ",".join(tables_synced_list), json.dumps(counts), elapsed)
        )
        conn.commit()
        conn.close()

        print(f"\nSync complete in {elapsed:.1f}s")
        for table, count in counts.items():
            print(f"  {table}: {count} records")
        print(f"\nDatabase saved to: {args.db}")

    finally:
        # Always remove the lock file so the indicator clears even on failure
        if lock_file and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass



if __name__ == "__main__":
    main()
