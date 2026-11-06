# Church POS - Charity Drive Point of Sale System

A password-protected point of sale for church charity drives. Cashiers pick an item, type the amount, and take cash, check, or Venmo. There are no tax calculations.

- React frontend, served by a Flask backend
- SQLite database stored on a Docker volume
- Each cashier logs in with their own PIN, and sales record who rang them up
- Daily multi-total and single-item reports

## Running on a server (Docker)

Requires Docker with the Compose plugin.

1. Create your settings file:
   ```bash
   cp .env.example .env
   ```
2. In `.env`, set `ADMIN_CODE` to the code that manages cashiers, and set `SECRET_KEY` to the output of `openssl rand -hex 32`. Check that `POS_TIMEZONE` is correct, because it decides which day a sale counts toward in reports.
3. Build and start:
   ```bash
   docker compose up -d --build
   ```
4. Open `http://<server-ip>:5000` from any device on the network.

The container restarts automatically unless you stop it. To update after a code change, run step 3 again. Sales data is kept.

This serves plain HTTP, so only run it on a network you trust. If it will be reachable from the internet, put it behind an HTTPS reverse proxy and set `COOKIE_SECURE=true`.

## Cashiers

Type the admin code on the login screen to add, edit, and remove cashiers. No one can ring up sales until at least one cashier exists. The admin code can also view reports, but sales must be rung up under a cashier's PIN.

- PINs are 4 to 8 digits, and each cashier's PIN must be different.
- PINs can't be viewed after they're saved. If one is forgotten, set a new one.
- Removing a cashier logs them out right away. Their past sales stay in the reports and keep their name.
- Changing `SECRET_KEY` makes every PIN stop working, so they'd all need to be set again.

## Data and backups

Sales are stored in the `church-pos-data` Docker volume, which survives restarts, rebuilds, and `docker compose down`. **`docker compose down -v` deletes it.**

To back up to the current folder:

```bash
docker compose exec web python -c "import sqlite3; sqlite3.connect('/data/church_pos.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp web:/data/backup.db "./church_pos_backup_$(date +%F).db"
```

Keep a copy somewhere other than the server.

To restore a backup, replacing all current data:

```bash
docker compose cp ./church_pos_backup.db web:/data/restore.db
docker compose exec web python -c "import sqlite3; sqlite3.connect('/data/restore.db').backup(sqlite3.connect('/data/church_pos.db'))"
docker compose restart web
```

## Journal

The **Journal** tab shows a receipt for every transaction, newest first, so each cashier's sales can be audited. Filter by date or cashier. Each receipt has a **Print** button and, for sales, a **Refund** button.

A refund covers the whole sale and can only be done once. It is saved as a separate negative receipt linked to the original, and it records who processed it. The original receipt stays and is marked as refunded.

## Reports

Refunds appear as negative amounts and reduce sales counts, payment totals, and item quantities on the day the refund was made. On Cashier Totals, a refund counts against whoever processed it, not whoever made the sale. If a sale from an earlier day is refunded, the earlier day's totals don't change.

Open the **Reports** tab after logging in. It shows today by default, and you can pick a date range or all dates, then print.

- **Daily Multi-Total:** sales count, each payment method, and the total for each day
- **Payment Method Totals:** count and total for cash, check, and Venmo
- **Cashier Totals:** sales count and total per cashier, using the name they had when each sale was made
- **Department Totals:** quantity and total per department
- **Single Item Totals:** quantity and total per item, including donations

## Local development

Start the backend:

```bash
pip install -r requirements.txt
ADMIN_CODE=<your code> SECRET_KEY=dev python app.py
```

In a second terminal, start the frontend:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5175`. Vite forwards `/api` requests to Flask on port 5001, which avoids the macOS AirPlay Receiver on port 5000.

Run the backend tests with:

```bash
python -m unittest test_app
```

## API

All endpoints except login, session, and health require a logged-in session.

| Endpoint | Description |
|---|---|
| `POST /api/login` | Log in with `{"code": "..."}`, using a cashier PIN or the admin code |
| `POST /api/logout` | Log out |
| `GET /api/session` | Whether this browser is logged in |
| `GET /api/health` | Health check used by Docker |
| `GET`, `POST /api/cashiers` | List or add cashiers (admin only) |
| `PUT`, `DELETE /api/cashiers/<id>` | Edit or remove a cashier (admin only) |
| `GET /api/items` | Items available to ring up |
| `POST /api/transactions` | Record a sale (cashier only) |
| `GET /api/transactions?start=YYYY-MM-DD&end=YYYY-MM-DD` | Sales and refunds with their items; both dates are optional |
| `POST /api/transactions/<id>/refund` | Refund a whole sale |
| `GET /api/reports/summary?start=YYYY-MM-DD&end=YYYY-MM-DD` | Report totals; both dates are optional |
| `GET /api/stats` | All-time totals by payment method |
| `GET /api/item-sales` | All-time totals by item |

A sale looks like this, with the price entered at the register:

```json
{
  "items": [{"id": 9, "quantity": 1, "price": 25.00}],
  "tender": "cash"
}
```

## Items

Items are defined in `ITEMS` in `app.py`. Keep existing IDs unchanged so past sales stay matched to their department.

- **Food:** Nuts, Baked Goods, Honey, Jam
- **Misc:** Rada, Mom & Pop Shop, Danny Duzits, Crafts, Donation
