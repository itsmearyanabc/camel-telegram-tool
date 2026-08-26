#!/usr/bin/env bash
#
# ARMEDIAS AI — one-shot VPS bootstrap (Ubuntu / Debian)
#
#   sudo bash setup.sh bot.yourdomain.com
#
# Safe to re-run: every step checks before it acts. It never overwrites an
# existing .env, and it never touches sessions/ or data/.
#
set -euo pipefail

DOMAIN="${1:-}"
REPO="https://github.com/itsmearyanabc/camel-telegram-tool.git"
APP_DIR="/opt/armedias"
APP_USER="armedias"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; OFF=$'\e[0m'
say()  { echo "${BLD}==>${OFF} $*"; }
ok()   { echo "  ${GRN}ok${OFF}  $*"; }
warn() { echo "  ${YLW}!!${OFF}  $*"; }
die()  { echo "${RED}error:${OFF} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$DOMAIN" ]] || die "usage: sudo bash setup.sh bot.yourdomain.com"

say "Deploying ARMEDIAS to ${BLD}${DOMAIN}${OFF}"

# ── 0. Find a free loopback port — 5001 may belong to another project ──
APP_PORT=""
for p in $(seq 5001 5040); do
    if ! ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"; then
        APP_PORT="$p"; break
    fi
done
[[ -n "$APP_PORT" ]] || die "no free port in 5001-5040"
if [[ "$APP_PORT" != "5001" ]]; then
    warn "port 5001 is taken by another service; using $APP_PORT instead"
else
    ok "using port $APP_PORT"
fi

# ── 0b. Refuse to hijack a subdomain another site already serves ──
if grep -rqs "server_name.*${DOMAIN}" /etc/nginx/sites-enabled/ 2>/dev/null; then
    die "$DOMAIN is already served by an existing nginx site. Resolve that first — refusing to collide."
fi

# ── 1. Check DNS actually points here before we bother with certificates ──
say "Checking DNS"
SERVER_IP="$(curl -fsS --max-time 10 https://api.ipify.org || echo '')"
DOMAIN_IP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || echo '')"
if [[ -z "$DOMAIN_IP" ]]; then
    warn "$DOMAIN does not resolve yet."
    warn "Add an A record for it pointing to ${SERVER_IP:-this server} in Hostinger's DNS,"
    warn "then re-run. Continuing to install, but SSL will be skipped."
elif [[ -n "$SERVER_IP" && "$DOMAIN_IP" != "$SERVER_IP" ]]; then
    warn "$DOMAIN resolves to $DOMAIN_IP but this server is $SERVER_IP."
    warn "SSL will fail until DNS propagates. Continuing anyway."
else
    ok "$DOMAIN -> $DOMAIN_IP"
fi

# ── 2. Packages ──
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev build-essential \
    git nginx curl ufw certbot python3-certbot-nginx
ok "packages installed"

# ── 3. Service account (no login shell — it only runs the app) ──
if id "$APP_USER" &>/dev/null; then
    ok "user $APP_USER exists"
else
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    ok "created user $APP_USER"
fi

# ── 4. Code ──
if [[ -d "$APP_DIR/.git" ]]; then
    say "Updating existing checkout"
    git -C "$APP_DIR" fetch --all -q
    git -C "$APP_DIR" reset --hard origin/main -q
    ok "updated to $(git -C "$APP_DIR" rev-parse --short HEAD)"
else
    say "Cloning repository"
    mkdir -p "$APP_DIR"
    git clone -q "$REPO" "$APP_DIR"
    ok "cloned"
fi

# These hold Telegram sessions, the databases and uploads. Never wiped.
mkdir -p "$APP_DIR"/{sessions,logs,data/uploads}

# ── 5. Virtualenv ──
say "Installing Python dependencies"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "dependencies installed"

# ── 6. Environment file ──
if [[ -f "$APP_DIR/.env" ]]; then
    ok ".env already present — left untouched"
    NEEDS_ENV=0
else
    say "Creating .env"
    SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$APP_DIR/.env" <<EOF
# ARMEDIAS AI — secrets. Never commit this file.

ADMIN_USER=admin
ADMIN_PASS=CHANGE_ME_BEFORE_YOU_LOG_IN
SECRET_KEY=${SECRET}

# Supabase (optional on a VPS — the disk already persists.
# Keep it for off-box backups.)
SUPABASE_URL=
SUPABASE_KEY=

LOG_LEVEL=INFO
PORT=${APP_PORT}
EOF
    NEEDS_ENV=1
    ok ".env created with a generated SECRET_KEY"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# ── 7. systemd ──
say "Installing systemd service"
sed "s/127.0.0.1:5001/127.0.0.1:${APP_PORT}/" "$APP_DIR/deploy/armedias.service"     > /etc/systemd/system/armedias.service
chmod 644 /etc/systemd/system/armedias.service
systemctl daemon-reload
systemctl enable -q armedias
ok "service installed"

# ── 8. nginx ──
say "Configuring nginx"
sed -e "s/__DOMAIN__/${DOMAIN}/g" \
    -e "s/127\.0\.0\.1:5001/127.0.0.1:${APP_PORT}/" \
    "$APP_DIR/deploy/nginx-armedias.conf" > /etc/nginx/sites-available/armedias
ln -sf /etc/nginx/sites-available/armedias /etc/nginx/sites-enabled/armedias
# NOTE: the default site is deliberately left in place. On a shared VPS it may
# be a real site belonging to another project, and removing it is not ours to do.
if ! nginx -t; then
    rm -f /etc/nginx/sites-enabled/armedias
    die "nginx config test failed — our site was removed again, nothing else was touched"
fi
systemctl reload nginx
ok "nginx site added for $DOMAIN (existing sites untouched)"

# ── 9. Firewall ──
say "Checking firewall"
if command -v ufw >/dev/null && ufw status 2>/dev/null | head -1 | grep -qi "^Status: active"; then
    # Already on — only ever ADD rules.
    ufw allow OpenSSH      >/dev/null 2>&1 || true
    ufw allow 'Nginx Full' >/dev/null 2>&1 || true
    ok "ufw already active; allowed ssh + http/https"
else
    warn "ufw is inactive — deliberately NOT enabling it."
    warn "Turning a firewall on here could block ports your other production"
    warn "services rely on. Enable it yourself once you have listed their rules."
fi
ok "port $APP_PORT is bound to loopback only and is not publicly reachable"

# ── 10. Start ──
say "Starting application"
systemctl restart armedias
sleep 5
if systemctl is-active --quiet armedias; then
    ok "armedias is running"
else
    echo
    journalctl -u armedias -n 40 --no-pager
    die "service failed to start — log above"
fi

# ── 11. TLS ──
if [[ -n "$DOMAIN_IP" && ( -z "$SERVER_IP" || "$DOMAIN_IP" == "$SERVER_IP" ) ]]; then
    say "Requesting Let's Encrypt certificate"
    # --cert-name scopes this to our domain so certbot cannot rewrite or
    # renew certificates belonging to the other sites on this box.
    if certbot --nginx -d "$DOMAIN" --cert-name "$DOMAIN" \
               --non-interactive --agree-tos \
               --register-unsafely-without-email --redirect; then
        ok "HTTPS enabled (auto-renews via certbot timer)"
        SCHEME=https
    else
        warn "certbot failed — site is up on http, re-run: certbot --nginx -d $DOMAIN"
        SCHEME=http
    fi
else
    warn "skipping SSL until DNS points here. Then run: certbot --nginx -d $DOMAIN"
    SCHEME=http
fi

echo
echo "${GRN}${BLD}Deployment complete.${OFF}"
echo "  URL      ${SCHEME}://${DOMAIN}"
echo "  Logs     journalctl -u armedias -f"
echo "  Restart  systemctl restart armedias"
echo "  Update   cd $APP_DIR && sudo bash deploy/update.sh"
if [[ "${NEEDS_ENV}" == "1" ]]; then
    echo
    echo "${YLW}${BLD}Before you log in:${OFF}"
    echo "  1. sudo nano $APP_DIR/.env      set ADMIN_PASS (and Supabase keys if you want them)"
    echo "  2. sudo systemctl restart armedias"
    echo
    echo "  ADMIN_PASS is read once at startup, so a restart is required for it to take effect."
fi
