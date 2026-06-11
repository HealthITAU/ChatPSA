"""routes.py — Web and API route handlers."""
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

from flask import (Blueprint, jsonify, render_template,
                   request, session, url_for)

from agent import chat, ClaudeAPIError
from memory_store import upsert_memory, update_memory, delete_memory
from auth import api_login_required, check_all_features, feature_required, login_required
from config import (APP_DIR, is_azure_enabled,
                    is_cipp_enabled, is_duo_enabled,
                    is_huntress_enabled, is_threatlocker_enabled,
                    DB_PATH, MEMORIES_DB_PATH, get_tz_offset_sql)
from settings import get_setting
from db import (get_db, get_db_readonly, store_example,
                _get_conv_db, _ensure_usage_table, get_query_themes,
                save_query_themes, log_page_view)
from insights import get_insights, get_weekly_ticket_volume
from pins import add_pin, dismiss_pin, get_pins_with_results

log = logging.getLogger("chatpsa.routes")

main_bp = Blueprint("main", __name__)

# ── Docs URL helper ───────────────────────────────────────────────────────────

def _resolve_docs_url(relative_path: str) -> str:
    """Resolve a relative docs path against docs_base_url.

    Returns the full URL (e.g. https://github.com/.../docs/authentication.md#setup)
    or an empty string if docs_base_url is not configured.
    """
    docs_base = (get_setting("docs_base_url", "") or "").rstrip("/")
    if not docs_base:
        return ""
    return f"{docs_base}/{relative_path}"


# ── Changelog (single source of truth) ────────────────────────────────────────
_CHANGELOG_PATH = os.path.join(APP_DIR, "changelog.json")

def _load_changelog():
    """Load the changelog entries from the JSON file."""
    try:
        with open(_CHANGELOG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return []

def _current_version():
    """Return the latest version string from changelog.json."""
    entries = _load_changelog()
    return f"v{entries[0]['version']}" if entries else "v2.0"


# ── Sync source registry ─────────────────────────────────────────────────────

def get_sync_sources():
    """Return an ordered dict of configured sync sources.

    Each source defines:
      - label:          Human-readable name shown in the UI
      - script:         Python script filename (relative to APP_DIR)
      - lock_file:      Filename (not full path) of the in-progress lock file
      - tables_pattern: SQL LIKE pattern that matches this source's sync_log rows
      - notes_pattern:  Optional SQL LIKE pattern to identify notes-only sync rows
    """
    sources = {
        "cw": {
            "label": "ConnectWise",
            "script": "sync_cw_data.py",
            "lock_file": ".sync_running_cw",
            "tables_pattern": "%tickets%",
            "notes_pattern": "%ticket_notes%",
        },
    }
    if is_cipp_enabled():
        sources["cipp"] = {
            "label": "Microsoft 365",
            "script": "sync_cipp_data.py",
            "lock_file": ".sync_running_cipp",
            "tables_pattern": "%cipp_tenants%",
            "notes_pattern": None,
        }
    if is_duo_enabled():
        sources["duo"] = {
            "label": "Duo Security",
            "script": "sync_duo_data.py",
            "lock_file": ".sync_running_duo",
            "tables_pattern": "%duo_users%",
            "notes_pattern": None,
        }
    if is_huntress_enabled():
        sources["huntress"] = {
            "label": "Huntress EDR",
            "script": "sync_huntress_data.py",
            "lock_file": ".sync_running_huntress",
            "tables_pattern": "%huntress_%",
            "notes_pattern": None,
        }
    if is_threatlocker_enabled():
        sources["threatlocker"] = {
            "label": "ThreatLocker",
            "script": "sync_threatlocker_data.py",
            "lock_file": ".sync_running_threatlocker",
            "tables_pattern": "%threatlocker_%",
            "notes_pattern": None,
        }
    return sources


# ── Tables counted per source for the status panel ────────────────────────────

_SOURCE_TABLES = {
    "cw":       ["tickets", "time_entries", "agreements", "companies",
                 "members", "contacts", "configurations", "projects",
                 "invoices", "ticket_notes"],
    "cipp":     ["cipp_tenants", "cipp_licenses", "cipp_alerts", "cipp_policies"],
    "duo":      ["duo_users", "duo_accounts"],
    "huntress": ["huntress_organizations", "huntress_agents", "huntress_incidents"],
    "threatlocker": ["threatlocker_organizations", "threatlocker_computers"],
}


def _live_counts(conn, tables):
    """Return {table: row_count} for each table that exists."""
    counts = {}
    for t in tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            pass
    return counts


def _track_page(page_name):
    """Log a page view for the current user (fire-and-forget)."""
    try:
        user = session.get("user", {}) if is_azure_enabled() else {}
        log_page_view(
            page=page_name,
            user_email=user.get("email", "anonymous"),
            user_name=user.get("name", "anonymous"),
            referrer=request.referrer,
        )
    except Exception:
        pass  # Never let tracking break the page


@main_bp.route("/", endpoint="index")
@login_required
def index():
    _track_page("chat")
    user = session.get("user", {}) if is_azure_enabled() else {}
    email = user.get("email", "")
    feat = check_all_features(email)
    return render_template("index.html",
                           auth_enabled=is_azure_enabled(),
                           user_name=user.get("name", ""),
                           user_email=user.get("email", ""),
                           has_timeline=feat["timeline"],
                           has_analytics=feat["admin"],
                           has_admin=feat["admin"],
                           app_version=_current_version())


@main_bp.route("/api/chat", methods=["POST"], endpoint="api_chat")
@api_login_required
def api_chat():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"error": "Missing 'message' in request body"}), 400

    session_id = data.get("session_id", "default")
    user_message = data["message"]
    source = data.get("source", "typed")  # "typed" or "suggested"
    anomaly_context = data.get("anomaly_context")  # optional silent context from insight chip

    user = session.get("user", {}) if is_azure_enabled() else {}
    user_email = user.get("email", "anonymous")
    user_name = user.get("name", "anonymous")

    t_start = datetime.now()
    errored = False
    try:
        result = chat(session_id, user_message, user_email=user_email, user_name=user_name, anomaly_context=anomaly_context)
        if result.get("error"):
            errored = True
        # Include the original question so the frontend can send it back with feedback
        result["question"] = user_message
        return jsonify(result)
    except ClaudeAPIError as e:
        errored = True
        log.error("ClaudeAPIError: %s | detail: %s", e, e.error_detail)
        from db import log_admin_event
        log_admin_event("ai", "error", "AI request failed",
                        detail=e.error_detail, user_email=user_email)
        return jsonify({
            "response": str(e),
            "question": user_message,
        }), 200  # 200 so the frontend renders the message normally
    except Exception as e:
        errored = True
        log.exception("api_chat unexpected error")
        from db import log_admin_event
        log_admin_event("ai", "error", "Unexpected AI error",
                        detail=str(e)[:500], user_email=user_email)
        return jsonify({"error": "internal_error", "response": "Something went wrong. Please try again."}), 500
    finally:
        from db import log_usage
        response_ms = int((datetime.now() - t_start).total_seconds() * 1000)
        log_usage(session_id, user_email, user_name, user_message, source, response_ms, errored)


@main_bp.route("/api/memory/approve", methods=["POST"], endpoint="api_memory_approve")
@api_login_required
def api_memory_approve():
    """Approve a pending memory operation (store/update/delete)."""
    data = request.get_json()
    if not data or "action" not in data:
        return jsonify({"error": "Missing 'action' in request body"}), 400

    user = session.get("user", {}) if is_azure_enabled() else {}
    user_email = user.get("email", "anonymous")
    action = data["action"]

    try:
        if action == "store":
            result = upsert_memory(
                MEMORIES_DB_PATH, user_email,
                data["key"], data["value"],
                source="agent", session_id=data.get("session_id", ""),
            )
            return jsonify({"ok": True, "message": f"Memory saved: {result['key']}"})

        elif action == "update":
            updated = update_memory(
                MEMORIES_DB_PATH, user_email,
                data["memory_id"], value=data["value"],
                source="agent", session_id=data.get("session_id", ""),
            )
            if not updated:
                return jsonify({"ok": False, "message": "Memory not found."}), 404
            return jsonify({"ok": True, "message": f"Memory updated: {updated['key']}"})

        elif action == "delete":
            deleted = delete_memory(MEMORIES_DB_PATH, user_email, data["memory_id"])
            if not deleted:
                return jsonify({"ok": False, "message": "Memory not found."}), 404
            return jsonify({"ok": True, "message": "Memory deleted."})

        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        log.exception("api_memory_approve error")
        return jsonify({"error": "Memory operation failed."}), 500


@main_bp.route("/healthz", endpoint="healthz")
def healthz():
    """Lightweight liveness probe for Docker HEALTHCHECK.

    Returns 200 if the app process is running and can serve HTTP.
    Does NOT check data state — use /api/status for that.
    """
    return jsonify({"ok": True})


@main_bp.route("/api/status", endpoint="api_status")
def api_status():
    """Health check with per-source sync status.

    Basic ok/not-ok is public; full sync details require authentication.
    """
    issues = []
    initial_sync_pending = False

    if not os.path.exists(DB_PATH):
        initial_sync_pending = True
    else:
        conn = get_db()
        if conn:
            try:
                count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                if count == 0:
                    initial_sync_pending = True
            except Exception:
                initial_sync_pending = True
            conn.close()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        issues.append("ANTHROPIC_API_KEY not set.")

    is_ok = len(issues) == 0 and not initial_sync_pending

    # Unauthenticated callers get only ok/not-ok
    user = session.get("user") if is_azure_enabled() else None
    if not user and is_azure_enabled():
        return jsonify({"ok": is_ok, "initial_sync_pending": initial_sync_pending, "auth_enabled": is_azure_enabled()})

    # Build per-source sync info from sync_log + lock files
    sync_sources = get_sync_sources()
    data_dir = os.path.dirname(DB_PATH)
    sources_info = {}

    try:
        conn = get_db()
        if conn:
            for src_key, src_cfg in sync_sources.items():
                row = conn.execute(
                    "SELECT synced_at, duration_seconds FROM sync_log "
                    "WHERE tables_synced LIKE ? ORDER BY id DESC LIMIT 1",
                    (src_cfg["tables_pattern"],),
                ).fetchone()
                notes_synced_at = None
                if src_cfg.get("notes_pattern"):
                    notes_row = conn.execute(
                        "SELECT synced_at FROM sync_log WHERE tables_synced LIKE ? "
                        "ORDER BY id DESC LIMIT 1",
                        (src_cfg["notes_pattern"],),
                    ).fetchone()
                    if notes_row:
                        notes_synced_at = notes_row["synced_at"]

                tables_for_source = _SOURCE_TABLES.get(src_key, _SOURCE_TABLES["cw"])
                counts = _live_counts(conn, tables_for_source)

                lock_path = os.path.join(data_dir, src_cfg["lock_file"])
                sources_info[src_key] = {
                    "label": src_cfg["label"],
                    "last_sync": row["synced_at"] if row else None,
                    "last_sync_duration": row["duration_seconds"] if row else None,
                    "last_notes_sync": notes_synced_at,
                    "record_counts": counts,
                    "syncing": os.path.exists(lock_path),
                }
            conn.close()
    except Exception:
        pass

    return jsonify({
        "ok": is_ok,
        "initial_sync_pending": initial_sync_pending,
        "issues": issues,
        "sources": sources_info,
        "auth_enabled": is_azure_enabled(),
    })


@main_bp.route("/api/sync", methods=["POST"], endpoint="api_sync")
@api_login_required
@feature_required("admin")
def api_sync():
    """Trigger a data sync from the web UI.

    Starts the sync subprocess in the background and returns immediately —
    the frontend poller detects progress and completion via the lock file.

    Optional JSON body: {"source": "cw"} or {"source": "cipp"}.
    Defaults to "cw" if not specified.
    """
    body = request.get_json(silent=True) or {}
    source_key = body.get("source", "cw")

    sync_sources = get_sync_sources()
    if source_key not in sync_sources:
        return jsonify({"ok": False, "error": f"Unknown sync source: {source_key}"}), 400

    src_cfg = sync_sources[source_key]

    # Refuse to start if already running
    lock_path = os.path.join(os.path.dirname(DB_PATH), src_cfg["lock_file"])
    if os.path.exists(lock_path):
        return jsonify({"ok": False, "error": "Sync already in progress"}), 409

    sync_script = os.path.join(APP_DIR, src_cfg["script"])
    cmd = [sys.executable, sync_script, "--db", DB_PATH]
    if source_key == "cw":
        cmd += ["--recent"]

    try:
        env = os.environ.copy()
        env["CW_DB_PATH"] = DB_PATH
        # Fire and forget — detach from the gunicorn worker so it outlives the request.
        subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("sync started source=%s cmd=%s", source_key, " ".join(cmd))
        return jsonify({"ok": True, "source": source_key, "started": True})
    except Exception as e:
        log.exception("sync failed to start source=%s", source_key)
        return jsonify({"ok": False, "error": "Failed to start sync process."}), 500


@main_bp.route("/api/sync/status", endpoint="api_sync_status")
@api_login_required
@feature_required("admin")
def api_sync_status():
    """Return rich per-source sync state for the admin Sync tab.

    Combines sync_state per-entity rows, lock-file syncing indicators,
    source labels, last sync timestamps from sync_log, and live table counts.
    """
    sync_sources = get_sync_sources()
    data_dir = os.path.dirname(DB_PATH)

    # Build per-source info with labels and syncing indicator
    per_source = {}
    for key, cfg in sync_sources.items():
        per_source[key] = {
            "label": cfg["label"],
            "syncing": os.path.exists(os.path.join(data_dir, cfg["lock_file"])),
        }
    any_syncing = any(v["syncing"] for v in per_source.values())

    entities = []
    live_counts = {}
    conn = get_db_readonly()
    if conn:
        # Per-entity status from sync_state
        try:
            rows = conn.execute("""
                SELECT source, entity, last_started_at, last_completed_at,
                       last_status, last_error, last_record_count, last_duration_sec
                FROM sync_state
                ORDER BY source, entity
            """).fetchall()
            entities = [dict(r) for r in rows]
        except Exception as e:
            log.debug("sync_state not available: %s", e)

        # Last sync timestamp per source from sync_log
        for key, cfg in sync_sources.items():
            try:
                row = conn.execute(
                    "SELECT synced_at, duration_seconds FROM sync_log "
                    "WHERE tables_synced LIKE ? ORDER BY id DESC LIMIT 1",
                    (cfg["tables_pattern"],),
                ).fetchone()
                if row:
                    per_source[key]["last_sync"] = row["synced_at"]
                    per_source[key]["last_duration"] = row["duration_seconds"]
            except Exception:
                pass

        # Live record counts for all known data tables
        all_tables = [t for tables in _SOURCE_TABLES.values() for t in tables]
        for t in set(all_tables):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                live_counts[t] = n
            except Exception:
                pass
        conn.close()

    return jsonify({
        "syncing": any_syncing,
        "sources": per_source,
        "entities": entities,
        "live_counts": live_counts,
    })



@main_bp.route("/api/feedback", methods=["POST"], endpoint="api_feedback")
@api_login_required
def api_feedback():
    """Store or reject a query example based on user feedback (thumbs up/down)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    vote = data.get("vote")  # "up" or "down"
    question = data.get("question", "").strip()
    sql_text = data.get("sql", "").strip()

    if vote not in ("up", "down"):
        return jsonify({"error": "vote must be 'up' or 'down'"}), 400

    if vote == "up":
        if not question or not sql_text:
            return jsonify({"error": "question and sql are required for positive feedback"}), 400

        # Length caps to prevent prompt bloat
        if len(question) > 500:
            return jsonify({"error": "Question too long (max 500 characters)"}), 400
        if len(sql_text) > 2000:
            return jsonify({"error": "SQL too long (max 2000 characters)"}), 400

        # Run the same safety checks as execute_sql — reject dangerous keywords
        # and blocked tables so poisoned examples can't influence the AI
        import re as _re
        sql_upper = sql_text.upper().strip()
        if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
            return jsonify({"error": "Only SELECT queries can be saved as examples"}), 400
        _DANGEROUS = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                       "ATTACH", "DETACH", "PRAGMA", "EXPLAIN"]
        for word in _DANGEROUS:
            if _re.search(rf'\b{word}\b', sql_upper):
                return jsonify({"error": f"SQL contains disallowed keyword: {word}"}), 400
        from db import _BLOCKED_TABLES
        for blocked in _BLOCKED_TABLES:
            if _re.search(rf'\b{blocked}\b', sql_text, _re.IGNORECASE):
                return jsonify({"error": f"SQL references a restricted table"}), 400

        user = session.get("user", {}) if is_azure_enabled() else {}
        user_email = user.get("email", "anonymous")
        result = store_example(question, sql_text, user_email=user_email)
        return jsonify(result)

    # vote == "down" — just acknowledge for now (could log for analysis later)
    return jsonify({"stored": False, "acknowledged": True})


@main_bp.route("/api/history", methods=["DELETE"], endpoint="clear_history")
@api_login_required
def clear_history():
    """Clear conversation history for a session (scoped to the authenticated user)."""
    data = request.get_json() or {}
    session_id = data.get("session_id", "default")
    user = session.get("user", {}) if is_azure_enabled() else {}
    user_email = user.get("email", "anonymous")
    conn = _get_conv_db()
    try:
        conn.execute(
            "DELETE FROM conversations WHERE session_id=? AND user_email=?",
            (session_id, user_email)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@main_bp.route("/api/insights", endpoint="api_insights")
@api_login_required
def api_insights():
    """Return auto-detected trend insights plus a dict of active service flags."""
    force = request.args.get("refresh") == "1"
    insights = get_insights(force=force)

    # One chip per anomaly — take the first (most specific) chip only.
    # Each chip object carries a context string so the agent is pre-briefed
    # when the user clicks it, without re-running the detection query.
    # Group by type first, then interleave so we get a mix of anomaly types.
    by_type = {}
    for insight in insights:
        first = (insight.get("chips") or [None])[0]
        if first:
            context = (
                f"This question was triggered by a live anomaly detection. "
                f"Anomaly type: {insight['type'].replace('_', ' ')}. "
                f"Company: {insight['company']}. "
                f"Finding: {insight['detail']}. "
                f"Use this as a starting point — do not re-run the anomaly detection, "
                f"just answer the user's question with this context in mind."
            )
            by_type.setdefault(insight["type"], []).append({"text": first, "context": context})

    # Round-robin across types to ensure diversity
    chips = []
    type_lists = list(by_type.values())
    idx = 0
    while type_lists and len(chips) < 16:
        bucket = type_lists[idx % len(type_lists)]
        if bucket:
            chips.append(bucket.pop(0))
        if not bucket:
            type_lists.pop(idx % len(type_lists))
            if not type_lists:
                break
        else:
            idx += 1

    # Determine which cross-service integrations are actually built and have data.
    # A service is only "active" if its feature table has at least one row.
    services = {"cipp": False, "duo": False, "huntress": False,
                "cove": False, "automate": False}

    SERVICE_TABLES = {
        "cipp":     "cipp_tenants",
        "duo":      "duo_users",
        "huntress": "huntress_agents",
        "cove":     "cove_devices",
        "automate": "automate_clients",
    }

    conn = get_db()
    if conn:
        try:
            for svc, table in SERVICE_TABLES.items():
                try:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    services[svc] = count > 0
                except Exception:
                    services[svc] = False
        finally:
            conn.close()

    return jsonify({"insights": insights, "chips": chips[:16], "services": services})


@main_bp.route("/api/pin_trend", methods=["POST"], endpoint="api_pin_trend")
@api_login_required
def api_pin_trend():
    """Save a trend pin (can also be called directly from the UI)."""
    data = request.get_json() or {}
    title   = data.get("title", "").strip()
    summary = data.get("summary", "").strip()
    sql_text = data.get("sql", "").strip() or None

    if not title or not summary:
        return jsonify({"error": "title and summary are required"}), 400

    user = session.get("user", {}) if is_azure_enabled() else {}
    pin_id, err = add_pin(
        title=title,
        summary=summary,
        sql_text=sql_text,
        pinned_by_name=user.get("name", "Agent"),
        pinned_by_email=user.get("email", "agent"),
    )
    return jsonify({"ok": bool(pin_id), "pin_id": pin_id, "error": err})


@main_bp.route("/api/unpin_trend/<int:pin_id>", methods=["POST"], endpoint="api_unpin_trend")
@api_login_required
def api_unpin_trend(pin_id):
    """Dismiss a trend pin."""
    ok = dismiss_pin(pin_id)
    return jsonify({"ok": ok})


@main_bp.route("/trends", endpoint="trends")
@login_required
def trends():
    _track_page("trends")
    user = session.get("user", {}) if is_azure_enabled() else {}
    email = user.get("email", "")
    feat = check_all_features(email)
    insights  = get_insights()
    pins      = get_pins_with_results()
    weekly    = get_weekly_ticket_volume()
    return render_template(
        "trends.html",
        insights=insights,
        pins=pins,
        weekly=weekly,
        auth_enabled=is_azure_enabled(),
        duo_enabled=is_duo_enabled(),
        cipp_enabled=is_cipp_enabled(),
        huntress_enabled=is_huntress_enabled(),
        threatlocker_enabled=is_threatlocker_enabled(),
        user_name=user.get("name", ""),
        has_analytics=feat["admin"],
        has_timeline=feat["timeline"],
        has_admin=feat["admin"],
        app_version=_current_version(),
    )



@main_bp.route("/api/changelog", endpoint="api_changelog")
@api_login_required
def api_changelog():
    """Return changelog entries as JSON. Used by the welcome screen popup."""
    entries = _load_changelog()
    limit = request.args.get("limit", type=int)
    if limit:
        entries = entries[:limit]
    return jsonify(entries)


@main_bp.route("/changelog", endpoint="changelog")
@login_required
def changelog():
    _track_page("changelog")
    user = session.get("user", {}) if is_azure_enabled() else {}
    feat = check_all_features(user.get("email", ""))
    return render_template("changelog.html",
                           auth_enabled=is_azure_enabled(),
                           user_name=user.get("name", ""),
                           entries=_load_changelog(),
                           has_timeline=feat["timeline"],
                           has_analytics=feat["admin"],
                           has_admin=feat["admin"],
                           app_version=_current_version())



@main_bp.route("/api/admin/analytics", endpoint="api_admin_analytics")
@api_login_required
@feature_required("admin")
def api_admin_analytics():
    """Return analytics data as JSON for the admin Analytics tab."""
    conn = _get_conv_db()
    try:
        _ensure_usage_table(conn)
        total = conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]
        total_7d = conn.execute(
            "SELECT COUNT(*) FROM usage_log WHERE timestamp >= DATE('now', '-7 days')"
        ).fetchone()[0]
        total_errors = conn.execute(
            "SELECT COUNT(*) FROM usage_log WHERE errored = 1"
        ).fetchone()[0]
        error_rate = round((total_errors / total * 100), 1) if total > 0 else 0
        avg_response = conn.execute(
            "SELECT ROUND(AVG(response_ms) / 1000.0, 1) FROM usage_log WHERE errored = 0"
        ).fetchone()[0] or 0

        top_users = conn.execute("""
            SELECT user_name, user_email, COUNT(*) as queries,
                   ROUND(AVG(response_ms) / 1000.0, 1) as avg_secs,
                   SUM(CASE WHEN errored=1 THEN 1 ELSE 0 END) as errors
            FROM usage_log GROUP BY user_email ORDER BY queries DESC LIMIT 20
        """).fetchall()

        TZ_OFFSET = get_tz_offset_sql()

        daily = conn.execute(f"""
            SELECT DATE(timestamp, {TZ_OFFSET}) as day, COUNT(*) as queries
            FROM usage_log
            WHERE datetime(timestamp, {TZ_OFFSET}) >= DATE('now', {TZ_OFFSET}, '-30 days')
            GROUP BY day ORDER BY day
        """).fetchall()

        sources = conn.execute("""
            SELECT source, COUNT(*) as cnt FROM usage_log GROUP BY source
        """).fetchall()

        hourly = conn.execute(f"""
            SELECT CAST(strftime('%H', timestamp, {TZ_OFFSET}) AS INTEGER) as hour,
                   COUNT(*) as queries
            FROM usage_log GROUP BY hour ORDER BY hour
        """).fetchall()
        hourly_map = {r["hour"]: r["queries"] for r in hourly}
        hourly_full = [{"hour": h, "queries": hourly_map.get(h, 0)} for h in range(24)]

        recent = conn.execute(f"""
            SELECT user_name, query_text, source,
                   datetime(timestamp, {TZ_OFFSET}) as timestamp,
                   ROUND(response_ms / 1000.0, 1) as secs, errored
            FROM usage_log ORDER BY timestamp DESC LIMIT 30
        """).fetchall()

        def row_to_dict(r):
            return {k: r[k] for k in r.keys()}

        return jsonify({
            "ok": True,
            "total": total,
            "total_7d": total_7d,
            "total_errors": total_errors,
            "error_rate": error_rate,
            "avg_response": avg_response,
            "top_users": [row_to_dict(r) for r in top_users],
            "daily": [row_to_dict(r) for r in daily],
            "sources": {r["source"]: r["cnt"] for r in sources},
            "hourly": hourly_full,
            "recent": [row_to_dict(r) for r in recent],
        })
    finally:
        conn.close()


@main_bp.route("/analytics", endpoint="analytics")
@login_required
@feature_required("admin")
def analytics():
    _track_page("analytics")
    user = session.get("user", {}) if is_azure_enabled() else {}
    conn = _get_conv_db()
    try:
        _ensure_usage_table(conn)

        # Total queries
        total = conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]

        # Queries last 7 days
        total_7d = conn.execute(
            "SELECT COUNT(*) FROM usage_log WHERE timestamp >= DATE('now', '-7 days')"
        ).fetchone()[0]

        # Error count + rate
        total_errors = conn.execute(
            "SELECT COUNT(*) FROM usage_log WHERE errored = 1"
        ).fetchone()[0]
        error_rate = round((total_errors / total * 100), 1) if total > 0 else 0

        # Average response time
        avg_response = conn.execute(
            "SELECT ROUND(AVG(response_ms) / 1000.0, 1) FROM usage_log WHERE errored = 0"
        ).fetchone()[0] or 0

        # Top users
        top_users = conn.execute("""
            SELECT user_name, user_email, COUNT(*) as queries,
                   ROUND(AVG(response_ms) / 1000.0, 1) as avg_secs,
                   SUM(CASE WHEN errored=1 THEN 1 ELSE 0 END) as errors
            FROM usage_log
            GROUP BY user_email
            ORDER BY queries DESC
            LIMIT 20
        """).fetchall()

        # usage_log timestamps are UTC — apply offset for local time in all time queries
        TZ_OFFSET = get_tz_offset_sql()

        # Queries per day (last 30 days, local time)
        daily = conn.execute(f"""
            SELECT DATE(timestamp, {TZ_OFFSET}) as day, COUNT(*) as queries
            FROM usage_log
            WHERE datetime(timestamp, {TZ_OFFSET}) >= DATE('now', {TZ_OFFSET}, '-30 days')
            GROUP BY day
            ORDER BY day
        """).fetchall()

        # Typed vs suggested breakdown
        sources = conn.execute("""
            SELECT source, COUNT(*) as cnt
            FROM usage_log
            GROUP BY source
        """).fetchall()

        # Hourly activity distribution (all time, local time)
        hourly = conn.execute(f"""
            SELECT CAST(strftime('%H', timestamp, {TZ_OFFSET}) AS INTEGER) as hour,
                   COUNT(*) as queries
            FROM usage_log
            GROUP BY hour
            ORDER BY hour
        """).fetchall()
        # Fill in missing hours with 0
        hourly_map = {r["hour"]: r["queries"] for r in hourly}
        hourly_full = [{"hour": h, "queries": hourly_map.get(h, 0)} for h in range(24)]

        # Response time trend (daily avg, last 30 days, local time)
        response_trend = conn.execute(f"""
            SELECT DATE(timestamp, {TZ_OFFSET}) as day,
                   ROUND(AVG(response_ms) / 1000.0, 1) as avg_secs,
                   COUNT(*) as queries
            FROM usage_log
            WHERE errored = 0
              AND datetime(timestamp, {TZ_OFFSET}) >= DATE('now', {TZ_OFFSET}, '-30 days')
            GROUP BY day
            ORDER BY day
        """).fetchall()

        # Error rate trend (daily, last 30 days, local time)
        error_trend = conn.execute(f"""
            SELECT DATE(timestamp, {TZ_OFFSET}) as day,
                   COUNT(*) as total,
                   SUM(CASE WHEN errored=1 THEN 1 ELSE 0 END) as errors
            FROM usage_log
            WHERE datetime(timestamp, {TZ_OFFSET}) >= DATE('now', {TZ_OFFSET}, '-30 days')
            GROUP BY day
            ORDER BY day
        """).fetchall()

        # Most common queries (raw — kept as fallback)
        top_queries = conn.execute("""
            SELECT query_text, COUNT(*) as cnt
            FROM usage_log
            GROUP BY LOWER(TRIM(query_text))
            ORDER BY cnt DESC
            LIMIT 15
        """).fetchall()

        # AI-generated query themes (cached)
        query_themes = get_query_themes(max_age_hours=6)

        # Recent activity (last 50, timestamps as local time)
        recent = conn.execute(f"""
            SELECT user_name, query_text, source,
                   datetime(timestamp, {TZ_OFFSET}) as timestamp,
                   ROUND(response_ms / 1000.0, 1) as secs, errored
            FROM usage_log
            ORDER BY timestamp DESC
            LIMIT 50
        """).fetchall()

        # ── Page view analytics ──────────────────────────────────────────
        # page_views timestamps are already local time (written by log_page_view)
        from db import _ensure_page_views_table
        _ensure_page_views_table(conn)

        # Page popularity (all time)
        page_popularity = conn.execute("""
            SELECT page, COUNT(*) as views
            FROM page_views
            GROUP BY page
            ORDER BY views DESC
        """).fetchall()

        # Total page views
        total_page_views = conn.execute(
            "SELECT COUNT(*) FROM page_views"
        ).fetchone()[0]

        # Page views last 7 days
        page_views_7d = conn.execute(
            f"SELECT COUNT(*) FROM page_views WHERE timestamp >= DATE('now', {TZ_OFFSET}, '-7 days')"
        ).fetchone()[0]

        # Page views by user (top 10)
        page_views_by_user = conn.execute("""
            SELECT user_name, COUNT(*) as views,
                   COUNT(DISTINCT page) as pages_used
            FROM page_views
            GROUP BY user_email
            ORDER BY views DESC
            LIMIT 10
        """).fetchall()

        # Daily page views trend (last 30 days)
        daily_page_views = conn.execute(f"""
            SELECT DATE(timestamp) as day, COUNT(*) as views
            FROM page_views
            WHERE timestamp >= DATE('now', {TZ_OFFSET}, '-30 days')
            GROUP BY day
            ORDER BY day
        """).fetchall()

        def row_to_dict(r):
            return {k: r[k] for k in r.keys()}

        feat = check_all_features(user.get("email", ""))
        return render_template("analytics.html",
            total=total,
            total_7d=total_7d,
            total_errors=total_errors,
            error_rate=error_rate,
            avg_response=avg_response,
            top_users=[row_to_dict(r) for r in top_users],
            daily=[row_to_dict(r) for r in daily],
            sources={r["source"]: r["cnt"] for r in sources},
            hourly=hourly_full,
            response_trend=[row_to_dict(r) for r in response_trend],
            error_trend=[row_to_dict(r) for r in error_trend],
            top_queries=[row_to_dict(r) for r in top_queries],
            query_themes=query_themes,
            recent=[row_to_dict(r) for r in recent],
            page_popularity=[row_to_dict(r) for r in page_popularity],
            total_page_views=total_page_views,
            page_views_7d=page_views_7d,
            page_views_by_user=[row_to_dict(r) for r in page_views_by_user],
            daily_page_views=[row_to_dict(r) for r in daily_page_views],
            auth_enabled=is_azure_enabled(),
            user_name=user.get("name", ""),
            has_timeline=feat["timeline"],
            has_admin=feat["admin"],
            app_version=_current_version(),
        )
    finally:
        conn.close()


@main_bp.route("/api/analytics/themes", methods=["POST"], endpoint="api_analytics_themes")
@api_login_required
@feature_required("admin")
def api_analytics_themes():
    """Generate AI-powered query theme consolidation.

    Reads all queries from usage_log, sends them to Claude to cluster
    into themes, caches the result for 6 hours.
    """
    # Check for fresh cache first
    cached = get_query_themes(max_age_hours=6)
    if cached and not request.args.get("force"):
        return jsonify({"ok": True, "themes": cached, "source": "cache"})

    conn = _get_conv_db()
    try:
        _ensure_usage_table(conn)
        rows = conn.execute("""
            SELECT query_text, COUNT(*) as cnt
            FROM usage_log
            GROUP BY LOWER(TRIM(query_text))
            ORDER BY cnt DESC
            LIMIT 200
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify({"ok": True, "themes": [], "source": "empty"})

    # Build the query list for Claude
    query_list = "\n".join(f"- ({r['cnt']}x) {r['query_text']}" for r in rows)

    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set"}), 500

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=get_setting("claude_model", "claude-sonnet-4-6"),
            max_tokens=1024,
            system=(
                "You are an analytics assistant for an MSP's internal chat tool. "
                "Given a list of user queries with their frequency counts, group them "
                "into 8-15 meaningful themes. Each theme should have a short descriptive name "
                "(2-5 words) and the total count of queries in that theme.\n\n"
                "Return ONLY valid JSON — an array of objects with keys: "
                "\"theme_name\", \"query_count\", \"example_queries\" (a comma-separated string "
                "of 2-3 representative query snippets, max 40 chars each).\n\n"
                "Sort by query_count descending. Combine similar themes. "
                "Use clear MSP-relevant names like \"Ticket Status Inquiries\", "
                "\"Time Entry Analysis\", \"Agreement & Contract Reviews\", "
                "\"Client Health Checks\", \"License & M365 Queries\", etc."
            ),
            messages=[{"role": "user", "content": f"Here are the user queries to categorise:\n\n{query_list}"}],
        )
        raw = response.content[0].text.strip()

        # Parse JSON from response (handle markdown fences)
        import re as _re
        if raw.startswith("```"):
            raw = _re.sub(r"^```(?:json)?\s*", "", raw)
            raw = _re.sub(r"\s*```$", "", raw)

        themes = json.loads(raw)
        if not isinstance(themes, list):
            return jsonify({"ok": False, "error": "Unexpected AI response format"}), 500

        # Save to cache
        save_query_themes(themes)
        return jsonify({"ok": True, "themes": themes, "source": "generated"})

    except json.JSONDecodeError as e:
        log.warning("Theme generation JSON parse error: %s — raw: %s", e, raw[:200])
        return jsonify({"ok": False, "error": f"Failed to parse AI response: {e}"}), 500
    except Exception as e:
        log.warning("Theme generation error: %s", e)
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500


# ── Admin: Permissions ────────────────────────────────────────────────────────

# The list of features that can be toggled in the permissions UI.
GATED_FEATURES = ["timeline", "admin"]


@main_bp.route("/admin", endpoint="admin_page")
@main_bp.route("/admin/permissions", endpoint="admin_permissions")
@login_required
@feature_required("admin")
def admin_permissions_page():
    _track_page("permissions")
    user = session.get("user", {}) if is_azure_enabled() else {}
    feat = check_all_features(user.get("email", ""))
    return render_template("admin_permissions.html",
                           auth_enabled=is_azure_enabled(),
                           user_name=user.get("name", ""),
                           has_timeline=feat["timeline"],
                           has_analytics=feat["admin"],
                           has_admin=True,
                           cipp_enabled=is_cipp_enabled(),
                           duo_enabled=is_duo_enabled(),
                           huntress_enabled=is_huntress_enabled(),
                           threatlocker_enabled=is_threatlocker_enabled(),
                           app_version=_current_version())


@main_bp.route("/api/admin/permissions", endpoint="api_admin_permissions")
@api_login_required
@feature_required("admin")
def api_admin_permissions():
    """Return all known users and their feature access.

    Query params:
        show_hidden=1  — include blacklisted users (hidden by default)
    """
    # Refresh known_users from CW members + usage_log each time an admin
    # views permissions.  This is idempotent (INSERT OR IGNORE) and ensures
    # the list stays current even if the initial sync hadn't finished when
    # the app first booted.
    from db import seed_known_users
    seed_known_users()

    conn = get_db()
    if not conn:
        return jsonify({"users": [], "features": GATED_FEATURES})

    show_hidden = request.args.get("show_hidden", "0") == "1"

    try:
        if show_hidden:
            users = conn.execute(
                "SELECT email, name, first_seen, last_seen, activated, hidden "
                "FROM known_users ORDER BY name"
            ).fetchall()
        else:
            users = conn.execute(
                "SELECT email, name, first_seen, last_seen, activated, hidden "
                "FROM known_users WHERE hidden = 0 ORDER BY name"
            ).fetchall()

        grants = conn.execute(
            "SELECT feature, LOWER(user_email) as user_email FROM feature_access"
        ).fetchall()

        # Build a lookup: email → set of features
        access_map = {}
        for g in grants:
            access_map.setdefault(g["user_email"], set()).add(g["feature"])

        result = []
        for u in users:
            email = u["email"].lower()
            result.append({
                "email": email,
                "name": u["name"],
                "first_seen": u["first_seen"],
                "last_seen": u["last_seen"],
                "activated": bool(u["activated"]),
                "hidden": bool(u["hidden"]),
                "features": {f: f in access_map.get(email, set()) for f in GATED_FEATURES},
            })

        return jsonify({"users": result, "features": GATED_FEATURES})
    except Exception as e:
        log.warning("api_admin_permissions error: %s", e)
        return jsonify({"users": [], "features": GATED_FEATURES, "error": "An internal error occurred"})
    finally:
        conn.close()


@main_bp.route("/api/admin/permissions", methods=["POST"],
               endpoint="api_admin_permissions_update")
@api_login_required
@feature_required("admin")
def api_admin_permissions_update():
    """Toggle a feature for a user. Body: {email, feature, enabled}"""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    feature = (data.get("feature") or "").strip()
    enabled = data.get("enabled", False)

    if not email or feature not in GATED_FEATURES:
        return jsonify({"ok": False, "error": "Invalid email or feature"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500

    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if enabled:
            conn.execute(
                "INSERT OR IGNORE INTO feature_access (feature, user_email, granted_at) VALUES (?, ?, ?)",
                (feature, email, now)
            )
        else:
            # Prevent revoking the last admin — would lock everyone out
            if feature == "admin":
                admin_count = conn.execute(
                    "SELECT COUNT(*) FROM feature_access WHERE feature = 'admin'"
                ).fetchone()[0]
                if admin_count <= 1:
                    return jsonify({"ok": False, "error": "Cannot remove the last admin. At least one admin must remain."}), 400
            conn.execute(
                "DELETE FROM feature_access WHERE feature = ? AND LOWER(user_email) = ?",
                (feature, email)
            )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("api_admin_permissions_update error: %s", e)
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500
    finally:
        conn.close()


@main_bp.route("/api/admin/permissions/hide", methods=["POST"],
               endpoint="api_admin_hide_user")
@api_login_required
@feature_required("admin")
def api_admin_hide_user():
    """Hide or unhide a user from the permissions list. Body: {email, hidden}"""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    hidden = 1 if data.get("hidden", True) else 0

    if not email:
        return jsonify({"ok": False, "error": "Email required"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500

    try:
        conn.execute(
            "UPDATE known_users SET hidden = ? WHERE LOWER(email) = ?",
            (hidden, email)
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("api_admin_hide_user error: %s", e)
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500
    finally:
        conn.close()


# ── Admin settings ────────────────────────────────────────────────────────────

# ── Sync restart signals ────────────────────────────────────────────────────
# Maps CONFIGURABLE_SETTINGS group names to signal file names.
# When an integration credential is saved via Admin → Settings, we touch the
# corresponding signal file so the sync container (which may be waiting for
# credentials) retries immediately instead of sleeping.
_GROUP_TO_SIGNAL = {
    "CIPP / Microsoft 365": ".restart_cipp_sync",
    "Duo Security":         ".restart_duo_sync",
    "Huntress EDR":         ".restart_huntress_sync",
    "ThreatLocker":         ".restart_threatlocker_sync",
}


def _signal_sync_restart(setting_key):
    """Touch the signal file for the integration that owns *setting_key*."""
    from db import CONFIGURABLE_SETTINGS
    meta = CONFIGURABLE_SETTINGS.get(setting_key, {})
    group = meta.get("group")
    signal_name = _GROUP_TO_SIGNAL.get(group)
    if not signal_name:
        return
    signal_path = os.path.join(os.path.dirname(DB_PATH), signal_name)
    try:
        open(signal_path, "a").close()  # touch
        log.info("Touched %s (credential %s updated)", signal_name, setting_key)
    except OSError as e:
        log.warning("Could not touch %s: %s", signal_path, e)


@main_bp.route("/api/admin/settings", endpoint="api_admin_settings")
@api_login_required
@feature_required("admin")
def api_admin_settings():
    """Return all configurable settings for the admin UI."""
    from settings import get_all_settings, ENV_OVERRIDE
    result = {
        "settings": get_all_settings(),
        "env_override": ENV_OVERRIDE,
    }
    if ENV_OVERRIDE:
        result["env_override_docs_url"] = _resolve_docs_url(
            "docs/authentication.md#env_override-mode"
        )
    return jsonify(result)


def _validate_azure_secret(new_secret):
    """Test an Azure client secret via MSAL before allowing it to be saved.

    Attempts a client-credentials token acquisition using the new secret
    combined with the existing client_id and tenant_id from the DB.

    Returns (ok: bool, error: str | None).
    Skips validation (returns True) if client_id or tenant_id aren't
    configured yet (first-time setup).
    """
    from settings import get_setting
    client_id = (get_setting("azure_client_id") or "").strip()
    tenant_id = (get_setting("azure_tenant_id") or "").strip()

    if not client_id or not tenant_id:
        # Can't validate without the other credentials — allow the save
        # (first-time setup or partial configuration)
        return True, None

    try:
        import msal
        app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=new_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" in result:
            return True, None

        error_code = result.get("error", "")
        error_desc = result.get("error_description", "")

        # Provide a clear, actionable error message
        if "AADSTS7000215" in error_desc or "invalid_client" in error_code:
            return False, "This client secret is not valid. Please check the value and try again."
        if "AADSTS700016" in error_desc:
            return False, "Application not found in this tenant. Check the Client ID and Tenant ID."
        if "AADSTS90002" in error_desc:
            return False, "Tenant not found. Check the Tenant ID."

        # Generic auth failure
        return False, f"Azure authentication failed: {error_code}. Please verify the secret value."

    except Exception as e:
        # Network errors, DNS failures, etc. — don't block the save
        log.warning("Azure secret validation could not reach Azure AD: %s", e)
        return True, None


@main_bp.route("/api/admin/settings", methods=["POST"],
               endpoint="api_admin_settings_update")
@api_login_required
@feature_required("admin")
def api_admin_settings_update():
    """Update a setting. Body: {key, value}"""
    from settings import set_setting, ENV_OVERRIDE, SECRET_MASK
    if ENV_OVERRIDE:
        return jsonify({"ok": False, "error": "Settings are managed via environment variables (ENV_OVERRIDE=true)."}), 403

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    value = data.get("value", "")

    from db import CONFIGURABLE_SETTINGS, validate_setting
    if key not in CONFIGURABLE_SETTINGS:
        return jsonify({"ok": False, "error": f"Unknown setting: {key}"}), 400

    # Server-side validation
    valid, error = validate_setting(key, str(value))
    if not valid:
        return jsonify({"ok": False, "error": error}), 400

    user = session.get("user", {}) if is_azure_enabled() else {}
    updated_by = user.get("email", "admin")

    # Warn (but don't block) when credential-bearing URLs use HTTP
    _CREDENTIAL_URL_KEYS = {"cipp_api_url", "cipp_token_url"}
    meta = CONFIGURABLE_SETTINGS[key]
    if key in _CREDENTIAL_URL_KEYS or (meta.get("group") and meta.get("validate", {}).get("type") == "hostname"):
        v = str(value).strip().lower()
        if v and v.startswith("http://") and not v.startswith("http://localhost") and not v.startswith("http://127.0.0.1"):
            from db import log_admin_event
            log_admin_event("settings", "warning",
                            f"Insecure URL: {key} uses HTTP",
                            detail=f"{key} is configured with an HTTP URL. Credentials may be sent in plaintext. Use HTTPS unless this is a local development instance.",
                            user_email=updated_by)

    # Live-test Azure client secret before saving — catches typos immediately
    # rather than discovering a bad secret when the old one expires.
    if key == "azure_client_secret" and value != SECRET_MASK:
        ok, err = _validate_azure_secret(value)
        if not ok:
            from db import log_admin_event
            log_admin_event("auth", "warning", "Azure secret validation failed on save",
                            detail=err, user_email=updated_by)
            return jsonify({"ok": False, "error": err}), 400

    if set_setting(key, str(value), updated_by):
        # Signal the sync container to retry if an integration credential changed
        _signal_sync_restart(key)
        from db import log_admin_event
        display_val = "\u2022" * 8 if CONFIGURABLE_SETTINGS[key].get("type") == "secret" else str(value)[:100]
        log_admin_event("settings", "info", f"Setting updated: {key}",
                        detail=display_val, user_email=updated_by)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Failed to save setting."}), 500


@main_bp.route("/api/admin/test-connection", methods=["POST"],
               endpoint="api_admin_test_connection")
@api_login_required
@feature_required("admin")
def api_admin_test_connection():
    """Test connectivity to an integration service.

    Body: {"service": "cipp"|"duo"|"huntress"|"threatlocker"}
    Returns: {"ok": true/false, "message": "...", "details": "..."}
    """
    data = request.get_json(silent=True) or {}
    service = (data.get("service") or "").lower().strip()

    from settings import get_setting

    try:
        if service == "cipp":
            api_url = get_setting("cipp_api_url")
            client_id = get_setting("cipp_client_id")
            client_secret = get_setting("cipp_client_secret")
            tenant_id = get_setting("cipp_tenant_id")
            token_url = get_setting("cipp_token_url")

            if not all([api_url, client_id, client_secret]):
                return jsonify({"ok": False, "message": "Missing required CIPP credentials."})

            if not token_url:
                if not tenant_id:
                    return jsonify({"ok": False, "message": "Either Token URL or Tenant ID is required."})
                token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

            scope = f"{client_id}/.default"

            import requests as req
            # Step 1: Get OAuth token
            token_resp = req.post(token_url, data={
                "client_id": client_id, "client_secret": client_secret,
                "scope": scope, "grant_type": "client_credentials",
            }, timeout=15)
            token_resp.raise_for_status()
            token = token_resp.json().get("access_token")
            if not token:
                return jsonify({"ok": False, "message": "OAuth token request succeeded but no access_token returned."})

            # Step 2: Call ListTenants
            tenants_resp = req.get(
                f"{api_url.rstrip('/')}/ListTenants",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=30,
            )
            tenants_resp.raise_for_status()
            tenants = tenants_resp.json()
            count = len(tenants) if isinstance(tenants, list) else 0
            return jsonify({"ok": True, "message": f"Connected successfully. Found {count} tenant(s)."})

        elif service == "duo":
            ikey = get_setting("duo_ikey")
            skey = get_setting("duo_skey")
            host = get_setting("duo_host")

            if not all([ikey, skey, host]):
                return jsonify({"ok": False, "message": "Missing required Duo credentials."})

            # Use the sync script's HMAC-signed API call
            from sync_duo_data import duo_api
            resp = duo_api("POST", host, "/accounts/v1/account/list", {}, ikey, skey, retries=1)
            if resp.get("stat") == "OK":
                accounts = resp.get("response", [])
                return jsonify({"ok": True, "message": f"Connected successfully. Found {len(accounts)} account(s)."})
            else:
                return jsonify({"ok": False, "message": f"API returned: {resp.get('message', 'Unknown error')}"})

        elif service == "huntress":
            api_key = get_setting("huntress_api_key")
            api_secret = get_setting("huntress_api_secret")

            if not all([api_key, api_secret]):
                return jsonify({"ok": False, "message": "Missing required Huntress credentials."})

            from sync_huntress_data import _build_auth_header, huntress_api
            auth = _build_auth_header(api_key, api_secret)
            resp = huntress_api("/organizations", auth, params={"page": 1, "per_page": 1}, retries=1)
            total = resp.get("pagination", {}).get("total_count", 0) if isinstance(resp, dict) else 0
            return jsonify({"ok": True, "message": f"Connected successfully. Found {total} organization(s)."})

        elif service == "threatlocker":
            api_key = get_setting("threatlocker_api_key")
            if not api_key:
                return jsonify({"ok": False, "message": "Missing ThreatLocker API key."})

            from sync_threatlocker_data import tl_api, fetch_organizations
            orgs = fetch_organizations(api_key)
            if orgs is not None:
                return jsonify({"ok": True, "message": f"Connected successfully. Found {len(orgs)} organization(s)."})
            # Fallback: try computers endpoint if orgs endpoint unavailable
            result = tl_api(
                "/Computer/ComputerGetByAllParameters",
                api_key,
                {"pageNumber": 1, "pageSize": 1, "childOrganizations": True,
                 "showDeleted": False, "orderBy": "computername", "isAscending": True},
                retries=1, timeout=30,
            )
            total = result[0].get("totalRows", 0) if isinstance(result, list) and result else 0
            return jsonify({"ok": True, "message": f"Connected successfully. {total} computer(s) reported."})

        else:
            return jsonify({"ok": False, "message": f"Unknown service: {service}"}), 400

    except Exception as e:
        log.warning("test-connection %s failed: %s", service, e)
        err_msg = str(e)
        # Trim overly verbose request exception messages
        if len(err_msg) > 200:
            err_msg = err_msg[:200] + "..."
        from db import log_admin_event
        log_admin_event("integration", "error", f"Connection test failed: {service}",
                        detail=err_msg)
        return jsonify({"ok": False, "message": f"Connection failed: {err_msg}"})



@main_bp.route("/api/admin/secret-expiry", endpoint="api_admin_secret_expiry")
@api_login_required
@feature_required("admin")
def api_admin_secret_expiry():
    """Return Azure client-secret expiry status.

    Uses Microsoft Graph API to auto-detect the expiry date for the
    Azure auth app registration.  Falls back to the manual date in
    Admin settings if Graph is unavailable.

    Response: {
      "secrets": [...],   // tracked secrets with expiry info
      "warnings": [...],  // only secrets needing attention (≤30 days or expired)
    }
    """
    from secret_expiry import check_secret_expiry
    force = request.args.get("refresh", "").lower() in ("1", "true")
    secrets = check_secret_expiry(force_refresh=force)
    warnings = [s for s in secrets if s["status"] != "ok"]

    # Include docs URLs so the UI banner can link to relevant documentation
    has_auth_failure = any(s["status"] == "auth_failed" for s in warnings)
    if has_auth_failure:
        docs_path = "docs/authentication.md#troubleshooting-authentication"
    else:
        docs_path = "docs/authentication.md#rotating-a-client-secret"
    docs_url = _resolve_docs_url(docs_path)

    return jsonify({
        "secrets": secrets,
        "warnings": warnings,
        "docs_url": docs_url,
    })



@main_bp.route("/api/admin/events", endpoint="api_admin_events")
@api_login_required
@feature_required("admin")
def api_admin_events():
    """Return admin diagnostic events with optional filters.

    Query params:
        category — filter by category (ai, auth, sync, settings, integration)
        level    — filter by level (error, warning, info)
        limit    — max events to return (default 200)
        offset   — pagination offset (default 0)
    """
    from db import get_admin_events
    category = request.args.get("category") or None
    level = request.args.get("level") or None
    limit = min(request.args.get("limit", 200, type=int), 500)
    offset = request.args.get("offset", 0, type=int)

    events, total = get_admin_events(
        category=category, level=level, limit=limit, offset=offset,
    )

    # Also pull recent sync errors from sync_state for a unified view
    sync_errors = []
    try:
        conn = get_db_readonly()
        if conn:
            rows = conn.execute("""
                SELECT source, entity, last_status, last_error,
                       last_completed_at as timestamp
                FROM sync_state
                WHERE last_status = 'error'
                ORDER BY last_completed_at DESC
                LIMIT 20
            """).fetchall()
            for r in rows:
                sync_errors.append({
                    "source": r["source"],
                    "entity": r["entity"],
                    "error": r["last_error"],
                    "timestamp": r["timestamp"],
                })
            conn.close()
    except Exception:
        pass  # sync_state may not exist yet

    return jsonify({
        "events": events,
        "total": total,
        "sync_errors": sync_errors,
    })


@main_bp.route("/api/admin/restart", methods=["POST"],
               endpoint="api_admin_restart")
@api_login_required
@feature_required("admin")
def api_admin_restart():
    """Restart the application -- soft (reload workers) or hard (kill process).

    mode=soft (default): sends SIGHUP to Gunicorn master, gracefully reloading
    all workers. Picks up settings cache changes without downtime.

    mode=hard: sends SIGTERM to Gunicorn master, causing the process to exit.
    Docker's restart policy brings the container back up (~5s downtime).
    Use when you need a full clean restart.
    """
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "soft")
    if mode not in ("soft", "hard"):
        return jsonify({"ok": False, "error": f"Invalid mode: {mode}. Must be 'soft' or 'hard'."}), 400

    user = session.get("user", {}) if is_azure_enabled() else {}
    requester = user.get("email", "admin")
    log.info("Application %s restart requested by %s", mode, requester)

    master_pid = os.getppid()

    if mode == "hard":
        try:
            os.kill(master_pid, signal.SIGTERM)
        except OSError as e:
            log.error("Failed to send SIGTERM to Gunicorn master (pid %s): %s", master_pid, e)
            return jsonify({"ok": False, "error": "Failed to signal hard restart."}), 500
        return jsonify({"ok": True, "mode": "hard",
                        "message": "Container restarting. Page will reload in a few seconds."})
    else:
        try:
            os.kill(master_pid, signal.SIGHUP)
        except OSError as e:
            log.error("Failed to send SIGHUP to Gunicorn master (pid %s): %s", master_pid, e)
            return jsonify({"ok": False, "error": "Failed to signal reload."}), 500
        return jsonify({"ok": True, "mode": "soft",
                        "message": "Workers reloading. Changes take effect momentarily."})


# ── Mappings ─────────────────────────────────────────────────────────────────

def _fuzzy_score(a, b):
    """Simple word-overlap fuzzy match score between two strings (0..1)."""
    if not a or not b:
        return 0.0
    a_words = set(a.lower().split())
    b_words = set(b.lower().split())
    # Remove very short noise words
    a_words = {w for w in a_words if len(w) > 1}
    b_words = {w for w in b_words if len(w) > 1}
    if not a_words or not b_words:
        return 0.0
    overlap = len(a_words & b_words)
    # Also check substring containment for single-word names
    if overlap == 0:
        a_lower, b_lower = a.lower().strip(), b.lower().strip()
        if a_lower in b_lower or b_lower in a_lower:
            return 0.6
    return (2.0 * overlap) / (len(a_words) + len(b_words))


@main_bp.route("/api/admin/mappings", endpoint="api_admin_mappings")
@api_login_required
@feature_required("admin")
def api_admin_mappings():
    """Return all CW companies with their current mappings and available targets."""
    conn = get_db_readonly()
    if not conn:
        return jsonify({"companies": [], "targets": {}})

    try:
        # All CW companies
        companies = [dict(r) for r in conn.execute(
            "SELECT id, name, status_name FROM companies ORDER BY name"
        ).fetchall()]

        # Current mappings
        mappings = {}
        for r in conn.execute("SELECT * FROM customer_map").fetchall():
            mappings[str(r["cw_manage_company_id"])] = dict(r)

        # Available targets per enabled service
        targets = {}

        if is_cipp_enabled():
            try:
                tenants = [dict(r) for r in conn.execute(
                    "SELECT tenant_id, display_name, default_domain FROM cipp_tenants ORDER BY display_name"
                ).fetchall()]
                targets["cipp"] = {"label": "CIPP / M365 Tenant", "column": "cipp_tenant_id",
                                   "items": [{"id": t["default_domain"] or t["tenant_id"],
                                              "name": t["display_name"] or t["default_domain"]}
                                             for t in tenants]}
            except Exception:
                pass

        if is_duo_enabled():
            try:
                # Join with duo_accounts for human-readable names when available
                duo_accts = [dict(r) for r in conn.execute("""
                    SELECT u.duo_account_id, COUNT(*) as user_count,
                           da.name as account_name
                    FROM duo_users u
                    LEFT JOIN duo_accounts da ON da.account_id = u.duo_account_id
                    GROUP BY u.duo_account_id
                    ORDER BY COALESCE(da.name, u.duo_account_id)
                """).fetchall()]
                targets["duo"] = {"label": "Duo Account", "column": "duo_account_id",
                                  "items": [{"id": a["duo_account_id"],
                                             "name": (f"{a['account_name']} ({a['user_count']} users)"
                                                      if a.get("account_name")
                                                      else f"{a['duo_account_id']} ({a['user_count']} users)")}
                                            for a in duo_accts]}
            except Exception:
                pass

        # Huntress: show if configured (with synced org list) or if legacy mappings exist
        if is_huntress_enabled():
            try:
                h_orgs = [dict(r) for r in conn.execute(
                    "SELECT id, name, agent_count FROM huntress_organizations ORDER BY name"
                ).fetchall()]
                targets["huntress"] = {
                    "label": "Huntress Organization",
                    "column": "huntress_org_id",
                    "items": [{"id": str(o["id"]),
                               "name": f"{o['name']} ({o['agent_count']} agents)"
                                       if o.get("agent_count")
                                       else o["name"] or str(o["id"])}
                              for o in h_orgs],
                }
            except Exception:
                # Table might not exist yet (syncer hasn't run) — fall back to freetext
                targets["huntress"] = {"label": "Huntress Org ID", "column": "huntress_org_id",
                                       "freetext": True, "items": []}
        else:
            try:
                has_huntress = conn.execute(
                    "SELECT 1 FROM customer_map WHERE huntress_org_id IS NOT NULL AND huntress_org_id != '' LIMIT 1"
                ).fetchone()
                if has_huntress:
                    # Legacy mappings exist — show freetext so they can be edited
                    targets["huntress"] = {"label": "Huntress Org ID", "column": "huntress_org_id",
                                           "freetext": True, "items": []}
            except Exception as e:
                log.warning("Huntress mapping check failed: %s", e)

        # ThreatLocker: show if configured (with synced org list) or freetext fallback
        if is_threatlocker_enabled():
            try:
                tl_orgs = [dict(r) for r in conn.execute(
                    "SELECT id, name, computer_count FROM threatlocker_organizations ORDER BY name"
                ).fetchall()]
                targets["threatlocker"] = {
                    "label": "ThreatLocker Organization",
                    "column": "threatlocker_org_id",
                    "items": [{"id": str(o["id"]),
                               "name": f"{o['name']} ({o['computer_count']} computers)"
                                       if o.get("computer_count")
                                       else o["name"] or str(o["id"])}
                              for o in tl_orgs],
                }
            except Exception:
                # Table might not exist yet — fall back to freetext
                targets["threatlocker"] = {"label": "ThreatLocker Org ID", "column": "threatlocker_org_id",
                                           "freetext": True, "items": []}
        else:
            try:
                has_tl = conn.execute(
                    "SELECT 1 FROM customer_map WHERE threatlocker_org_id IS NOT NULL AND threatlocker_org_id != '' LIMIT 1"
                ).fetchone()
                if has_tl:
                    targets["threatlocker"] = {"label": "ThreatLocker Org ID", "column": "threatlocker_org_id",
                                               "freetext": True, "items": []}
            except Exception as e:
                log.warning("ThreatLocker mapping check failed: %s", e)

        # Merge current mapping into each company
        for c in companies:
            c["mapping"] = mappings.get(str(c["id"]), {})

        return jsonify({"companies": companies, "targets": targets})
    except Exception as e:
        log.warning("api_admin_mappings error: %s", e)
        return jsonify({"companies": [], "targets": {}, "error": "An internal error occurred"})
    finally:
        conn.close()


@main_bp.route("/api/admin/mappings/suggest", endpoint="api_admin_mappings_suggest")
@api_login_required
@feature_required("admin")
def api_admin_mappings_suggest():
    """Return fuzzy match suggestions for unmapped CW companies."""
    conn = get_db_readonly()
    if not conn:
        return jsonify({"suggestions": []})

    try:
        # Get unmapped companies (no customer_map row, or row with all NULLs)
        companies = [dict(r) for r in conn.execute("""
            SELECT c.id, c.name
            FROM companies c
            LEFT JOIN customer_map cm ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
            WHERE c.status_name = 'Active'
              AND (cm.cw_manage_company_id IS NULL
                   OR (COALESCE(cm.cipp_tenant_id, '') = '' AND COALESCE(cm.duo_account_id, '') = ''
                       AND COALESCE(cm.huntress_org_id, '') = '' AND COALESCE(cm.threatlocker_org_id, '') = ''))
            ORDER BY c.name
        """).fetchall()]

        # Build target name lists for fuzzy matching
        cipp_tenants = []
        duo_accounts = []

        if is_cipp_enabled():
            try:
                cipp_tenants = [dict(r) for r in conn.execute(
                    "SELECT tenant_id, display_name, default_domain FROM cipp_tenants"
                ).fetchall()]
            except Exception:
                pass

        if is_duo_enabled():
            try:
                duo_accounts = [dict(r) for r in conn.execute("""
                    SELECT u.duo_account_id,
                           da.name as account_name
                    FROM (SELECT DISTINCT duo_account_id FROM duo_users) u
                    LEFT JOIN duo_accounts da ON da.account_id = u.duo_account_id
                """).fetchall()]
            except Exception:
                pass

        # Huntress orgs for fuzzy matching
        huntress_orgs = []
        if is_huntress_enabled():
            try:
                huntress_orgs = [dict(r) for r in conn.execute(
                    "SELECT id, name FROM huntress_organizations"
                ).fetchall()]
            except Exception:
                pass

        # ThreatLocker orgs for fuzzy matching
        threatlocker_orgs = []
        if is_threatlocker_enabled():
            try:
                threatlocker_orgs = [dict(r) for r in conn.execute(
                    "SELECT id, name FROM threatlocker_organizations"
                ).fetchall()]
            except Exception:
                pass

        # Already-mapped target IDs (so we don't suggest them again)
        mapped_cipp = set()
        mapped_duo = set()
        mapped_huntress = set()
        mapped_threatlocker = set()
        for r in conn.execute("SELECT cipp_tenant_id, duo_account_id, huntress_org_id, threatlocker_org_id FROM customer_map").fetchall():
            if r["cipp_tenant_id"]:
                mapped_cipp.add(r["cipp_tenant_id"])
            if r["duo_account_id"]:
                mapped_duo.add(r["duo_account_id"])
            if r["huntress_org_id"]:
                mapped_huntress.add(str(r["huntress_org_id"]))
            if r["threatlocker_org_id"]:
                mapped_threatlocker.add(str(r["threatlocker_org_id"]))

        suggestions = []
        for co in companies:
            matches = {}

            # CIPP fuzzy match
            if is_cipp_enabled() and cipp_tenants:
                best_score, best_tenant = 0, None
                for t in cipp_tenants:
                    tid = t["default_domain"] or t["tenant_id"]
                    if tid in mapped_cipp:
                        continue
                    name = t["display_name"] or t["default_domain"] or ""
                    score = _fuzzy_score(co["name"], name)
                    if score > best_score:
                        best_score = score
                        best_tenant = {"id": tid, "name": name, "score": round(score, 2)}
                if best_tenant and best_score >= 0.65:
                    matches["cipp"] = best_tenant

            # Duo fuzzy match — use account names when available
            if is_duo_enabled() and duo_accounts:
                unmapped_duo = [a for a in duo_accounts if a["duo_account_id"] not in mapped_duo]
                best_score, best_duo = 0, None
                for a in unmapped_duo:
                    name = a.get("account_name") or ""
                    if name:
                        score = _fuzzy_score(co["name"], name)
                        if score > best_score:
                            best_score = score
                            best_duo = {"id": a["duo_account_id"],
                                        "name": name or a["duo_account_id"],
                                        "score": round(score, 2)}
                if best_duo and best_score >= 0.65:
                    matches["duo"] = best_duo

            # Huntress fuzzy match — match CW company names to Huntress org names
            if is_huntress_enabled() and huntress_orgs:
                best_score, best_h = 0, None
                for ho in huntress_orgs:
                    hid = str(ho["id"])
                    if hid in mapped_huntress:
                        continue
                    hname = ho.get("name") or ""
                    if hname:
                        score = _fuzzy_score(co["name"], hname)
                        if score > best_score:
                            best_score = score
                            best_h = {"id": hid, "name": hname, "score": round(score, 2)}
                if best_h and best_score >= 0.65:
                    matches["huntress"] = best_h

            # ThreatLocker fuzzy match
            if is_threatlocker_enabled() and threatlocker_orgs:
                best_score, best_tl = 0, None
                for tlo in threatlocker_orgs:
                    tlid = str(tlo["id"])
                    if tlid in mapped_threatlocker:
                        continue
                    tlname = tlo.get("name") or ""
                    if tlname:
                        score = _fuzzy_score(co["name"], tlname)
                        if score > best_score:
                            best_score = score
                            best_tl = {"id": tlid, "name": tlname, "score": round(score, 2)}
                if best_tl and best_score >= 0.65:
                    matches["threatlocker"] = best_tl

            if matches:
                suggestions.append({"company_id": co["id"], "company_name": co["name"],
                                    "matches": matches})

        return jsonify({"suggestions": suggestions})
    except Exception as e:
        log.warning("api_admin_mappings_suggest error: %s", e)
        return jsonify({"suggestions": [], "error": "An internal error occurred"})
    finally:
        conn.close()



_ALLOWED_MAPPING_COLUMNS = frozenset({
    "cipp_tenant_id", "duo_account_id", "huntress_org_id", "threatlocker_org_id",
})


def _mapping_write_with_retry(rows, max_attempts=4):
    """Write mapping rows with retry on database lock.

    rows: list of (company_id, column_name, target_id, now_str)
    Returns (True, None) on success or (False, error_message) on failure.

    Uses a short busy_timeout (2s) so we fail fast per attempt, then sleep
    briefly to let sync containers finish their batch commit before retrying.
    """
    import time as _time
    import random as _random
    # Validate column names before touching the database
    for _, column, _, _ in rows:
        if column not in _ALLOWED_MAPPING_COLUMNS:
            log.warning("Mapping write rejected: invalid column %r", column)
            return False, "An internal error occurred"
    if max_attempts <= 0:
        return False, "Database busy — please try again shortly."
    for attempt in range(max_attempts):
        conn = get_db()
        if not conn:
            return False, "Database unavailable"
        try:
            # Use a 5s busy_timeout — long enough to wait for one batch commit
            # cycle (sync containers commit every 50 rows) but short enough to
            # retry several times within a reasonable request window.
            conn.execute("PRAGMA busy_timeout=5000")
            for company_id, column, target_id, now in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO customer_map (cw_manage_company_id, updated_at) VALUES (?, ?)",
                    (company_id, now),
                )
                conn.execute(
                    f"UPDATE customer_map SET {column} = ?, updated_at = ? WHERE cw_manage_company_id = ?",
                    (target_id, now, company_id),
                )
            conn.commit()
            return True, None
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_attempts - 1:
                log.info("Mapping write locked (attempt %d/%d), retrying...", attempt + 1, max_attempts)
                # Jittered backoff — avoids repeatedly colliding with the sync batch cycle
                _time.sleep(1.0 + _random.uniform(0, 1.5))
                continue
            log.warning("Mapping write failed after %d attempts: %s", attempt + 1, e)
            return False, "Database busy — a sync is in progress. Please try again shortly."
        except Exception as e:
            log.warning("Mapping write error: %s", e)
            return False, "An internal error occurred"
        finally:
            try:
                conn.close()
            except Exception:
                pass


@main_bp.route("/api/admin/mappings", methods=["POST"],
               endpoint="api_admin_mappings_update")
@api_login_required
@feature_required("admin")
def api_admin_mappings_update():
    """Save a manual mapping. Body: {company_id, service, target_id}

    service is one of: cipp, duo, huntress, threatlocker
    target_id is the service-specific identifier (or empty string to clear)
    """
    data = request.get_json(silent=True) or {}
    company_id = str(data.get("company_id", "")).strip()
    service = (data.get("service") or "").strip()
    target_id = (data.get("target_id") or "").strip() or None

    SERVICE_COLUMNS = {
        "cipp": "cipp_tenant_id",
        "duo": "duo_account_id",
        "huntress": "huntress_org_id",
        "threatlocker": "threatlocker_org_id",
    }

    if not company_id:
        return jsonify({"ok": False, "error": "company_id is required"}), 400
    if service not in SERVICE_COLUMNS:
        return jsonify({"ok": False, "error": f"Unknown service: {service}"}), 400

    column = SERVICE_COLUMNS[service]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Validate with a read-only connection (no write lock needed) ──
    ro = get_db_readonly()
    if not ro:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500
    try:
        row = ro.execute(
            "SELECT id FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": f"Company {company_id} not found"}), 400

        if target_id and service == "cipp":
            valid = ro.execute(
                "SELECT 1 FROM cipp_tenants WHERE default_domain = ? OR tenant_id = ?",
                (target_id, target_id),
            ).fetchone()
            if not valid:
                return jsonify({"ok": False, "error": "CIPP tenant not found"}), 400
        elif target_id and service == "duo":
            valid = ro.execute(
                "SELECT 1 FROM duo_users WHERE duo_account_id = ? LIMIT 1",
                (target_id,),
            ).fetchone()
            if not valid:
                return jsonify({"ok": False, "error": "Duo account not found"}), 400
        elif target_id and service == "threatlocker":
            import re
            if re.match(r'^[0-9a-fA-F-]{20,}$', target_id):
                valid = ro.execute(
                    "SELECT 1 FROM threatlocker_organizations WHERE id = ? LIMIT 1",
                    (target_id,),
                ).fetchone()
                if not valid:
                    return jsonify({"ok": False, "error": "ThreatLocker organization not found"}), 400
    finally:
        ro.close()

    # ── Write with retry (sync containers may hold the WAL lock briefly) ──
    ok, err = _mapping_write_with_retry([
        (company_id, column, target_id, now),
    ])
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err}), 500


@main_bp.route("/api/admin/mappings/bulk", methods=["POST"],
               endpoint="api_admin_mappings_bulk")
@api_login_required
@feature_required("admin")
def api_admin_mappings_bulk():
    """Save multiple mappings in a single transaction.

    Body: {mappings: [{company_id, service, target_id}, ...]}
    Skips validation per-item (suggestions already came from our own data).
    """
    data = request.get_json(silent=True) or {}
    items = data.get("mappings")
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "mappings array is required"}), 400

    SERVICE_COLUMNS = {
        "cipp": "cipp_tenant_id",
        "duo": "duo_account_id",
        "huntress": "huntress_org_id",
        "threatlocker": "threatlocker_org_id",
    }

    # Validate shape (lightweight — no DB reads)
    cleaned = []
    for item in items:
        company_id = str(item.get("company_id", "")).strip()
        service = (item.get("service") or "").strip()
        target_id = (item.get("target_id") or "").strip() or None
        if not company_id or service not in SERVICE_COLUMNS:
            continue  # skip malformed items silently
        cleaned.append((company_id, service, target_id))

    if not cleaned:
        return jsonify({"ok": False, "error": "No valid mappings in request"}), 400

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [(cid, SERVICE_COLUMNS[svc], tid, now) for cid, svc, tid in cleaned]
    ok, err = _mapping_write_with_retry(rows)
    if ok:
        return jsonify({"ok": True, "saved": len(rows)})
    return jsonify({"ok": False, "error": err}), 500


@main_bp.route("/api/admin/mappings/ignore", methods=["POST"],
               endpoint="api_admin_mappings_ignore")
@api_login_required
@feature_required("admin")
def api_admin_mappings_ignore():
    """Toggle the mapping_ignored flag for a company.

    Body: {company_id: int, ignored: bool}
    """
    data = request.get_json(silent=True) or {}
    company_id = str(data.get("company_id", "")).strip()
    ignored = 1 if data.get("ignored") else 0

    if not company_id:
        return jsonify({"ok": False, "error": "company_id is required"}), 400

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "INSERT OR IGNORE INTO customer_map (cw_manage_company_id, updated_at) VALUES (?, ?)",
            (company_id, now),
        )
        conn.execute(
            "UPDATE customer_map SET mapping_ignored = ?, updated_at = ? WHERE cw_manage_company_id = ?",
            (ignored, now, company_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    except sqlite3.OperationalError as e:
        log.warning("Mapping ignore toggle failed: %s", e)
        return jsonify({"ok": False, "error": "Database busy — please try again shortly."}), 500
    except Exception as e:
        log.warning("Mapping ignore error: %s", e)
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Timeline ──────────────────────────────────────────────────────────────────

@main_bp.route("/timeline", endpoint="timeline")
@login_required
@feature_required("timeline")
def timeline_page():
    _track_page("timeline")
    user = session.get("user", {}) if is_azure_enabled() else {}
    feat = check_all_features(user.get("email", ""))
    cw_site = os.environ.get("CW_SITE", "")
    cw_ticket_url = f"https://{cw_site}/v4_6_release/services/system_io/Service/fv_sr100_request.rails?service_recid=" if cw_site else ""
    ai_summaries_enabled = get_setting("timeline_ai_summaries", "true").lower() == "true"
    return render_template("timeline.html",
                           auth_enabled=is_azure_enabled(),
                           user_name=user.get("name", ""),
                           user_email=user.get("email", ""),
                           has_timeline=True,
                           has_analytics=feat["admin"],
                           has_admin=feat["admin"],
                           app_version=_current_version(),
                           cw_ticket_url=cw_ticket_url,
                           ai_summaries_enabled=ai_summaries_enabled)


@main_bp.route("/timeline/debug", endpoint="timeline_debug")
@login_required
@feature_required("timeline")
def timeline_debug_page():
    _track_page("timeline-debug")
    user = session.get("user", {}) if is_azure_enabled() else {}
    feat = check_all_features(user.get("email", ""))
    return render_template("timeline_debug.html",
                           auth_enabled=is_azure_enabled(),
                           user_name=user.get("name", ""),
                           user_email=user.get("email", ""),
                           has_timeline=True,
                           has_analytics=feat["admin"],
                           has_admin=feat["admin"],
                           app_version=_current_version())


@main_bp.route("/api/timeline", endpoint="api_timeline")
@api_login_required
@feature_required("timeline")
def api_timeline():
    """Return active timeline events. Supports ?company=, ?from=, ?to=, ?board=, ?confidence= filters."""
    conn = get_db_readonly()
    if not conn:
        return jsonify({"events": []})

    try:
        conditions = ["te.status = 'active'"]
        params = []

        company = request.args.get("company", "").strip()
        if company:
            conditions.append("te.company_name LIKE ?")
            params.append(f"%{company}%")

        date_from = request.args.get("from", "").strip()
        if date_from:
            conditions.append("te.parsed_date >= ?")
            params.append(date_from)

        date_to = request.args.get("to", "").strip()
        if date_to:
            conditions.append("te.parsed_date <= ?")
            params.append(date_to)

        board = request.args.get("board", "").strip()
        if board:
            conditions.append("te.board_name = ?")
            params.append(board)

        # Apply global board filter from settings (inclusion list)
        allowed_boards = get_setting("timeline_boards", "")
        if allowed_boards:
            board_list = [b.strip().lower() for b in allowed_boards.split(",") if b.strip()]
            if board_list:
                placeholders = ",".join("?" for _ in board_list)
                conditions.append(f"LOWER(te.board_name) IN ({placeholders})")
                params.extend(board_list)

        confidence = request.args.get("confidence", "").strip()
        if confidence:
            # Support comma-separated values e.g. "confirmed,likely"
            levels = [c.strip() for c in confidence.split(",") if c.strip()]
            if levels:
                placeholders = ",".join("?" for _ in levels)
                conditions.append(f"COALESCE(te.confidence, 'confirmed') IN ({placeholders})")
                params.extend(levels)

        where = " AND ".join(conditions)
        rows = conn.execute(f"""
            SELECT te.id, te.ticket_id, te.company_name, te.board_name,
                   te.parsed_date, te.ticket_summary, te.matched_text,
                   te.context_snippet, te.pattern_name, te.member_name,
                   te.note_date, te.ai_description,
                   COALESCE(te.confidence, 'confirmed') AS confidence
            FROM timeline_events te
            WHERE {where}
            ORDER BY te.parsed_date ASC
            LIMIT 200
        """, params).fetchall()

        events = [dict(r) for r in rows]
        return jsonify({"events": events})
    except Exception as e:
        log.warning("api_timeline error: %s", e)
        return jsonify({"events": [], "error": "An internal error occurred"})
    finally:
        conn.close()


@main_bp.route("/api/timeline/<int:event_id>/dismiss", methods=["POST"],
               endpoint="api_timeline_dismiss")
@api_login_required
@feature_required("timeline")
def api_timeline_dismiss(event_id):
    """Soft-delete a timeline event by setting status='dismissed'."""
    for attempt in range(3):
        conn = get_db()
        if not conn:
            return jsonify({"ok": False, "error": "Database unavailable"}), 500
        try:
            conn.execute(
                "UPDATE timeline_events SET status = 'dismissed' WHERE id = ?",
                (event_id,)
            )
            conn.commit()
            return jsonify({"ok": True})
        except sqlite3.OperationalError as e:
            conn.close()
            if "locked" in str(e) and attempt < 2:
                log.info("dismiss: DB locked (attempt %d/3), retrying...", attempt + 1)
                time.sleep(2)
                continue
            log.warning("dismiss error: %s", e)
            return jsonify({"ok": False, "error": "An internal error occurred"}), 500
        except Exception as e:
            conn.close()
            log.warning("dismiss error: %s", e)
            return jsonify({"ok": False, "error": "An internal error occurred"}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass


@main_bp.route("/api/timeline/<int:event_id>/restore", methods=["POST"],
               endpoint="api_timeline_restore")
@api_login_required
@feature_required("timeline")
def api_timeline_restore(event_id):
    """Restore a dismissed timeline event."""
    for attempt in range(3):
        conn = get_db()
        if not conn:
            return jsonify({"ok": False, "error": "Database unavailable"}), 500
        try:
            conn.execute(
                "UPDATE timeline_events SET status = 'active' WHERE id = ?",
                (event_id,)
            )
            conn.commit()
            return jsonify({"ok": True})
        except sqlite3.OperationalError as e:
            conn.close()
            if "locked" in str(e) and attempt < 2:
                log.info("restore: DB locked (attempt %d/3), retrying...", attempt + 1)
                time.sleep(2)
                continue
            log.warning("restore error: %s", e)
            return jsonify({"ok": False, "error": "An internal error occurred"}), 500
        except Exception as e:
            conn.close()
            log.warning("restore error: %s", e)
            return jsonify({"ok": False, "error": "An internal error occurred"}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass


@main_bp.route("/api/timeline/generate-descriptions", methods=["POST"],
               endpoint="api_timeline_generate_descriptions")
@api_login_required
@feature_required("timeline")
def api_timeline_generate_descriptions():
    """Manually trigger AI description generation for events missing one."""
    if get_setting("timeline_ai_summaries", "true").lower() != "true":
        return jsonify({"ok": False, "error": "AI summaries are disabled in settings"}), 400
    from parse_timeline import generate_ai_descriptions
    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500
    try:
        conn.row_factory = sqlite3.Row
        # Count how many are pending
        pending = conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE status = 'active' AND ai_description IS NULL"
        ).fetchone()[0]
        generated = generate_ai_descriptions(conn)
        return jsonify({"ok": True, "generated": generated, "pending_before": pending})
    except Exception as e:
        log.warning("generate_descriptions error: %s", e)
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500
    finally:
        conn.close()


@main_bp.route("/api/timeline/<int:event_id>/generate-description", methods=["POST"],
               endpoint="api_timeline_generate_single")
@api_login_required
@feature_required("timeline")
def api_timeline_generate_single(event_id):
    """Generate an AI description for a single timeline event."""
    if get_setting("timeline_ai_summaries", "true").lower() != "true":
        return jsonify({"ok": False, "error": "AI summaries are disabled in settings"}), 400
    # This function does a SELECT then an UPDATE + commit.
    # The SELECT (read) works fine under WAL, but the UPDATE (write) can
    # fail with "database is locked" if a sync is running.  We split the
    # work: read with a read-only conn, call the API, then write with
    # retries on a fresh read/write conn.
    ro = get_db_readonly()
    if not ro:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500
    try:
        row = ro.execute(
            "SELECT id, ticket_id, ticket_summary, context_snippet, matched_text "
            "FROM timeline_events WHERE id = ?", (event_id,)
        ).fetchone()
    finally:
        ro.close()
    if not row:
        return jsonify({"ok": False, "error": f"Event {event_id} not found"}), 404

    # Call the Anthropic API (no DB lock needed for this)
    import anthropic, re as _re, json as _json
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set"}), 500

    try:
        from parse_timeline import AI_MODEL, AI_SYSTEM_PROMPT, _truncate_context
        ctx = _truncate_context(row["context_snippet"])
        user_prompt = (
            "Describe this timeline event. Return a single plain-text sentence (no JSON).\n\n"
            f"Ticket: {row['ticket_summary'] or 'No summary'}\n"
            f"Context: {ctx}\n"
            f"Matched: {row['matched_text']}"
        )
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=AI_MODEL, max_tokens=128,
            system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        desc = response.content[0].text.strip()
        # Clean markdown fences, unwrap JSON arrays, strip quotes
        if desc.startswith("```"):
            desc = _re.sub(r"^```(?:json)?\s*", "", desc)
            desc = _re.sub(r"\s*```$", "", desc)
        if desc.startswith("[") and desc.endswith("]"):
            try:
                parsed = _json.loads(desc)
                if isinstance(parsed, list) and len(parsed) == 1:
                    desc = str(parsed[0]).strip()
            except _json.JSONDecodeError:
                pass
        if len(desc) > 2 and desc[0] == '"' and desc[-1] == '"':
            desc = desc[1:-1]
    except Exception as e:
        log.warning("generate_single AI call error: %s", e)
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500

    # Now write the result with retries
    for attempt in range(3):
        conn = get_db()
        if not conn:
            return jsonify({"ok": False, "error": "Database unavailable"}), 500
        try:
            conn.execute("UPDATE timeline_events SET ai_description = ? WHERE id = ?", (desc, event_id))
            conn.commit()
            return jsonify({"ok": True, "description": desc})
        except sqlite3.OperationalError as e:
            conn.close()
            if "locked" in str(e) and attempt < 2:
                log.info("generate_single: DB locked writing result (attempt %d/3), retrying...", attempt + 1)
                time.sleep(2)
                continue
            log.warning("generate_single DB write error: %s", e)
            # Return the description anyway — it was generated successfully
            return jsonify({"ok": True, "description": desc, "warning": "AI generated but DB save failed"})
        except Exception as e:
            conn.close()
            log.warning("generate_single DB write error: %s", e)
            return jsonify({"ok": True, "description": desc, "warning": "AI generated but DB save failed"})
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # Should never reach here, but just in case
    return jsonify({"ok": True, "description": desc, "warning": "DB save failed after retries"})


@main_bp.route("/api/timeline/debug", endpoint="api_timeline_debug")
@api_login_required
@feature_required("timeline")
def api_timeline_debug():
    """Return all timeline events (including dismissed) with full pattern info."""
    conn = get_db_readonly()
    if not conn:
        return jsonify({"events": []})

    try:
        conditions = []
        params = []

        pattern = request.args.get("pattern", "").strip()
        if pattern:
            conditions.append("te.pattern_name = ?")
            params.append(pattern)

        status_filter = request.args.get("status", "").strip()
        if status_filter:
            conditions.append("te.status = ?")
            params.append(status_filter)

        company = request.args.get("company", "").strip()
        if company:
            conditions.append("te.company_name LIKE ?")
            params.append(f"%{company}%")

        board = request.args.get("board", "").strip()
        if board:
            conditions.append("te.board_name = ?")
            params.append(board)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(f"""
            SELECT te.id, te.ticket_id, te.company_name, te.company_id,
                   te.board_name, te.parsed_date, te.ticket_summary,
                   te.matched_text, te.context_snippet, te.pattern_name,
                   te.member_name, te.note_date, te.note_id, te.status,
                   te.created_at, te.ai_description,
                   COALESCE(te.confidence, 'confirmed') AS confidence
            FROM timeline_events te
            {where}
            ORDER BY te.parsed_date ASC
            LIMIT 500
        """, params).fetchall()

        events = [dict(r) for r in rows]

        # Also return pattern summary for the filter dropdown
        pattern_counts = conn.execute("""
            SELECT pattern_name, COUNT(*) as cnt
            FROM timeline_events
            GROUP BY pattern_name
            ORDER BY cnt DESC
        """).fetchall()
        patterns = [{"name": r["pattern_name"], "count": r["cnt"]} for r in pattern_counts]

        return jsonify({"events": events, "patterns": patterns})
    except Exception as e:
        log.warning("api_timeline_debug error: %s", e)
        return jsonify({"events": [], "patterns": [], "error": "An internal error occurred"})
    finally:
        conn.close()


@main_bp.route("/api/timeline/exclusions", endpoint="api_timeline_exclusions")
@api_login_required
@feature_required("timeline")
def api_timeline_exclusions():
    """List all timeline exclusions."""
    conn = get_db_readonly()
    if not conn:
        return jsonify({"exclusions": []})
    try:
        rows = conn.execute(
            "SELECT id, type, value, created_by, created_at FROM timeline_exclusions ORDER BY created_at DESC"
        ).fetchall()
        return jsonify({"exclusions": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"exclusions": [], "error": "An internal error occurred"})
    finally:
        conn.close()


@main_bp.route("/api/timeline/exclusions", methods=["POST"],
               endpoint="api_timeline_exclusion_add")
@api_login_required
@feature_required("timeline")
def api_timeline_exclusion_add():
    """Add a new exclusion rule. Body: {type: "ticket_id"|"substring", value: "..."}"""
    data = request.get_json() or {}
    exc_type = data.get("type", "").strip()
    exc_value = data.get("value", "").strip()

    if exc_type not in ("ticket_id", "substring", "board", "company", "bypass_email"):
        return jsonify({"ok": False, "error": "type must be 'ticket_id', 'substring', 'board', 'company', or 'bypass_email'"}), 400
    if not exc_value:
        return jsonify({"ok": False, "error": "value is required"}), 400

    # Validate ticket_id is numeric
    if exc_type == "ticket_id":
        try:
            int(exc_value)
        except ValueError:
            return jsonify({"ok": False, "error": "ticket_id must be a number"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500
    try:
        user = session.get("user", {}) if is_azure_enabled() else {}
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT OR IGNORE INTO timeline_exclusions (type, value, created_by, created_at) VALUES (?, ?, ?, ?)",
            (exc_type, exc_value, user.get("email", "anonymous"), now)
        )
        # Also retroactively dismiss matching existing events
        if exc_type == "ticket_id":
            conn.execute(
                "UPDATE timeline_events SET status = 'excluded' WHERE ticket_id = ? AND status = 'active'",
                (int(exc_value),)
            )
        elif exc_type == "substring":
            # Dismiss events whose context contains the substring
            conn.execute(
                "UPDATE timeline_events SET status = 'excluded' WHERE status = 'active' AND LOWER(context_snippet) LIKE ?",
                (f"%{exc_value.lower()}%",)
            )
        elif exc_type == "board":
            conn.execute(
                "UPDATE timeline_events SET status = 'excluded' WHERE status = 'active' AND LOWER(board_name) = LOWER(?)",
                (exc_value,)
            )
        elif exc_type == "company":
            conn.execute(
                "UPDATE timeline_events SET status = 'excluded' WHERE status = 'active' AND LOWER(company_name) = LOWER(?)",
                (exc_value,)
            )
        # bypass_email doesn't retroactively change anything — it only affects
        # future parser runs by allowing notes through a company exclusion
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": "An internal error occurred"}), 500
    finally:
        conn.close()


@main_bp.route("/api/timeline/exclusions/<int:exc_id>", methods=["DELETE"],
               endpoint="api_timeline_exclusion_delete")
@api_login_required
@feature_required("timeline")
def api_timeline_exclusion_delete(exc_id):
    """Delete an exclusion rule. Previously excluded events stay excluded
    until a reparse — but the rule won't suppress future matches."""
    conn = get_db()
    if not conn:
        return jsonify({"ok": False, "error": "Database unavailable"}), 500
    try:
        conn.execute("DELETE FROM timeline_exclusions WHERE id = ?", (exc_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

