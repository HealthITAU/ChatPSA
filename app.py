#!/usr/bin/env python3
"""
ChatPSA — bootstrap.

All business logic lives in:
  config.py   — constants and logging setup
  db.py       — database layer
  insights.py — trend/anomaly detection
  pins.py     — trend pin management
  agent.py    — Claude integration
  auth.py     — Azure AD authentication (Blueprint)
  routes.py   — web and API routes (Blueprint)
"""
import argparse
import os
from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from config import APP_DIR, DB_PATH, AZURE_ENABLED, MEMORIES_DB_PATH, is_azure_enabled
from db import get_or_create_secret_key, seed_initial_examples, init_timeline_tables, seed_known_users, seed_app_settings, validate_azure_credentials_on_startup
from memory_routes import memory_bp
from memory_store import init_memories
from auth import auth_bp
from routes import main_bp

app = Flask(__name__,
            template_folder=os.path.join(APP_DIR, "templates"),
            static_folder=os.path.join(APP_DIR, "static"))

app.secret_key = get_or_create_secret_key()
app.config["MEMORIES_DB_PATH"] = MEMORIES_DB_PATH

# ── Session cookie hardening ─────────────────────────────────────────────
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = AZURE_ENABLED  # True when behind HTTPS/Azure

# ── CSRF protection for API routes ──────────────────────────────────────
# All POST/PUT/DELETE requests to /api/* must include a custom header.
# Browsers enforce that cross-origin requests with custom headers trigger
# a CORS preflight, which will fail (no CORS is configured), blocking
# cross-site request forgery.  The frontend already uses fetch() with
# JSON bodies, so adding this header is a one-line change.
_CSRF_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))

@app.before_request
def _csrf_check():
    if request.method in _CSRF_SAFE_METHODS:
        return None
    if not request.path.startswith("/api/"):
        return None
    if not request.headers.get("X-Requested-With"):
        return jsonify({"error": "Missing X-Requested-With header"}), 403

# ── Security response headers ──────────────────────────────────────────
@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if is_azure_enabled():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# Initialise the agent_memories table at import time so gunicorn workers
# have it available without needing to go through main().
init_memories(MEMORIES_DB_PATH)

# Trust one level of reverse proxy (Apache).
# This makes Flask correctly detect HTTPS and generate https:// URLs for
# OAuth callbacks, and sets REMOTE_ADDR from X-Forwarded-For.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Make app_name available in all templates without passing it in every route.
@app.context_processor
def _inject_app_name():
    from settings import get_setting
    return {"app_name": get_setting("app_name", "ChatPSA")}

# Register blueprints
app.register_blueprint(memory_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)

# Seed few-shot SQL examples if the table is empty (runs once on first boot).
seed_initial_examples()

# Create timeline + feature_access tables if they don't exist.
init_timeline_tables()

# Pre-populate the permissions user list from CW members and usage history
# so admins see the full team without waiting for each person to sign in.
seed_known_users()

# Seed configurable settings from env vars (only writes defaults for keys
# that don't already have a DB row — never overwrites admin changes).
seed_app_settings()

# Validate Azure credentials at startup so bad secrets from .env are caught
# immediately in the container logs, not discovered when users can't log in.
validate_azure_credentials_on_startup()

def main():
    from settings import get_setting
    app_name = get_setting("app_name", "ChatPSA")
    parser = argparse.ArgumentParser(description=app_name)
    parser.add_argument("--port", type=int, default=5001, help="Port to run on (default: 5001)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    # Debug mode is only available via explicit env var — never via CLI flag,
    # to prevent accidental exposure of the Werkzeug interactive debugger.
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("true", "1")
    if debug:
        print("WARNING: Running in debug mode — do NOT use in production!")

    print(f"{app_name} starting on http://{args.host}:{args.port}")
    print(f"Database: {DB_PATH}")
    print(f"API key: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT SET'}")
    print(f"Auth: {'Microsoft Entra ID (Azure AD)' if is_azure_enabled() else 'disabled (local mode)'}")
    print()

    app.run(host=args.host, port=args.port, debug=debug)


if __name__ == "__main__":
    main()
