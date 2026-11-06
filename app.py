import hashlib
import hmac
import json
import math
import sqlite3
import os
import re
import logging
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from flask import Flask, g, jsonify, request, send_from_directory, session

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("church-pos")

# Serve built frontend from frontend_build if present
static_dir = os.environ.get("FLASK_STATIC_DIR", "frontend_build")
app = Flask(__name__, static_folder=static_dir, static_url_path="/")

# Refuse to start without credentials rather than run with the API open
ADMIN_CODE = os.environ.get("ADMIN_CODE")
SECRET_KEY = os.environ.get("SECRET_KEY")
if not ADMIN_CODE or not SECRET_KEY:
    raise RuntimeError("ADMIN_CODE and SECRET_KEY environment variables must be set")

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=true when serving over HTTPS
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

PUBLIC_API_PATHS = {"/api/login", "/api/session", "/api/health"}

# Failed login tracking, kept per process (the container runs a single worker)
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
login_failures = {}
login_failures_lock = threading.Lock()

# Database file path (default to data dir for a container)
DB_FILE = os.environ.get("DB_FILE", "church_pos.db")

# In-memory items
ITEMS = [
    {"id": 1, "name": "Nuts", "department": "Food"},
    {"id": 2, "name": "Rada", "department": "Misc"},
    {"id": 3, "name": "Baked Goods", "department": "Food"},
    {"id": 4, "name": "Honey", "department": "Food"},
    {"id": 5, "name": "Jam", "department": "Food"},
    {"id": 6, "name": "Mom & Pop Shop", "department": "Misc"},
    {"id": 7, "name": "Danny Duzits", "department": "Misc"},
    {"id": 8, "name": "Crafts", "department": "Misc"},
    {"id": 9, "name": "Donation", "department": "Misc"},
]

VALID_TENDERS = ["cash", "check", "venmo"]

def init_db():
    try:
        # Ensure directory for DB exists
        db_dir = os.path.dirname(DB_FILE)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                tender TEXT NOT NULL,
                total REAL NOT NULL,
                user TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transaction_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY (transaction_id) REFERENCES transactions(id)
            )
        """)
        # AUTOINCREMENT so a removed cashier's id, and any session using it, is never reused
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cashiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pin_hash TEXT NOT NULL UNIQUE
            )
        """)
        # A refund is its own transaction with negative amounts, linked to the original sale.
        # The unique index means a sale can only ever be refunded once.
        columns = [row[1] for row in cursor.execute("PRAGMA table_info(transactions)")]
        if "refund_of" not in columns:
            cursor.execute("ALTER TABLE transactions ADD COLUMN refund_of INTEGER REFERENCES transactions(id)")
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_refund_of
            ON transactions(refund_of) WHERE refund_of IS NOT NULL
        """)
        conn.commit()
        conn.close()
        logger.info("Initialized SQLite DB at %s", DB_FILE)
    except Exception:
        logger.exception("Failed to initialize DB at %s", DB_FILE)
        raise

def get_db_connection():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except Exception:
        logger.exception("Failed to get DB connection for %s", DB_FILE)
        raise

# Initialize DB at import time so it's ready under gunicorn.
# If this fails, let it bubble so the process fails fast and logs show the problem.
init_db()

def is_locked_out(ip):
    with login_failures_lock:
        failure = login_failures.get(ip)
        if not failure:
            return False
        count, last_failure = failure
        if time.monotonic() - last_failure >= LOGIN_LOCKOUT_SECONDS:
            del login_failures[ip]
            return False
        return count >= MAX_LOGIN_FAILURES

def record_login_failure(ip):
    with login_failures_lock:
        count, _ = login_failures.get(ip, (0, 0))
        login_failures[ip] = (count + 1, time.monotonic())

def hash_pin(pin):
    # Keyed so PINs can be looked up directly but can't be guessed from a copy of the database.
    # Changing SECRET_KEY invalidates every PIN.
    return hmac.new(SECRET_KEY.encode(), pin.encode(), hashlib.sha256).hexdigest()

def current_user():
    """Return the logged in user as {"role", "name"}, or None."""
    role = session.get("role")
    if role == "admin":
        return {"role": "admin", "name": "Admin"}
    if role == "cashier":
        with closing(get_db_connection()) as conn:
            cashier = conn.execute(
                "SELECT name FROM cashiers WHERE id = ?", (session.get("cashier_id"),)
            ).fetchone()
        # A removed cashier is logged out on their next request
        if cashier:
            return {"role": "cashier", "name": cashier["name"]}
        session.clear()
    return None

@app.before_request
def require_login():
    if not request.path.startswith("/api/") or request.path in PUBLIC_API_PATHS:
        return None

    g.user = current_user()
    if not g.user:
        return jsonify({"error": "Authentication required"}), 401
    if request.path.startswith("/api/cashiers") and g.user["role"] != "admin":
        return jsonify({"error": "Admin access required"}), 403
    if request.path == "/api/transactions" and request.method == "POST" and g.user["role"] != "cashier":
        return jsonify({"error": "Log in as a cashier to record sales"}), 403
    return None

@app.route("/api/health", methods=["GET"])
def health():
    try:
        with closing(get_db_connection()) as conn:
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
    ip = request.remote_addr
    if is_locked_out(ip):
        return jsonify({"error": "Too many attempts. Try again in a minute."}), 429

    data = request.get_json(silent=True)
    code = data.get("code") if isinstance(data, dict) else None

    role = None
    cashier = None
    if isinstance(code, str):
        if hmac.compare_digest(code.encode(), ADMIN_CODE.encode()):
            role = "admin"
        else:
            with closing(get_db_connection()) as conn:
                cashier = conn.execute(
                    "SELECT id FROM cashiers WHERE pin_hash = ?", (hash_pin(code),)
                ).fetchone()
            if cashier:
                role = "cashier"

    if not role:
        record_login_failure(ip)
        return jsonify({"error": "Incorrect code"}), 401

    with login_failures_lock:
        login_failures.pop(ip, None)
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

def clean_cashier_name(name):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 50:
        raise ValueError("Name must be 1 to 50 characters")
    return name.strip()

def clean_cashier_pin(pin):
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,8}", pin):
        raise ValueError("PIN must be 4 to 8 digits")
    if hmac.compare_digest(pin.encode(), ADMIN_CODE.encode()):
        raise ValueError("That PIN is not available")
    return pin

def cashier_conflict_message(error):
    if "cashiers.name" in str(error):
        return "A cashier with that name already exists"
    return "That PIN is not available"

@app.route("/api/cashiers", methods=["GET"])
def get_cashiers():
    with closing(get_db_connection()) as conn:
        rows = conn.execute("SELECT id, name FROM cashiers ORDER BY name").fetchall()
    return jsonify([{"id": row["id"], "name": row["name"]} for row in rows])

@app.route("/api/cashiers", methods=["POST"])
def create_cashier():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "No data provided"}), 400
    try:
        name = clean_cashier_name(data.get("name"))
        pin = clean_cashier_pin(data.get("pin"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        with closing(get_db_connection()) as conn:
            cursor = conn.execute(
                "INSERT INTO cashiers (name, pin_hash) VALUES (?, ?)", (name, hash_pin(pin))
            )
            conn.commit()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": cashier_conflict_message(e)}), 409
    return jsonify({"id": cursor.lastrowid, "name": name}), 201

@app.route("/api/cashiers/<int:cashier_id>", methods=["PUT"])
def update_cashier(cashier_id):
    """Update a cashier's name and/or PIN. Fields left out stay the same."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "No data provided"}), 400

    updates = {}
    try:
        if "name" in data:
            updates["name"] = clean_cashier_name(data["name"])
        if "pin" in data:
            updates["pin_hash"] = hash_pin(clean_cashier_pin(data["pin"]))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    # Column names come from the fixed keys above, never from the request
    assignments = ", ".join(f"{column} = ?" for column in updates)
    try:
        with closing(get_db_connection()) as conn:
            cursor = conn.execute(
                f"UPDATE cashiers SET {assignments} WHERE id = ?",
                (*updates.values(), cashier_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return jsonify({"error": "Cashier not found"}), 404
            row = conn.execute("SELECT id, name FROM cashiers WHERE id = ?", (cashier_id,)).fetchone()
    except sqlite3.IntegrityError as e:
        return jsonify({"error": cashier_conflict_message(e)}), 409
    return jsonify({"id": row["id"], "name": row["name"]})

@app.route("/api/cashiers/<int:cashier_id>", methods=["DELETE"])
def delete_cashier(cashier_id):
    # Past sales keep the cashier's name, which is stored on each transaction
    with closing(get_db_connection()) as conn:
        cursor = conn.execute("DELETE FROM cashiers WHERE id = ?", (cashier_id,))
        conn.commit()
    if cursor.rowcount == 0:
        return jsonify({"error": "Cashier not found"}), 404
    return "", 204

@app.route("/api/items", methods=["GET"])
def get_items():
    return jsonify(ITEMS)

def validate_line_items(raw_items):
    """Return clean line items, or raise ValueError so bad sales never reach the reports."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Transaction must contain at least one item")

    line_items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Invalid item")
        item_id = raw.get("id")
        found_item = next((i for i in ITEMS if i["id"] == item_id), None)
        if not found_item:
            raise ValueError(f"Unknown item: {item_id}")

        quantity = raw.get("quantity", 1)
        if type(quantity) is not int or quantity < 1:
            raise ValueError(f"Invalid quantity for {found_item['name']}")

        price = raw.get("price")
        if type(price) not in (int, float) or not math.isfinite(price) or round(price, 2) <= 0:
            raise ValueError(f"Invalid price for {found_item['name']}")

        line_items.append(
            {"id": found_item["id"], "name": found_item["name"], "quantity": quantity, "price": round(price, 2)}
        )
    return line_items

@app.route("/api/transactions", methods=["POST"])
def create_transaction():
    try:
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "No data provided"}), 400
        if "items" not in data or "tender" not in data:
            return jsonify({"error": "Missing required fields: items, tender"}), 400
        if data["tender"] not in VALID_TENDERS:
            return jsonify({"error": f"Invalid tender. Must be one of: {', '.join(VALID_TENDERS)}"}), 400

        try:
            line_items = validate_line_items(data["items"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        total = sum(item["price"] * item["quantity"] for item in line_items)

        timestamp = datetime.now().isoformat()
        tender = data["tender"]
        total = round(total, 2)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transactions (timestamp, tender, total, user) VALUES (?, ?, ?, ?)",
            (timestamp, tender, total, g.user["name"]),
        )
        transaction_id = cursor.lastrowid

        for item in line_items:
            cursor.execute(
                """INSERT INTO transaction_items
                   (transaction_id, item_id, item_name, quantity, price)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    transaction_id,
                    item["id"],
                    item["name"],
                    item["quantity"],
                    item["price"],
                ),
            )

        conn.commit()
        conn.close()

        transaction = {
            "id": transaction_id,
            "timestamp": timestamp,
            "items": line_items,
            "tender": tender,
            "total": total,
        }
        return jsonify(transaction), 201

    except Exception:
        logger.exception("Error creating transaction")
        return jsonify({"error": "Internal server error while creating transaction"}), 500

@app.route("/api/transactions/<int:transaction_id>/refund", methods=["POST"])
def refund_transaction(transaction_id):
    """Refund a whole sale by recording a negative transaction against it."""
    try:
        with closing(get_db_connection()) as conn:
            original = conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
            ).fetchone()
            if not original:
                return jsonify({"error": "Transaction not found"}), 404
            if original["refund_of"] is not None:
                return jsonify({"error": "A refund cannot be refunded"}), 400

            timestamp = datetime.now().isoformat()
            total = -original["total"]
            try:
                cursor = conn.execute(
                    """INSERT INTO transactions (timestamp, tender, total, user, refund_of)
                       VALUES (?, ?, ?, ?, ?)""",
                    (timestamp, original["tender"], total, g.user["name"], transaction_id),
                )
            except sqlite3.IntegrityError:
                return jsonify({"error": "This sale has already been refunded"}), 409
            refund_id = cursor.lastrowid
            conn.execute(
                """INSERT INTO transaction_items
                   (transaction_id, item_id, item_name, quantity, price)
                   SELECT ?, item_id, item_name, -quantity, price
                   FROM transaction_items WHERE transaction_id = ?""",
                (refund_id, transaction_id),
            )
            conn.commit()

        return jsonify(
            {
                "id": refund_id,
                "timestamp": timestamp,
                "tender": original["tender"],
                "total": total,
                "cashier": g.user["name"],
                "refund_of": transaction_id,
            }
        ), 201
    except Exception:
        logger.exception("Error refunding transaction %s", transaction_id)
        return jsonify({"error": "Internal server error while refunding transaction"}), 500

def date_range_filter():
    """Read optional start/end args. Returns (start, end, where_sql, params) for the alias t."""
    try:
        start = parse_report_date(request.args.get("start"))
        end = parse_report_date(request.args.get("end"))
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format") from None
    if start and end and start > end:
        raise ValueError("Start date must not be after end date")

    # Timestamps are local ISO strings, so the first 10 characters are the date
    where = []
    params = []
    if start:
        where.append("substr(t.timestamp, 1, 10) >= ?")
        params.append(start)
    if end:
        where.append("substr(t.timestamp, 1, 10) <= ?")
        params.append(end)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return start, end, where_sql, params

@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    try:
        _, _, where_sql, params = date_range_filter()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT t.*, r.id AS refund_id, r.timestamp AS refund_timestamp, r.user AS refund_cashier
                FROM transactions t
                LEFT JOIN transactions r ON r.refund_of = t.id
                {where_sql}
                ORDER BY t.id""",
            params,
        )
        rows = cursor.fetchall()
        transactions = []
        for row in rows:
            cursor.execute(
                """SELECT item_id, item_name, quantity, price
                   FROM transaction_items
                   WHERE transaction_id = ?""",
                (row["id"],),
            )
            items = cursor.fetchall()
            transactions.append(
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "items": [
                        {
                            "id": item["item_id"],
                            "name": item["item_name"],
                            "quantity": item["quantity"],
                            "price": item["price"],
                        }
                        for item in items
                    ],
                    "tender": row["tender"],
                    "total": row["total"],
                    "cashier": row["user"],
                    "refund_of": row["refund_of"],
                    "refund_id": row["refund_id"],
                    "refund_timestamp": row["refund_timestamp"],
                    "refund_cashier": row["refund_cashier"],
                }
            )
        conn.close()
        return jsonify(transactions)
    except Exception:
        logger.exception("Error fetching transactions")
        return jsonify({"error": "Internal server error while fetching transactions"}), 500

@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        stats = {
            "cash": {"count": 0, "total": 0},
            "check": {"count": 0, "total": 0},
            "venmo": {"count": 0, "total": 0},
        }

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT tender, total, refund_of FROM transactions")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            tender = row["tender"]
            if tender not in stats:
                # Add unexpected tender types safely
                stats[tender] = {"count": 0, "total": 0}
            stats[tender]["count"] += -1 if row["refund_of"] else 1
            stats[tender]["total"] += row["total"]

        overall_total = sum(stats[t]["total"] for t in stats)
        overall_count = sum(stats[t]["count"] for t in stats)

        return jsonify(
            {"by_tender": stats, "overall": {"count": overall_count, "total": round(overall_total, 2)}}
        )
    except Exception:
        logger.exception("Error computing stats")
        return jsonify({"error": "Internal server error while computing stats"}), 500

@app.route("/api/item-sales", methods=["GET"])
def get_item_sales():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                item_id,
                item_name,
                SUM(quantity) as total_quantity,
                SUM(quantity * price) as total_amount
            FROM transaction_items
            GROUP BY item_id, item_name
            ORDER BY total_amount DESC
        """)
        rows = cursor.fetchall()
        conn.close()

        item_sales = [
            {
                "id": row["item_id"],
                "name": row["item_name"],
                "total_quantity": row["total_quantity"],
                "total_amount": round(row["total_amount"], 2),
            }
            for row in rows
        ]

        return jsonify(item_sales)
    except Exception:
        logger.exception("Error fetching item sales")
        return jsonify({"error": "Internal server error while fetching item sales"}), 500

def parse_report_date(value):
    """Return a YYYY-MM-DD string, or None if no date was given."""
    if not value:
        return None
    return date.fromisoformat(value).isoformat()

@app.route("/api/reports/summary", methods=["GET"])
def get_report_summary():
    """Daily multi-totals and single-item totals, optionally limited to start..end (inclusive)."""
    try:
        start, end, where_sql, params = date_range_filter()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        # Refunds are negative transactions, so summed amounts and quantities are already
        # net of them. Counts subtract a refund, and refunds are also reported separately.
        net_count = "SUM(CASE WHEN t.refund_of IS NULL THEN 1 ELSE -1 END)"
        refund_count = "SUM(CASE WHEN t.refund_of IS NULL THEN 0 ELSE 1 END)"
        refund_total = "SUM(CASE WHEN t.refund_of IS NULL THEN 0 ELSE t.total END)"

        with closing(get_db_connection()) as conn:
            tender_rows = conn.execute(
                f"""SELECT substr(t.timestamp, 1, 10) AS sale_date, t.tender,
                           {net_count} AS count, SUM(t.total) AS total,
                           {refund_count} AS refund_count, {refund_total} AS refund_total
                    FROM transactions t {where_sql}
                    GROUP BY sale_date, t.tender
                    ORDER BY sale_date""",
                params,
            ).fetchall()
            cashier_rows = conn.execute(
                f"""SELECT t.user AS cashier, {net_count} AS count, SUM(t.total) AS total,
                           {refund_total} AS refund_total
                    FROM transactions t {where_sql}
                    GROUP BY t.user
                    ORDER BY total DESC""",
                params,
            ).fetchall()
            item_rows = conn.execute(
                f"""SELECT ti.item_id, ti.item_name,
                           SUM(ti.quantity) AS quantity,
                           SUM(ti.quantity * ti.price) AS total
                    FROM transaction_items ti
                    JOIN transactions t ON t.id = ti.transaction_id
                    {where_sql}
                    GROUP BY ti.item_id, ti.item_name
                    ORDER BY total DESC""",
                params,
            ).fetchall()

        by_tender = {tender: {"count": 0, "total": 0} for tender in VALID_TENDERS}
        by_day = {}
        refunds = {"count": 0, "total": 0}
        for row in tender_rows:
            refunds["count"] += row["refund_count"]
            refunds["total"] += row["refund_total"]
            tender = by_tender.setdefault(row["tender"], {"count": 0, "total": 0})
            tender["count"] += row["count"]
            tender["total"] += row["total"]

            day = by_day.setdefault(
                row["sale_date"],
                {
                    "date": row["sale_date"],
                    "count": 0,
                    "total": 0,
                    "refunds": 0,
                    "tenders": {t: 0 for t in VALID_TENDERS},
                },
            )
            day["count"] += row["count"]
            day["total"] += row["total"]
            day["refunds"] += row["refund_total"]
            day["tenders"][row["tender"]] = day["tenders"].get(row["tender"], 0) + row["total"]

        for tender in by_tender.values():
            tender["total"] = round(tender["total"], 2)
        for day in by_day.values():
            day["total"] = round(day["total"], 2)
            day["refunds"] = round(day["refunds"], 2)
            day["tenders"] = {t: round(amount, 2) for t, amount in day["tenders"].items()}

        departments = {item["id"]: item["department"] for item in ITEMS}
        items = []
        by_department = {}
        for row in item_rows:
            department = departments.get(row["item_id"], "Other")
            items.append(
                {
                    "id": row["item_id"],
                    "name": row["item_name"],
                    "department": department,
                    "quantity": row["quantity"],
                    "total": round(row["total"], 2),
                }
            )
            entry = by_department.setdefault(
                department, {"department": department, "quantity": 0, "total": 0}
            )
            entry["quantity"] += row["quantity"]
            entry["total"] += row["total"]
        for entry in by_department.values():
            entry["total"] = round(entry["total"], 2)

        return jsonify(
            {
                "start": start,
                "end": end,
                "overall": {
                    "count": sum(t["count"] for t in by_tender.values()),
                    "total": round(sum(t["total"] for t in by_tender.values()), 2),
                },
                "by_tender": by_tender,
                "refunds": {"count": refunds["count"], "total": round(refunds["total"], 2)},
                "by_day": list(by_day.values()),
                "by_cashier": [
                    {
                        "cashier": row["cashier"],
                        "count": row["count"],
                        "refunds": round(row["refund_total"], 2),
                        "total": round(row["total"], 2),
                    }
                    for row in cashier_rows
                ],
                "by_department": sorted(by_department.values(), key=lambda d: -d["total"]),
                "items": items,
            }
        )
    except Exception:
        logger.exception("Error building report")
        return jsonify({"error": "Internal server error while building report"}), 500

# Serve frontend app (if built) for SPA routes
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if app.static_folder and (path == "" or not os.path.exists(os.path.join(app.static_folder, path))):
        return send_from_directory(app.static_folder, "index.html")
    return send_from_directory(app.static_folder, path)

if __name__ == "__main__":
    # Only used when running directly with python app.py
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)