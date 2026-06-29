# Deploying Nifty 50 Options Bot on Oracle Cloud VM

End-to-end. Every command. Run from top to bottom on a fresh Oracle Cloud Ampere/AMD VM and the bot + dashboard will be live at `http://<your-vm-ip>/`.

---

## Part 1 — Provision the Oracle VM

1. Log in to Oracle Cloud → **Compute → Instances → Create Instance**.
2. **Image**: Ubuntu 22.04 (Canonical Ubuntu-22.04 Minimal is fine).
3. **Shape**: `VM.Standard.A1.Flex` (Ampere, Always-Free). 1 OCPU + 6 GB RAM is plenty.
4. **Networking**:
   - Pick or create a VCN with a public subnet.
   - Check "Assign a public IPv4 address".
5. **SSH keys**: paste your public key (or download the generated one).
6. Click **Create**. Wait until **State = RUNNING**, then copy the **Public IP**.

### Open the firewall (Oracle does NOT open ports by default)

**a. Oracle Security List** — VCN → your subnet → Security List → Add Ingress Rules:

| Source CIDR | Protocol | Destination Port |
|---|---|---|
| 0.0.0.0/0 | TCP | 22  (SSH) |
| 0.0.0.0/0 | TCP | 80  (HTTP) |
| 0.0.0.0/0 | TCP | 443 (HTTPS) |

**b. Ubuntu's local firewall (`iptables`)** — Oracle's Ubuntu image blocks everything except 22 by default. SSH in first:

```bash
ssh ubuntu@<your-vm-ip>
```

then open 80 & 443 permanently:

```bash
sudo iptables -I INPUT 6 -p tcp -m state --state NEW -m tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## Part 2 — System dependencies

All commands as the default `ubuntu` user. `sudo` where shown.

```bash
sudo apt update && sudo apt upgrade -y

# Python 3.11 + venv + build tools
sudo apt install -y python3.11 python3.11-venv python3.11-dev build-essential \
                    git curl gnupg ca-certificates lsb-release

# Node.js 20 + Yarn 1 (Classic — matches the codebase's package.json)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g yarn

# Web server + process manager + SSL
sudo apt install -y nginx supervisor certbot python3-certbot-nginx

# Persist firewall changes across reboot
sudo apt install -y iptables-persistent
```

Verify:

```bash
python3.11 --version    # Python 3.11.x
node --version          # v20.x
yarn --version          # 1.22.x
nginx -v
supervisord --version
```

---

## Part 3 — Pull the code

```bash
sudo mkdir -p /opt/nifty-bot && sudo chown -R ubuntu:ubuntu /opt/nifty-bot
cd /opt
git clone https://github.com/<your-org>/<your-repo>.git nifty-bot
cd /opt/nifty-bot
```

> Repo layout expected: `/opt/nifty-bot/backend`, `/opt/nifty-bot/frontend`.

---

## Part 4 — Backend setup

### 4.1 Python virtualenv + dependencies

```bash
cd /opt/nifty-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r backend/requirements.txt
```

### 4.2 Configure `backend/.env`

```bash
nano /opt/nifty-bot/backend/.env
```

Paste (replace placeholders — never commit real values to git):

```ini
# ──────────── Angel One SmartAPI credentials (REQUIRED for live mode)
ANGEL_API_KEY=xxxxxxxxxxxx
ANGEL_CLIENT_ID=AAAA1234
ANGEL_PIN=1234
ANGEL_TOTP_KEY=YOUR_BASE32_TOTP_SECRET

# ──────────── Trading mode
TRADING_MODE=sim                # 'sim' = paper, 'live' = real money
PAPER_STARTING_CAPITAL=100000

# ──────────── Database
MONGO_URL=mongodb://localhost:27017   # only if you use it; bot itself uses SQLite
DB_NAME=nifty_bot
DB_PATH=/opt/nifty-bot/backend/nifty_bot.db

# ──────────── Manual-entry policy (PART 3 spec)
AUTO_ENTRY_ENABLED=false        # manual-only by default
MANUAL_SL_PCT=15
MANUAL_TP_PCT=30
TRAIL_STEP_PCT=10
SMC_MAX_SIGNAL_AGE_MIN=5
FEED_STALE_SECONDS=10
```

Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

### 4.3 Whitelist your VM IP on Angel One (LIVE mode only)

If `TRADING_MODE=live`:

1. Go to https://smartapi.angelbroking.com → My Apps → Edit your app.
2. Add the **public IP of this VM** to the IP-whitelist.
3. Wait 5–10 minutes for it to propagate. Without this, every live order returns `errorCode AG7002`.

### 4.4 Smoke-test the API manually

```bash
cd /opt/nifty-bot
source .venv/bin/activate
cd backend
uvicorn server:app --host 127.0.0.1 --port 8001
```

In another SSH session:

```bash
curl -s http://127.0.0.1:8001/api/bot/status | head -c 200
```

You should see JSON. Stop the manual run with `Ctrl+C` — Supervisor will take over next.

---

## Part 5 — Frontend build

### 5.1 Configure `frontend/.env.production`

> **Important**: this codebase is **Create React App**, not Vite. The variable name is `REACT_APP_BACKEND_URL`. Leave it empty so axios calls `/api/*` relative to the current origin — Nginx will proxy them.

```bash
echo "REACT_APP_BACKEND_URL=" > /opt/nifty-bot/frontend/.env.production
```

### 5.2 Install deps + build

```bash
cd /opt/nifty-bot/frontend
yarn install
yarn build
```

This produces `/opt/nifty-bot/frontend/build/` containing `index.html` and the static bundle.

### 5.3 Move the build into Nginx's webroot

```bash
sudo mkdir -p /var/www/nifty-bot
sudo rsync -a --delete /opt/nifty-bot/frontend/build/ /var/www/nifty-bot/
sudo chown -R www-data:www-data /var/www/nifty-bot
```

---

## Part 6 — Nginx (reverse proxy + SPA fallback)

Create the site config:

```bash
sudo nano /etc/nginx/sites-available/nifty-bot
```

Paste:

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    server_name _;

    root /var/www/nifty-bot;
    index index.html;

    # React SPA — let the client router handle all non-API routes
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API — proxy to FastAPI on 127.0.0.1:8001
    location /api/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # Cache the JS/CSS hashed bundles aggressively
    location /static/ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

Activate it:

```bash
sudo ln -sf /etc/nginx/sites-available/nifty-bot /etc/nginx/sites-enabled/nifty-bot
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t            # must say "test is successful"
sudo systemctl reload nginx
sudo systemctl enable nginx
```

Verify:

```bash
curl -sI http://127.0.0.1/        | head -1   # → HTTP/1.1 200 OK  (React index.html)
curl -s  http://127.0.0.1/api/bot/status | head -c 80   # → JSON (proxied to FastAPI)
```

---

## Part 7 — Supervisor (keep the API + bot daemon alive)

### 7.1 API process (FastAPI / uvicorn)

```bash
sudo nano /etc/supervisor/conf.d/nifty-api.conf
```

```ini
[program:nifty_api]
command=/opt/nifty-bot/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8001 --workers 1
directory=/opt/nifty-bot/backend
user=ubuntu
autostart=true
autorestart=true
startretries=5
stopsignal=TERM
stopwaitsecs=20
environment=PYTHONUNBUFFERED="1",HOME="/home/ubuntu"
stdout_logfile=/var/log/supervisor/nifty_api.out.log
stderr_logfile=/var/log/supervisor/nifty_api.err.log
```

### 7.2 Bot daemon (FSM loop + WebSocket)

```bash
sudo nano /etc/supervisor/conf.d/nifty-bot.conf
```

```ini
[program:nifty_bot]
command=/opt/nifty-bot/.venv/bin/python -m main
directory=/opt/nifty-bot/backend
user=ubuntu
autostart=false         ; START MANUALLY — don't fire on boot
autorestart=true
startretries=3
stopsignal=TERM
stopwaitsecs=30
environment=PYTHONUNBUFFERED="1",HOME="/home/ubuntu"
stdout_logfile=/var/log/supervisor/nifty_bot.out.log
stderr_logfile=/var/log/supervisor/nifty_bot.err.log
```

> `autostart=false` is intentional — you want to start the bot **manually** from the dashboard or via SSH, especially before market open. Auto-start on every reboot is risky if the VM reboots mid-trading-day.

Reload Supervisor:

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status
```

Expected:

```
nifty_api      RUNNING   pid 12345, uptime 0:00:05
nifty_bot      STOPPED   (started manually)
```

---

## Part 8 — HTTPS (optional but recommended)

If you have a domain (e.g. `bot.yourdomain.com`) pointing at the VM IP:

```bash
sudo certbot --nginx -d bot.yourdomain.com
```

Certbot will rewrite the Nginx config to redirect HTTP → HTTPS and auto-renew. Then access the dashboard at `https://bot.yourdomain.com/`.

For IP-only access (no domain), skip this section — the bot works fine over plain HTTP.

---

## Part 9 — First-time verification

Open in browser: **`http://<your-vm-ip>/`**

You should see the dashboard load with:

- Top bar: SIM/LIVE toggle, Broker badge 🟢/🔴, Feed badge 🟢/🔴, Exit Position
- Engine Selector (Indicator | SMC)
- Twin Advisory cards (Indicator left, SMC right)
- Signal diagnostic + position card + trades table

If the dashboard renders but Broker shows 🔴:

```bash
sudo supervisorctl start nifty_bot
sleep 10
tail -n 50 /var/log/supervisor/nifty_bot.err.log
```

Common errors:

- `Invalid OTP / TOTP` → your `ANGEL_TOTP_KEY` is wrong (use the **base32 secret**, not the 6-digit code).
- `AG7002` → IP whitelist on Angel One not yet active. Wait 10 min.
- `Address already in use` → uvicorn already running under supervisor; don't run it manually too.

---

## Part 10 — The daily routine

### Start trading day (≈ 09:00 IST)

```bash
sudo supervisorctl start nifty_bot
```

Watch logs:

```bash
sudo tail -F /var/log/supervisor/nifty_bot.err.log
```

### Stop trading day (≈ 15:30 IST, after square-off)

```bash
sudo supervisorctl stop nifty_bot
```

### Optional: automate start/stop via cron

```bash
crontab -e
```

Add (Mon–Fri):

```
10 9  * * 1-5  /usr/bin/sudo /usr/bin/supervisorctl start nifty_bot
35 15 * * 1-5  /usr/bin/sudo /usr/bin/supervisorctl stop  nifty_bot
```

---

## Part 11 — Updating after a code change

This is the routine you'll repeat any time you `git pull`:

```bash
cd /opt/nifty-bot && git pull

# (a) backend deps — only if requirements.txt changed
source /opt/nifty-bot/.venv/bin/activate
pip install -r backend/requirements.txt

# (b) frontend — only if anything under frontend/ changed
cd /opt/nifty-bot/frontend
yarn install
yarn build
sudo rsync -a --delete build/ /var/www/nifty-bot/
sudo systemctl reload nginx

# (c) restart services
sudo supervisorctl restart nifty_api
sudo supervisorctl restart nifty_bot   # only if it was already running
```

---

## Part 12 — Common gotchas (don't skip)

1. **`http://<vm-ip>:8001/` returns 404** — **normal**. FastAPI has no `/` route by design. Open `http://<vm-ip>/` (port 80, served by Nginx) for the dashboard, or `http://<vm-ip>/api/bot/status` for the API.
2. **Emergent preview URL (`*.preview.emergentagent.com`)** never reaches your VPS — it's the chat-development pod. Always test the deployed app against your VM IP/domain.
3. **`REACT_APP_BACKEND_URL`** is the CRA env var name (not `VITE_API_BASE_URL`). Leave it empty in `.env.production` so axios uses relative `/api/*` and Nginx proxies.
4. **Always rebuild the frontend** after a `git pull` if any file under `frontend/` changed. Browser caches will lie to you otherwise — force-reload with `Ctrl+Shift+R`.
5. **`.venv` lives at `/opt/nifty-bot/.venv`**, not inside `backend/`. Activate with `source /opt/nifty-bot/.venv/bin/activate`.
6. **`supervisor_state: "STOPPED"`** in `/api/bot/status` is the bot **daemon's** flag (FSM is idle), NOT Supervisor itself. The API stays up regardless — that's why curling `/api/bot/status` works even when the bot is stopped.
7. **Oracle ingress rule + Ubuntu `iptables` are two separate firewalls.** Both must allow 80 and 443. Most "site unreachable" reports come from forgetting one of them.
8. **SQLite DB path**: keep it inside `/opt/nifty-bot/backend/` (already configured via `DB_PATH`). Don't put it in `/tmp` — it gets wiped on reboot.
9. **Time zone**: keep the VM on UTC (`timedatectl status`). The bot does its own IST conversion internally and assumes the OS is UTC.
10. **LIVE-mode pilot**: always start LIVE trading with `MIN_LOTS=1` for the first day, validate at least one full SL-hit + TP-hit + Trail-bump cycle, then scale up.

---

## Cost summary (Always-Free tier)

| Item | Cost |
|---|---|
| Oracle Ampere VM (2 OCPU + 12 GB RAM possible on free tier) | **₹0/mo** |
| Domain + Let's Encrypt SSL (optional) | ~₹100/mo |
| Angel SmartAPI + market data | **₹0** |
| **Total** | **₹0 – ₹100/mo** |

---

That's the entire stack. SSH in, follow Parts 1-9 once, then Part 11 is your forever-update workflow.
