"""db.py — Database layer for ChatPSA."""
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime

from config import (DB_PATH, HELPDESK_BOARD, MAX_ROWS,
                    MEMORIES_DB_PATH, get_tz_offset_sql)

log = logging.getLogger("chatpsa.db")

# Shared regex pattern for extracting table names from SQL — used by both
# store_example() and seed_initial_examples() to populate tables_used metadata.
_TABLE_NAME_PATTERN = (
    r'\b(tickets|companies|contacts|time_entries|agreements|'
    r'configurations|projects|invoices|catalog_items|members|'
    r'ticket_notes|cipp_tenants|cipp_licenses|cipp_alerts|'
    r'cipp_policies|cipp_policy_snapshots|'
    r'customer_map|agreement_additions|'
    r'duo_users|huntress_organizations|huntress_agents|huntress_incidents|'
    r'threatlocker_computers|threatlocker_organizations)\b'
)

# Internal/admin tables hidden from the AI agent's schema view and (for the
# security-sensitive subset) blocked from agent-generated SQL queries.
# Schema exclusion: all tables listed here are omitted from get_schema_description()
#   so the AI never advertises them.  This reduces noise and prevents the AI from
#   spontaneously querying operational tables.
# Query block: the subset marked with _BLOCKED (secrets/PII) is hard-rejected in
#   execute_sql() even if a user explicitly asks the AI to query them.
_INTERNAL_TABLES = frozenset({
    # Security-sensitive (also blocked in execute_sql)
    "app_settings",       # plaintext credentials
    "feature_access",     # user permissions
    "known_users",        # user PII
    "admin_events",       # admin diagnostics
    # Operational (hidden from schema only)
    "sync_log",
    "sync_state",
    "sqlite_sequence",
    "sql_examples",
    "conversations",
    "usage_log",
    "page_views",
    "query_themes",
})

# Subset of _INTERNAL_TABLES that contain secrets or PII — hard-blocked in execute_sql().
_BLOCKED_TABLES = frozenset({
    "app_settings", "feature_access", "known_users", "admin_events",
})


def get_or_create_secret_key():
    """Return a stable secret key shared across all gunicorn workers.

    Order of preference:
      1. FLASK_SECRET_KEY env var (set this in .env for full control)
      2. Key persisted on the Docker volume at /data/.secret_key
      3. Generate a new key, persist it, then return it
    """
    key = os.environ.get("FLASK_SECRET_KEY")
    if key:
        return key
    data_dir = os.path.dirname(DB_PATH)
    key_file = os.path.join(data_dir, ".secret_key")
    if os.path.exists(key_file):
        stored = open(key_file).read().strip()
        if stored:
            return stored
    key = secrets.token_hex(32)
    os.makedirs(data_dir, exist_ok=True)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


# Conversation memory is stored in SQLite so all gunicorn workers share it.
# Functions below (get_history, save_message, trim_history) replace the old dict.

def _ensure_conversations_table(conn):
    """Create the conversations table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id  TEXT    NOT NULL,
            user_email  TEXT    NOT NULL DEFAULT 'anonymous',
            turn_index  INTEGER NOT NULL,
            role        TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (session_id, user_email, turn_index)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_conv_session_user
        ON conversations(session_id, user_email)
    """)
    conn.commit()
    # Migrate legacy rows that lack user_email (added in open-source release)
    _migrate_conversations_add_user_email(conn)


def _migrate_conversations_add_user_email(conn):
    """Add user_email column to conversations if it was created before the IDOR fix."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()]
        if "user_email" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_email TEXT NOT NULL DEFAULT 'anonymous'")
            conn.commit()
            log.info("Migrated conversations table: added user_email column")
    except Exception as e:
        log.warning("conversations migration check: %s", e)


def _get_conv_db():
    """Open a connection to the conversation store, creating the table if needed."""
    data_dir = os.path.dirname(DB_PATH)
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "conversations.db")
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_conversations_table(conn)
    return conn


def get_history(session_id, user_email="anonymous"):
    """Return conversation history as a list of {role, content} dicts.

    Results are scoped to the (session_id, user_email) pair so users
    cannot read each other's conversations.
    """
    conn = _get_conv_db()
    try:
        rows = conn.execute(
            "SELECT role, content FROM conversations "
            "WHERE session_id=? AND user_email=? ORDER BY turn_index",
            (session_id, user_email)
        ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]
    finally:
        conn.close()


def save_message(session_id, role, content, user_email="anonymous"):
    """Append a single message to the conversation history."""
    conn = _get_conv_db()
    try:
        next_index = conn.execute(
            "SELECT COALESCE(MAX(turn_index)+1, 0) FROM conversations "
            "WHERE session_id=? AND user_email=?",
            (session_id, user_email)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations (session_id, user_email, turn_index, role, content) "
            "VALUES (?,?,?,?,?)",
            (session_id, user_email, next_index, role, content)
        )
        conn.commit()
    finally:
        conn.close()


def trim_history(session_id, max_turns, user_email="anonymous"):
    """Keep only the most recent max_turns*2 messages for a session."""
    conn = _get_conv_db()
    try:
        keep_from = conn.execute(
            """SELECT turn_index FROM conversations
               WHERE session_id=? AND user_email=?
               ORDER BY turn_index DESC LIMIT 1 OFFSET ?""",
            (session_id, user_email, max_turns * 2 - 1)
        ).fetchone()
        if keep_from:
            conn.execute(
                "DELETE FROM conversations WHERE session_id=? AND user_email=? AND turn_index<?",
                (session_id, user_email, keep_from[0])
            )
            conn.commit()
    finally:
        conn.close()


def _ensure_examples_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sql_examples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            question      TEXT NOT NULL,
            sql_text      TEXT NOT NULL,
            tables_used   TEXT,
            user_email    TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            active        INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_examples_active ON sql_examples(active)")
    conn.commit()


def store_example(question, sql_text, user_email="anonymous"):
    """Store a validated question→SQL pair from user feedback."""
    conn = _get_conv_db()
    try:
        _ensure_examples_table(conn)

        # Extract table names from the SQL for keyword matching later
        tables = set(re.findall(_TABLE_NAME_PATTERN, sql_text, re.IGNORECASE))
        tables_str = ",".join(sorted(t.lower() for t in tables)) if tables else None

        # Avoid exact duplicates
        existing = conn.execute(
            "SELECT id FROM sql_examples WHERE LOWER(TRIM(question)) = LOWER(TRIM(?)) AND active = 1",
            (question,)
        ).fetchone()
        if existing:
            return {"stored": False, "reason": "duplicate"}

        conn.execute(
            "INSERT INTO sql_examples (question, sql_text, tables_used, user_email) VALUES (?, ?, ?, ?)",
            (question, sql_text, tables_str, user_email)
        )
        conn.commit()
        return {"stored": True}
    except Exception as e:
        return {"stored": False, "reason": str(e)}
    finally:
        conn.close()


def find_similar_examples(question, limit=3):
    """Find the most relevant few-shot examples for a given question.

    Uses keyword overlap scoring: extracts meaningful words from the question,
    scores each stored example by how many words overlap, and returns the top
    matches. Fast and effective at <100 examples — no embeddings needed.
    """
    conn = _get_conv_db()
    try:
        _ensure_examples_table(conn)
        examples = conn.execute(
            "SELECT question, sql_text, tables_used FROM sql_examples WHERE active = 1"
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    if not examples:
        return []

    # Tokenise: lowercase, strip punctuation, remove stop words
    stop_words = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "and", "but", "or", "nor", "not", "so", "yet",
        "both", "either", "neither", "each", "every", "all", "any", "few",
        "more", "most", "other", "some", "such", "no", "only", "own", "same",
        "than", "too", "very", "just", "about", "above", "below", "between",
        "up", "down", "out", "off", "over", "under", "again", "further",
        "then", "once", "here", "there", "when", "where", "why", "how",
        "what", "which", "who", "whom", "this", "that", "these", "those",
        "i", "me", "my", "we", "our", "you", "your", "it", "its", "they",
        "them", "their", "he", "she", "him", "her", "his",
        "show", "tell", "give", "get", "many", "much",
    }

    def tokenise(text):
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        return words - stop_words

    q_tokens = tokenise(question)
    if not q_tokens:
        return []

    scored = []
    for ex in examples:
        ex_tokens = tokenise(ex["question"])
        if not ex_tokens:
            continue
        overlap = len(q_tokens & ex_tokens)
        if overlap == 0:
            continue
        # Jaccard-ish score: overlap / union, weighted towards overlap count
        score = overlap / len(q_tokens | ex_tokens)
        scored.append((score, ex))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"question": s[1]["question"], "sql": s[1]["sql_text"]}
            for s in scored[:limit] if s[0] > 0.1]


def seed_initial_examples():
    """Seed the examples table with known-good queries if empty."""
    conn = _get_conv_db()
    try:
        _ensure_examples_table(conn)

        seeds = [
            {
                "question": "How many Business Basic licenses does a specific company have?",
                "sql": """SELECT c.name AS company_name, ct.display_name AS tenant_name,
       cl.sku_name, cl.active_units, cl.consumed_units, cl.available_units
FROM companies c
JOIN customer_map cm ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
JOIN cipp_tenants ct ON cm.cipp_tenant_id = ct.default_domain
JOIN cipp_licenses cl ON ct.tenant_id = cl.tenant_id
WHERE c.name LIKE '%example%'
  AND cl.sku_name LIKE '%Business Basic%'"""
            },
            {
                "question": "Which clients are set up in ConnectWise but missing from CIPP?",
                "sql": """SELECT c.id, c.name AS company_name
FROM companies c
LEFT JOIN customer_map cm ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
WHERE cm.cipp_tenant_id IS NULL
  AND c.status_name = 'Active'
ORDER BY c.name"""
            },
            {
                "question": "Which clients have Duo enabled?",
                "sql": """SELECT c.name AS company_name, cm.duo_account_id
FROM customer_map cm
JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
WHERE cm.duo_account_id IS NOT NULL
ORDER BY c.name"""
            },
            {
                "question": "Show M365 license counts by client",
                "sql": """SELECT c.name AS company_name, ct.display_name AS tenant_name,
       SUM(cl.active_units) AS total_licenses,
       SUM(cl.consumed_units) AS total_consumed,
       SUM(cl.available_units) AS total_available
FROM companies c
JOIN customer_map cm ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
JOIN cipp_tenants ct ON cm.cipp_tenant_id = ct.default_domain
JOIN cipp_licenses cl ON ct.tenant_id = cl.tenant_id
GROUP BY c.name, ct.display_name
ORDER BY total_licenses DESC"""
            },
            {
                "question": "Do clients with more M365 licenses submit more tickets?",
                "sql": f"""SELECT c.name AS company_name,
       COALESCE(lic.total_licenses, 0) AS m365_licenses,
       COUNT(t.id) AS ticket_count
FROM companies c
LEFT JOIN (
    SELECT cm.cw_manage_company_id,
           SUM(cl.active_units) AS total_licenses
    FROM customer_map cm
    JOIN cipp_tenants ct ON cm.cipp_tenant_id = ct.default_domain
    JOIN cipp_licenses cl ON ct.tenant_id = cl.tenant_id
    WHERE cl.sku_name LIKE '%E3%' OR cl.sku_name LIKE '%E5%'
       OR cl.sku_name LIKE '%F3%' OR cl.sku_name LIKE '%Business Basic%'
       OR cl.sku_name LIKE '%Business Standard%' OR cl.sku_name LIKE '%Business Premium%'
    GROUP BY cm.cw_manage_company_id
) lic ON CAST(lic.cw_manage_company_id AS INTEGER) = c.id
LEFT JOIN tickets t ON t.company_id = c.id
  AND t.board_name = '{HELPDESK_BOARD}'
  AND date(t.date_entered, {get_tz_offset_sql()}) >= date('now', {get_tz_offset_sql()}, '-90 days')
WHERE lic.total_licenses > 0
GROUP BY c.name
ORDER BY m365_licenses DESC
LIMIT 25"""
            },
            {
                "question": "Which clients have Huntress deployed and how many endpoint tickets do they have?",
                "sql": f"""SELECT c.name AS company_name, cm.huntress_org_id,
       COUNT(t.id) AS endpoint_tickets
FROM customer_map cm
JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
LEFT JOIN tickets t ON t.company_id = c.id
  AND t.board_name = '{HELPDESK_BOARD}'
  AND (t.summary LIKE '%endpoint%' OR t.summary LIKE '%virus%'
       OR t.summary LIKE '%malware%' OR t.summary LIKE '%security%'
       OR t.summary LIKE '%threat%' OR t.summary LIKE '%ransomware%')
WHERE cm.huntress_org_id IS NOT NULL
GROUP BY c.name
ORDER BY endpoint_tickets DESC"""
            },
            {
                "question": "What high priority tickets have been open for more than 48 hours?",
                "sql": """SELECT t.id, t.summary, t.company_name, t.status_name,
       t.priority_name, t.assigned_name,
       ROUND((julianday('now') - julianday(t.date_entered)) * 24, 1) AS hours_open
FROM tickets t
WHERE t.date_closed IS NULL
  AND t.priority_name IN ('Priority 1 - Critical', 'Priority 2 - High')
  AND datetime(t.date_entered) < datetime('now', '-48 hours')
ORDER BY t.date_entered ASC"""
            },


            {
                "question": "Which clients have CIPP alerts and also have open tickets?",
                "sql": """SELECT c.name AS company_name, ca.tenant_name,
       ca.type AS alert_type, ca.severity,
       COUNT(t.id) AS open_tickets
FROM cipp_alerts ca
JOIN cipp_tenants ct ON ca.tenant_name = ct.display_name
JOIN customer_map cm ON cm.cipp_tenant_id = ct.default_domain
JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
LEFT JOIN tickets t ON t.company_id = c.id AND t.date_closed IS NULL
GROUP BY c.name, ca.tenant_name, ca.type, ca.severity
ORDER BY open_tickets DESC"""
            },
            {
                "question": "Which clients have Duo enabled and how many users are enrolled?",
                "sql": """SELECT c.name AS company_name,
       COUNT(du.user_id) AS total_duo_users,
       SUM(CASE WHEN du.is_enrolled = 1 THEN 1 ELSE 0 END) AS enrolled,
       SUM(CASE WHEN du.is_enrolled = 0 THEN 1 ELSE 0 END) AS not_enrolled,
       SUM(CASE WHEN du.status = 'bypass' THEN 1 ELSE 0 END) AS bypass_users
FROM customer_map cm
JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
JOIN duo_users du ON du.duo_account_id = cm.duo_account_id
WHERE cm.duo_account_id IS NOT NULL
GROUP BY c.name
ORDER BY not_enrolled DESC"""
            },
            {
                "question": "Which Duo users are not enrolled or are in bypass mode?",
                "sql": """SELECT c.name AS company_name,
       du.username, du.email, du.realname, du.status,
       du.is_enrolled, du.last_login
FROM duo_users du
JOIN customer_map cm ON du.duo_account_id = cm.duo_account_id
JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
WHERE du.is_enrolled = 0 OR du.status = 'bypass'
ORDER BY c.name, du.username"""
            },
        ]

        for s in seeds:
            tables = set(re.findall(_TABLE_NAME_PATTERN, s["sql"], re.IGNORECASE))
            tables_str = ",".join(sorted(t.lower() for t in tables)) if tables else None
            # Upsert: update SQL if seed already exists, insert if new
            existing = conn.execute(
                "SELECT id FROM sql_examples WHERE question = ? AND user_email = 'seed'",
                (s["question"],)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE sql_examples SET sql_text = ?, tables_used = ? WHERE id = ?",
                    (s["sql"], tables_str, existing[0])
                )
            else:
                conn.execute(
                    "INSERT INTO sql_examples (question, sql_text, tables_used, user_email) VALUES (?, ?, ?, ?)",
                    (s["question"], s["sql"], tables_str, "seed")
                )
        conn.commit()
    except Exception:
        pass  # Don't let seeding failures break startup
    finally:
        conn.close()


def _ensure_usage_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            user_email   TEXT,
            user_name    TEXT,
            query_text   TEXT NOT NULL,
            source       TEXT NOT NULL DEFAULT 'typed',
            timestamp    TEXT NOT NULL DEFAULT (datetime('now')),
            response_ms  INTEGER,
            errored      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_email ON usage_log(user_email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(timestamp)")
    conn.commit()


def log_usage(session_id, user_email, user_name, query_text, source, response_ms, errored):
    conn = _get_conv_db()
    try:
        _ensure_usage_table(conn)
        # Let SQLite DEFAULT (datetime('now')) handle the UTC timestamp —
        # keeps existing data consistent. Timezone offset applied at query time.
        conn.execute(
            """INSERT INTO usage_log
               (session_id, user_email, user_name, query_text, source, response_ms, errored)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_email, user_name, query_text, source, response_ms, 1 if errored else 0)
        )
        conn.commit()
    except Exception:
        pass  # Never let logging failures break the chat
    finally:
        conn.close()


def _ensure_page_views_table(conn):
    """Create the page_views table in conversations.db."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_views (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            page         TEXT NOT NULL,
            user_email   TEXT,
            user_name    TEXT,
            timestamp    TEXT NOT NULL,
            referrer     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_page ON page_views(page)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_email ON page_views(user_email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_ts ON page_views(timestamp)")
    conn.commit()


def log_page_view(page, user_email, user_name, referrer=None):
    """Record a page view with local timestamp."""
    from config import APP_TZ
    conn = _get_conv_db()
    try:
        _ensure_page_views_table(conn)
        now_local = datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO page_views (page, user_email, user_name, timestamp, referrer) "
            "VALUES (?, ?, ?, ?, ?)",
            (page, user_email, user_name, now_local, referrer)
        )
        conn.commit()
    except Exception:
        pass  # Never let tracking break the page
    finally:
        conn.close()


def _ensure_query_themes_table(conn):
    """Create the query_themes cache table in conversations.db."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_themes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            theme_name   TEXT NOT NULL,
            query_count  INTEGER NOT NULL DEFAULT 0,
            example_queries TEXT,
            generated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_themes_gen ON query_themes(generated_at)")
    conn.commit()


def get_query_themes(max_age_hours=6):
    """Return cached query themes if fresh enough, else None."""
    conn = _get_conv_db()
    try:
        _ensure_query_themes_table(conn)
        latest = conn.execute(
            "SELECT generated_at FROM query_themes ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        if not latest:
            return None
        from datetime import datetime, timedelta
        gen_time = datetime.fromisoformat(latest[0])
        if datetime.utcnow() - gen_time > timedelta(hours=max_age_hours):
            return None
        rows = conn.execute(
            "SELECT theme_name, query_count, example_queries FROM query_themes "
            "WHERE generated_at = ? ORDER BY query_count DESC",
            (latest[0],)
        ).fetchall()
        return [{"theme": r[0], "count": r[1], "examples": r[2]} for r in rows]
    except Exception:
        return None
    finally:
        conn.close()


def save_query_themes(themes):
    """Save a batch of AI-generated query themes.

    themes: list of dicts with keys: theme_name, query_count, example_queries
    """
    conn = _get_conv_db()
    try:
        _ensure_query_themes_table(conn)
        from datetime import datetime
        now = datetime.utcnow().isoformat(timespec="seconds")
        # Clear old themes and insert fresh batch
        conn.execute("DELETE FROM query_themes")
        for t in themes:
            conn.execute(
                "INSERT INTO query_themes (theme_name, query_count, example_queries, generated_at) "
                "VALUES (?, ?, ?, ?)",
                (t["theme_name"], t["query_count"], t.get("example_queries", ""), now)
            )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def init_timeline_tables(conn=None):
    """Create timeline_events, timeline_meta, and feature_access tables.

    Called at app startup and by parse_timeline.py before its first run.
    Safe to call repeatedly — all statements use IF NOT EXISTS.
    Retries on database-locked errors since sync containers may hold the
    WAL lock during startup.
    """
    import time as _time
    close_after = False
    if conn is None:
        import os
        if not os.path.exists(DB_PATH):
            return
        conn = sqlite3.connect(DB_PATH, timeout=30)
        close_after = True

    for _attempt in range(5):
        try:
            _init_timeline_tables_inner(conn)
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and _attempt < 4:
                _time.sleep(2)
                continue
            raise

    if close_after:
        conn.close()


def _init_timeline_tables_inner(conn):

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
    # Seed the watermark if it doesn't exist
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_access (
            feature     TEXT NOT NULL,
            user_email  TEXT NOT NULL,
            granted_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (feature, user_email)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS known_users (
            email       TEXT PRIMARY KEY,
            name        TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            activated   INTEGER NOT NULL DEFAULT 0,
            hidden      INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by  TEXT
        )
    """)

    # customer_map — cross-service company associations, managed via the
    # Admin → Mappings UI.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_map (
            cw_manage_company_id   TEXT PRIMARY KEY,
            cipp_tenant_id         TEXT,
            cove_partner_id        TEXT,
            duo_account_id         TEXT,
            huntress_org_id        TEXT,
            threatlocker_org_id    TEXT,
            cw_automate_client_id  TEXT,
            updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


    # Huntress EDR tables — populated by sync_huntress_data.py
    conn.executescript("""
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
    """)

    # Duo account metadata — populated by sync_duo_data.py so the mappings
    # UI can show account names instead of opaque IDs.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS duo_accounts (
            account_id      TEXT PRIMARY KEY,
            name            TEXT,
            api_hostname    TEXT,
            synced_at       TEXT
        )
    """)

    # ThreatLocker — populated by sync_threatlocker_data.py
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threatlocker_organizations (
            id              TEXT PRIMARY KEY,
            name            TEXT,
            computer_count  INTEGER DEFAULT 0,
            updated_at      TEXT
        )
    """)

    conn.executescript("""
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
    """)

    # Add threatlocker_org_id to customer_map if missing (migration)
    try:
        conn.execute("ALTER TABLE customer_map ADD COLUMN threatlocker_org_id TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            pass  # Column already exists
        else:
            logging.getLogger(__name__).error("Failed to add threatlocker_org_id column: %s", e)
            raise

    # Add mapping_ignored flag to customer_map (migration)
    try:
        conn.execute("ALTER TABLE customer_map ADD COLUMN mapping_ignored INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            logging.getLogger(__name__).error("Failed to add mapping_ignored column: %s", e)
            raise

    # Migrate existing tables that lack the new columns
    try:
        conn.execute("ALTER TABLE known_users ADD COLUMN activated INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE known_users ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # Column already exists

    # Migrate: consolidate "analytics" feature into "admin".
    # Anyone who had "analytics" but not "admin" gets promoted to "admin".
    try:
        conn.execute("""
            INSERT OR IGNORE INTO feature_access (feature, user_email)
            SELECT 'admin', user_email FROM feature_access WHERE feature = 'analytics'
        """)
        conn.execute("DELETE FROM feature_access WHERE feature = 'analytics'")
    except Exception as e:
        log.error("analytics→admin migration failed (INSERT/DELETE on feature_access): %s", e)
        raise

    conn.commit()


def upsert_known_user(email, name):
    """Record or update a user who has authenticated with the app.

    Sets activated=1 because this is only called from the Azure AD
    callback — meaning the user has genuinely signed in.
    """
    if not email:
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("""
            INSERT INTO known_users (email, name, first_seen, last_seen, activated)
            VALUES (LOWER(?), ?, ?, ?, 1)
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                last_seen = excluded.last_seen,
                activated = 1
        """, (email, name, now, now))
        conn.commit()
        conn.close()
    except Exception:
        pass  # Never let this break login


def seed_known_users():
    """Pre-populate known_users from CW members and historical usage_log.

    Runs at app startup so the permissions page shows the full team
    immediately — admins don't have to wait for each person to sign in.

    Uses INSERT OR IGNORE so Azure sign-in data (which has accurate names
    and last_seen timestamps) always takes priority.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    # ── 1. Seed from CW members (active only, must have an email) ─────────
    members = []
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        has_members = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='members'"
        ).fetchone()
        if has_members:
            members = [dict(r) for r in conn.execute(
                "SELECT email, full_name FROM members "
                "WHERE inactive_flag = 0 AND email IS NOT NULL AND email != ''"
            ).fetchall()]
        conn.close()
    except Exception as e:
        log.warning("seed_known_users (SQLite members): %s", e)

    # Write to known_users (local app DB)
    if members:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            for m in members:
                conn.execute("""
                    INSERT OR IGNORE INTO known_users (email, name, first_seen, last_seen)
                    VALUES (LOWER(?), ?, ?, ?)
                """, (m["email"], m["full_name"], now, now))
            conn.commit()
            conn.close()
            log.info("seed_known_users: seeded %d active CW members", len(members))
        except Exception as e:
            log.warning("seed_known_users (write): %s", e)

    # ── 2. Seed from usage_log in conversations.db ────────────────────────
    try:
        data_dir = os.path.dirname(DB_PATH)
        conv_path = os.path.join(data_dir, "conversations.db")
        if not os.path.exists(conv_path):
            return

        conv_conn = sqlite3.connect(conv_path, timeout=10)
        conv_conn.row_factory = sqlite3.Row

        # Check usage_log table exists
        has_usage = conv_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_log'"
        ).fetchone()
        if not has_usage:
            conv_conn.close()
            return

        # Get distinct users — pick the most recent name per email
        usage_users = conv_conn.execute("""
            SELECT user_email, user_name,
                   MIN(timestamp) as first_seen,
                   MAX(timestamp) as last_seen
            FROM usage_log
            WHERE user_email IS NOT NULL AND user_email != ''
            GROUP BY LOWER(user_email)
        """).fetchall()
        conv_conn.close()

        if usage_users:
            main_conn = sqlite3.connect(DB_PATH, timeout=10)
            main_conn.execute("PRAGMA busy_timeout=5000")
            count = 0
            for u in usage_users:
                name = u["user_name"] or u["user_email"]
                main_conn.execute("""
                    INSERT OR IGNORE INTO known_users (email, name, first_seen, last_seen)
                    VALUES (LOWER(?), ?, ?, ?)
                """, (u["user_email"], name, u["first_seen"], u["last_seen"]))
                count += 1
            main_conn.commit()
            main_conn.close()
            log.info("seed_known_users: seeded %d users from usage_log", count)

    except Exception as e:
        log.warning("seed_known_users (usage_log): %s", e)


def get_db():
    """Get a read/write database connection.

    Used internally by the app for things like schema introspection and the
    insights queries. NOT used for the agent's tool-call SQL or pin SQL —
    those go through get_db_readonly() so the SQLite engine itself enforces
    that the agent can never write, regardless of any regex validator.
    """
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_db_readonly():
    """Get a strictly read-only connection to the CW data DB.

    Any attempt to INSERT/UPDATE/DELETE/ALTER/etc. through this connection
    will raise sqlite3.OperationalError at the engine level, regardless of
    what the SQL string looks like. This is the defence-in-depth backstop
    for agent-generated and user-pinned SQL.
    """
    if not os.path.exists(DB_PATH):
        return None
    # file: URI with mode=ro opens the database in true read-only mode.
    # immutable=0 (default) so we still see writes from the sync containers.
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    # busy_timeout still useful while the sync container holds a write lock.
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def get_schema_description():
    """Get a human-readable description of the database schema."""
    conn = get_db()
    if not conn:
        return "No database found. Check local database path."

    placeholders = ",".join("?" for _ in _INTERNAL_TABLES)
    tables = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' "
        f"AND name NOT IN ({placeholders}) ORDER BY name",
        tuple(_INTERNAL_TABLES),
    ).fetchall()
    schema_parts = []

    for table in tables:
        table_name = table["name"]
        columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        count = conn.execute(f"SELECT COUNT(*) as c FROM {table_name}").fetchone()["c"]
        col_list = ", ".join(f"{c['name']} ({c['type'] or 'TEXT'})" for c in columns if c['name'] != 'raw_json')
        schema_parts.append(f"**{table_name}** ({count} rows): {col_list}")

    try:
        last_sync = conn.execute("SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
        sync_info = f"Last sync: {last_sync['synced_at']}" if last_sync else "No sync recorded"
    except Exception:
        sync_info = "No sync recorded"

    conn.close()
    return "\n\n".join(schema_parts) + f"\n\n{sync_info}"


def get_sample_data():
    """Get a small sample of data to help Claude understand the data shape."""
    conn = get_db()
    if not conn:
        return ""

    samples = []

    try:
        statuses = conn.execute("SELECT DISTINCT status_name FROM tickets WHERE status_name IS NOT NULL LIMIT 20").fetchall()
        if statuses:
            samples.append("Ticket statuses in use: " + ", ".join(r["status_name"] for r in statuses))
    except Exception:
        pass

    try:
        boards = conn.execute("SELECT DISTINCT board_name FROM tickets WHERE board_name IS NOT NULL LIMIT 10").fetchall()
        if boards:
            samples.append("Service boards: " + ", ".join(r["board_name"] for r in boards))
    except Exception:
        pass

    try:
        top_companies = conn.execute("""
            SELECT company_name, COUNT(*) as cnt FROM tickets
            WHERE company_name IS NOT NULL
            GROUP BY company_name ORDER BY cnt DESC LIMIT 10
        """).fetchall()
        if top_companies:
            parts = [r["company_name"] + " (" + str(r["cnt"]) + ")" for r in top_companies]
            samples.append("Top companies by tickets: " + ", ".join(parts))
    except Exception:
        pass

    try:
        members = conn.execute("SELECT full_name, identifier FROM members WHERE inactive_flag=0 LIMIT 15").fetchall()
        if members:
            parts = [r["full_name"] + " (" + r["identifier"] + ")" for r in members]
            samples.append("Active team members: " + ", ".join(parts))
    except Exception:
        pass

    try:
        priorities = conn.execute("SELECT DISTINCT priority_name FROM tickets WHERE priority_name IS NOT NULL LIMIT 10").fetchall()
        if priorities:
            samples.append("Priority levels: " + ", ".join(r["priority_name"] for r in priorities))
    except Exception:
        pass

    conn.close()
    return "\n".join(samples)


def execute_sql(sql):
    """Execute a SQL query (agent-generated) and return results.

    Uses the local SQLite database in read-only mode.
    """
    started = time.monotonic()

    # ── Pre-flight checks ─────────────────────────────────────────────────
    # Strip leading SQL comments (-- ...) before checking the statement type
    cleaned = sql.strip()
    while cleaned.startswith("--"):
        before, sep, after = cleaned.partition("\n")
        if not sep:
            cleaned = ""
            break
        cleaned = after.strip()
    stripped = cleaned.upper()
    stripped = cleaned.upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        log.info("execute_sql rejected non-SELECT statement")
        return {"error": "Only SELECT queries are allowed."}

    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "DETACH",
                  "PRAGMA", "EXPLAIN"]
    for word in dangerous:
        if re.search(rf'\b{word}\b', stripped):
            log.warning("execute_sql denylist hit: %s", word)
            return {"error": f"Dangerous SQL keyword detected: {word}"}

    # Block access to internal/admin tables that contain secrets or PII.
    # Matches table names anywhere in the query (including string literals).
    # This is intentionally conservative — a false positive on a query like
    # SELECT 'app_settings' is preferable to a false negative that leaks
    # credentials.  In practice the AI never generates queries containing
    # these names as string literals since the tables are hidden from the schema.
    for blocked in _BLOCKED_TABLES:
        if re.search(rf'\b{blocked}\b', cleaned, re.IGNORECASE):
            log.warning("execute_sql blocked access to restricted table: %s", blocked)
            return {"error": f"Access to {blocked} is not permitted."}

    conn = get_db_readonly()
    if not conn:
        log.warning("execute_sql called but DB not found at %s", DB_PATH)
        return {"error": "Database not found. Check local database path."}

    try:
        cursor = conn.execute(cleaned)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(MAX_ROWS)
        results = [dict(zip(columns, row)) for row in rows]

        extra = cursor.fetchone()
        truncated = extra is not None

        conn.close()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("execute_sql ok rows=%d truncated=%s ms=%d", len(results), truncated, elapsed_ms)
        return {
            "columns": columns,
            "rows": results,
            "row_count": len(results),
            "truncated": truncated,
        }
    except Exception as e:
        conn.close()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.warning("execute_sql failed ms=%d err=%s sql=%s", elapsed_ms, e, sql[:200])
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
# App settings — DB-managed configuration
# ═════════════════════════════════════════════════════════════════════════════

# Settings that can be managed via the admin UI. Each entry maps a DB key
# to the env var name that seeds its initial value and a human-readable label.
# Settings with a "validate" key are checked on save — see validate_setting().

import re as _re

def validate_setting(key, value):
    """Validate a setting value against its declared rules.

    Returns (ok: bool, error: str | None).
    """
    meta = CONFIGURABLE_SETTINGS.get(key)
    if not meta:
        return False, f"Unknown setting: {key}"

    rules = meta.get("validate")
    if not rules:
        return True, None  # no rules → always valid

    # Allow empty for optional fields
    if rules.get("allow_empty") and not value.strip():
        return True, None

    vtype = rules.get("type")

    if vtype == "integer":
        try:
            n = int(value)
        except (ValueError, TypeError):
            return False, "Must be a whole number."
        if "min" in rules and n < rules["min"]:
            return False, f"Must be at least {rules['min']}."
        if "max" in rules and n > rules["max"]:
            return False, f"Must be at most {rules['max']}."

    elif vtype == "url":
        v = value.strip()
        if v and not _re.match(r'^https?://.+', v):
            return False, "Must be a valid URL starting with https:// or http://."

    elif vtype == "guid":
        v = value.strip()
        if v and not _re.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', v, _re.IGNORECASE
        ):
            return False, "Must be a valid GUID (e.g. 12345678-abcd-1234-abcd-123456789abc)."

    elif vtype == "hostname":
        v = value.strip()
        if v and not _re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$', v):
            return False, "Must be a valid hostname (e.g. api-XXXXXXXX.duosecurity.com)."

    elif vtype == "choice":
        if value not in rules.get("choices", []):
            return False, f"Must be one of: {', '.join(rules['choices'])}."

    elif vtype == "timezone":
        v = value.strip()
        if v:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(v)
            except (KeyError, Exception):
                return False, f"'{v}' is not a valid IANA timezone. Examples: Australia/Brisbane, America/New_York, Europe/London."

    elif vtype == "date":
        v = value.strip()
        if v:
            try:
                from datetime import datetime as _dt
                _dt.strptime(v, "%Y-%m-%d")
            except ValueError:
                return False, "Must be a valid date in YYYY-MM-DD format (e.g. 2026-12-31)."

    return True, None


CONFIGURABLE_SETTINGS = {
    # ── Application ──────────────────────────────────────────────────────────
    "app_name":                  {"env": "APP_NAME",                  "label": "Application name",              "default": "ChatPSA",
                                  "description": "Display name shown in the nav bar, browser tab, and AI system prompt."},
    "docs_base_url":             {"env": "DOCS_BASE_URL",             "label": "Documentation URL",             "default": "",
                                  "description": "Base URL for documentation links in the admin UI. Set to your GitHub repo URL, e.g. https://github.com/yourorg/ChatPSA/blob/main. Leave blank to hide doc links.",
                                  "help_url": "docs/administration.md#documentation-links",
                                  "validate": {"type": "url", "allow_empty": True}},
    "helpdesk_board":            {"env": "HELPDESK_BOARD",            "label": "Helpdesk board name",           "default": "Help Desk",
                                  "description": "ConnectWise service board name used for ticket queries and trend analysis."},
    "app_tz_name":               {"env": "APP_TZ_NAME",               "label": "Timezone (IANA name)",          "default": "Australia/Brisbane",
                                  "description": "IANA timezone name for date/time display and queries. Examples: Australia/Brisbane, America/New_York, Europe/London. Handles DST automatically. Requires container restart to take effect.",
                                  "help_url": "docs/timezone.md",
                                  "validate": {"type": "timezone"}},
    "trends_exclude_companies":  {"env": "TRENDS_EXCLUDE_COMPANIES",  "label": "Excluded companies",            "default": "",
                                  "description": "Comma-separated company names to exclude from trend and anomaly analysis. Typically your own MSP company name."},
    "trends_high_priorities":    {"env": "TRENDS_HIGH_PRIORITIES",     "label": "High-priority names",           "default": "",
                                  "description": "Comma-separated priority names that count as high priority for stale-ticket insights. Leave blank to auto-detect Critical/High."},
    # ── Authentication (Azure AD / Entra ID) ────────────────────────────────
    "azure_client_id":           {"env": "AZURE_CLIENT_ID",           "label": "Azure Client ID",               "default": "",
                                  "description": "Application (client) ID from your Azure app registration. Found on the app's Overview page in Entra admin centre.",
                                  "help_url": "docs/authentication.md#setup",
                                  "group": "Authentication"},
    "azure_client_secret":       {"env": "AZURE_CLIENT_SECRET",       "label": "Azure Client Secret",           "default": "",
                                  "description": "Client secret value for Azure AD authentication. Rotate before expiry to avoid lockouts — see Secret Expiry Monitoring.",
                                  "help_url": "docs/authentication.md#rotating-a-client-secret",
                                  "type": "secret", "group": "Authentication"},
    "azure_client_secret_previous": {"env": "",                       "label": "Previous Client Secret",        "default": "",
                                  "description": "Automatically saved when you rotate the primary secret. Copy this value back to Azure Client Secret if you need to roll back.",
                                  "type": "secret", "group": "Authentication"},
    "azure_tenant_id":           {"env": "AZURE_TENANT_ID",           "label": "Azure Tenant ID",               "default": "",
                                  "description": "Directory (tenant) ID from your Azure app registration. Found on the app's Overview page in Entra admin centre.",
                                  "help_url": "docs/authentication.md#setup",
                                  "group": "Authentication"},
    # ── AI Model ─────────────────────────────────────────────────────────────
    "claude_model":              {"env": "CLAUDE_MODEL",              "label": "Claude model",                  "default": "claude-sonnet-4-6",
                                  "description": "Anthropic model ID for the chat agent. Options: claude-sonnet-4-6, claude-haiku-4-5.",
                                  "validate": {"type": "choice", "choices": ["claude-sonnet-4-6", "claude-haiku-4-5"]}},
    "claude_max_tokens":         {"env": "CLAUDE_MAX_TOKENS",         "label": "Max response tokens",           "default": "4096",
                                  "description": "Maximum tokens in each Claude response. Higher values allow longer answers but increase cost.",
                                  "validate": {"type": "integer", "min": 100, "max": 32000}},
    # ── CIPP / Microsoft 365 ─────────────────────────────────────────────────
    "cipp_api_url":              {"env": "CIPP_API_URL",              "label": "CIPP API URL",                  "default": "",
                                  "description": "Base URL of your CIPP API Azure Function, e.g. https://cipp-api.azurewebsites.net.",
                                  "group": "CIPP / Microsoft 365",
                                  "validate": {"type": "url", "allow_empty": True}},
    "cipp_client_id":            {"env": "CIPP_CLIENT_ID",            "label": "CIPP Client ID",                "default": "",
                                  "description": "Azure app-registration client ID (GUID) for CIPP. Found in CIPP Settings > Backend > SAM Setup.",
                                  "type": "secret", "group": "CIPP / Microsoft 365",
                                  "validate": {"type": "guid", "allow_empty": True}},
    "cipp_client_secret":        {"env": "CIPP_CLIENT_SECRET",        "label": "CIPP Client Secret",            "default": "",
                                  "description": "Azure app-registration client secret for CIPP.",
                                  "type": "secret", "group": "CIPP / Microsoft 365"},
    "cipp_tenant_id":            {"env": "CIPP_TENANT_ID",            "label": "CIPP Tenant ID",                "default": "",
                                  "description": "Your MSP Azure tenant ID. Used to construct the OAuth2 token URL if CIPP Token URL is not set.",
                                  "group": "CIPP / Microsoft 365",
                                  "validate": {"type": "guid", "allow_empty": True}},
    "cipp_token_url":            {"env": "CIPP_TOKEN_URL",            "label": "CIPP Token URL",                "default": "",
                                  "description": "Full OAuth2 token endpoint URL. If blank, derived from CIPP Tenant ID.",
                                  "group": "CIPP / Microsoft 365",
                                  "validate": {"type": "url", "allow_empty": True}},
    # ── Duo Security ─────────────────────────────────────────────────────────
    "duo_ikey":                  {"env": "DUO_IKEY",                  "label": "Duo Integration Key",           "default": "",
                                  "description": "Duo Admin API integration key. Create an Admin API application in the Duo Admin Panel.",
                                  "type": "secret", "group": "Duo Security"},
    "duo_skey":                  {"env": "DUO_SKEY",                  "label": "Duo Secret Key",                "default": "",
                                  "description": "Duo Admin API secret key.",
                                  "type": "secret", "group": "Duo Security"},
    "duo_host":                  {"env": "DUO_HOST",                  "label": "Duo API Host",                  "default": "",
                                  "description": "Duo API hostname, e.g. api-XXXXXXXX.duosecurity.com.",
                                  "group": "Duo Security",
                                  "validate": {"type": "hostname", "allow_empty": True}},
    # ── Huntress EDR ─────────────────────────────────────────────────────────
    "huntress_api_key":          {"env": "HUNTRESS_API_KEY",           "label": "Huntress API Key",              "default": "",
                                  "description": "Huntress API key (used as the Basic auth username). Found in your Huntress account under API Credentials.",
                                  "type": "secret", "group": "Huntress EDR"},
    "huntress_api_secret":       {"env": "HUNTRESS_API_SECRET",        "label": "Huntress API Secret",           "default": "",
                                  "description": "Huntress API secret (used as the Basic auth password).",
                                  "type": "secret", "group": "Huntress EDR"},
    # ── ThreatLocker ─────────────────────────────────────────────────────────
    "threatlocker_api_key":      {"env": "THREATLOCKER_API_KEY",       "label": "ThreatLocker API Key",          "default": "",
                                  "description": "ThreatLocker portal API key (parent-level). Enables endpoint inventory sync across all managed orgs.",
                                  "type": "secret", "group": "ThreatLocker"},
    # ── Secret Expiry ─────────────────────────────────────────────────────────
    "azure_secret_expiry":       {"env": "",                              "label": "Auth secret expiry date",       "default": "",
                                  "description": "Expiry date of the Azure AD client secret used for authentication (YYYY-MM-DD). Shown in Azure portal when creating the secret. If Application.Read.All is granted, this is detected automatically.",
                                  "help_url": "docs/authentication.md#secret-expiry-monitoring",
                                  "group": "Secret Expiry Tracking",
                                  "validate": {"type": "date", "allow_empty": True}},

    # ── Timeline ─────────────────────────────────────────────────────────────
    "timeline_ai_summaries":     {"env": "TIMELINE_AI_SUMMARIES",      "label": "AI ticket summaries",           "default": "true",
                                  "description": "Automatically generate AI summaries for timeline events during sync. Disable to save API costs or if summaries aren't needed.",
                                  "group": "Timeline",
                                  "validate": {"type": "choice", "choices": ["true", "false"]}},
    "timeline_boards":           {"env": "TIMELINE_BOARDS",            "label": "Board filter",                  "default": "",
                                  "description": "Comma-separated list of board names to include in the timeline. Leave blank to include all boards.",
                                  "group": "Timeline"},
}


def seed_app_settings():
    """Seed app_settings from environment variables on startup.

    Normal mode: only writes a row if one doesn't already exist for that
    key — env values act as initial defaults, never overwriting admin changes.

    ENV_OVERRIDE mode: overwrites DB values with env vars for any variable
    that is explicitly set in the environment.  Missing/commented-out vars
    are skipped (DB value preserved).  Empty vars (VAR="" or VAR=) write an
    empty string to the DB, effectively nulling that setting.  This allows
    admins to push a full config reset via .env + ENV_OVERRIDE.

    Called once at app startup.
    """
    if not os.path.exists(DB_PATH):
        return
    env_override = os.environ.get("ENV_OVERRIDE", "").lower() in ("true", "1", "yes")
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            for key, meta in CONFIGURABLE_SETTINGS.items():
                env_var = meta.get("env", "")
                if not env_var:
                    continue  # no env mapping for this setting

                if env_override:
                    # ENV_OVERRIDE mode: overwrite DB if var is present in env
                    env_val = os.environ.get(env_var)  # None = missing, "" = explicit empty
                    if env_val is None:
                        continue  # var not in env — leave DB value alone
                    conn.execute("""
                        INSERT INTO app_settings (key, value, updated_at, updated_by)
                        VALUES (?, ?, datetime('now'), ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at,
                            updated_by = excluded.updated_by
                    """, (key, env_val, "system (env override)"))
                else:
                    # Normal mode: seed defaults for missing keys only
                    value = os.environ.get(env_var, meta["default"])
                    if meta.get("type") == "secret" and not value:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO app_settings (key, value, updated_by) "
                        "VALUES (?, ?, ?)",
                        (key, value, "system (seeded from env)"),
                    )
            conn.commit()
        if env_override:
            log.info("seed_app_settings: ENV_OVERRIDE active — wrote env values to DB")
        else:
            log.info("seed_app_settings: seeded defaults for any missing keys")
    except Exception as e:
        log.warning("seed_app_settings error: %s", e)


def validate_azure_credentials_on_startup():
    """Test Azure AD credentials at startup and log a clear warning if invalid.

    Called once after seed_app_settings() so that credentials from .env or
    ENV_OVERRIDE are in the DB before we test them.  Only runs when all three
    Azure credential fields are populated.  Failures are logged at ERROR level
    so they're visible in container logs.

    Network errors are logged as warnings — the credentials might be fine but
    Azure AD might be unreachable at startup (e.g. DNS not ready in Docker).
    """
    from config import is_azure_enabled, get_azure_credentials

    if not is_azure_enabled():
        return  # Azure not configured — nothing to test

    client_id, client_secret, tenant_id = get_azure_credentials()

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
            log.info("Azure AD credentials validated successfully at startup")
            return

        error_code = result.get("error", "")
        error_desc = result.get("error_description", "")
        log.error(
            "AZURE CREDENTIAL VALIDATION FAILED — the configured client secret "
            "did not authenticate. Users will not be able to log in. "
            "Error: %s — %s. "
            "Fix: update AZURE_CLIENT_SECRET in .env or Admin > Settings.",
            error_code, error_desc[:200],
        )
        log_admin_event("auth", "error", "Azure credential validation failed at startup",
                        detail=f"{error_code}: {error_desc[:200]}")
    except Exception as e:
        log.warning(
            "Azure credential validation skipped — could not reach Azure AD: %s. "
            "This may be a transient network issue at startup.", e
        )


# ── Admin Events (actionable diagnostics log) ──────────────────────────────

def _ensure_admin_events_table(conn=None):
    """Create the admin_events table if it doesn't exist."""
    close = False
    if conn is None:
        if not os.path.exists(DB_PATH):
            return
        conn = sqlite3.connect(DB_PATH, timeout=5)
        close = True
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
                category    TEXT NOT NULL,
                level       TEXT NOT NULL DEFAULT 'info',
                title       TEXT NOT NULL,
                detail      TEXT,
                user_email  TEXT,
                metadata    TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_events_ts
            ON admin_events (timestamp DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_events_cat
            ON admin_events (category, timestamp DESC)
        """)
        conn.commit()
    finally:
        if close:
            conn.close()


def log_admin_event(category, level, title, detail=None, user_email=None, metadata=None):
    """Write an actionable event to the admin diagnostics log.

    Categories: ai, auth, sync, settings, integration
    Levels: error, warning, info

    Events older than 30 days are pruned on each insert to keep the table
    from growing unbounded.
    """
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        _ensure_admin_events_table(conn)
        meta_json = json.dumps(metadata) if metadata else None
        conn.execute(
            "INSERT INTO admin_events (category, level, title, detail, user_email, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (category, level, title, detail, user_email, meta_json),
        )
        # Prune old events (keep last 30 days)
        conn.execute(
            "DELETE FROM admin_events WHERE timestamp < datetime('now', '-30 days')"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("log_admin_event failed: %s", e)


def get_admin_events(category=None, level=None, limit=200, offset=0):
    """Read admin events with optional filters.

    Returns a list of dicts, most recent first.
    """
    if not os.path.exists(DB_PATH):
        return [], 0
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        _ensure_admin_events_table(conn)

        where_clauses = []
        params = []
        if category:
            where_clauses.append("category = ?")
            params.append(category)
        if level:
            where_clauses.append("level = ?")
            params.append(level)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.extend([limit, offset])

        rows = conn.execute(
            f"SELECT * FROM admin_events {where_sql} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM admin_events {where_sql}",
            params[:-2],  # exclude limit/offset
        ).fetchone()[0]

        conn.close()

        events = []
        for r in rows:
            evt = dict(r)
            if evt.get("metadata"):
                try:
                    evt["metadata"] = json.loads(evt["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            events.append(evt)
        return events, total
    except Exception as e:
        log.debug("get_admin_events failed: %s", e)
        return [], 0
