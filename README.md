# Church POS - Charity Drive Point of Sale System

A simple POS system for church charity drives with 10 items and 3 payment tender types (cash, check, venmo).

## Features

- 10 pre-configured charity items
- 3 payment tender types: Cash, Check, and Venmo
- No tax calculations (as specified)
- Simple, bare-bones React frontend
- Flask Python backend with in-memory storage

## Setup and Running

### Backend (Flask)

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Run the Flask server:
```bash
python app.py
```

The backend will start on `http://localhost:5000`

### Frontend (React)

1. Navigate to the frontend directory:
```bash
cd frontend
```

2. Install dependencies (if not already done):
```bash
npm install
```

3. Start the development server:
```bash
npm run dev
```

The frontend will start on `http://localhost:5173`

## Usage

1. Start both the backend (Flask) and frontend (React) servers
2. Open your browser to the frontend URL (typically `http://localhost:5173`)
3. Click on items to add them to the cart
4. Adjust quantities using the + and - buttons
5. Select a payment tender type (Cash, Check, or Venmo)
6. Click "Complete Transaction" to process the sale

## API Endpoints

### GET /api/items
Returns the list of all 10 available items.

### POST /api/transactions
Creates a new transaction. Expects:
```json
{
  "items": [{"id": 1, "quantity": 2}],
  "tender": "cash"
}
```

### GET /api/transactions
Returns all completed transactions.

### GET /api/stats
Returns statistics grouped by tender type.

## Items List

1. Candles - $5.00
2. Prayer Book - $10.00
3. Rosary Beads - $8.00
4. Cross Pendant - $12.00
5. Holy Water Bottle - $3.00
6. Devotional Cards - $2.00
7. Scripture Journal - $15.00
8. Blessed Oil - $6.00
9. Religious Art Print - $20.00
10. Charity Bracelet - $4.00