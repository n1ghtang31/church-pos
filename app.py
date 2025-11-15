# app.py
# This version supports either SQLite (default) or Postgres if DATABASE_URL is set.
import os
import logging
from datetime import datetime
import urllib.parse as up
import socket
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# try to import psycopg2; only used when DATABASE_URL is provided
try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None

import sqlite3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("church-pos")

static_dir = os.environ.get("FLASK_STATIC_DIR", "frontend_build")
app = Flask(__name__, static_folder=static_dir, static_url_path="/")
CORS(app)

# Config
DB_FILE = os.environ.get("DB_FILE", "/data/church_pos.db")
DATABASE_URL = os.environ.get("DATABASE_URL")  # Supabase/Managed Postgres URL (if present)

ITEMS = [
    {"id": 1, "name": "Nuts", "department": "Food"},
    {"id": 2, "name": "Rada", "department": "Misc"},
    {"id": 3, "name": "Baked Goods", "department": "Food"},
    {"id": 4, "name": "Honey", "department": "Food"},
    {"id": 5, "name": "Jam", "department": "Food"},
    {"id": 6, "name": "Mom & Pop Shop", "department": "Misc"},
    {"id": 7, "name": "Danny Duzits", "department": "Misc"},
    {"id": 8, "name": "Crafts", "department": "Misc"},
]

# Helper functions for IPv4-first Postgres connection
def parse_db_url_to_params(url):
    """
    Parse a DATABASE_URL (postgres://user:pass@host:port/dbname) into connection parameters.
    Returns dict with keys: host, port, dbname, user, password.
    """
    parsed = up.urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") if parsed.path else "postgres",
        "user": parsed.username or "",
        "password": parsed.password or "",
    }

def resolve_ipv4(host, port=5432):
    """
    Resolve hostname to an IPv4 address. Returns the first IPv4 address found,
    or the original host if none is available.
    """
    try:
        # Get address info for the host, filtering for IPv4 (AF_INET)
        addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if addr_info:
            ipv4_address = addr_info[0][4][0]
            logger.info("Resolved %s to IPv4 address: %s", host, ipv4_address)
            return ipv4_address
    except socket.gaierror as e:
        logger.warning("Could not resolve %s to IPv4: %s. Using original host.", host, e)
    except Exception as e:
        logger.warning("Error resolving %s to IPv4: %s. Using original host.", host, e)
    return host

# Database helpers: choose backend depending on DATABASE_URL
def init_sqlite():
    try:
        os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                tender TEXT NOT NULL,
                total REAL NOT NULL,
                "user" TEXT NOT NULL
            )
        """)
        cur.execute("""
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
        conn.commit()
        conn.close()
        logger.info("Initialized SQLite DB at %s", DB_FILE)
    except Exception:
        logger.exception("Failed to initialize SQLite DB")
        raise

def get_sqlite_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_postgres():
    """
    Initialize Postgres DB with retry logic for transient network issues.
    Attempts connection up to 3 times with exponential backoff.
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed but DATABASE_URL is set")
    
    # Parse DATABASE_URL to extract connection parameters
    params = parse_db_url_to_params(DATABASE_URL)
    
    # Resolve hostname to IPv4 address
    ipv4_host = resolve_ipv4(params["host"], params["port"])
    
    # Retry logic: 3 attempts with exponential backoff
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("Initializing Postgres DB (attempt %d/%d) using host %s (resolved from %s)", 
                       attempt, max_attempts, ipv4_host, params["host"])
            
            # Connect using the IPv4 address
            conn = psycopg2.connect(
                host=ipv4_host,
                port=params["port"],
                dbname=params["dbname"],
                user=params["user"],
                password=params["password"],
                sslmode="require"
            )
            cur = conn.cursor()
            
            # Create tables
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    tender TEXT NOT NULL,
                    total REAL NOT NULL,
                    "user" TEXT NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transaction_items (
                    id SERIAL PRIMARY KEY,
                    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL
                );
            """)
            conn.commit()
            cur.close()
            conn.close()
            
            logger.info("Successfully initialized Postgres DB from %s (using host %s)", 
                       DATABASE_URL, ipv4_host)
            return  # Success, exit the function
            
        except Exception as e:
            logger.warning("Attempt %d/%d to initialize Postgres failed: %s", 
                          attempt, max_attempts, str(e))
            
            if attempt < max_attempts:
                # Exponential backoff: 2^(attempt-1) seconds
                wait_time = 2 ** (attempt - 1)
                logger.info("Retrying in %d seconds...", wait_time)
                time.sleep(wait_time)
            else:
                # Final attempt failed, raise the exception
                logger.error("Failed to initialize Postgres DB after %d attempts", max_attempts)
                raise

def get_postgres_conn():
    """
    Connect to Postgres using IPv4-resolved host from DATABASE_URL.
    Returns (connection, cursor) tuple with RealDictCursor.
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed but DATABASE_URL is set")
    
    # Parse DATABASE_URL to extract connection parameters
    params = parse_db_url_to_params(DATABASE_URL)
    
    # Resolve hostname to IPv4 address
    ipv4_host = resolve_ipv4(params["host"], params["port"])
    
    # Connect using the IPv4 address
    conn = psycopg2.connect(
        host=ipv4_host,
        port=params["port"],
        dbname=params["dbname"],
        user=params["user"],
        password=params["password"],
        sslmode="require"
    )
    
    # Return connection and dict-like cursor
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# choose backend at startup
USING_POSTGRES = bool(DATABASE_URL)

try:
    if USING_POSTGRES:
        init_postgres()
    else:
        init_sqlite()
except Exception:
    logger.exception("DB initialization failed")

# API endpoints (use appropriate DB APIs depending on backend)
@app.route("/api/items", methods=["GET"])
def get_items():
    return jsonify(ITEMS)

@app.route("/api/transactions", methods=["POST"])
def create_transaction():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        if "items" not in data or "tender" not in data:
            return jsonify({"error": "Missing required fields: items, tender"}), 400
        valid_tenders = ["cash", "check", "venmo"]
        if data["tender"] not in valid_tenders:
            return jsonify({"error": f"Invalid tender. Must be one of: {', '.join(valid_tenders)}"}), 400

        total = 0
        for item in data["items"]:
            quantity = item.get("quantity", 1)
            price = item.get("price", 0)
            total += price * quantity
        timestamp = datetime.now().isoformat()
        tender = data["tender"]
        total = round(total, 2)

        if USING_POSTGRES:
            conn, cur = get_postgres_conn()
            cur.execute(
                'INSERT INTO transactions (timestamp, tender, total, "user") VALUES (%s, %s, %s, %s) RETURNING id;',
                (timestamp, tender, total, "default_user"),
            )
            transaction_id = cur.fetchone()["id"]
            # Insert items
            for item in data["items"]:
                cur.execute(
                    'INSERT INTO transaction_items (transaction_id, item_id, item_name, quantity, price) VALUES (%s, %s, %s, %s, %s);',
                    (transaction_id, item.get("id"), item.get("name"), item.get("quantity", 1), item.get("price", 0)),
                )
            conn.commit()
            cur.close()
            conn.close()
        else:
            conn = get_sqlite_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO transactions (timestamp, tender, total, user) VALUES (?, ?, ?, ?)",
                (timestamp, tender, total, "default_user"),
            )
            transaction_id = cur.lastrowid
            for item in data["items"]:
                cur.execute(
                    """INSERT INTO transaction_items
                       (transaction_id, item_id, item_name, quantity, price)
                       VALUES (?, ?, ?, ?, ?)""",
                    (transaction_id, item.get("id"), item.get("name"), item.get("quantity", 1), item.get("price", 0)),
                )
            conn.commit()
            conn.close()

        transaction = {"id": transaction_id, "timestamp": timestamp, "items": data["items"], "tender": tender, "total": total}
        return jsonify(transaction), 201

    except Exception:
        logger.exception("Error creating transaction")
        return jsonify({"error": "Internal server error while creating transaction"}), 500

@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    try:
        if USING_POSTGRES:
            conn, cur = get_postgres_conn()
            cur.execute("SELECT id, timestamp, tender, total FROM transactions ORDER BY id;")
            rows = cur.fetchall()
            transactions = []
            for row in rows:
                cur.execute("SELECT item_id, item_name, quantity, price FROM transaction_items WHERE transaction_id = %s;", (row["id"],))
                items = cur.fetchall()
                transactions.append(
                    {
                        "id": row["id"],
                        "timestamp": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else row["timestamp"],
                        "items": [{"id": it["item_id"], "name": it["item_name"], "quantity": it["quantity"], "price": it["price"]} for it in items],
                        "tender": row["tender"],
                        "total": float(row["total"]),
                    }
                )
            cur.close()
            conn.close()
            return jsonify(transactions)
        else:
            conn = get_sqlite_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM transactions ORDER BY id")
            rows = cur.fetchall()
            transactions = []
            for row in rows:
                cur.execute("""SELECT item_id, item_name, quantity, price
                               FROM transaction_items WHERE transaction_id = ?""",
                            (row["id"],))
                items = cur.fetchall()
                transactions.append(
                    {
                        "id": row["id"],
                        "timestamp": row["timestamp"],
                        "items": [{"id": item["item_id"], "name": item["item_name"], "quantity": item["quantity"], "price": item["price"]} for item in items],
                        "tender": row["tender"],
                        "total": row["total"],
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
        stats = {"cash": {"count": 0, "total": 0}, "check": {"count": 0, "total": 0}, "venmo": {"count": 0, "total": 0}}
        if USING_POSTGRES:
            conn, cur = get_postgres_conn()
            cur.execute("SELECT tender, total FROM transactions;")
            rows = cur.fetchall()
            cur.close()
            conn.close()
            for row in rows:
                t = row["tender"]
                if t not in stats:
                    stats[t] = {"count": 0, "total": 0}
                stats[t]["count"] += 1
                stats[t]["total"] += float(row["total"])
        else:
            conn = get_sqlite_conn()
            cur = conn.cursor()
            cur.execute("SELECT tender, total FROM transactions")
            rows = cur.fetchall()
            conn.close()
            for row in rows:
                tender = row["tender"]
                stats[tender]["count"] += 1
                stats[tender]["total"] += row["total"]

        overall_total = sum(stats[t]["total"] for t in stats)
        overall_count = sum(stats[t]["count"] for t in stats)
        return jsonify({"by_tender": stats, "overall": {"count": overall_count, "total": round(overall_total, 2)}})
    except Exception:
        logger.exception("Error computing stats")
        return jsonify({"error": "Internal server error while computing stats"}), 500

# Serve frontend for SPA
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if app.static_folder and (path == "" or not os.path.exists(os.path.join(app.static_folder, path))):
        return send_from_directory(app.static_folder, "index.html")
    return send_from_directory(app.static_folder, path)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)