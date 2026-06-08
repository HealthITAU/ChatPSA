"""pins.py — Trend pin management."""
import logging
import re

from config import MAX_QUERY_PINS
from db import _get_conv_db, get_db_readonly

log = logging.getLogger("chatpsa.pins")


def _ensure_pins_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trend_pins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            summary         TEXT NOT NULL,
            sql_text        TEXT,
            pin_type        TEXT NOT NULL DEFAULT 'finding',
            pinned_by_name  TEXT,
            pinned_by_email TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            dismissed       INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pins_dismissed ON trend_pins(dismissed)")
    # Add pin_type column to existing tables that lack it
    try:
        conn.execute("ALTER TABLE trend_pins ADD COLUMN pin_type TEXT NOT NULL DEFAULT 'finding'")
    except Exception:
        pass  # Column already exists
    conn.commit()


def _validate_query_sql(sql_text):
    """Validate that a query pin's SQL is safe to auto-execute.

    Returns (is_valid, error_message).
    """
    if not sql_text or not sql_text.strip():
        return False, "SQL query is required for query pins."
    cleaned = sql_text.strip().rstrip(";").strip()
    # Must start with SELECT
    if not cleaned.upper().startswith("SELECT"):
        return False, "Query pins must be SELECT statements only."
    # Block dangerous keywords
    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX"]
    upper = cleaned.upper()
    for kw in dangerous:
        # Check for keyword as a whole word (not inside a column name)
        if re.search(r'\b' + kw + r'\b', upper):
            return False, f"Query pins cannot contain {kw} statements."
    return True, None


def get_pins():
    """Return all active (non-dismissed) trend pins, newest first."""
    conn = _get_conv_db()
    try:
        _ensure_pins_table(conn)
        rows = conn.execute(
            "SELECT * FROM trend_pins WHERE dismissed = 0 ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _execute_query_pin(pin):
    """Execute a query pin's SQL against the read-only SQLite DB and return results.

    Pinned SQL is user-supplied and treated as untrusted, even though
    _validate_query_sql() screens it on creation.

    Returns a list of dicts (max 20 rows) or an error string.
    """
    conn = get_db_readonly()
    if not conn:
        return "Database unavailable"
    try:
        rows = conn.execute(pin["sql_text"]).fetchmany(20)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("query pin failed pin_id=%s err=%s", pin.get("id"), e)
        return f"Query error: {e}"
    finally:
        conn.close()


def get_pins_with_results():
    """Return all active pins. Query pins include live results."""
    pins = get_pins()
    for pin in pins:
        if pin.get("pin_type") == "query":
            pin["results"] = _execute_query_pin(pin)
        else:
            pin["results"] = None
    return pins


def add_pin(title, summary, sql_text=None, pin_type="finding",
            pinned_by_name="Agent", pinned_by_email="agent"):
    """Save a new trend pin. Returns the new row id or (None, error)."""
    if pin_type == "query":
        valid, err = _validate_query_sql(sql_text)
        if not valid:
            return None, err
        # Check cap
        conn = _get_conv_db()
        try:
            _ensure_pins_table(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM trend_pins WHERE pin_type = 'query' AND dismissed = 0"
            ).fetchone()[0]
            if count >= MAX_QUERY_PINS:
                return None, f"Maximum of {MAX_QUERY_PINS} active query pins reached. Dismiss one first."
        finally:
            conn.close()

    conn = _get_conv_db()
    try:
        _ensure_pins_table(conn)
        cur = conn.execute(
            """INSERT INTO trend_pins (title, summary, sql_text, pin_type, pinned_by_name, pinned_by_email)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, summary, sql_text, pin_type, pinned_by_name, pinned_by_email)
        )
        conn.commit()
        return cur.lastrowid, None
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()


def dismiss_pin(pin_id):
    """Soft-delete a trend pin."""
    conn = _get_conv_db()
    try:
        _ensure_pins_table(conn)
        conn.execute("UPDATE trend_pins SET dismissed = 1 WHERE id = ?", (pin_id,))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()
