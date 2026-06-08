"""auth.py — Azure AD / Entra ID authentication blueprint.

Uses DB-managed credentials (via get_setting) so secrets can be rotated
through the Admin UI without rebuilding the container.  Falls back to
env vars when the DB has no value (fresh deploy / migration).

When the client secret is rotated, the previous secret is saved
automatically and tried as a fallback if the primary fails — covering
the transition window during rotation.
"""
import logging
import secrets
import sqlite3
from functools import wraps

from flask import (Blueprint, jsonify, redirect, request, session, url_for)

from config import (DB_PATH, is_azure_enabled, get_azure_credentials)
from db import upsert_known_user

log = logging.getLogger("chatpsa.auth")

auth_bp = Blueprint("auth", __name__)

# Scopes required for authentication — only basic profile info.
_AZURE_SCOPES = ["User.Read"]


def _make_msal_app(client_id, client_secret, tenant_id):
    """Create an MSAL confidential client application with explicit credentials."""
    import msal
    return msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )


def get_msal_app():
    """Create an MSAL app using the current (DB-first) Azure credentials."""
    client_id, client_secret, tenant_id = get_azure_credentials()
    return _make_msal_app(client_id, client_secret, tenant_id)


def login_required(f):
    """Decorator: require Microsoft OAuth login if Azure is configured."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_azure_enabled():
            return f(*args, **kwargs)
        if "user" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """Decorator for API routes: return 401 JSON instead of redirect."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_azure_enabled():
            return f(*args, **kwargs)
        if "user" not in session:
            return jsonify({"error": "Not authenticated. Please log in."}), 401
        return f(*args, **kwargs)
    return decorated


def check_feature_access(feature_name, user_email):
    """Check if a user has access to a specific feature. Returns True/False.

    Used by templates to conditionally show/hide nav links.
    """
    if not is_azure_enabled():
        return True  # No auth = no gating
    if not user_email:
        return False
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 FROM feature_access WHERE feature = ? AND LOWER(user_email) = LOWER(?)",
            (feature_name, user_email)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def check_all_features(user_email, features=("timeline", "admin")):
    """Check multiple features in a single DB query. Returns dict {feature: bool}.

    This avoids opening N separate connections when every page needs to check
    the same set of features for the nav bar.
    """
    result = {f: False for f in features}
    if not is_azure_enabled():
        return {f: True for f in features}
    if not user_email:
        return result
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in features)
        rows = conn.execute(
            f"SELECT feature FROM feature_access WHERE feature IN ({placeholders}) AND LOWER(user_email) = LOWER(?)",
            (*features, user_email)
        ).fetchall()
        conn.close()
        for r in rows:
            result[r["feature"]] = True
    except Exception:
        pass
    return result


def feature_required(feature_name):
    """Decorator: restrict access to users granted a specific feature in feature_access."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not is_azure_enabled():
                return f(*args, **kwargs)
            user = session.get("user", {})
            email = user.get("email", "")
            if check_feature_access(feature_name, email):
                return f(*args, **kwargs)
            return jsonify({"error": "Access denied"}), 403
        return decorated
    return decorator


_ALL_FEATURES = ("admin", "timeline")


def _auto_grant_first_admin(user_email):
    """Grant all features to the first user who logs in on a fresh deployment.

    Only fires when the feature_access table is completely empty (no one has
    any feature grants yet).  After the first admin is set, this becomes a
    no-op and all further grants go through the admin permissions page.

    Uses BEGIN IMMEDIATE to acquire a write lock before the COUNT check,
    preventing a TOCTOU race where two concurrent logins both see zero
    rows and both get admin.
    """
    if not user_email:
        return
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT COUNT(*) FROM feature_access").fetchone()[0]
            if count == 0:
                for feat in _ALL_FEATURES:
                    conn.execute(
                        "INSERT OR IGNORE INTO feature_access (feature, user_email) VALUES (?, LOWER(?))",
                        (feat, user_email),
                    )
                conn.commit()
                log.info("First login — auto-granted all features to first user")
            else:
                conn.rollback()
    except Exception as e:
        log.warning("_auto_grant_first_admin error: %s", e)


@auth_bp.route("/login", endpoint="login")
def login():
    if not is_azure_enabled():
        return redirect(url_for("main.index"))

    msal_app = get_msal_app()
    redirect_uri = url_for("auth.auth_callback", _external=True)
    # Generate a random state token to prevent login CSRF / auth code injection
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    auth_url = msal_app.get_authorization_request_url(
        _AZURE_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
    )
    return redirect(auth_url)


@auth_bp.route("/auth/callback", endpoint="auth_callback")
def auth_callback():
    if not is_azure_enabled():
        return redirect(url_for("main.index"))

    # Verify the state parameter to prevent login CSRF
    expected_state = session.pop("oauth_state", None)
    received_state = request.args.get("state")
    if not expected_state or expected_state != received_state:
        log.warning("OAuth state mismatch: expected=%s received=%s",
                    expected_state[:8] if expected_state else "None",
                    received_state[:8] if received_state else "None")
        return "Authentication failed: state mismatch. Please try logging in again.", 403

    code = request.args.get("code")
    if not code:
        return "Authentication failed: no code received", 400

    # Exchange the authorization code for tokens
    redirect_uri = url_for("auth.auth_callback", _external=True)

    msal_app = get_msal_app()
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=_AZURE_SCOPES,
        redirect_uri=redirect_uri,
    )

    if "error" in result:
        log.error("OAuth token error: %s — %s", result.get("error"), result.get("error_description"))
        from db import log_admin_event
        log_admin_event("auth", "error", "Login failed: OAuth token exchange error",
                        detail=f"{result.get('error')}: {(result.get('error_description') or '')[:200]}")
        return "Authentication failed. Please try again or contact your administrator.", 400

    # Store user info in session
    claims = result.get("id_token_claims", {})
    session["user"] = {
        "name": claims.get("name", "Unknown"),
        "email": claims.get("preferred_username", ""),
        "oid": claims.get("oid", ""),
    }

    # Track authenticated users for the permissions page
    user_email = claims.get("preferred_username", "")
    upsert_known_user(user_email, claims.get("name", "Unknown"))

    # Auto-grant admin + all features to the first user who logs in.
    _auto_grant_first_admin(user_email)

    return redirect(url_for("main.index"))


@auth_bp.route("/logout", endpoint="logout")
def logout():
    session.clear()
    if is_azure_enabled():
        _, _, tenant_id = get_azure_credentials()
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        logout_url = f"{authority}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('main.index', _external=True)}"
        return redirect(logout_url)
    return redirect(url_for("main.index"))
