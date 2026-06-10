#!/usr/bin/env python3
"""
parse_timeline.py — Extract future dates from ticket notes and store them
as timeline events.

Runs incrementally: only processes notes added since the last run (tracked
via the `last_parsed_note_id` watermark in `timeline_meta`).  Each unique
(ticket_id, parsed_date) pair is stored once.

Usage:
    python parse_timeline.py --db /data/cw_data.db              # Normal run
    python parse_timeline.py --db /data/cw_data.db --reparse    # Reset and reparse all
    python parse_timeline.py --db /data/cw_data.db --dry-run    # Show matches, no writes
"""

import argparse
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

log = logging.getLogger("chatpsa.timeline")

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = os.environ.get(
    "CW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cw_data.db"),
)

from config import APP_TZ
LOCAL_TZ = APP_TZ

# Signature markers — if a match appears after one of these, skip it
SIGNATURE_MARKERS = re.compile(
    r"^(?:--|Regards|Kind regards|Best regards|Cheers|Thanks|Sent from|_{3,}|"
    r"From:|To:|Subject:|Date:)",
    re.IGNORECASE | re.MULTILINE,
)

# Month name mappings
MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_FULL = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
ALL_MONTHS = {**MONTH_ABBR, **MONTH_FULL}

# Build month alternation strings for regex
_abbr_alt = "|".join(MONTH_ABBR.keys())
_full_alt = "|".join(MONTH_FULL.keys())
_all_alt = "|".join(ALL_MONTHS.keys())


# ── Regex patterns ────────────────────────────────────────────────────────────
# Each entry: (pattern_name, compiled_regex, parser_function)
# Parser functions return a date object or None.

def _parse_dmy_slash(m):
    """DD/MM/YYYY — prefer DD/MM (Australian). Fall back to MM/DD if day > 12."""
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if d > 12 and mo <= 12:
        # Unambiguous DD/MM
        pass
    elif mo > 12 and d <= 12:
        # Must be MM/DD (US)
        d, mo = mo, d
    # else: ambiguous — prefer DD/MM (Australian convention)
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_dmy_dash(m):
    """DD-MM-YYYY"""
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if d > 12 and mo <= 12:
        pass
    elif mo > 12 and d <= 12:
        d, mo = mo, d
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_ymd(m):
    """YYYY-MM-DD (ISO)"""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_dd_mon_yyyy(m):
    """DD Mon YYYY or DD Month YYYY"""
    d = int(m.group(1))
    mo_str = m.group(2).lower()
    y = int(m.group(3))
    mo = ALL_MONTHS.get(mo_str)
    if not mo:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_ddth_month_yyyy(m):
    """DDth Month YYYY — e.g. '15th May 2026', '1st of June 2026'"""
    d = int(m.group(1))
    # group 2 = ordinal suffix, group 3 = optional 'of ', group 4 = month
    mo_str = m.group(4).lower()
    y = int(m.group(5))
    mo = ALL_MONTHS.get(mo_str)
    if not mo:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_ddth_month_noyear(m):
    """DDth Month (no year) — assume next occurrence."""
    d = int(m.group(1))
    mo_str = m.group(4).lower()
    mo = ALL_MONTHS.get(mo_str)
    if not mo:
        return None
    today = datetime.now(LOCAL_TZ).date()
    try:
        candidate = date(today.year, mo, d)
    except ValueError:
        return None
    if candidate <= today:
        try:
            candidate = date(today.year + 1, mo, d)
        except ValueError:
            return None
    return candidate


def _parse_day_ddmm(m):
    """Day DD/MM — e.g. 'Fri 23/05'. No year; assume next occurrence."""
    d, mo = int(m.group(2)), int(m.group(3))
    if d > 12 and mo <= 12:
        pass
    elif mo > 12 and d <= 12:
        d, mo = mo, d
    today = datetime.now(LOCAL_TZ).date()
    try:
        candidate = date(today.year, mo, d)
    except ValueError:
        return None
    if candidate <= today:
        try:
            candidate = date(today.year + 1, mo, d)
        except ValueError:
            return None
    return candidate


# ── Relative date parsers (need a reference date) ────────────────────────────

WEEKDAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _parse_next_weekday(m, ref_date):
    """'next Monday', 'next Fri' — the named day in the FOLLOWING week."""
    day_name = m.group(1).lower()
    target_wd = WEEKDAY_MAP.get(day_name)
    if target_wd is None:
        return None
    days_ahead = (target_wd - ref_date.weekday()) % 7
    # "next" always means the following week — add 7 if it would land this week
    if days_ahead <= 0:
        days_ahead += 7
    # Always push to next week for "next X"
    if days_ahead <= 7 and days_ahead > 0:
        candidate = ref_date + timedelta(days=days_ahead)
        # If that's still this week (same ISO week), push another 7
        if candidate.isocalendar()[1] == ref_date.isocalendar()[1]:
            days_ahead += 7
    return ref_date + timedelta(days=days_ahead)


def _parse_this_weekday(m, ref_date):
    """'this Monday', 'this Friday', 'on Wednesday' — named day this week or next."""
    day_name = m.group(1).lower()
    target_wd = WEEKDAY_MAP.get(day_name)
    if target_wd is None:
        return None
    days_ahead = (target_wd - ref_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # Same day means next occurrence
    return ref_date + timedelta(days=days_ahead)


def _parse_tomorrow(m, ref_date):
    """'tomorrow'"""
    return ref_date + timedelta(days=1)


def _parse_in_n_weeks(m, ref_date):
    """'in 2 weeks', 'in 3 weeks', 'in a week', 'in two weeks'"""
    WORD_TO_NUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}
    n_str = m.group(1).lower()
    if n_str in WORD_TO_NUM:
        n = WORD_TO_NUM[n_str]
    else:
        try:
            n = int(n_str)
        except ValueError:
            return None
    # Anchor to the Monday of that week
    target = ref_date + timedelta(weeks=n)
    # Shift to Monday
    target = target - timedelta(days=target.weekday())
    return target


def _parse_fortnight(m, ref_date):
    """'in a fortnight', 'a fortnight', 'fortnight'"""
    target = ref_date + timedelta(weeks=2)
    target = target - timedelta(days=target.weekday())
    return target


def _parse_next_week(m, ref_date):
    """'next week' — Monday of the following week."""
    days_to_next_monday = (7 - ref_date.weekday()) % 7
    if days_to_next_monday == 0:
        days_to_next_monday = 7
    return ref_date + timedelta(days=days_to_next_monday)


def _parse_weekday_the_nth(m, ref_date):
    """'Monday the 15th', 'Friday 23rd' — resolve to that day number in current/next month."""
    d = int(m.group(2))
    today = ref_date
    # Try current month first
    try:
        candidate = date(today.year, today.month, d)
        if candidate > today:
            return candidate
    except ValueError:
        pass
    # Try next month
    if today.month == 12:
        try:
            return date(today.year + 1, 1, d)
        except ValueError:
            return None
    else:
        try:
            return date(today.year, today.month + 1, d)
        except ValueError:
            return None


def _parse_in_month(m, ref_date):
    """'in June', 'by August', 'due in May' — anchor to 1st of that month."""
    mo_str = m.group(1).lower()
    mo = ALL_MONTHS.get(mo_str)
    if not mo:
        return None
    today = ref_date
    try:
        candidate = date(today.year, mo, 1)
    except ValueError:
        return None
    if candidate <= today:
        try:
            candidate = date(today.year + 1, mo, 1)
        except ValueError:
            return None
    return candidate


# ── Pattern definitions ──────────────────────────────────────────────────────
# Each entry: (pattern_name, compiled_regex, parser_function, confidence)
# Parser functions: absolute patterns take (match), relative patterns take (match, ref_date)
# confidence: "confirmed" for explicit dates, "likely" for relative/inferred dates

PATTERNS = [
    # ── Confirmed: explicit date formats ─────────────────────────────────
    (
        "YYYY-MM-DD",
        re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
        _parse_ymd,
        "confirmed",
    ),
    (
        "DDth_Month_YYYY",
        re.compile(
            r"\b(\d{1,2})(st|nd|rd|th)\s+(of\s+)?(" + _all_alt + r")\s+(\d{4})\b",
            re.IGNORECASE,
        ),
        _parse_ddth_month_yyyy,
        "confirmed",
    ),
    (
        "DD_Month_YYYY",
        re.compile(
            r"\b(\d{1,2})\s+(" + _full_alt + r")\s+(\d{4})\b",
            re.IGNORECASE,
        ),
        _parse_dd_mon_yyyy,
        "confirmed",
    ),
    (
        "DD_Mon_YYYY",
        re.compile(
            r"\b(\d{1,2})\s+(" + _abbr_alt + r")\s+(\d{4})\b",
            re.IGNORECASE,
        ),
        _parse_dd_mon_yyyy,
        "confirmed",
    ),
    (
        "DDth_Month",
        re.compile(
            r"\b(\d{1,2})(st|nd|rd|th)\s+(of\s+)?(" + _all_alt + r")\b",
            re.IGNORECASE,
        ),
        _parse_ddth_month_noyear,
        "confirmed",
    ),
    (
        "DD/MM/YYYY",
        re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
        _parse_dmy_slash,
        "confirmed",
    ),
    (
        "DD-MM-YYYY",
        re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"),
        _parse_dmy_dash,
        "confirmed",
    ),
    (
        "Day_DD/MM",
        re.compile(
            r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+(\d{1,2})/(\d{1,2})\b",
            re.IGNORECASE,
        ),
        _parse_day_ddmm,
        "confirmed",
    ),

    # ── Likely: relative and inferred dates ──────────────────────────────
    (
        "next_weekday",
        re.compile(
            r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
            r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
            re.IGNORECASE,
        ),
        _parse_next_weekday,
        "likely",
    ),
    (
        "this_weekday",
        re.compile(
            r"\b(?:this|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
            r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
            re.IGNORECASE,
        ),
        _parse_this_weekday,
        "likely",
    ),
    (
        "tomorrow",
        re.compile(r"\btomorrow\b", re.IGNORECASE),
        _parse_tomorrow,
        "likely",
    ),
    (
        "in_N_weeks",
        re.compile(
            r"\bin\s+(a|one|two|three|four|\d{1,2})\s+weeks?\b",
            re.IGNORECASE,
        ),
        _parse_in_n_weeks,
        "likely",
    ),
    (
        "fortnight",
        re.compile(r"\b(?:in\s+a\s+)?fortnight\b", re.IGNORECASE),
        _parse_fortnight,
        "likely",
    ),
    (
        "next_week",
        re.compile(r"\bnext\s+week\b", re.IGNORECASE),
        _parse_next_week,
        "likely",
    ),
    (
        "weekday_the_Nth",
        re.compile(
            r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
            r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)"
            r"\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b",
            re.IGNORECASE,
        ),
        _parse_weekday_the_nth,
        "likely",
    ),
    (
        "in_month",
        re.compile(
            r"\b(?:in|by|during|due\s+in|due\s+by)\s+(" + _all_alt + r")\b",
            re.IGNORECASE,
        ),
        _parse_in_month,
        "likely",
    ),
]


# ── Filtering ─────────────────────────────────────────────────────────────────

def _is_in_signature(text, match_start):
    """Return True if the match position is after a signature marker."""
    # Search for signature markers before the match position
    for m in SIGNATURE_MARKERS.finditer(text):
        if m.start() < match_start:
            # Check that the marker is reasonably close (within 500 chars)
            # and no substantial content follows that resets context
            # text[m.start():match_start] intentionally skipped
            # If there's a blank-line gap after the marker, the signature
            # section might have ended — but for simplicity, if a marker
            # appears anywhere before the match in the same note, flag it.
            # This is intentionally aggressive; we can refine later via the
            # debug view.
            return True
    return False


def _extract_context(text, start, end, radius=100):
    """Extract ~radius chars on either side of the match."""
    ctx_start = max(0, start - radius)
    ctx_end = min(len(text), end + radius)
    snippet = text[ctx_start:ctx_end].replace("\n", " ").strip()
    if ctx_start > 0:
        snippet = "..." + snippet
    if ctx_end < len(text):
        snippet = snippet + "..."
    return snippet


def find_dates_in_text(text, ref_date=None):
    """Return a list of (parsed_date, pattern_name, matched_text, context_snippet, confidence).

    ref_date: the date the note was created — used to resolve relative patterns
              like "next Tuesday". Defaults to today (local time) if not provided.
    """
    if not text:
        return []

    today = datetime.now(LOCAL_TZ).date()
    if ref_date is None:
        ref_date = today
    max_future = today + timedelta(days=365)
    results = []
    # Track which character ranges have already been matched to avoid overlaps
    matched_ranges = []

    for pattern_name, regex, parser, confidence in PATTERNS:
        # Relative parsers (confidence='likely') take (match, ref_date)
        is_relative = (confidence == "likely")
        for m in regex.finditer(text):
            # Skip if this range overlaps with an already-matched range
            if any(m.start() < er and m.end() > sr for sr, er in matched_ranges):
                continue

            if is_relative:
                parsed = parser(m, ref_date)
            else:
                parsed = parser(m)

            if parsed is None:
                continue

            # Future only (relative to today, not ref_date)
            if parsed <= today:
                continue

            # Within 12 months
            if parsed > max_future:
                continue

            # Not in signature block
            if _is_in_signature(text, m.start()):
                continue

            context = _extract_context(text, m.start(), m.end())
            results.append((parsed, pattern_name, m.group(0), context, confidence))
            matched_ranges.append((m.start(), m.end()))

    return results


# ── Database ──────────────────────────────────────────────────────────────────

def init_tables(conn):
    """Ensure timeline tables exist (delegates to db.py's function)."""
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS timeline_events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id         INTEGER NOT NULL,
            company_id        INTEGER,
            company_name      TEXT,
            board_name        TEXT,
            parsed_date       TEXT NOT NULL,
            pattern_name      TEXT NOT NULL,
            matched_text      TEXT NOT NULL,
            context_snippet   TEXT,
            note_id           INTEGER,
            note_date         TEXT,
            member_name       TEXT,
            ticket_summary    TEXT,
            status            TEXT DEFAULT 'active',
            created_at        TEXT NOT NULL,
            UNIQUE(ticket_id, parsed_date)
        )
    """)
    # Migrate existing DBs that lack new columns
    try:
        conn.execute("ALTER TABLE timeline_events ADD COLUMN board_name TEXT")
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE timeline_events ADD COLUMN ai_description TEXT")
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE timeline_events ADD COLUMN confidence TEXT DEFAULT 'confirmed'")
    except Exception:
        pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_events(parsed_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_company ON timeline_events(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_status ON timeline_events(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_board ON timeline_events(board_name)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS timeline_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO timeline_meta (key, value) VALUES ('last_parsed_note_id', '0')"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS timeline_exclusions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT NOT NULL,
            value       TEXT NOT NULL,
            created_by  TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(type, value)
        )
    """)
    conn.commit()


def get_watermark(conn):
    """Return the last_parsed_note_id watermark."""
    row = conn.execute(
        "SELECT value FROM timeline_meta WHERE key = 'last_parsed_note_id'"
    ).fetchone()
    return int(row[0]) if row else 0


def set_watermark(conn, note_id):
    """Update the last_parsed_note_id watermark."""
    conn.execute(
        "INSERT OR REPLACE INTO timeline_meta (key, value) VALUES ('last_parsed_note_id', ?)",
        (str(note_id),)
    )
    conn.commit()


def load_exclusions(conn):
    """Load all exclusions grouped by type."""
    exclusions = {
        "ticket_ids": set(),
        "substrings": [],
        "boards": set(),
        "companies": set(),
        "bypass_emails": set(),
    }
    try:
        rows = conn.execute("SELECT type, value FROM timeline_exclusions").fetchall()
        for r in rows:
            t, v = r["type"], r["value"]
            if t == "ticket_id":
                try:
                    exclusions["ticket_ids"].add(int(v))
                except ValueError:
                    pass
            elif t == "substring":
                exclusions["substrings"].append(v.lower())
            elif t == "board":
                exclusions["boards"].add(v.lower())
            elif t == "company":
                exclusions["companies"].add(v.lower())
            elif t == "bypass_email":
                exclusions["bypass_emails"].add(v.lower())
    except Exception:
        pass  # Table might not exist yet on very first run
    return exclusions


def _resolve_contact_email(conn, contact_name):
    """Look up a contact's email by name. Returns lowercase email or None."""
    if not contact_name:
        return None
    row = conn.execute(
        "SELECT email FROM contacts WHERE full_name = ? AND email IS NOT NULL LIMIT 1",
        (contact_name,)
    ).fetchone()
    return row["email"].lower() if row and row["email"] else None


def lookup_ticket(conn, ticket_id):
    """Get ticket info. Returns a dict or None."""
    row = conn.execute(
        """SELECT company_id, company_name, board_name, summary,
                  contact_name, parent_ticket_id
           FROM tickets WHERE id = ?""",
        (ticket_id,)
    ).fetchone()
    if row:
        return dict(row)
    return None


# ── Main processing ───────────────────────────────────────────────────────────

def process_notes(conn, dry_run=False):
    """Process new notes and extract timeline events. Returns count of new events."""
    watermark = get_watermark(conn)
    print(f"  Watermark: last_parsed_note_id = {watermark}")

    # Load exclusion rules
    exc = load_exclusions(conn)

    # Load board inclusion filter from settings
    allowed_boards = set()
    try:
        from settings import get_setting
        boards_raw = get_setting("timeline_boards", "")
        if boards_raw:
            allowed_boards = {b.strip().lower() for b in boards_raw.split(",") if b.strip()}
    except Exception:
        pass  # settings module may not be available in standalone runs
    if allowed_boards:
        print(f"  Board filter: only processing {allowed_boards}")
    parts = []
    if exc["ticket_ids"]:   parts.append(f"{len(exc['ticket_ids'])} ticket(s)")
    if exc["substrings"]:   parts.append(f"{len(exc['substrings'])} substring(s)")
    if exc["boards"]:       parts.append(f"{len(exc['boards'])} board(s)")
    if exc["companies"]:    parts.append(f"{len(exc['companies'])} company/ies")
    if exc["bypass_emails"]:parts.append(f"{len(exc['bypass_emails'])} bypass email(s)")
    if parts:
        print(f"  Exclusions: {', '.join(parts)}")

    # Cache for contact email lookups (contact_name → email)
    _email_cache = {}

    def resolve_email(contact_name):
        if contact_name not in _email_cache:
            _email_cache[contact_name] = _resolve_contact_email(conn, contact_name)
        return _email_cache[contact_name]

    notes = conn.execute("""
        SELECT id, ticket_id, text, date_created, member_name, contact_name
        FROM ticket_notes
        WHERE id > ?
        ORDER BY id ASC
    """, (watermark,)).fetchall()

    print(f"  Notes to process: {len(notes)}")
    if not notes:
        return 0

    # Cache ticket lookups so we don't query the same ticket per-note
    _ticket_cache = {}
    skipped_merged = 0

    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    new_events = 0
    max_note_id = watermark
    batch = 0
    BATCH_SIZE = 50

    for note in notes:
        note_id = note["id"]
        ticket_id = note["ticket_id"]
        text = note["text"] or ""
        note_date = note["date_created"]
        member = note["member_name"]
        note_contact = note["contact_name"]

        if note_id > max_note_id:
            max_note_id = note_id

        # Skip excluded tickets entirely
        if ticket_id in exc["ticket_ids"]:
            continue

        # Parse note creation date for relative pattern resolution
        note_ref_date = None
        if note_date:
            try:
                note_ref_date = datetime.fromisoformat(
                    note_date.replace("Z", "+00:00")
                ).date()
            except (ValueError, TypeError):
                pass

        matches = find_dates_in_text(text, ref_date=note_ref_date)
        if not matches:
            continue

        # Look up ticket info once per ticket (cached)
        if ticket_id not in _ticket_cache:
            _ticket_cache[ticket_id] = lookup_ticket(conn, ticket_id)
        ticket = _ticket_cache[ticket_id]

        if not ticket:
            continue

        company_id   = ticket["company_id"]
        company_name = ticket["company_name"]
        board_name   = ticket["board_name"]
        summary      = ticket["summary"]
        ticket_contact = ticket["contact_name"]

        # ── Merged ticket detection ──────────────────────────────────────
        # If this ticket has a parent_ticket_id, it was merged into another.
        # Skip it — the surviving ticket should have the relevant notes.
        if ticket.get("parent_ticket_id"):
            skipped_merged += 1
            continue

        # ── Board exclusion ──────────────────────────────────────────────
        if board_name and exc["boards"] and board_name.lower() in exc["boards"]:
            continue

        # ── Board inclusion filter (from settings) ──────────────────────
        if allowed_boards and (not board_name or board_name.lower() not in allowed_boards):
            continue

        # ── Company exclusion with bypass ────────────────────────────────
        if company_name and exc["companies"] and company_name.lower() in exc["companies"]:
            # Check if the note's contact or the ticket's contact has a
            # bypass email. Try note contact first (more specific), then
            # fall back to ticket contact.
            bypassed = False
            if exc["bypass_emails"]:
                for cname in (note_contact, ticket_contact):
                    email = resolve_email(cname)
                    if email and email in exc["bypass_emails"]:
                        bypassed = True
                        break
            if not bypassed:
                continue

        for parsed_date, pattern_name, matched_text, context, confidence in matches:
            iso_date = parsed_date.isoformat()

            # Check substring exclusions against context snippet
            if exc["substrings"] and context:
                ctx_lower = context.lower()
                if any(sub in ctx_lower for sub in exc["substrings"]):
                    continue

            if dry_run:
                print(f"    [DRY-RUN] ticket={ticket_id} date={iso_date} "
                      f"pattern={pattern_name} match='{matched_text}' "
                      f"confidence={confidence} "
                      f"company={company_name} board={board_name}")
                new_events += 1
                continue

            try:
                conn.execute("""
                    INSERT OR IGNORE INTO timeline_events
                    (ticket_id, company_id, company_name, board_name, parsed_date,
                     pattern_name, matched_text, context_snippet, note_id, note_date,
                     member_name, ticket_summary, confidence, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """, (
                    ticket_id, company_id, company_name, board_name, iso_date,
                    pattern_name, matched_text, context, note_id, note_date,
                    member, summary, confidence, now,
                ))
                if conn.total_changes:
                    new_events += 1
                batch += 1
                if batch >= BATCH_SIZE:
                    conn.commit()
                    batch = 0
            except sqlite3.IntegrityError:
                # UNIQUE constraint — already have this (ticket, date) pair
                pass

    if skipped_merged:
        print(f"  Skipped {skipped_merged} note(s) from merged tickets")

    if not dry_run:
        conn.commit()
        set_watermark(conn, max_note_id)

        # Prune past events
        today_iso = datetime.now(LOCAL_TZ).date().isoformat()
        deleted = conn.execute(
            "DELETE FROM timeline_events WHERE parsed_date < ?", (today_iso,)
        ).rowcount
        conn.commit()
        if deleted:
            print(f"  Pruned {deleted} past event(s)")

    return new_events


# ── AI description generation ────────────────────────────────────────────────

AI_MODEL = "claude-haiku-4-5-20251001"
AI_PER_RUN_CAP = 50        # Max events to describe per parse run
AI_PER_TICKET_CAP = 3      # Max AI descriptions per ticket
AI_BATCH_SIZE = 10          # Events per API call
AI_CONTEXT_CHARS = 200      # Max context chars sent to the model

AI_SYSTEM_PROMPT = (
    "You summarise timeline events extracted from IT support ticket notes. "
    "For each event you receive (ticket summary + matched date context), "
    "write a single concise sentence (max 20 words) explaining what is "
    "happening or scheduled for that date. Be specific and action-oriented. "
    "Do NOT include the date itself in the description. "
    "Return a JSON array of strings, one per event, in the same order."
)


def _truncate_context(snippet, max_chars=AI_CONTEXT_CHARS):
    """Trim a context snippet to a reasonable size for the AI prompt."""
    if not snippet:
        return ""
    snippet = snippet.strip()
    if len(snippet) <= max_chars:
        return snippet
    return snippet[:max_chars].rsplit(" ", 1)[0] + "..."


def generate_ai_descriptions(conn, dry_run=False):
    """Generate AI descriptions for active events that don't have one yet.

    Respects per-run and per-ticket caps to manage token spend.
    Returns count of descriptions generated.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  AI descriptions: skipped (no ANTHROPIC_API_KEY)")
        return 0

    try:
        import anthropic
    except ImportError:
        print("  AI descriptions: skipped (anthropic package not installed)")
        return 0

    # Find active events missing an AI description
    rows = conn.execute("""
        SELECT id, ticket_id, ticket_summary, context_snippet, matched_text
        FROM timeline_events
        WHERE status = 'active' AND ai_description IS NULL
        ORDER BY parsed_date ASC
    """).fetchall()

    if not rows:
        print("  AI descriptions: nothing to describe")
        return 0

    # Apply per-ticket cap: count existing AI descriptions per ticket
    ticket_ai_counts = {}
    existing = conn.execute("""
        SELECT ticket_id, COUNT(*) as cnt
        FROM timeline_events
        WHERE ai_description IS NOT NULL
        GROUP BY ticket_id
    """).fetchall()
    for r in existing:
        ticket_ai_counts[r["ticket_id"]] = r["cnt"]

    # Filter to events that are within ticket cap
    eligible = []
    for r in rows:
        tid = r["ticket_id"]
        current_count = ticket_ai_counts.get(tid, 0)
        if current_count >= AI_PER_TICKET_CAP:
            continue
        eligible.append(dict(r))
        ticket_ai_counts[tid] = current_count + 1
        if len(eligible) >= AI_PER_RUN_CAP:
            break

    if not eligible:
        print("  AI descriptions: all eligible events at per-ticket cap")
        return 0

    print(f"  AI descriptions: generating for {len(eligible)} event(s) "
          f"(run cap={AI_PER_RUN_CAP}, ticket cap={AI_PER_TICKET_CAP})")

    if dry_run:
        for ev in eligible:
            print(f"    [DRY-RUN] would describe event id={ev['id']} "
                  f"ticket={ev['ticket_id']}")
        return len(eligible)

    client = anthropic.Anthropic(api_key=api_key)
    generated = 0

    # Process in batches
    for i in range(0, len(eligible), AI_BATCH_SIZE):
        batch = eligible[i:i + AI_BATCH_SIZE]

        # Build the user prompt
        items = []
        for idx, ev in enumerate(batch):
            ctx = _truncate_context(ev["context_snippet"])
            items.append(
                f"{idx + 1}. Ticket: {ev['ticket_summary'] or 'No summary'}\n"
                f"   Context: {ctx}\n"
                f"   Matched: {ev['matched_text']}"
            )
        user_prompt = (
            f"Describe these {len(batch)} timeline events. "
            f"Return a JSON array of {len(batch)} description strings.\n\n"
            + "\n\n".join(items)
        )

        try:
            response = client.messages.create(
                model=AI_MODEL,
                max_tokens=1024,
                system=AI_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            # Parse the response — extract JSON array
            text = response.content[0].text.strip()
            # Handle cases where the model wraps JSON in markdown code fences
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

            descriptions = json.loads(text)

            if not isinstance(descriptions, list):
                log.warning("AI returned non-list: %s", type(descriptions))
                continue

            # Update the DB with each description
            for idx, ev in enumerate(batch):
                if idx < len(descriptions) and descriptions[idx]:
                    desc = str(descriptions[idx]).strip()
                    if desc:
                        conn.execute(
                            "UPDATE timeline_events SET ai_description = ? WHERE id = ?",
                            (desc, ev["id"])
                        )
                        generated += 1

            conn.commit()

        except json.JSONDecodeError as e:
            log.warning("AI description JSON parse failed: %s", e)
            print(f"  AI descriptions: JSON parse error in batch {i // AI_BATCH_SIZE + 1}")
        except Exception as e:
            log.warning("AI description generation error: %s", e)
            print(f"  AI descriptions: error in batch {i // AI_BATCH_SIZE + 1}: {e}")

    print(f"  AI descriptions: generated {generated} description(s)")
    return generated


def generate_single_ai_description(conn, event_id):
    """Generate an AI description for a single timeline event by ID.

    Returns the description string on success, or raises on error.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    import anthropic

    row = conn.execute(
        "SELECT id, ticket_id, ticket_summary, context_snippet, matched_text "
        "FROM timeline_events WHERE id = ?",
        (event_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Event {event_id} not found")

    ctx = _truncate_context(row["context_snippet"])
    user_prompt = (
        "Describe this timeline event. Return a single plain-text sentence (no JSON).\n\n"
        f"Ticket: {row['ticket_summary'] or 'No summary'}\n"
        f"Context: {ctx}\n"
        f"Matched: {row['matched_text']}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=AI_MODEL,
        max_tokens=128,
        system=AI_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    desc = response.content[0].text.strip()
    # Clean markdown fences if present
    if desc.startswith("```"):
        desc = re.sub(r"^```(?:json)?\s*", "", desc)
        desc = re.sub(r"\s*```$", "", desc)
    # If it returned a JSON array for a single item, unwrap it
    if desc.startswith("[") and desc.endswith("]"):
        try:
            parsed = json.loads(desc)
            if isinstance(parsed, list) and len(parsed) == 1:
                desc = str(parsed[0]).strip()
        except json.JSONDecodeError:
            pass
    # Strip wrapping quotes
    if len(desc) > 2 and desc[0] == '"' and desc[-1] == '"':
        desc = desc[1:-1]

    conn.execute(
        "UPDATE timeline_events SET ai_description = ? WHERE id = ?",
        (desc, event_id)
    )
    conn.commit()
    return desc


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parse ticket notes for future dates")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--reparse", action="store_true", help="Reset watermark and reparse all notes")
    parser.add_argument("--dry-run", action="store_true", help="Show matches without writing to DB")
    args = parser.parse_args()

    print(f"Timeline parser — DB: {args.db}")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        init_tables(conn)

        if args.reparse:
            print("  Resetting watermark to 0 (full reparse)...")
            set_watermark(conn, 0)
            if not args.dry_run:
                conn.execute("DELETE FROM timeline_events")
                conn.commit()
                print("  Cleared existing timeline_events")

        new_events = process_notes(conn, dry_run=args.dry_run)

        # Generate AI descriptions for events that don't have one
        ai_enabled = True
        try:
            from settings import get_setting
            ai_enabled = get_setting("timeline_ai_summaries", "true").lower() == "true"
        except Exception:
            pass  # settings module may not be available in standalone runs
        if ai_enabled:
            ai_count = generate_ai_descriptions(conn, dry_run=args.dry_run)
        else:
            ai_count = 0
            print("  AI descriptions: disabled via settings")

        if args.dry_run:
            print(f"\nDry run complete. {new_events} potential event(s) found, "
                  f"{ai_count} would get AI descriptions.")
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM timeline_events WHERE status = 'active'"
            ).fetchone()[0]
            print(f"\nParse complete. {new_events} new event(s), "
                  f"{ai_count} AI description(s). "
                  f"{total} active event(s) total.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
