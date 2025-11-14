import json
import sqlite3
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Database file path
DB_FILE = "church_pos.db"

# In-memory storage for simplicity
# Items now organized by departments with open ring pricing (no fixed price)
ITEMS = [
    {"id": 1, "name": "Pecans", "department": "Food"},
    {"id": 2, "name": "Prayer Book", "department": "Books"},
    {"id": 3, "name": "Rosary Beads", "department": "Religious Items"},
    {"id": 4, "name": "Cross Pendant", "department": "Religious Items"},
    {"id": 5, "name": "Holy Water Bottle", "department": "Religious Items"},
    {"id": 6, "name": "Devotional Cards", "department": "Books"},
    {"id": 7, "name": "Scripture Journal", "department": "Books"},
    {"id": 8, "name": "Blessed Oil", "department": "Religious Items"},
    {"id": 9, "name": "Religious Art Print", "department": "Art & Decor"},
]


def init_db():
    """Initialize the SQLite database"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Create transactions table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            items TEXT NOT NULL,
            tender TEXT NOT NULL,
            total REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def get_db_connection():
    """Get a database connection"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/api/items", methods=["GET"])
def get_items():
    """Get all available items"""
    return jsonify(ITEMS)


@app.route("/api/transactions", methods=["POST"])
def create_transaction():
    """Create a new transaction"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Validate required fields
    if "items" not in data or "tender" not in data:
        return jsonify({"error": "Missing required fields: items, tender"}), 400

    # Validate tender type
    valid_tenders = ["cash", "check", "venmo"]
    if data["tender"] not in valid_tenders:
        return jsonify(
            {"error": f"Invalid tender. Must be one of: {', '.join(valid_tenders)}"}
        ), 400

    # Calculate total
    total = 0
    for item in data["items"]:
        item_id = item.get("id")
        quantity = item.get("quantity", 1)
        price = item.get("price", 0)  # Get custom price from transaction

        # Validate item exists in ITEMS list
        found_item = next((i for i in ITEMS if i["id"] == item_id), None)
        if found_item:
            total += price * quantity

    # Create transaction record
    timestamp = datetime.now().isoformat()
    items_json = json.dumps(data["items"])
    tender = data["tender"]
    total = round(total, 2)

    # Save to database
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO transactions (timestamp, items, tender, total) VALUES (?, ?, ?, ?)",
        (timestamp, items_json, tender, total),
    )
    conn.commit()
    transaction_id = cursor.lastrowid
    conn.close()

    transaction = {
        "id": transaction_id,
        "timestamp": timestamp,
        "items": data["items"],
        "tender": tender,
        "total": total,
    }

    return jsonify(transaction), 201


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    """Get all transactions"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transactions ORDER BY id")
    rows = cursor.fetchall()
    conn.close()

    transactions = []
    for row in rows:
        transactions.append(
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "items": json.loads(row["items"]),
                "tender": row["tender"],
                "total": row["total"],
            }
        )

    return jsonify(transactions)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Get statistics by tender type"""
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
        stats[tender]["count"] += 1
        stats[tender]["total"] += row["total"]

    # Calculate overall total
    overall_total = sum(stats[t]["total"] for t in stats)
    overall_count = sum(stats[t]["count"] for t in stats)

    return jsonify(
        {
            "by_tender": stats,
            "overall": {"count": overall_count, "total": round(overall_total, 2)},
        }
    )


if __name__ == "__main__":
    # Initialize database on startup
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
