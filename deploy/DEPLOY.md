# Deploying to a Hostinger VPS

Ubuntu 22.04 or 24.04. Takes about ten minutes, most of it waiting for DNS.

---

## 1. Point the subdomain at the VPS

In **hPanel → Domains → your domain → DNS Zone**, add:

| Type | Name | Points to | TTL |
|---|---|---|---|
| A | `bot` (or whatever subdomain you want) | your VPS IPv4 | 3600 |

Your VPS IP is on the hPanel VPS overview page. `bot` alone in the Name field
produces `bot.yourdomain.com` — do not type the full domain there.

DNS usually propagates in a few minutes. Check from your machine:

```bash
nslookup bot.yourdomain.com
```

Wait until it answers with your VPS IP. The setup script checks this too and
will tell you if it isn't ready — it still installs everything, it just skips
the certificate step, which you can run later.

## 2. SSH in

```bash
ssh root@YOUR_VPS_IP
```

## 3. Run the installer

```bash
curl -fsSL https://raw.githubusercontent.com/itsmearyanabc/camel-telegram-tool/main/deploy/setup.sh -o setup.sh && sudo bash setup.sh bot.yourdomain.com
```

It installs Python, nginx, certbot and the firewall; creates an unprivileged
`armedias` user; clones the repo to `/opt/armedias`; builds a virtualenv;
installs the systemd service; configures nginx; and requests a Let's Encrypt
certificate. Re-running it is safe — it never overwrites `.env`, `sessions/`
or `data/`.

## 4. Set your password

The installer generates a random `SECRET_KEY` but deliberately leaves the admin
password as a placeholder:

```bash
sudo nano /opt/armedias/.env
```

Set `ADMIN_PASS` to something strong. If you want off-box backups too, fill in
`SUPABASE_URL` and `SUPABASE_KEY` while you're in there. Then:

```bash
sudo systemctl restart armedias
```

The password is hashed once at startup, so the restart is required.

## 5. Log in

`https://bot.yourdomain.com` — username `admin`, the password you just set.

---

## Day-to-day

```bash
sudo bash /opt/armedias/deploy/update.sh   # pull latest code and restart
sudo systemctl restart armedias            # restart
sudo systemctl status armedias             # is it running
journalctl -u armedias -f                  # live logs
tail -f /opt/armedias/logs/bot.log         # app's own log
```

---

## What lives where

| Path | Contents | Survives updates |
|---|---|---|
| `/opt/armedias/.env` | admin password, secret key, Supabase keys | yes |
| `/opt/armedias/sessions/` | Telegram account logins | yes |
| `/opt/armedias/config.json` | accounts, targets, intervals | yes |
| `/opt/armedias/data/group_monitor.db` | monitored groups and members | yes |
| `/opt/armedias/data/message_bot.db` | bot token and user pool | yes |
| `/opt/armedias/logs/` | rotating application log | yes |

`update.sh` does `git reset --hard`, which only touches tracked files. Everything
above is gitignored, so none of it is at risk.

---

## VPS vs Render — what changes

**Supabase becomes optional.** A VPS has a real disk, so sessions and databases
persist on their own. Keep Supabase configured anyway if you want off-box
backups — it costs nothing and protects against losing the server itself.

**Keep-alive turns itself off.** `RENDER_EXTERNAL_URL` isn't set, so the
self-ping thread logs that it's disabled and exits. A VPS doesn't spin down,
so there is nothing to keep awake.

**`--workers 1` still matters.** Every Telegram session, worker and campaign
lives in process memory. A second worker would keep its own copy of all of it
and forward every message twice. Do not raise it. To handle more accounts,
give the VPS more RAM instead.

---

## If something goes wrong

**Service won't start**
```bash
journalctl -u armedias -n 50 --no-pager
```
Usually a missing value in `.env` or a dependency that failed to build.

**502 Bad Gateway** — nginx is up but the app isn't:
```bash
sudo systemctl status armedias
```

**Certificate failed** — DNS wasn't pointing here yet. Once `nslookup` resolves
correctly:
```bash
sudo certbot --nginx -d bot.yourdomain.com
```

**Dashboard loads but never updates** — Socket.IO isn't getting through the
proxy. Confirm the `/socket.io/` block is present:
```bash
sudo nginx -t && grep -A3 'socket.io' /etc/nginx/sites-available/armedias
```

**Uploads rejected around 1 MB** — `client_max_body_size` didn't apply. It must
be at least 60M in the server block; reload nginx after fixing.

**Telegram logins vanish after redeploy** — you wiped `/opt/armedias`. Restore
from Supabase by setting the keys in `.env` and restarting; the app pulls
sessions and both databases back on boot.
