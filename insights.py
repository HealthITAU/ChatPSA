"""insights.py — Trend/anomaly detection and caching."""
import logging
import time

from config import is_cipp_enabled, is_duo_enabled, is_huntress_enabled, is_threatlocker_enabled, get_tz_offset_sql
from db import get_db
from settings import get_setting, get_setting_int, get_setting_list

log = logging.getLogger("chatpsa.insights")

_insights_cache: tuple = (None, 0)
INSIGHTS_TTL = 1800


def _tz_offset_str():
    """SQLite timezone offset string — delegates to config for DST-aware offset."""
    # get_tz_offset_sql() returns e.g. "'+10 hours'" (with quotes).
    # Callers in this file embed it WITHOUT extra quotes, so strip them.
    return get_tz_offset_sql().strip("'")


def _ratio_phrase(ratio):
    """Convert a numeric ticket-spike ratio to plain English."""
    if ratio >= 10:
        return "a huge amount more tickets"
    elif ratio >= 6:
        return "significantly more tickets"
    elif ratio >= 4:
        return "far more tickets"
    elif ratio >= 3:
        return "triple the usual number of tickets"
    elif ratio >= 2:
        return "double the usual number of tickets"
    else:
        return "more tickets than usual"


def _run_insight_queries():
    """Run all trend detection queries. Returns a list of insight dicts."""
    conn = get_db()
    if not conn:
        return []

    insights = []

    helpdesk_board = get_setting("helpdesk_board", "Help Desk")
    excl_names = get_setting_list("trends_exclude_companies")
    high_priorities = get_setting_list("trends_high_priorities")
    excl_params: tuple = tuple(excl_names)

    def excl(col: str = "company_name") -> str:
        if not excl_names:
            return ""
        placeholders = ", ".join("?" for _ in excl_names)
        return f" AND {col} NOT IN ({placeholders})"

    tz = _tz_offset_str()

    # ── 1. Ticket spike ─────────────────────────────────────────────────────
    try:
        rows = conn.execute(f"""
            SELECT
                company_name,
                SUM(CASE WHEN date(date_entered, '{tz}') >= date('now', '{tz}', '-14 days')
                         THEN 1 ELSE 0 END) AS recent,
                SUM(CASE WHEN date(date_entered, '{tz}') < date('now', '{tz}', '-14 days')
                         THEN 1 ELSE 0 END) AS prior
            FROM tickets
            WHERE board_name = ?
              AND date(date_entered, '{tz}') >= date('now', '{tz}', '-104 days')
              AND company_name IS NOT NULL
              {excl()}
            GROUP BY company_name
            HAVING recent >= 8 AND prior >= 5
        """, (helpdesk_board,) + excl_params).fetchall()

        for r in rows:
            prior_14 = r["prior"] * (14.0 / 90.0)
            if prior_14 < 1:
                continue
            ratio = r["recent"] / prior_14
            if ratio >= 2.0:
                phrase = _ratio_phrase(ratio)
                insights.append({
                    "type": "ticket_spike",
                    "company": r["company_name"],
                    "metric": f"{ratio:.1f}×",
                    "detail": f"{r['recent']} helpdesk tickets in the last 14 days vs {prior_14:.1f} typical — {phrase}",
                    "severity": "high" if ratio >= 2.5 else "medium",
                    "chips": [
                        f"Why is {r['company_name']} submitting {phrase}?",
                        f"What are the most common issues for {r['company_name']} recently?",
                    ],
                })
    except Exception:
        pass

    # ── 2. Unused M365 licences (only when CIPP is enabled) ────────────────
    if is_cipp_enabled():
        try:
            rows = conn.execute(f"""
                SELECT c.name AS company_name,
                       SUM(cl.available_units) AS available,
                       SUM(cl.active_units)    AS total
                FROM companies c
                JOIN customer_map cm ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
                JOIN cipp_tenants ct ON cm.cipp_tenant_id = ct.default_domain
                JOIN cipp_licenses cl ON ct.tenant_id = cl.tenant_id
                WHERE (cl.sku_name LIKE '%E3%' OR cl.sku_name LIKE '%E5%'
                    OR cl.sku_name LIKE '%Business Basic%'
                    OR cl.sku_name LIKE '%Business Standard%'
                    OR cl.sku_name LIKE '%Business Premium%'
                    OR cl.sku_name LIKE '%F3%')
                  AND cl.available_units > 3
                  {excl('c.name')}
                GROUP BY c.name
                HAVING available > 3
                ORDER BY available DESC
                LIMIT 3
            """, excl_params).fetchall()

            for r in rows:
                insights.append({
                    "type": "unused_licenses",
                    "company": r["company_name"],
                    "metric": str(int(r["available"])),
                    "detail": f"{int(r['available'])} unused paid M365 licences of {int(r['total'])} total",
                    "severity": "medium" if r["available"] >= 10 else "low",
                    "chips": [
                        f"What unused M365 licences does {r['company_name']} have?",
                        f"Can we reduce {r['company_name']}'s M365 spend?",
                    ],
                })
        except Exception:
            pass

    # ── 3. Overdue tickets ──────────────────────────────────────────────────
    try:
        rows = conn.execute(f"""
            SELECT company_name, COUNT(*) AS cnt
            FROM tickets
            WHERE date_closed IS NULL
              AND board_name = ?
              AND required_date IS NOT NULL
              AND datetime(required_date, '{tz}') < datetime('now', '{tz}')
              AND company_name IS NOT NULL
              {excl()}
            GROUP BY company_name
            ORDER BY cnt DESC
            LIMIT 2
        """, (helpdesk_board,) + excl_params).fetchall()

        for r in rows:
            if r["cnt"] >= 2:
                insights.append({
                    "type": "overdue",
                    "company": r["company_name"],
                    "metric": str(r["cnt"]),
                    "detail": f"{r['cnt']} overdue tickets",
                    "severity": "high",
                    "chips": [
                        f"What's overdue for {r['company_name']}?",
                        f"Show me all overdue tickets for {r['company_name']} ordered by how late they are",
                    ],
                })
    except Exception:
        pass

    # ── 4. Stale high-priority tickets ──────────────────────────────────────
    try:
        # If high_priorities is set, match those exact names.
        # Otherwise, match any priority containing "Critical" or "High".
        if high_priorities:
            prio_placeholders = ", ".join("?" for _ in high_priorities)
            prio_clause = f"AND priority_name IN ({prio_placeholders})"
            prio_params = tuple(high_priorities)
        else:
            prio_clause = "AND (priority_name LIKE '%Critical%' OR priority_name LIKE '%High%')"
            prio_params = ()

        rows = conn.execute(f"""
            SELECT company_name, COUNT(*) AS cnt
            FROM tickets
            WHERE date_closed IS NULL
              AND board_name = ?
              {prio_clause}
              AND datetime(date_entered, '{tz}') < datetime('now', '{tz}', '-24 hours')
              AND company_name IS NOT NULL
              {excl()}
            GROUP BY company_name
            HAVING cnt >= 2
            ORDER BY cnt DESC
            LIMIT 2
        """, (helpdesk_board,) + prio_params + excl_params).fetchall()

        for r in rows:
            insights.append({
                "type": "stale_priority",
                "company": r["company_name"],
                "metric": str(r["cnt"]),
                "detail": f"{r['cnt']} high-priority tickets open more than 24 hours",
                "severity": "high",
                "chips": [
                    f"What high-priority tickets for {r['company_name']} need attention?",
                    f"Show me the oldest open priority tickets for {r['company_name']}",
                ],
            })
    except Exception:
        pass

    # ── 5. Quiet clients ────────────────────────────────────────────────────
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT c.name AS company_name
            FROM companies c
            JOIN agreements a ON a.company_id = c.id
            LEFT JOIN tickets t
                   ON t.company_id = c.id
                  AND t.board_name = ?
                  AND date(t.date_entered, '{tz}') >= date('now', '{tz}', '-60 days')
            WHERE a.cancelled_flag = 0
              AND (a.end_date IS NULL OR date(a.end_date) >= date('now'))
              AND t.id IS NULL
              AND c.name IS NOT NULL
              {excl('c.name')}
            LIMIT 3
        """, (helpdesk_board,) + excl_params).fetchall()

        for r in rows:
            insights.append({
                "type": "quiet_client",
                "company": r["company_name"],
                "metric": "60d",
                "detail": "No tickets in the last 60 days despite active agreement",
                "severity": "low",
                "chips": [
                    f"Show me the last few tickets for {r['company_name']}",
                    f"What's the agreement history with {r['company_name']}?",
                ],
            })
    except Exception:
        pass

    # ── 6. Duo bypass users (only when Duo is enabled) ─────────────────────
    if is_duo_enabled():
        try:
            rows = conn.execute("""
                SELECT c.name AS company_name, COUNT(*) AS cnt
                FROM duo_users du
                JOIN customer_map cm ON du.duo_account_id = cm.duo_account_id
                JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
                WHERE du.status = 'bypass'
                GROUP BY c.name
                HAVING cnt >= 1
                ORDER BY cnt DESC
                LIMIT 5
            """).fetchall()

            for r in rows:
                plural = "user" if r["cnt"] == 1 else "users"
                insights.append({
                    "type": "duo_bypass",
                    "company": r["company_name"],
                    "metric": str(r["cnt"]),
                    "detail": f"{r['cnt']} Duo {plural} in bypass mode — MFA is being skipped",
                    "severity": "high",
                    "chips": [
                        f"Which users at {r['company_name']} are in Duo bypass mode?",
                        f"Show me all Duo bypass users across all clients",
                    ],
                })
        except Exception:
            pass

    # ── 7. Duo unenrolled or stale users (only when Duo is enabled) ────────
    if is_duo_enabled():
        try:
            rows = conn.execute("""
                SELECT c.name AS company_name,
                       SUM(CASE WHEN du.is_enrolled = 0 OR du.phones_count = 0
                                THEN 1 ELSE 0 END) AS not_enrolled,
                       SUM(CASE WHEN du.is_enrolled = 1 AND du.phones_count > 0
                                AND (du.last_login IS NULL
                                     OR du.last_login < datetime('now', '-90 days'))
                                THEN 1 ELSE 0 END) AS stale,
                       COUNT(*) AS total
                FROM duo_users du
                JOIN customer_map cm ON du.duo_account_id = cm.duo_account_id
                JOIN companies c ON CAST(cm.cw_manage_company_id AS INTEGER) = c.id
                WHERE du.status = 'active'
                GROUP BY c.name
                HAVING (not_enrolled + stale) >= 2
                ORDER BY (not_enrolled + stale) DESC
                LIMIT 5
            """).fetchall()

            for r in rows:
                parts = []
                if r["not_enrolled"] > 0:
                    parts.append(f"{r['not_enrolled']} not enrolled")
                if r["stale"] > 0:
                    parts.append(f"{r['stale']} inactive 90+ days")
                detail = " and ".join(parts) + f" out of {r['total']} Duo users"
                count = r["not_enrolled"] + r["stale"]
                insights.append({
                    "type": "duo_inactive",
                    "company": r["company_name"],
                    "metric": str(count),
                    "detail": detail,
                    "severity": "high" if r["not_enrolled"] >= 3 else "medium",
                    "chips": [
                        f"Which {r['company_name']} users aren't enrolled in Duo?",
                        f"Who at {r['company_name']} hasn't used MFA in the last 90 days?",
                    ],
                })
        except Exception:
            pass


    # -- 8. Huntress open incidents -----------------------------------------------
    if is_huntress_enabled():
        try:
            rows = conn.execute(
                "SELECT ho.name AS company_name, "
                "COUNT(*) AS cnt, "
                "SUM(CASE WHEN hi.severity IN ('critical', 'high') THEN 1 ELSE 0 END) AS critical_cnt "
                "FROM huntress_incidents hi "
                "JOIN huntress_organizations ho ON hi.huntress_org_id = ho.id "
                "WHERE hi.status NOT IN ('closed', 'resolved') "
                "GROUP BY ho.name "
                "HAVING cnt >= 1 "
                "ORDER BY critical_cnt DESC, cnt DESC "
                "LIMIT 5"
            ).fetchall()

            for r in rows:
                detail = f"{r['cnt']} open incident{'s' if r['cnt'] != 1 else ''}"
                if r["critical_cnt"] > 0:
                    detail += f" ({r['critical_cnt']} critical/high)"
                insights.append({
                    "type": "huntress_incidents",
                    "company": r["company_name"],
                    "metric": str(r["cnt"]),
                    "detail": detail,
                    "severity": "high" if r["critical_cnt"] > 0 else "medium",
                    "chips": [
                        f"What Huntress incidents are open for {r['company_name']}?",
                        f"Show me all unresolved Huntress incidents",
                    ],
                })
        except Exception:
            pass

    # -- 9. ThreatLocker computers not in Secure mode -----------------------------
    if is_threatlocker_enabled():
        try:
            rows = conn.execute(
                "SELECT tc.organization_name AS company_name, "
                "COUNT(*) AS cnt, "
                "SUM(CASE WHEN tc.mode != 'Secure' THEN 1 ELSE 0 END) AS unsecured "
                "FROM threatlocker_computers tc "
                "WHERE tc.is_deleted = 0 "
                "GROUP BY tc.organization_name "
                "HAVING unsecured >= 2 "
                "ORDER BY unsecured DESC "
                "LIMIT 5"
            ).fetchall()

            for r in rows:
                pct = round(r["unsecured"] / r["cnt"] * 100) if r["cnt"] > 0 else 0
                insights.append({
                    "type": "tl_not_lockdown",
                    "company": r["company_name"],
                    "metric": str(r["unsecured"]),
                    "detail": f"{r['unsecured']} of {r['cnt']} endpoints not in Secure mode ({pct}%)",
                    "severity": "high" if pct >= 50 else "medium",
                    "chips": [
                        f"Which {r['company_name']} computers aren\'t in ThreatLocker Secure mode?",
                        f"Show me all ThreatLocker endpoints not in Secure mode",
                    ],
                })
        except Exception:
            pass

    # -- 10. ThreatLocker deny spikes ---------------------------------------------
    if is_threatlocker_enabled():
        try:
            rows = conn.execute(
                "SELECT tc.organization_name AS company_name, "
                "SUM(tc.deny_count_one_day) AS denies_today, "
                "SUM(tc.deny_count_seven_days) AS denies_week, "
                "COUNT(*) AS endpoints "
                "FROM threatlocker_computers tc "
                "WHERE tc.is_deleted = 0 "
                "AND tc.deny_count_one_day > 0 "
                "GROUP BY tc.organization_name "
                "HAVING denies_today >= 20 "
                "ORDER BY denies_today DESC "
                "LIMIT 5"
            ).fetchall()

            for r in rows:
                avg_daily = r["denies_week"] / 7.0 if r["denies_week"] > 0 else 0
                ratio = r["denies_today"] / avg_daily if avg_daily > 0 else 0
                detail = f"{r['denies_today']} application denials today across {r['endpoints']} endpoint{'s' if r['endpoints'] != 1 else ''}"
                if ratio >= 2.0:
                    detail += f" ({ratio:.1f}x the daily average)"
                insights.append({
                    "type": "tl_deny_spike",
                    "company": r["company_name"],
                    "metric": str(r["denies_today"]),
                    "detail": detail,
                    "severity": "high" if ratio >= 3.0 or r["denies_today"] >= 100 else "medium",
                    "chips": [
                        f"Which {r['company_name']} computers have ThreatLocker denials today?",
                        f"Show me all ThreatLocker deny activity across clients",
                    ],
                })
        except Exception:
            pass

    conn.close()
    return insights


# ═════════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════════

def get_insights(force=False):
    """Return cached insights, refreshing if stale or forced."""
    global _insights_cache
    cached, ts = _insights_cache
    if not force and cached is not None and (time.time() - ts) < INSIGHTS_TTL:
        return cached
    fresh = _run_insight_queries()
    _insights_cache = (fresh, time.time())
    return fresh


def get_weekly_ticket_volume():
    """Return ticket counts grouped by week for the last 8 weeks (for trend chart).

    Returns total (all boards) and helpdesk per week.
    """
    conn = get_db()
    if not conn:
        return []
    tz = _tz_offset_str()
    board = get_setting("helpdesk_board", "Help Desk")
    try:
        rows = conn.execute(f"""
            SELECT
                strftime('%Y-W%W', date_entered, '{tz}') AS week,
                strftime('%d %b', date_entered, '{tz}', 'weekday 0', '-6 days') AS week_label,
                COUNT(*) AS total,
                SUM(CASE WHEN board_name = ? THEN 1 ELSE 0 END) AS helpdesk
            FROM tickets
            WHERE date(date_entered, '{tz}') >= date('now', '{tz}', '-56 days')
              AND date_entered IS NOT NULL
            GROUP BY week
            ORDER BY week
        """, (board,)).fetchall()
        result = [dict(r) for r in rows]
        max_h = max((r['helpdesk'] for r in result), default=1) or 1
        for r in result:
            r['bar_h'] = round((r['helpdesk'] / max_h) * 82)
            if not r.get('week_label'):
                r['week_label'] = r['week']
        return result
    except Exception:
        return []
    finally:
        conn.close()