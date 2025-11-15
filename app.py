import json
import sqlite3
import os
import logging
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("church-pos")

# Serve built frontend from frontend_build if present
static_dir = os.environ.get("FLASK_STATIC_DIR", "frontend_build")
app = Flask(__name__, static_folder=static_dir, static_url_path="/")
CORS(app)

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
]

def init_db():
    try:
        # Ensure directory for DB exists
        db_dir = os.path.dirname(DB_FILE)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(DB_FILE)
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
        return conn
    except Exception:
        logger.exception("Failed to get DB connection for %s", DB_FILE)
        raise

# Initialize DB at import time so it's ready under gunicorn
try:
    init_db()
except Exception:
    # If DB init fails at import, let it bubble so the process fails fast and logs show the problem.
    pass

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
            item_id = item.get("id")
            quantity = item.get("quantity", 1)
            price = item.get("price", 0)
            found_item = next((i for i in ITEMS if i["id"] == item_id), None)
            if found_item:
                total += price * quantity

        timestamp = datetime.now().isoformat()
        tender = data["tender"]
        total = round(total, 2)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transactions (timestamp, tender, total, user) VALUES (?, ?, ?, ?)",
            (timestamp, tender, total, "default_user"),
        )
        transaction_id = cursor.lastrowid

        for item in data["items"]:
            cursor.execute(
                """INSERT INTO transaction_items
                   (transaction_id, item_id, item_name, quantity, price)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    transaction_id,
                    item.get("id"),
                    item.get("name"),
                    item.get("quantity", 1),
                    item.get("price", 0),
                ),
            )

        conn.commit()
        conn.close()

        transaction = {
            "id": transaction_id,
            "timestamp": timestamp,
            "items": data["items"],
            "tender": tender,
            "total": total,
        }
        return jsonify(transaction), 201

    except Exception:
        logger.exception("Error creating transaction")
        return jsonify({"error": "Internal server error while creating transaction"}), 500

@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions ORDER BY id")
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
        cursor.execute("SELECT tender, total FROM transactions")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            tender = row["tender"]
            if tender not in stats:
                # Add unexpected tender types safely
                stats[tender] = {"count": 0, "total": 0}
            stats[tender]["count"] += 1
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

# Serve frontend app (if built) for SPA routes
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if app.static_folder and (path == "" or not os.path.exists(os.path.join(app.static_folder, path))):
        return send_from_directory(app.static_folder, "index.html")
    return send_from_directory(app.static_folder, path)

if __name__ == "__main__":
    # Only used when running directly with python app.py
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)