# Deploying the Nifty Options Bot to a VPS

This is the **only reliable home for an always-on trading bot**. Emergent
preview sleeps; Emergent production deploys don't support long-running
daemons. A small Mumbai-region VPS solves both problems and gives you
sub-ms latency to NSE servers as a bonus.

## Recommended VPS

| Provider | Region | Plan | Cost |
|---|---|---|---|
| **DigitalOcean** | Bangalore (BLR1) | 1 vCPU / 1 GB RAM / 25 GB SSD | ~₹500/mo |
| **AWS Lightsail** | Mumbai (ap-south-1) | 1 vCPU / 1 GB RAM / 40 GB SSD | ~₹420/mo |
| **Hetzner** | (no India PoP — Helsinki/Falkenstein adds ~120 ms) | CX22 | ~€4/mo |

DO/Lightsail Mumbai is the right call for NSE latency.

## One-time setup (10 minutes)

```bash
# 1. SSH in
ssh root@<your-vps-ip>

# 2. Install Python 3.11, git, supervisor
apt update && apt install -y python3.11 python3.11-venv python3-pip git supervisor sqlite3

# 3. Get the code
cd /opt
git clone https://github.com/<your-user>/<your-repo> nifty_bot
# OR rsync from your laptop:
#   rsync -avz /local/path/to/app/backend/ root@vps:/opt/nifty_bot/

# 4. Virtualenv + deps
cd /opt/nifty_bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Drop in your .env (copy from preview)
nano .env
# Required keys:
#   ANGEL_API_KEY=...
#   ANGEL_CLIENT_ID=...
#   ANGEL_PIN=...
#   ANGEL_TOTP_KEY=...
#   TRADING_MODE=sim       # start in SIM, flip to live after a clean day
#   ENTRY_ORDER_TYPE=MARKET
#   SL_ORDER_TYPE=STOPLOSS_MARKET
#   BOT_DB_PATH=/opt/nifty_bot/data_store/nifty_bot.db
```

## Whitelist this VPS IP on Angel One

Go to https://smartapi.angelone.in → My Apps → your app → IP Whitelisting.
Add the VPS's public IP (`curl ifconfig.me` on the VPS), save.

## Supervisor unit

Create `/etc/supervisor/conf.d/nifty_bot.conf`:

```ini
[program:nifty_bot]
command=/opt/nifty_bot/.venv/bin/python /opt/nifty_bot/main.py
directory=/opt/nifty_bot
autostart=true
autorestart=true
startsecs=5
environment=PYTHONUNBUFFERED="1"
stderr_logfile=/var/log/supervisor/nifty_bot.err.log
stdout_logfile=/var/log/supervisor/nifty_bot.out.log
stopsignal=TERM
stopwaitsecs=30
```

Optional: also expose the FastAPI dashboard (run on VPS, bind to localhost,
front with nginx + Let's Encrypt cert if you want a public URL).

```ini
[program:nifty_dashboard]
command=/opt/nifty_bot/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8001
directory=/opt/nifty_bot
autostart=true
autorestart=true
stderr_logfile=/var/log/supervisor/dashboard.err.log
stdout_logfile=/var/log/supervisor/dashboard.out.log
```

Reload & start:
```bash
supervisorctl reread && supervisorctl update
supervisorctl start nifty_bot nifty_dashboard
supervisorctl status
```

## Daily operations

```bash
# Tail the bot
supervisorctl tail -f nifty_bot stderr

# Restart
supervisorctl restart nifty_bot

# Stop overnight (optional — saves Angel session quota)
crontab -e
# Add:
#   30 15 * * 1-5 /usr/bin/supervisorctl stop nifty_bot
#   10 9  * * 1-5 /usr/bin/supervisorctl start nifty_bot
```

## Securing the dashboard

If you expose the FastAPI dashboard publicly:
1. Put it behind nginx with HTTPS (`certbot --nginx -d bot.yourdomain.com`)
2. Add basic auth on the nginx location block (you're the only user)
3. Restrict by source IP (your home IP) for extra safety

## Cost summary

- VPS: ~₹500/mo
- Domain (optional): ~₹100/mo
- Angel SmartAPI: free
- Data feed: free (bundled with SmartAPI)

**Total: well under ₹600/mo for an institutional-grade always-on setup.**
