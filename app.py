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
    """Initialize the SQLite database"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Create transactions table if it doesn't exist
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
    tender = data["tender"]
    total = round(total, 2)

    # Save to database
    conn = get_db_connection()
    cursor = conn.cursor()

    # Insert main transaction record
    cursor.execute(
        "INSERT INTO transactions (timestamp, tender, total, user) VALUES (?, ?, ?, ?)",
        (timestamp, tender, total, "default_user"),
    )
    transaction_id = cursor.lastrowid

    # Insert each item into transaction_items table
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
                item.get("price", 0)
            )
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


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    """Get all transactions"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all transactions
    cursor.execute("SELECT * FROM transactions ORDER BY id")
    rows = cursor.fetchall()

    transactions = []
    for row in rows:
        # Get items for this transaction
        cursor.execute(
            """SELECT item_id, item_name, quantity, price
               FROM transaction_items
               WHERE transaction_id = ?""",
            (row["id"],)
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
                        "price": item["price"]
                    }
                    for item in items
                ],
                "tender": row["tender"],
                "total": row["total"],
            }
        )

    conn.close()
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
