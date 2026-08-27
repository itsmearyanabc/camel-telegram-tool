# Deploying to a Hostinger VPS

Ubuntu 22.04 or 24.04. Takes about ten minutes, most of it waiting for DNS.

## Running alongside other projects

This is written for a VPS that already hosts other production services. The
installer is strictly additive:

- **It never enables your firewall.** If `ufw` is inactive it stays inactive and
  says so. Turning a firewall on where other services are running would block
  every port not explicitly allowed and could take them offline. If `ufw` is
  already active it only *adds* allow rules for SSH and HTTP/HTTPS.
- **It never removes another nginx site**, including the default one — that may
  belong to someone else's project.
- **It picks a free port.** If 5001 is taken it scans up to 5040 and uses the
  first free one, wiring that port into both the systemd unit and the nginx
  proxy. It binds to loopback only, so nothing new is exposed publicly.
- **It refuses to hijack a subdomain** already served by an existing nginx site,
  and aborts instead of colliding.
- **It rolls back its own nginx site** if `nginx -t` fails, so a bad config can
  never break the reload for your other sites.
- **certbot is scoped with `--cert-name`** to this domain only, so it cannot
  rewrite or renew certificates belonging to your other sites.

Run `preflight.sh` first — it is read-only and reports exactly what is already
on the box and what would conflict:

```bash
sudo bash preflight.sh telegrambot.yourdomain.com
```

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


---

## Zero-disk mode (all state in Supabase)

By default the app keeps its state in `/opt/telegram-tool`. To write **nothing**
to persistent disk:

```bash
sudo bash /opt/telegram-tool/deploy/zero-disk.sh
```

| What | Default | Zero-disk |
|---|---|---|
| Telegram sessions | `sessions/` on disk | `/run/telegram-tool` (RAM) |
| Both databases | `data/*.db` on disk | `/run/telegram-tool` (RAM) |
| `config.json` | on disk | `/run/telegram-tool` (RAM) |
| File attachments | `data/uploads/` on disk | Supabase Storage, never on disk |
| Logs | `logs/bot.log` | journald only |

Supabase becomes the single durable copy — state uploads within seconds of any
change and is pulled back into RAM at boot. The script verifies a real Supabase
round-trip and backs up existing state *before* it removes anything, and refuses
to run if Supabase is not working.

The application code and virtualenv (~300 MB) stay on disk. Python has to load
them from a filesystem; that part cannot move.

**The trade-off:** if Supabase is unreachable at boot, the app starts empty and
Telegram accounts need logging in again. Sessions additionally sync every 15
minutes, since Pyrogram keeps writing to them after login.

To revert:

```bash
sudo rm /etc/systemd/system/telegram-tool.service.d/zero-disk.conf && sudo systemctl daemon-reload && sudo systemctl restart telegram-tool
```

## Checking the whole box

```bash
sudo bash /opt/telegram-tool/deploy/vps-status.sh
```

Read-only. Lists every listening socket, systemd service, PM2 process and
Docker container, all nginx sites and certificates, disk per project, and runs
an isolation check on this app — that it binds loopback only, runs unprivileged,
uses a single worker, and that no other site references its directory.


---

## Getting to this project on the VPS

```bash
cd /opt/telegram-tool
```

### Running it under PM2 (alongside your other apps)

By default the app runs under **systemd**, which is why it does not appear in
`pm2 list` — PM2 supervises Node apps, systemd supervises everything else. Both
do the same job. To manage it with PM2 instead, so every app answers to the
same commands:

```bash
sudo bash /opt/telegram-tool/deploy/use-pm2.sh
```

That stops and disables the systemd unit first (two supervisors running one
copy would send every message twice), carries the port across so nginx keeps
working, and saves it to PM2's startup list.

| | systemd | PM2 |
|---|---|---|
| List | `systemctl status telegram-tool` | `pm2 list` |
| Logs | `journalctl -u telegram-tool -f` | `pm2 logs telegram-tool` |
| Restart | `systemctl restart telegram-tool` | `pm2 restart telegram-tool` |
| Stop | `systemctl stop telegram-tool` | `pm2 stop telegram-tool` |
| Resources | `systemctl status` | `pm2 monit` |

**The trade-off:** your PM2 runs as root, while the systemd unit runs the app as
an unprivileged user with `ProtectSystem` and a restricted `ReadWritePaths`.
Moving to PM2 matches how your other apps already run, but it is a genuine
reduction in isolation for this one.

To switch back:

```bash
sudo pm2 delete telegram-tool && sudo pm2 save && sudo systemctl enable --now telegram-tool
```

Zero-disk mode works under either supervisor — the script detects which one is
in use.
