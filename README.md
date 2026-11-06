# Church POS - Charity Drive Point of Sale System

A password-protected point of sale for church charity drives. Cashiers pick an item, type the amount, and take cash, check, or Venmo. There are no tax calculations.

- React frontend, served by a Flask backend
- SQLite database stored on a Docker volume
- Each cashier logs in with their own PIN, and sales record who rang them up
- Daily multi-total and single-item reports

## Installing on Proxmox

In the Proxmox host's shell, as root, run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/n1ghtang31/church-pos/self_hosted/proxmox/install.sh)"
```

It asks a few questions, then creates a Debian LXC container with Docker, starts Church POS, and prints the address to open. The container starts when Proxmox boots. Inside it (`pct enter <id>`), two commands are installed:

- `church-pos-backup` copies the database and settings to `/opt/church-pos-backups`. It also runs nightly at 11:30 PM and keeps 30 days.
- `church-pos-update` backs up, then installs the latest version. Sales data is kept.

Those backups are on the same disk as the app, so also add the container to a Proxmox backup job (Datacenter > Backup).

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

The app itself serves plain HTTP. To reach it from the internet, put it behind an HTTPS reverse proxy. See [Hosting publicly](#hosting-publicly).

## Cashiers

Type the admin code on the login screen to add, edit, and remove cashiers. No one can ring up sales until at least one cashier exists. The admin code can also view reports, but sales must be rung up under a cashier's PIN.

- PINs are 4 to 8 digits, and each cashier's PIN must be different. The admin code is 8 to 32 letters or digits.
- PINs can't be viewed after they're saved. If one is forgotten, set a new one.
- After 10 wrong codes in a row from one device, it's blocked from logging in. A correct code resets the count, and wrong codes older than a day are forgotten. Unblock devices under **Blocked Devices** on the Cashiers page. If your own device is blocked, see [Unblocking from the server](#unblocking-from-the-server).
- Devices on a private network address (such as `192.168.x.x`) are only locked out for 5 minutes instead, because that address might be shared by everyone or be the reverse proxy itself.
- Removing a cashier logs them out right away. Their past sales stay in the reports and keep their name.
- Changing `SECRET_KEY` makes every PIN stop working, so they'd all need to be set again.

## Hosting publicly

Only expose the app through an HTTPS reverse proxy such as nginx. Forward ports 80 and 443 on your router to nginx, never the app's port, and never the Proxmox web interface (8006).

In `.env`, set:

```
COOKIE_SECURE=true
TRUSTED_PROXY_IPS=<nginx's IP address>
```

Then restart with `docker compose up -d`. The Proxmox installer sets both when you say you're using a reverse proxy.

With `COOKIE_SECURE=true`, logins only work through the HTTPS address. `TRUSTED_PROXY_IPS` lets the app see each visitor's real address, so wrong codes block only that visitor instead of everyone who comes through nginx.

Example nginx site, with the certificate from certbot (`certbot --nginx -d pos.example.com`):

```nginx
# At most 10 login attempts a minute from each address
limit_req_zone $binary_remote_addr zone=pos_login:10m rate=10r/m;

server {
    listen 80;
    server_name pos.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name pos.example.com;
    # ssl_certificate lines are added by certbot

    client_max_body_size 64k;

    proxy_set_header Host $host;
    # Replace, rather than append to, anything the visitor sent
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;

    location = /api/login {
        limit_req zone=pos_login burst=5 nodelay;
        proxy_pass http://<container-ip>:5000;
    }

    location / {
        proxy_pass http://<container-ip>:5000;
    }
}
```

Nginx Proxy Manager also works: create a proxy host to `http://<container-ip>:5000` with a Let's Encrypt certificate, Force SSL, and HSTS turned on.

To check that visitors' real addresses come through, enter a wrong code from a phone on mobile data, then run `docker compose logs web | grep "Failed login"` in `/opt/church-pos`. It should show the phone's address, not nginx's. If it shows nginx's address, set `TRUSTED_PROXY_IPS` to the address shown in the log, which is the address the app sees nginx connecting from.

When the sale is over, remove the port forwarding or stop the container.

### Unblocking from the server

If the admin's own device is blocked, unblock it from the container (`pct enter <id>`):

```bash
cd /opt/church-pos
docker compose exec web flask --app app bans              # list blocked addresses
docker compose exec web flask --app app unban <address>   # unblock one
docker compose exec web flask --app app unban --all       # unblock everyone
```

Containers set up with the Proxmox installer also have `church-pos-unban`, which runs the same commands: `church-pos-unban` lists, and `church-pos-unban <address>` or `church-pos-unban --all` unblocks.

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
ADMIN_CODE=<8+ character code> SECRET_KEY=dev python app.py
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
| `GET /api/login-blocks` | Blocked addresses (admin only) |
| `DELETE /api/login-blocks/<address>` | Unblock an address (admin only) |
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
