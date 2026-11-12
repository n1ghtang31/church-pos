import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import csv
import io

import click
import psycopg
from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from itsdangerous import BadSignature, URLSafeSerializer
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from werkzeug.security import check_password_hash, generate_password_hash

from pos import mail
from pos.money import CASH_BOX, allocate_discount, settle

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sale-pos")

# Serve built frontend from frontend_build if present
static_dir = os.environ.get("FLASK_STATIC_DIR", "frontend_build")
app = Flask(__name__, static_folder=static_dir, static_url_path="/")

# Refuse to start without credentials rather than run with the API open
SECRET_KEY = os.environ.get("SECRET_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
if not SECRET_KEY or not DATABASE_URL:
    raise RuntimeError("SECRET_KEY and DATABASE_URL environment variables must be set")

# The address people use to reach the site, for links in emails
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:5000").rstrip("/")

# New events use this time zone, which decides which day a sale counts toward in reports
DEFAULT_TIMEZONE = os.environ.get("POS_TIMEZONE", "America/Chicago")

# Reverse proxies (such as nginx) whose X-Forwarded-For header is trusted, as IPs or CIDRs
TRUSTED_PROXY_IPS = [
    ipaddress.ip_network(proxy.strip(), strict=False)
    for proxy in os.environ.get("TRUSTED_PROXY_IPS", "").split(",")
    if proxy.strip()
]

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=true when serving over HTTPS
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)

PUBLIC_API_PATHS = {"/api/login", "/api/session", "/api/health", "/api/password-reset"}
# Anyone holding one of these emailed links may use it without logging in
PUBLIC_API_PREFIXES = ("/api/password-reset/", "/api/invites/accept/", "/api/shared/")
# Only the site owner may use these
OWNER_API_PREFIXES = ("/api/login-blocks", "/api/invites", "/api/users")

# After this many wrong codes in a row from one address, a public address is banned
# until the admin unblocks it. A private address only gets a short lockout, because it
# may be the reverse proxy or a network everyone shares, and banning it would lock
# out every cashier.
MAX_LOGIN_FAILURES = 10
LOGIN_LOCKOUT_SECONDS = 5 * 60
# Wrong codes older than this are forgotten
FAILURE_WINDOW_SECONDS = 24 * 60 * 60

MODES = ("charity", "yard_sale")
PAYMENT_KINDS = ("cash", "check", "venmo", "card", "other")
# Payment methods a new event starts with
DEFAULT_PAYMENT_METHODS = {
    "charity": [("Cash", "cash"), ("Check", "check"), ("Venmo", "venmo")],
    "yard_sale": [("Cash", "cash")],
}
# No single line or sale may be more than this, which catches a misplaced decimal point
MAX_CENTS = 10_000_000
MAX_QUANTITY = 999
# Event codes leave out letters and digits that are easy to mix up
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

MIN_PASSWORD_LENGTH = 10
INVITE_LIFETIME = timedelta(days=7)
RESET_LIFETIME = timedelta(hours=1)
# A closed event and everything in it is deleted this long after it closes
RETENTION = "6 months"
# An open event with no sales for this long is closed for the organizer
IDLE_CLOSE = "60 days"

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Signs the report links emailed when an event closes
shared_links = URLSafeSerializer(SECRET_KEY, salt="shared-report")

pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=True
)

def get_db_connection():
    """A pooled connection. Use it in a with block, which commits on success."""
    return pool.connection()

def run_migrations():
    """Apply any migrations/NNN_*.sql files that haven't run yet, in order."""
    with get_db_connection() as conn:
        # Several workers may start at once, so only one migrates at a time
        conn.execute("SELECT pg_advisory_xact_lock(4242)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            logger.info("Applying migration %s", path.name)
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))

# Migrate at import time so it's ready under gunicorn.
# If this fails, let it bubble so the process fails fast and logs show the problem.
run_migrations()

def client_ip():
    """The visitor's IP address.

    Behind a trusted proxy, use the last X-Forwarded-For entry, which is the one the
    proxy itself added. Anyone else's header is ignored so it can't be faked.
    """
    remote = request.remote_addr
    try:
        trusted = any(ipaddress.ip_address(remote) in net for net in TRUSTED_PROXY_IPS)
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return remote

def can_be_banned(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False

def login_block(conn, ip):
    """Return "banned" or "locked" if ip may not log in right now, otherwise None."""
    row = conn.execute(
        "SELECT banned, locked_until FROM login_failures WHERE ip = %s", (ip,)
    ).fetchone()
    if not row:
        return None
    if row["banned"]:
        return "banned"
    if row["locked_until"] and row["locked_until"] > time.time():
        return "locked"
    return None

def record_login_failure(conn, ip):
    """Count a wrong code from ip. Returns "banned" or "locked" if this one blocked it."""
    now = time.time()
    conn.execute(
        "DELETE FROM login_failures WHERE NOT banned AND last_failure < %s",
        (now - FAILURE_WINDOW_SECONDS,),
    )
    row = conn.execute(
        "SELECT failures, locked_until FROM login_failures WHERE ip = %s FOR UPDATE", (ip,)
    ).fetchone()
    # Start counting again once an earlier lockout has expired
    if row is None or (row["locked_until"] and row["locked_until"] <= now):
        failures = 1
    else:
        failures = row["failures"] + 1

    banned = False
    locked_until = None
    result = None
    if failures >= MAX_LOGIN_FAILURES:
        if can_be_banned(ip):
            banned = True
            result = "banned"
        else:
            locked_until = now + LOGIN_LOCKOUT_SECONDS
            result = "locked"

    conn.execute(
        """INSERT INTO login_failures (ip, failures, last_failure, banned, locked_until)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (ip) DO UPDATE SET
               failures = excluded.failures,
               last_failure = excluded.last_failure,
               banned = excluded.banned,
               locked_until = excluded.locked_until""",
        (ip, failures, now, banned, locked_until),
    )
    return result

BLOCK_MESSAGES = {
    "banned": "Too many wrong codes. This device is blocked until the admin unblocks it.",
    "locked": "Too many wrong codes. Try again in 5 minutes.",
}

def hash_pin(event_id, pin):
    # Keyed so PINs can be looked up directly but can't be guessed from a copy of the database.
    # The event id is included so the same PIN at two events doesn't hash the same.
    # Changing SECRET_KEY invalidates every PIN.
    return hmac.new(SECRET_KEY.encode(), f"{event_id}:{pin}".encode(), hashlib.sha256).hexdigest()

def clean_email(value):
    if not isinstance(value, str) or len(value.strip()) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()):
        raise ValueError("Enter a valid email address")
    return value.strip().lower()

def clean_password(value):
    if not isinstance(value, str) or not MIN_PASSWORD_LENGTH <= len(value) <= 200:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return value

def create_user(email, name, password, is_owner=False):
    """Add an organizer account and return its id."""
    with get_db_connection() as conn:
        return conn.execute(
            "INSERT INTO users (email, name, password_hash, is_owner) VALUES (%s, %s, %s, %s) RETURNING id",
            (clean_email(email), clean_text(name, "Name"), generate_password_hash(clean_password(password)), is_owner),
        ).fetchone()["id"]

def password_tag(password_hash):
    # Kept in the session, so changing a password logs out every other session for that account
    return hmac.new(SECRET_KEY.encode(), password_hash.encode(), hashlib.sha256).hexdigest()[:16]

def new_token():
    """An emailed one-time token, and the hash of it that's stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def current_user():
    """Return the logged in user, or None.

    An organizer has "user_id", "email" and "is_owner". A cashier has "event_id".
    """
    role = session.get("role")
    if role == "organizer":
        with get_db_connection() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = %s", (session.get("user_id"),)).fetchone()
        if user and hmac.compare_digest(session.get("password_tag", ""), password_tag(user["password_hash"])):
            return {
                "role": "organizer",
                "user_id": user["id"],
                "name": user["name"],
                "email": user["email"],
                "is_owner": user["is_owner"],
            }
        session.clear()
    if role == "cashier":
        with get_db_connection() as conn:
            cashier = conn.execute(
                "SELECT name, event_id FROM cashiers WHERE id = %s", (session.get("cashier_id"),)
            ).fetchone()
        # A removed cashier is logged out on their next request
        if cashier:
            return {"role": "cashier", "name": cashier["name"], "event_id": cashier["event_id"]}
        session.clear()
    return None

@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/"):
        # Keep sales data out of browser and proxy caches
        response.headers["Cache-Control"] = "no-store"
    return response

@app.before_request
def require_login():
    if (
        not request.path.startswith("/api/")
        or request.path in PUBLIC_API_PATHS
        or request.path.startswith(PUBLIC_API_PREFIXES)
    ):
        return None

    g.user = current_user()
    if not g.user:
        return jsonify({"error": "Authentication required"}), 401
    if request.path.startswith(OWNER_API_PREFIXES) and not g.user.get("is_owner"):
        return jsonify({"error": "Only the site owner can do that"}), 403
    return None

def organizer_only(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user["role"] != "organizer":
            return jsonify({"error": "Organizer access required"}), 403
        return view(*args, **kwargs)
    return wrapper

def load_event(conn, event_id):
    return conn.execute(
        """SELECT e.*, u.email AS owner_email FROM events e
           LEFT JOIN users u ON u.id = e.owner_user_id WHERE e.id = %s""",
        (event_id,),
    ).fetchone()

def event_route(manage=False):
    """Load the event in the URL into g.event, if this user may see it.

    A cashier only sees their own event, and an organizer only the events they made,
    unless they're the site owner. Any other event looks like it doesn't exist, so no
    one can find out which events are on the site. manage=True keeps cashiers out.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(event_id, *args, **kwargs):
            if g.user["role"] == "cashier" and g.user["event_id"] != event_id:
                return jsonify({"error": "Event not found"}), 404
            if manage and g.user["role"] != "organizer":
                return jsonify({"error": "Organizer access required"}), 403
            with get_db_connection() as conn:
                g.event = load_event(conn, event_id)
            if not g.event or (
                g.user["role"] == "organizer"
                and not g.user["is_owner"]
                and g.event["owner_user_id"] != g.user["user_id"]
            ):
                return jsonify({"error": "Event not found"}), 404
            return view(event_id, *args, **kwargs)
        return wrapper
    return decorator

def require_open_event():
    if g.event["status"] != "open":
        return jsonify({"error": "This event is closed"}), 409
    return None

def iso(value):
    return value.isoformat(timespec="seconds") if value else None

@app.route("/api/health", methods=["GET"])
def health():
    try:
        with get_db_connection() as conn:
            conn.execute("SELECT 1")
        return jsonify({"status": "ok"})
    except Exception:
        logger.exception("Health check failed")
        return jsonify({"status": "error"}), 503

@app.route("/api/session", methods=["GET"])
def get_session():
    user = current_user()
    return jsonify({"authenticated": bool(user), **(user or {})})

# Checked when an email isn't found, so a wrong email takes as long as a wrong password
DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-password")

@app.route("/api/login", methods=["POST"])
def login():
    """Log in with an email and password, or with an event code and a cashier PIN."""
    ip = client_ip()
    with get_db_connection() as conn:
        block = login_block(conn, ip)
    if block:
        logger.warning("Login refused for %s address %s", block, ip)
        return jsonify({"error": BLOCK_MESSAGES[block]}), 403 if block == "banned" else 429

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    code = data.get("code")
    event_code = data.get("event_code")
    email = data.get("email")
    password = data.get("password")

    role = None
    cashier = None
    user = None
    if isinstance(email, str) and isinstance(password, str):
        with get_db_connection() as conn:
            user = conn.execute(
                "SELECT id, password_hash FROM users WHERE email = %s", (email.strip().lower(),)
            ).fetchone()
        if check_password_hash(user["password_hash"] if user else DUMMY_PASSWORD_HASH, password) and user:
            role = "organizer"
        else:
            user = None
    elif isinstance(code, str):
        if isinstance(event_code, str):
            with get_db_connection() as conn:
                event = conn.execute(
                    "SELECT id FROM events WHERE code = %s", (event_code.strip().upper(),)
                ).fetchone()
                if event:
                    cashier = conn.execute(
                        "SELECT id FROM cashiers WHERE event_id = %s AND pin_hash = %s",
                        (event["id"], hash_pin(event["id"], code)),
                    ).fetchone()
            if cashier:
                role = "cashier"

    if not role:
        logger.warning("Failed login from %s", ip)
        with get_db_connection() as conn:
            block = record_login_failure(conn, ip)
        if block:
            logger.warning("%s %s after %d wrong codes", block.capitalize(), ip, MAX_LOGIN_FAILURES)
            return jsonify({"error": BLOCK_MESSAGES[block]}), 403 if block == "banned" else 429
        return jsonify({"error": "Incorrect code"}), 401

    # A correct code clears the count, so occasional typos never add up to a ban
    with get_db_connection() as conn:
        conn.execute("DELETE FROM login_failures WHERE ip = %s", (ip,))
    start_session(role, user=user, cashier=cashier)
    return jsonify({"authenticated": True, **current_user()})

def start_session(role, user=None, cashier=None):
    session.clear()
    session.permanent = True
    session["role"] = role
    if user:
        session["user_id"] = user["id"]
        session["password_tag"] = password_tag(user["password_hash"])
    if cashier:
        session["cashier_id"] = cashier["id"]

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"authenticated": False})

def local_time(seconds):
    return datetime.fromtimestamp(seconds).isoformat(timespec="seconds") if seconds else None

def blocked_addresses(conn):
    return conn.execute(
        """SELECT * FROM login_failures
           WHERE banned OR locked_until > %s
           ORDER BY last_failure DESC""",
        (time.time(),),
    ).fetchall()

@app.route("/api/login-blocks", methods=["GET"])
def get_login_blocks():
    with get_db_connection() as conn:
        rows = blocked_addresses(conn)
    return jsonify(
        [
            {
                "ip": row["ip"],
                "failures": row["failures"],
                "last_failure": local_time(row["last_failure"]),
                "banned": row["banned"],
                "locked_until": local_time(row["locked_until"]),
            }
            for row in rows
        ]
    )

@app.route("/api/login-blocks/<ip>", methods=["DELETE"])
def delete_login_block(ip):
    with get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM login_failures WHERE ip = %s", (ip,))
    if cursor.rowcount == 0:
        return jsonify({"error": "That address is not blocked"}), 404
    logger.warning("Admin unblocked %s", ip)
    return "", 204

# Run in the container if the admin's own device is blocked:
#   docker compose exec web flask --app app bans
#   docker compose exec web flask --app app unban <ip>
@app.cli.command("bans")
def list_bans_command():
    """List blocked addresses."""
    with get_db_connection() as conn:
        rows = blocked_addresses(conn)
    if not rows:
        click.echo("No addresses are blocked.")
    for row in rows:
        status = "banned" if row["banned"] else f"locked until {local_time(row['locked_until'])}"
        click.echo(f"{row['ip']}  {status}, {row['failures']} wrong codes, last {local_time(row['last_failure'])}")

@app.cli.command("unban")
@click.argument("ip", required=False)
@click.option("--all", "unban_all", is_flag=True, help="Unblock every address.")
def unban_command(ip, unban_all):
    """Unblock an address, or every address with --all."""
    if not ip and not unban_all:
        raise click.UsageError("Give an address to unblock, or --all.")
    with get_db_connection() as conn:
        if unban_all:
            cursor = conn.execute("DELETE FROM login_failures")
        else:
            cursor = conn.execute("DELETE FROM login_failures WHERE ip = %s", (ip,))
    click.echo(f"Unblocked {cursor.rowcount} address(es).")

# ----- Events -----

def clean_text(value, field, max_length=50, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value.strip()) > max_length or (required and not value.strip()):
        raise ValueError(f"{field} must be {1 if required else 0} to {max_length} characters")
    return value.strip()

def clean_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("Unknown time zone") from None
    return value

def new_event_code(conn):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not conn.execute("SELECT 1 FROM events WHERE code = %s", (code,)).fetchone():
            return code

def event_json(event):
    return {
        "id": event["id"],
        "code": event["code"],
        "name": event["name"],
        "mode": event["mode"],
        "status": event["status"],
        "timezone": event["timezone"],
        "created_at": iso(event["created_at"]),
        "closed_at": iso(event["closed_at"]),
        "purge_after": iso(event["purge_after"]),
        "owner_email": event.get("owner_email"),
    }

@app.route("/api/events", methods=["GET"])
def list_events():
    if g.user["role"] == "cashier":
        where, params = "e.id = %s", (g.user["event_id"],)
    elif g.user["is_owner"]:
        where, params = "TRUE", ()
    else:
        where, params = "e.owner_user_id = %s", (g.user["user_id"],)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""SELECT e.*, u.email AS owner_email FROM events e
                LEFT JOIN users u ON u.id = e.owner_user_id
                WHERE {where} ORDER BY e.created_at DESC""",
            params,
        ).fetchall()
    return jsonify([event_json(row) for row in rows])

@app.route("/api/events", methods=["POST"])
@organizer_only
def create_event():
    data = request.get_json(silent=True) or {}
    try:
        name = clean_text(data.get("name"), "Name", 80)
        if data.get("mode") not in MODES:
            raise ValueError("Mode must be charity or yard_sale")
        timezone = clean_timezone(data.get("timezone") or DEFAULT_TIMEZONE)
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400

    with get_db_connection() as conn:
        event_id = conn.execute(
            """INSERT INTO events (code, name, mode, timezone, owner_user_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (new_event_code(conn), name, data["mode"], timezone, g.user["user_id"]),
        ).fetchone()["id"]
        for label, kind in DEFAULT_PAYMENT_METHODS[data["mode"]]:
            conn.execute(
                "INSERT INTO payment_methods (event_id, label, kind) VALUES (%s, %s, %s)",
                (event_id, label, kind),
            )
        event = load_event(conn, event_id)
    return jsonify(event_json(event)), 201

def event_setup(conn, event_id):
    """Everything the register needs to ring up sales at an event."""
    items = conn.execute(
        "SELECT id, name, department FROM items WHERE event_id = %s AND active ORDER BY id", (event_id,)
    ).fetchall()
    sellers = conn.execute(
        "SELECT id, name FROM sellers WHERE event_id = %s AND active ORDER BY id", (event_id,)
    ).fetchall()
    payment_methods = conn.execute(
        """SELECT id, label, kind, holder_seller_id FROM payment_methods
           WHERE event_id = %s AND active ORDER BY id""",
        (event_id,),
    ).fetchall()
    return {"items": items, "sellers": sellers, "payment_methods": payment_methods}

@app.route("/api/events/<int:event_id>", methods=["GET"])
@event_route()
def get_event(event_id):
    with get_db_connection() as conn:
        setup = event_setup(conn, event_id)
    return jsonify({**event_json(g.event), **setup})

@app.route("/api/events/<int:event_id>", methods=["PUT"])
@event_route(manage=True)
def update_event(event_id):
    data = request.get_json(silent=True) or {}
    try:
        name = clean_text(data.get("name", g.event["name"]), "Name", 80)
        timezone = clean_timezone(data.get("timezone", g.event["timezone"]))
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE events SET name = %s, timezone = %s WHERE id = %s", (name, timezone, event_id)
        )
        event = load_event(conn, event_id)
    return jsonify(event_json(event))

@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@event_route(manage=True)
def delete_event(event_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM events WHERE id = %s", (event_id,))
    logger.warning("%s deleted event %s (%s)", g.user["email"], event_id, g.event["name"])
    return "", 204

def close_and_notify(event_id):
    """Close an open event, start its retention clock, and email its organizer.

    Returns the event. Closing an event that's already closed changes nothing.
    """
    with get_db_connection() as conn:
        closed = conn.execute(
            f"""UPDATE events SET status = 'closed', closed_at = now(),
                       purge_after = now() + interval '{RETENTION}'
                WHERE id = %s AND status = 'open' RETURNING id""",
            (event_id,),
        ).fetchone()
        event = load_event(conn, event_id)
    if closed and event["owner_email"]:
        # The event is closed either way. If the email fails, the organizer can still
        # download everything from the event's Reports.
        try:
            send_close_email(event)
        except Exception:
            logger.exception("Could not send the closing email for event %s", event_id)
    return event

@app.route("/api/events/<int:event_id>/close", methods=["POST"])
@event_route(manage=True)
def close_event(event_id):
    return jsonify(event_json(close_and_notify(event_id)))

@app.route("/api/events/<int:event_id>/reopen", methods=["POST"])
@event_route(manage=True)
def reopen_event(event_id):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE events SET status = 'open', closed_at = NULL, purge_after = NULL WHERE id = %s",
            (event_id,),
        )
        event = load_event(conn, event_id)
    return jsonify(event_json(event))

# ----- Items, sellers and payment methods -----

def clean_item(conn, data):
    if g.event["mode"] != "charity":
        raise ValueError("Items are only used at a charity drive")
    return {
        "name": clean_text(data.get("name"), "Name"),
        "department": clean_text(data.get("department"), "Department", required=False),
    }

def clean_seller(conn, data):
    if g.event["mode"] != "yard_sale":
        raise ValueError("Sellers are only used at a yard sale")
    return {"name": clean_text(data.get("name"), "Name")}

def clean_payment_method(conn, data):
    if data.get("kind") not in PAYMENT_KINDS:
        raise ValueError(f"Kind must be one of: {', '.join(PAYMENT_KINDS)}")
    holder = data.get("holder_seller_id")
    if holder is not None:
        if g.event["mode"] != "yard_sale":
            raise ValueError("Only a yard sale payment method can go to a seller")
        if type(holder) is not int or not conn.execute(
            "SELECT 1 FROM sellers WHERE id = %s AND event_id = %s AND active", (holder, g.event["id"])
        ).fetchone():
            raise ValueError("Unknown seller")
    return {
        "label": clean_text(data.get("label"), "Label"),
        "kind": data["kind"],
        "holder_seller_id": holder,
    }

# The URL name of each kind of setup row, with its table and how to check what's sent
SETUP_KINDS = {
    "items": ("items", clean_item),
    "sellers": ("sellers", clean_seller),
    "payment-methods": ("payment_methods", clean_payment_method),
}

@app.route("/api/events/<int:event_id>/<kind>", methods=["POST"])
@event_route(manage=True)
def create_setup_row(event_id, kind):
    if kind not in SETUP_KINDS:
        return jsonify({"error": "Not found"}), 404
    table, clean = SETUP_KINDS[kind]
    data = request.get_json(silent=True) or {}
    with get_db_connection() as conn:
        try:
            fields = clean(conn, data)
        except (ValueError, AttributeError) as e:
            return jsonify({"error": str(e)}), 400
        columns = ", ".join(["event_id", *fields])
        placeholders = ", ".join(["%s"] * (len(fields) + 1))
        row = conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING *",
            (event_id, *fields.values()),
        ).fetchone()
    return jsonify(row), 201

@app.route("/api/events/<int:event_id>/<kind>/<int:row_id>", methods=["PUT"])
@event_route(manage=True)
def update_setup_row(event_id, kind, row_id):
    if kind not in SETUP_KINDS:
        return jsonify({"error": "Not found"}), 404
    table, clean = SETUP_KINDS[kind]
    data = request.get_json(silent=True) or {}
    with get_db_connection() as conn:
        try:
            fields = clean(conn, data)
        except (ValueError, AttributeError) as e:
            return jsonify({"error": str(e)}), 400
        assignments = ", ".join(f"{column} = %s" for column in fields)
        row = conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = %s AND event_id = %s AND active RETURNING *",
            (*fields.values(), row_id, event_id),
        ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)

@app.route("/api/events/<int:event_id>/<kind>/<int:row_id>", methods=["DELETE"])
@event_route(manage=True)
def delete_setup_row(event_id, kind, row_id):
    if kind not in SETUP_KINDS:
        return jsonify({"error": "Not found"}), 404
    table, _ = SETUP_KINDS[kind]
    with get_db_connection() as conn:
        if kind == "sellers" and conn.execute(
            "SELECT 1 FROM payment_methods WHERE holder_seller_id = %s AND active", (row_id,)
        ).fetchone():
            return jsonify({"error": "A payment method still goes to this seller"}), 409
        try:
            with conn.transaction():
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE id = %s AND event_id = %s", (row_id, event_id)
                )
        except psycopg.errors.ForeignKeyViolation:
            # Already on a sale, so hide it instead and keep the sale's history whole
            cursor = conn.execute(
                f"UPDATE {table} SET active = FALSE WHERE id = %s AND event_id = %s", (row_id, event_id)
            )
    if cursor.rowcount == 0:
        return jsonify({"error": "Not found"}), 404
    return "", 204

# ----- Cashiers -----

def clean_cashier_pin(pin):
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,8}", pin):
        raise ValueError("PIN must be 4 to 8 digits")
    return pin

def cashier_conflict_message(error):
    if error.diag.constraint_name == "idx_cashiers_event_name":
        return "A cashier with that name already exists"
    return "That PIN is not available"

@app.route("/api/events/<int:event_id>/cashiers", methods=["GET"])
@event_route(manage=True)
def get_cashiers(event_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, name FROM cashiers WHERE event_id = %s ORDER BY name", (event_id,)
        ).fetchall()
    return jsonify(rows)

@app.route("/api/events/<int:event_id>/cashiers", methods=["POST"])
@event_route(manage=True)
def create_cashier(event_id):
    data = request.get_json(silent=True) or {}
    try:
        name = clean_text(data.get("name"), "Name")
        pin = clean_cashier_pin(data.get("pin"))
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "INSERT INTO cashiers (event_id, name, pin_hash) VALUES (%s, %s, %s) RETURNING id, name",
                (event_id, name, hash_pin(event_id, pin)),
            ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        return jsonify({"error": cashier_conflict_message(e)}), 409
    return jsonify(row), 201

@app.route("/api/events/<int:event_id>/cashiers/<int:cashier_id>", methods=["PUT"])
@event_route(manage=True)
def update_cashier(event_id, cashier_id):
    """Rename a cashier and/or set a new PIN. Leave pin out to keep the current one."""
    data = request.get_json(silent=True) or {}
    try:
        name = clean_text(data.get("name"), "Name")
        pin = data.get("pin")
        pin_hash = hash_pin(event_id, clean_cashier_pin(pin)) if pin not in (None, "") else None
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """UPDATE cashiers SET name = %s, pin_hash = COALESCE(%s, pin_hash)
                   WHERE id = %s AND event_id = %s RETURNING id, name""",
                (name, pin_hash, cashier_id, event_id),
            ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        return jsonify({"error": cashier_conflict_message(e)}), 409
    if not row:
        return jsonify({"error": "Cashier not found"}), 404
    return jsonify(row)

@app.route("/api/events/<int:event_id>/cashiers/<int:cashier_id>", methods=["DELETE"])
@event_route(manage=True)
def delete_cashier(event_id, cashier_id):
    with get_db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM cashiers WHERE id = %s AND event_id = %s", (cashier_id, event_id)
        )
    if cursor.rowcount == 0:
        return jsonify({"error": "Cashier not found"}), 404
    return "", 204

# ----- Sales -----

def is_cents(value, allow_zero=False):
    return type(value) is int and (0 if allow_zero else 1) <= value <= MAX_CENTS

def validate_lines(conn, raw_lines):
    """Return clean sale lines, or raise ValueError so bad sales never reach the reports.

    At a charity drive each line names an item. At a yard sale it names whose stuff it is.
    """
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValueError("A sale must have at least one item")
    if len(raw_lines) > 200:
        raise ValueError("Too many items in one sale")

    charity = g.event["mode"] == "charity"
    lookup = conn.execute(
        f"SELECT id, name FROM {'items' if charity else 'sellers'} WHERE event_id = %s AND active",
        (g.event["id"],),
    ).fetchall()
    names = {row["id"]: row["name"] for row in lookup}

    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ValueError("Invalid item")
        ref = raw.get("item_id" if charity else "seller_id")
        if type(ref) is not int or ref not in names:
            raise ValueError("Unknown item" if charity else "Unknown seller")

        quantity = raw.get("quantity", 1)
        if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
            raise ValueError(f"Invalid quantity for {names[ref]}")
        price = raw.get("price_cents")
        if not is_cents(price) or price * quantity > MAX_CENTS:
            raise ValueError(f"Invalid price for {names[ref]}")
        note = clean_text(raw.get("description"), "Description", 80, required=False)

        lines.append(
            {
                "item_id": ref if charity else None,
                "seller_id": None if charity else ref,
                "description": note or names[ref],
                "quantity": quantity,
                "list_cents": price,
            }
        )
    return lines

@app.route("/api/events/<int:event_id>/sales", methods=["POST"])
@event_route()
def create_sale(event_id):
    if g.user["role"] != "cashier":
        return jsonify({"error": "Log in as a cashier to record sales"}), 403
    if closed := require_open_event():
        return closed
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "No data provided"}), 400

    with get_db_connection() as conn:
        method = conn.execute(
            "SELECT id, label FROM payment_methods WHERE id = %s AND event_id = %s AND active",
            (data.get("payment_method_id"), event_id),
        ).fetchone() if type(data.get("payment_method_id")) is int else None
        if not method:
            return jsonify({"error": "Choose how the customer is paying"}), 400
        try:
            lines = validate_lines(conn, data.get("lines"))
            line_totals = [line["list_cents"] * line["quantity"] for line in lines]
            subtotal = sum(line_totals)
            if subtotal > MAX_CENTS:
                raise ValueError("The sale is too large")
            # A total lower than the items add up to is an agreed price for the lot
            total = data.get("total_cents", subtotal)
            if not is_cents(total, allow_zero=True):
                raise ValueError("Invalid total")
            shares = allocate_discount(line_totals, total)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        sale = conn.execute(
            """INSERT INTO sales (event_id, cashier_name, payment_method_id, subtotal_cents, total_cents)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (event_id, g.user["name"], method["id"], subtotal, total),
        ).fetchone()
        for line, share in zip(lines, shares):
            line["final_cents"] = share
            conn.execute(
                """INSERT INTO sale_lines
                   (sale_id, event_id, item_id, seller_id, description, quantity, list_cents, final_cents)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (sale["id"], event_id, line["item_id"], line["seller_id"], line["description"],
                 line["quantity"], line["list_cents"], share),
            )
    return jsonify({**sale_json(sale, method["label"]), "lines": lines}), 201

def sale_json(sale, payment_label):
    return {
        "id": sale["id"],
        "created_at": iso(sale["created_at"]),
        "cashier": sale["cashier_name"],
        "payment_method_id": sale["payment_method_id"],
        "payment_method": payment_label,
        "subtotal_cents": sale["subtotal_cents"],
        "total_cents": sale["total_cents"],
        "refund_of": sale["refund_of"],
    }

@app.route("/api/events/<int:event_id>/sales/<int:sale_id>/refund", methods=["POST"])
@event_route()
def refund_sale(event_id, sale_id):
    """Refund a whole sale by recording a negative sale against it."""
    if closed := require_open_event():
        return closed
    with get_db_connection() as conn:
        original = conn.execute(
            """SELECT s.*, pm.label FROM sales s JOIN payment_methods pm ON pm.id = s.payment_method_id
               WHERE s.id = %s AND s.event_id = %s""",
            (sale_id, event_id),
        ).fetchone()
        if not original:
            return jsonify({"error": "Sale not found"}), 404
        if original["refund_of"] is not None:
            return jsonify({"error": "A refund cannot be refunded"}), 400
        try:
            with conn.transaction():
                refund = conn.execute(
                    """INSERT INTO sales
                       (event_id, cashier_name, payment_method_id, subtotal_cents, total_cents, refund_of)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                    (event_id, g.user["name"], original["payment_method_id"],
                     -original["subtotal_cents"], -original["total_cents"], sale_id),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            return jsonify({"error": "This sale has already been refunded"}), 409
        conn.execute(
            """INSERT INTO sale_lines
               (sale_id, event_id, item_id, seller_id, description, quantity, list_cents, final_cents)
               SELECT %s, event_id, item_id, seller_id, description, -quantity, list_cents, -final_cents
               FROM sale_lines WHERE sale_id = %s""",
            (refund["id"], sale_id),
        )
    return jsonify(sale_json(refund, original["label"])), 201

def parse_report_date(value):
    """Return a date, or None if no date was given."""
    if not value:
        return None
    return date.fromisoformat(value)

def report_dates():
    """Read optional start/end args as dates, or raise ValueError."""
    try:
        start = parse_report_date(request.args.get("start"))
        end = parse_report_date(request.args.get("end"))
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format") from None
    if start and end and start > end:
        raise ValueError("Start date must not be after end date")
    return start, end

def sales_filter(event, start=None, end=None):
    """WHERE clause and params for the event's sales (alias s) from start to end.

    Days are counted in the event's time zone.
    """
    where = ["s.event_id = %s"]
    params = [event["id"]]
    if start:
        where.append("(s.created_at AT TIME ZONE %s)::date >= %s")
        params += [event["timezone"], start]
    if end:
        where.append("(s.created_at AT TIME ZONE %s)::date <= %s")
        params += [event["timezone"], end]
    return f"WHERE {' AND '.join(where)}", params

@app.route("/api/events/<int:event_id>/sales", methods=["GET"])
@event_route()
def list_sales(event_id):
    try:
        where_sql, params = sales_filter(g.event, *report_dates())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with get_db_connection() as conn:
        rows = conn.execute(
            f"""SELECT s.*, pm.label, r.id AS refund_id, r.created_at AS refund_created_at,
                       r.cashier_name AS refund_cashier
                FROM sales s
                JOIN payment_methods pm ON pm.id = s.payment_method_id
                LEFT JOIN sales r ON r.refund_of = s.id
                {where_sql}
                ORDER BY s.id""",
            params,
        ).fetchall()
        lines = conn.execute(
            """SELECT sale_id, item_id, seller_id, description, quantity, list_cents, final_cents
               FROM sale_lines WHERE sale_id = ANY(%s) ORDER BY id""",
            ([row["id"] for row in rows],),
        ).fetchall()

    lines_by_sale = {}
    for line in lines:
        lines_by_sale.setdefault(line.pop("sale_id"), []).append(line)
    return jsonify(
        [
            {
                **sale_json(row, row["label"]),
                "lines": lines_by_sale.get(row["id"], []),
                "refund_id": row["refund_id"],
                "refund_created_at": iso(row["refund_created_at"]),
                "refund_cashier": row["refund_cashier"],
            }
            for row in rows
        ]
    )

@app.route("/api/events/<int:event_id>/report", methods=["GET"])
@event_route()
def get_report(event_id):
    try:
        start, end = report_dates()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(build_report(g.event, start, end))

def build_report(event, start=None, end=None):
    """Totals by day, payment method, cashier, and item or seller, optionally for start..end."""
    where_sql, params = sales_filter(event, start, end)

    # Refunds are negative sales, so summed amounts and quantities are already net of
    # them. Counts subtract a refund, and refunds are also reported separately.
    net_count = "SUM(CASE WHEN s.refund_of IS NULL THEN 1 ELSE -1 END)::int"
    refund_count = "SUM(CASE WHEN s.refund_of IS NULL THEN 0 ELSE 1 END)::int"
    refund_total = "SUM(CASE WHEN s.refund_of IS NULL THEN 0 ELSE s.total_cents END)::bigint"
    sale_date = "(s.created_at AT TIME ZONE %s)::date"

    with get_db_connection() as conn:
        method_rows = conn.execute(
            f"""SELECT {sale_date} AS sale_date, pm.id AS payment_method_id, pm.label,
                       {net_count} AS count, SUM(s.total_cents)::bigint AS total_cents,
                       {refund_count} AS refund_count, {refund_total} AS refund_cents,
                       SUM(s.subtotal_cents - s.total_cents)::bigint AS discount_cents
                FROM sales s JOIN payment_methods pm ON pm.id = s.payment_method_id
                {where_sql}
                GROUP BY sale_date, pm.id, pm.label
                ORDER BY sale_date, pm.id""",
            [event["timezone"], *params],
        ).fetchall()
        cashier_rows = conn.execute(
            f"""SELECT s.cashier_name AS cashier, {net_count} AS count,
                       SUM(s.total_cents)::bigint AS total_cents, {refund_total} AS refund_cents
                FROM sales s {where_sql}
                GROUP BY s.cashier_name
                ORDER BY total_cents DESC""",
            params,
        ).fetchall()
        line_rows = conn.execute(
            f"""SELECT COALESCE(sl.item_id, sl.seller_id) AS id,
                       COALESCE(i.name, se.name) AS name, COALESCE(i.department, '') AS department,
                       SUM(sl.quantity)::int AS quantity,
                       SUM(sl.quantity * sl.list_cents)::bigint AS list_cents,
                       SUM(sl.final_cents)::bigint AS total_cents
                FROM sale_lines sl
                JOIN sales s ON s.id = sl.sale_id
                LEFT JOIN items i ON i.id = sl.item_id
                LEFT JOIN sellers se ON se.id = sl.seller_id
                {where_sql}
                GROUP BY 1, 2, 3
                ORDER BY total_cents DESC""",
            params,
        ).fetchall()

    by_method = {}
    by_day = {}
    refunds = {"count": 0, "total_cents": 0}
    discount = 0
    for row in method_rows:
        refunds["count"] += row["refund_count"]
        refunds["total_cents"] += row["refund_cents"]
        discount += row["discount_cents"]
        method = by_method.setdefault(
            row["payment_method_id"],
            {"id": row["payment_method_id"], "label": row["label"], "count": 0, "total_cents": 0},
        )
        method["count"] += row["count"]
        method["total_cents"] += row["total_cents"]

        day = by_day.setdefault(
            row["sale_date"],
            {"date": row["sale_date"].isoformat(), "count": 0, "total_cents": 0, "refund_cents": 0, "by_method": {}},
        )
        day["count"] += row["count"]
        day["total_cents"] += row["total_cents"]
        day["refund_cents"] += row["refund_cents"]
        day["by_method"][row["payment_method_id"]] = row["total_cents"]

    by_department = {}
    for row in line_rows:
        entry = by_department.setdefault(
            row["department"], {"department": row["department"], "quantity": 0, "total_cents": 0}
        )
        entry["quantity"] += row["quantity"]
        entry["total_cents"] += row["total_cents"]

    return (
        {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "overall": {
                "count": sum(m["count"] for m in by_method.values()),
                "total_cents": sum(m["total_cents"] for m in by_method.values()),
                "discount_cents": discount,
            },
            "by_method": list(by_method.values()),
            "refunds": refunds,
            "by_day": list(by_day.values()),
            "by_cashier": cashier_rows,
            # Items at a charity drive, sellers at a yard sale
            "lines": line_rows,
            "by_department": sorted(by_department.values(), key=lambda d: -d["total_cents"])
            if event["mode"] == "charity" else [],
        }
    )

@app.route("/api/events/<int:event_id>/settlement", methods=["GET"])
@event_route()
def get_settlement(event_id):
    if g.event["mode"] != "yard_sale":
        return jsonify({"error": "Settlement is only for yard sales"}), 400
    return jsonify(build_settlement(g.event))

def build_settlement(event):
    """What each seller sold, who is holding the money, and who pays whom at the end."""
    event_id = event["id"]

    with get_db_connection() as conn:
        sellers = conn.execute(
            "SELECT id, name, active FROM sellers WHERE event_id = %s ORDER BY id", (event_id,)
        ).fetchall()
        owed_rows = conn.execute(
            """SELECT seller_id, SUM(final_cents)::bigint AS cents,
                      SUM(quantity * list_cents - final_cents)::bigint AS discount_cents
               FROM sale_lines WHERE event_id = %s GROUP BY seller_id""",
            (event_id,),
        ).fetchall()
        held_rows = conn.execute(
            """SELECT pm.id, pm.label, pm.holder_seller_id, COALESCE(SUM(s.total_cents), 0)::bigint AS cents
               FROM payment_methods pm LEFT JOIN sales s ON s.payment_method_id = pm.id
               WHERE pm.event_id = %s
               GROUP BY pm.id ORDER BY pm.id""",
            (event_id,),
        ).fetchall()

    owed = {row["seller_id"]: row["cents"] for row in owed_rows}
    discounts = {row["seller_id"]: row["discount_cents"] for row in owed_rows}
    held = {}
    for row in held_rows:
        held[row["holder_seller_id"]] = held.get(row["holder_seller_id"], 0) + row["cents"]

    names = {seller["id"]: seller["name"] for seller in sellers}
    names[CASH_BOX] = "Cash box"
    transfers = settle(owed, held)
    return (
        {
            "status": event["status"],
            "sellers": [
                {
                    "id": seller["id"],
                    "name": seller["name"],
                    "sold_cents": owed.get(seller["id"], 0),
                    "discount_cents": discounts.get(seller["id"], 0),
                    "held_cents": held.get(seller["id"], 0),
                }
                for seller in sellers
                if seller["active"] or seller["id"] in owed or seller["id"] in held
            ],
            "cash_box_cents": held.get(CASH_BOX, 0),
            "payment_methods": [
                {"id": row["id"], "label": row["label"], "holder_seller_id": row["holder_seller_id"], "total_cents": row["cents"]}
                for row in held_rows
            ],
            "transfers": [
                {"from_id": payer, "from": names[payer], "to_id": payee, "to": names[payee], "cents": cents}
                for payer, payee, cents in transfers
            ],
        }
    )

# ----- Exports, closing email and shared report links -----

def dollars(cents):
    return f"{cents / 100:.2f}"

def to_csv(rows):
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()

def sales_csv(event):
    """Every sale line, one per row, with times in the event's time zone."""
    with get_db_connection() as conn:
        rows = conn.execute(
            """SELECT s.id, s.created_at AT TIME ZONE %s AS local_time, s.cashier_name, pm.label,
                      s.refund_of, COALESCE(i.name, se.name) AS name, sl.description,
                      sl.quantity, sl.list_cents, sl.final_cents
               FROM sale_lines sl
               JOIN sales s ON s.id = sl.sale_id
               JOIN payment_methods pm ON pm.id = s.payment_method_id
               LEFT JOIN items i ON i.id = sl.item_id
               LEFT JOIN sellers se ON se.id = sl.seller_id
               WHERE s.event_id = %s ORDER BY s.id, sl.id""",
            (event["timezone"], event["id"]),
        ).fetchall()
    who = "Seller" if event["mode"] == "yard_sale" else "Item"
    header = ["Sale", "Time", "Cashier", "Payment", "Refund of", who, "Description",
              "Quantity", "Price", "Line total after offers"]
    return to_csv(
        [header]
        + [
            [row["id"], row["local_time"].isoformat(sep=" ", timespec="seconds"), row["cashier_name"],
             row["label"], row["refund_of"] or "", row["name"], row["description"], row["quantity"],
             dollars(row["list_cents"]), dollars(row["final_cents"])]
            for row in rows
        ]
    )

def payouts_csv(event):
    settlement = build_settlement(event)
    rows = [["From", "To", "Amount"]]
    rows += [[t["from"], t["to"], dollars(t["cents"])] for t in settlement["transfers"]]
    rows += [[], ["Seller", "Sold", "Given off", "Holding"]]
    rows += [[s["name"], dollars(s["sold_cents"]), dollars(s["discount_cents"]), dollars(s["held_cents"])]
             for s in settlement["sellers"]]
    rows += [["Cash box", "", "", dollars(settlement["cash_box_cents"])]]
    return to_csv(rows)

def event_files(event):
    files = [("sales.csv", sales_csv(event))]
    if event["mode"] == "yard_sale":
        files.append(("payouts.csv", payouts_csv(event)))
    return files

def shared_link(event):
    # Tied to this close, so reopening and closing again retires the old link
    token = shared_links.dumps([event["id"], iso(event["closed_at"])])
    return f"{PUBLIC_URL}/r/{token}"

def send_close_email(event):
    report = build_report(event)
    lines = [
        f'"{event["name"]}" is closed.',
        "",
        f'Total: ${dollars(report["overall"]["total_cents"])} from {report["overall"]["count"]} sales.',
    ]
    if event["mode"] == "yard_sale":
        transfers = build_settlement(event)["transfers"]
        lines += ["", "Who pays whom:"]
        lines += [f'  {t["from"]} pays {t["to"]} ${dollars(t["cents"])}' for t in transfers] or ["  Nothing to pay out."]
    purge = event["purge_after"].astimezone(ZoneInfo(event["timezone"])).strftime("%B %-d, %Y")
    lines += [
        "",
        "The full report is attached, and you can view it here:",
        shared_link(event),
        "",
        "Anyone with this link can see the report, so only share it with people you trust.",
        f"The link stops working on {purge}, when this event and all of its sales are",
        "permanently deleted. Save the attached files if you want to keep a copy.",
    ]
    mail.send(event["owner_email"], f'{event["name"]}: final report', "\n".join(lines), event_files(event))

def shared_event(token):
    """The closed event a report link is for, or None if the link isn't valid any more."""
    try:
        event_id, closed_at = shared_links.loads(token)
    except (BadSignature, ValueError, TypeError):
        return None
    with get_db_connection() as conn:
        event = load_event(conn, event_id)
    if (
        not event
        or event["status"] != "closed"
        or iso(event["closed_at"]) != closed_at
        or event["purge_after"] <= datetime.now(event["purge_after"].tzinfo)
    ):
        return None
    return event

def csv_response(text, filename):
    return Response(
        text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.route("/api/shared/<token>", methods=["GET"])
def get_shared_report(token):
    event = shared_event(token)
    if not event:
        return jsonify({"error": "This link has expired or isn't valid"}), 404
    public = {k: v for k, v in event_json(event).items() if k not in ("code", "owner_email")}
    return jsonify(
        {
            "event": public,
            "report": build_report(event),
            "settlement": build_settlement(event) if event["mode"] == "yard_sale" else None,
        }
    )

@app.route("/api/shared/<token>/<name>.csv", methods=["GET"])
def get_shared_csv(token, name):
    event = shared_event(token)
    files = dict(event_files(event)) if event else {}
    if f"{name}.csv" not in files:
        return jsonify({"error": "Not found"}), 404
    return csv_response(files[f"{name}.csv"], f"{name}.csv")

@app.route("/api/events/<int:event_id>/<name>.csv", methods=["GET"])
@event_route(manage=True)
def get_event_csv(event_id, name):
    files = dict(event_files(g.event))
    if f"{name}.csv" not in files:
        return jsonify({"error": "Not found"}), 404
    return csv_response(files[f"{name}.csv"], f"{name}.csv")

# ----- Accounts, invites and password resets -----

@app.route("/api/users", methods=["GET"])
def list_users():
    with get_db_connection() as conn:
        rows = conn.execute(
            """SELECT u.id, u.email, u.name, u.is_owner, u.created_at, count(e.id)::int AS events
               FROM users u LEFT JOIN events e ON e.owner_user_id = u.id
               GROUP BY u.id ORDER BY u.created_at"""
        ).fetchall()
    return jsonify([{**row, "created_at": iso(row["created_at"])} for row in rows])

@app.route("/api/invites", methods=["GET"])
def list_invites():
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, email, expires_at FROM invites WHERE expires_at > now() ORDER BY created_at"
        ).fetchall()
    return jsonify([{**row, "expires_at": iso(row["expires_at"])} for row in rows])

@app.route("/api/invites", methods=["POST"])
def create_invite():
    data = request.get_json(silent=True) or {}
    try:
        email = clean_email(data.get("email"))
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400

    token, token_hash = new_token()
    with get_db_connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
            return jsonify({"error": "That person already has an account"}), 409
        # A new invite replaces any earlier one to the same address
        conn.execute("DELETE FROM invites WHERE email = %s", (email,))
        invite = conn.execute(
            """INSERT INTO invites (email, token_hash, invited_by, expires_at)
               VALUES (%s, %s, %s, now() + %s) RETURNING id, email, expires_at""",
            (email, token_hash, g.user["user_id"], INVITE_LIFETIME),
        ).fetchone()
    mail.send(
        email,
        "You're invited to run sales on Sale POS",
        f"{g.user['name']} invited you to set up charity drives and yard sales on Sale POS.\n\n"
        f"Create your account here:\n{PUBLIC_URL}/invite/{token}\n\n"
        f"This link works once and expires in {INVITE_LIFETIME.days} days.",
    )
    return jsonify({**invite, "expires_at": iso(invite["expires_at"])}), 201

@app.route("/api/invites/<int:invite_id>", methods=["DELETE"])
def delete_invite(invite_id):
    with get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM invites WHERE id = %s", (invite_id,))
    if cursor.rowcount == 0:
        return jsonify({"error": "Invite not found"}), 404
    return "", 204

def find_invite(conn, token):
    return conn.execute(
        "SELECT * FROM invites WHERE token_hash = %s AND expires_at > now()", (hash_token(token),)
    ).fetchone()

@app.route("/api/invites/accept/<token>", methods=["GET"])
def get_invite(token):
    with get_db_connection() as conn:
        invite = find_invite(conn, token)
    if not invite:
        return jsonify({"error": "This invite has expired or was already used"}), 404
    return jsonify({"email": invite["email"]})

@app.route("/api/invites/accept/<token>", methods=["POST"])
def accept_invite(token):
    data = request.get_json(silent=True) or {}
    with get_db_connection() as conn:
        invite = find_invite(conn, token)
        if not invite:
            return jsonify({"error": "This invite has expired or was already used"}), 404
        try:
            name = clean_text(data.get("name"), "Name")
            password = clean_password(data.get("password"))
        except (ValueError, AttributeError) as e:
            return jsonify({"error": str(e)}), 400
        try:
            user = conn.execute(
                """INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s)
                   RETURNING id, password_hash""",
                (invite["email"], name, generate_password_hash(password)),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            return jsonify({"error": "That email already has an account"}), 409
        conn.execute("DELETE FROM invites WHERE email = %s", (invite["email"],))
    start_session("organizer", user=user)
    return jsonify({"authenticated": True, **current_user()}), 201

@app.route("/api/password-reset", methods=["POST"])
def request_password_reset():
    """Email a reset link. The answer is the same whether or not the email has an account."""
    data = request.get_json(silent=True) or {}
    email = data.get("email") if isinstance(data, dict) else None
    if isinstance(email, str):
        token, token_hash = new_token()
        with get_db_connection() as conn:
            user = conn.execute("SELECT id FROM users WHERE email = %s", (email.strip().lower(),)).fetchone()
            if user:
                conn.execute("DELETE FROM password_resets WHERE user_id = %s", (user["id"],))
                conn.execute(
                    "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (%s, %s, now() + %s)",
                    (user["id"], token_hash, RESET_LIFETIME),
                )
        if user:
            mail.send(
                email.strip().lower(),
                "Reset your Sale POS password",
                f"Someone asked to reset the password for this account.\n\n"
                f"Choose a new password here:\n{PUBLIC_URL}/reset/{token}\n\n"
                f"This link works once and expires in an hour. If it wasn't you, ignore this email.",
            )
    return jsonify({"message": "If that email has an account, a reset link is on its way."})

@app.route("/api/password-reset/<token>", methods=["POST"])
def reset_password(token):
    data = request.get_json(silent=True) or {}
    try:
        password = clean_password(data.get("password"))
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400
    with get_db_connection() as conn:
        reset = conn.execute(
            "DELETE FROM password_resets WHERE token_hash = %s AND expires_at > now() RETURNING user_id",
            (hash_token(token),),
        ).fetchone()
        if not reset:
            return jsonify({"error": "This link has expired or was already used"}), 400
        # Changing the hash also logs out every session that used the old password
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(password), reset["user_id"]),
        )
        conn.execute("DELETE FROM password_resets WHERE user_id = %s", (reset["user_id"],))
    return jsonify({"message": "Password changed. You can log in now."})

# Run once in the container to make your own account, or again to reset its password:
#   docker compose exec web flask --app app create-owner you@example.com
@app.cli.command("create-owner")
@click.argument("email")
@click.option("--name", help="Your name. Defaults to the start of the email address.")
def create_owner_command(email, name):
    """Create the site owner account, or set a new password for it."""
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        email = clean_email(email)
        password_hash = generate_password_hash(clean_password(password))
        name = clean_text(name or email.split("@")[0], "Name")
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    with get_db_connection() as conn:
        updated = conn.execute(
            "UPDATE users SET password_hash = %s, is_owner = TRUE WHERE email = %s", (password_hash, email)
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO users (email, name, password_hash, is_owner) VALUES (%s, %s, %s, TRUE)",
                (email, name, password_hash),
            )
    click.echo(f"{'Updated' if updated else 'Created'} site owner {email}.")

# Run daily (the purger service in docker-compose.yml does this)
@app.cli.command("purge-expired")
def purge_expired_command():
    """Close idle events, then delete events past their purge date and expired links."""
    with get_db_connection() as conn:
        idle = conn.execute(
            f"""SELECT e.id FROM events e
                WHERE e.status = 'open'
                  AND GREATEST(e.created_at,
                               (SELECT max(created_at) FROM sales WHERE event_id = e.id))
                      < now() - interval '{IDLE_CLOSE}'"""
        ).fetchall()
    for row in idle:
        close_and_notify(row["id"])
    with get_db_connection() as conn:
        deleted = conn.execute("DELETE FROM events WHERE purge_after < now() RETURNING id, name").fetchall()
        conn.execute("DELETE FROM invites WHERE expires_at < now()")
        conn.execute("DELETE FROM password_resets WHERE expires_at < now()")
        conn.execute(
            "DELETE FROM login_failures WHERE NOT banned AND last_failure < %s",
            (time.time() - FAILURE_WINDOW_SECONDS,),
        )
    for event in deleted:
        logger.info("Purged event %s (%s)", event["id"], event["name"])
    click.echo(f"Closed {len(idle)} idle event(s). Deleted {len(deleted)} event(s).")

# Serve frontend app (if built) for SPA routes
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if app.static_folder and (path == "" or not os.path.exists(os.path.join(app.static_folder, path))):
        return send_from_directory(app.static_folder, "index.html")
    return send_from_directory(app.static_folder, path)

# Links handed out to people: a cashier's login, an invite, a password reset and a
# shared report. Flask's static route would otherwise answer them with a 404.
@app.route("/e/<token>")
@app.route("/invite/<token>")
@app.route("/reset/<token>")
@app.route("/r/<token>")
def frontend_link(token):
    return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    # Only used when running directly with python app.py
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
