from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import json
import os

app = Flask(__name__)
CORS(app)

# In-memory storage for simplicity
ITEMS = [
    {"id": 1, "name": "Candles", "price": 5.00},
    {"id": 2, "name": "Prayer Book", "price": 10.00},
    {"id": 3, "name": "Rosary Beads", "price": 8.00},
    {"id": 4, "name": "Cross Pendant", "price": 12.00},
    {"id": 5, "name": "Holy Water Bottle", "price": 3.00},
    {"id": 6, "name": "Devotional Cards", "price": 2.00},
    {"id": 7, "name": "Scripture Journal", "price": 15.00},
    {"id": 8, "name": "Blessed Oil", "price": 6.00},
    {"id": 9, "name": "Religious Art Print", "price": 20.00},
    {"id": 10, "name": "Charity Bracelet", "price": 4.00}
]

TRANSACTIONS = []

@app.route('/api/items', methods=['GET'])
def get_items():
    """Get all available items"""
    return jsonify(ITEMS)

@app.route('/api/transactions', methods=['POST'])
def create_transaction():
    """Create a new transaction"""
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "No data provided"}), 400
    
    # Validate required fields
    if 'items' not in data or 'tender' not in data:
        return jsonify({"error": "Missing required fields: items, tender"}), 400
    
    # Validate tender type
    valid_tenders = ['cash', 'check', 'venmo']
    if data['tender'] not in valid_tenders:
        return jsonify({"error": f"Invalid tender. Must be one of: {', '.join(valid_tenders)}"}), 400
    
    # Calculate total
    total = 0
    for item in data['items']:
        item_id = item.get('id')
        quantity = item.get('quantity', 1)
        
        # Find item in ITEMS list
        found_item = next((i for i in ITEMS if i['id'] == item_id), None)
        if found_item:
            total += found_item['price'] * quantity
    
    # Create transaction record
    transaction = {
        "id": len(TRANSACTIONS) + 1,
        "timestamp": datetime.now().isoformat(),
        "items": data['items'],
        "tender": data['tender'],
        "total": round(total, 2)
    }
    
    TRANSACTIONS.append(transaction)
    
    return jsonify(transaction), 201

@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    """Get all transactions"""
    return jsonify(TRANSACTIONS)

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get statistics by tender type"""
    stats = {
        "cash": {"count": 0, "total": 0},
        "check": {"count": 0, "total": 0},
        "venmo": {"count": 0, "total": 0}
    }
    
    for transaction in TRANSACTIONS:
        tender = transaction['tender']
        stats[tender]['count'] += 1
        stats[tender]['total'] += transaction['total']
    
    # Calculate overall total
    overall_total = sum(stats[t]['total'] for t in stats)
    overall_count = sum(stats[t]['count'] for t in stats)
    
    return jsonify({
        "by_tender": stats,
        "overall": {
            "count": overall_count,
            "total": round(overall_total, 2)
        }
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)
