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

import click
import psycopg
from flask import Flask, g, jsonify, request, send_from_directory, session
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from pos.money import CASH_BOX, allocate_discount, settle

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sale-pos")

# Serve built frontend from frontend_build if present
static_dir = os.environ.get("FLASK_STATIC_DIR", "frontend_build")
app = Flask(__name__, static_folder=static_dir, static_url_path="/")

# Refuse to start without credentials rather than run with the API open
ADMIN_CODE = os.environ.get("ADMIN_CODE")
SECRET_KEY = os.environ.get("SECRET_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
if not ADMIN_CODE or not SECRET_KEY or not DATABASE_URL:
    raise RuntimeError("ADMIN_CODE, SECRET_KEY and DATABASE_URL environment variables must be set")
if not re.fullmatch(r"[A-Za-z0-9]{8,32}", ADMIN_CODE):
    raise RuntimeError("ADMIN_CODE must be 8 to 32 letters or digits")

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

PUBLIC_API_PATHS = {"/api/login", "/api/session", "/api/health"}

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

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

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

def current_user():
    """Return the logged in user as {"role", "name"} (plus "event_id" for a cashier), or None."""
    role = session.get("role")
    if role == "admin":
        return {"role": "admin", "name": "Admin"}
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
    if not request.path.startswith("/api/") or request.path in PUBLIC_API_PATHS:
        return None

    g.user = current_user()
    if not g.user:
        return jsonify({"error": "Authentication required"}), 401
    if request.path.startswith("/api/login-blocks") and g.user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    return None

def admin_only(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user["role"] != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return view(*args, **kwargs)
    return wrapper

def event_route(admin=False):
    """Load the event in the URL into g.event, if this user may see it.

    A cashier only ever sees their own event. Any other event looks like it doesn't
    exist, so cashiers can't find out which events are on the site.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(event_id, *args, **kwargs):
            if g.user["role"] == "cashier" and g.user["event_id"] != event_id:
                return jsonify({"error": "Event not found"}), 404
            if admin and g.user["role"] != "admin":
                return jsonify({"error": "Admin access required"}), 403
            with get_db_connection() as conn:
                g.event = conn.execute("SELECT * FROM events WHERE id = %s", (event_id,)).fetchone()
            if not g.event:
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

@app.route("/api/login", methods=["POST"])
def login():
    """Log in with the admin code, or with an event code and a cashier PIN."""
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

    role = None
    cashier = None
    if isinstance(code, str):
        if hmac.compare_digest(code.encode(), ADMIN_CODE.encode()):
            role = "admin"
        elif isinstance(event_code, str):
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
    session.clear()
    session.permanent = True
    session["role"] = role
    if cashier:
        session["cashier_id"] = cashier["id"]
    user = current_user()
    return jsonify({"authenticated": True, **user})

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
    }

@app.route("/api/events", methods=["GET"])
def list_events():
    with get_db_connection() as conn:
        if g.user["role"] == "admin":
            rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM events WHERE id = %s", (g.user["event_id"],)).fetchall()
    return jsonify([event_json(row) for row in rows])

@app.route("/api/events", methods=["POST"])
@admin_only
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
        event = conn.execute(
            "INSERT INTO events (code, name, mode, timezone) VALUES (%s, %s, %s, %s) RETURNING *",
            (new_event_code(conn), name, data["mode"], timezone),
        ).fetchone()
        for label, kind in DEFAULT_PAYMENT_METHODS[data["mode"]]:
            conn.execute(
                "INSERT INTO payment_methods (event_id, label, kind) VALUES (%s, %s, %s)",
                (event["id"], label, kind),
            )
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
@event_route(admin=True)
def update_event(event_id):
    data = request.get_json(silent=True) or {}
    try:
        name = clean_text(data.get("name", g.event["name"]), "Name", 80)
        timezone = clean_timezone(data.get("timezone", g.event["timezone"]))
    except (ValueError, AttributeError) as e:
        return jsonify({"error": str(e)}), 400
    with get_db_connection() as conn:
        event = conn.execute(
            "UPDATE events SET name = %s, timezone = %s WHERE id = %s RETURNING *",
            (name, timezone, event_id),
        ).fetchone()
    return jsonify(event_json(event))

@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@event_route(admin=True)
def delete_event(event_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM events WHERE id = %s", (event_id,))
    logger.warning("Admin deleted event %s (%s)", event_id, g.event["name"])
    return "", 204

@app.route("/api/events/<int:event_id>/close", methods=["POST"])
@event_route(admin=True)
def close_event(event_id):
    with get_db_connection() as conn:
        event = conn.execute(
            """UPDATE events SET status = 'closed', closed_at = COALESCE(closed_at, now())
               WHERE id = %s RETURNING *""",
            (event_id,),
        ).fetchone()
    return jsonify(event_json(event))

@app.route("/api/events/<int:event_id>/reopen", methods=["POST"])
@event_route(admin=True)
def reopen_event(event_id):
    with get_db_connection() as conn:
        event = conn.execute(
            "UPDATE events SET status = 'open', closed_at = NULL WHERE id = %s RETURNING *",
            (event_id,),
        ).fetchone()
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
@event_route(admin=True)
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
@event_route(admin=True)
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
@event_route(admin=True)
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
    if hmac.compare_digest(pin.encode(), ADMIN_CODE.encode()):
        raise ValueError("That PIN is not available")
    return pin

def cashier_conflict_message(error):
    if error.diag.constraint_name == "idx_cashiers_event_name":
        return "A cashier with that name already exists"
    return "That PIN is not available"

@app.route("/api/events/<int:event_id>/cashiers", methods=["GET"])
@event_route(admin=True)
def get_cashiers(event_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, name FROM cashiers WHERE event_id = %s ORDER BY name", (event_id,)
        ).fetchall()
    return jsonify(rows)

@app.route("/api/events/<int:event_id>/cashiers", methods=["POST"])
@event_route(admin=True)
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
@event_route(admin=True)
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
@event_route(admin=True)
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

def date_range_filter():
    """Read optional start/end args. Returns (start, end, where_sql, params) for the alias s.

    Days are counted in the event's time zone.
    """
    try:
        start = parse_report_date(request.args.get("start"))
        end = parse_report_date(request.args.get("end"))
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format") from None
    if start and end and start > end:
        raise ValueError("Start date must not be after end date")

    where = ["s.event_id = %s"]
    params = [g.event["id"]]
    if start:
        where.append("(s.created_at AT TIME ZONE %s)::date >= %s")
        params += [g.event["timezone"], start]
    if end:
        where.append("(s.created_at AT TIME ZONE %s)::date <= %s")
        params += [g.event["timezone"], end]
    return start, end, f"WHERE {' AND '.join(where)}", params

@app.route("/api/events/<int:event_id>/sales", methods=["GET"])
@event_route()
def list_sales(event_id):
    try:
        _, _, where_sql, params = date_range_filter()
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
    """Totals by day, payment method, cashier, and item or seller, optionally for start..end."""
    try:
        start, end, where_sql, params = date_range_filter()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

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
            [g.event["timezone"], *params],
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

    return jsonify(
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
            if g.event["mode"] == "charity" else [],
        }
    )

@app.route("/api/events/<int:event_id>/settlement", methods=["GET"])
@event_route()
def get_settlement(event_id):
    """What each seller sold, who is holding the money, and who pays whom at the end."""
    if g.event["mode"] != "yard_sale":
        return jsonify({"error": "Settlement is only for yard sales"}), 400

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
    return jsonify(
        {
            "status": g.event["status"],
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

# Serve frontend app (if built) for SPA routes
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if app.static_folder and (path == "" or not os.path.exists(os.path.join(app.static_folder, path))):
        return send_from_directory(app.static_folder, "index.html")
    return send_from_directory(app.static_folder, path)

# A cashier's login link. Flask's static route would otherwise answer it with a 404.
@app.route("/e/<code>")
def event_link(code):
    return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    # Only used when running directly with python app.py
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
