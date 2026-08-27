#!/usr/bin/env bash
#
# ARMEDIAS Telegram Tool — VPS bootstrap (Ubuntu / Debian)
#
#   sudo bash setup.sh telegrambot.yourdomain.com [app-name]
#
# app-name defaults to "telegram-tool", giving /opt/telegram-tool and a
# systemd unit of the same name. Pass a second argument to change it.
#
# Designed for a VPS already running other production services:
#   • never enables or resets the firewall
#   • never touches an nginx site it did not create
#   • picks a free loopback port instead of assuming one
#   • refuses to claim a subdomain another site already serves
#   • rolls back its own nginx site if the config test fails
#
# Safe to re-run. Never overwrites .env, sessions/ or data/.
#
set -euo pipefail

DOMAIN="${1:-}"
APP_NAME="${2:-telegram-tool}"
REPO="https://github.com/itsmearyanabc/camel-telegram-tool.git"
APP_DIR="/opt/${APP_NAME}"
# Linux usernames are safest as plain lowercase alphanumerics.
APP_USER="$(echo "$APP_NAME" | tr -cd '[:alnum:]' | cut -c1-30)"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; OFF=$'\e[0m'
say()  { echo "${BLD}==>${OFF} $*"; }
ok()   { echo "  ${GRN}ok${OFF}  $*"; }
warn() { echo "  ${YLW}!!${OFF}  $*"; }
die()  { echo "${RED}error:${OFF} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$DOMAIN" ]] || die "usage: sudo bash setup.sh telegrambot.yourdomain.com [app-name]"

# Catch the copy-paste-the-example mistake before installing anything.
case "$DOMAIN" in
    *yourdomain.com|*example.com|*yoursite.com)
        die "'$DOMAIN' is the placeholder from the docs. Pass your real subdomain." ;;
esac

say "Deploying to ${BLD}${DOMAIN}${OFF}"
say "Path ${BLD}${APP_DIR}${OFF} · service ${BLD}${APP_NAME}${OFF} · user ${BLD}${APP_USER}${OFF}"

# ── 0. Loopback port ──
# On a re-run the port is already held by THIS service, which must not be
# mistaken for a conflict — keep the port the existing install already uses.
APP_PORT="$(grep -oP 'bind 127\.0\.0\.1:\K[0-9]+'             "/etc/systemd/system/${APP_NAME}.service" 2>/dev/null | head -1 || true)"
if [[ -n "$APP_PORT" ]]; then
    ok "reusing port $APP_PORT from the existing install"
else
    for p in $(seq 5001 5040); do
        if ! ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"; then
            APP_PORT="$p"; break
        fi
    done
    [[ -n "$APP_PORT" ]] || die "no free port in 5001-5040"
    if [[ "$APP_PORT" == "5001" ]]; then ok "using port $APP_PORT"
    else warn "port 5001 taken by another service; using $APP_PORT"; fi
fi

# ── 0b. Never collide with an existing site ──
OWNER="$(grep -rls "server_name.*${DOMAIN}" /etc/nginx/sites-enabled/ 2>/dev/null | head -1 || true)"
if [[ -n "$OWNER" && "$(basename "$OWNER")" != "$APP_NAME" ]]; then
    die "$DOMAIN is already served by nginx site '$(basename "$OWNER")'. Refusing to collide."
fi

# ── 1. DNS ──
say "Checking DNS"
SERVER_IP="$(curl -fsS --max-time 10 https://api.ipify.org || echo '')"
DOMAIN_IP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || echo '')"
if [[ -z "$DOMAIN_IP" ]]; then
    warn "$DOMAIN does not resolve yet — SSL will be skipped"
elif [[ -n "$SERVER_IP" && "$DOMAIN_IP" != "$SERVER_IP" ]]; then
    warn "$DOMAIN -> $DOMAIN_IP but this server is $SERVER_IP — SSL will be skipped"
else
    ok "$DOMAIN -> $DOMAIN_IP"
fi

# ── 2. Packages ──
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
# Stop needrestart interrupting with its interactive service prompt.
export NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev build-essential \
    git nginx curl certbot python3-certbot-nginx
ok "packages installed"

# ── 3. Service account ──
# --no-create-home is deliberate: useradd would otherwise fill APP_DIR with
# shell skeleton files, and a git clone then refuses the non-empty directory.
if id "$APP_USER" &>/dev/null; then
    ok "user $APP_USER exists"
else
    useradd --system --no-create-home --home-dir "$APP_DIR" \
            --shell /usr/sbin/nologin "$APP_USER"
    ok "created user $APP_USER"
fi

# ── 4. Code ──
# init + fetch rather than clone, so this works whether the directory is
# missing, empty, or already holds stray files from an earlier attempt.
say "Fetching application code"
mkdir -p "$APP_DIR"
# The checkout is owned by the service account but git runs here as root,
# which trips git's "dubious ownership" guard. Mark it trusted explicitly.
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
if [[ ! -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" init -q
    git -C "$APP_DIR" remote add origin "$REPO"
fi
git -C "$APP_DIR" remote set-url origin "$REPO"
git -C "$APP_DIR" fetch --depth 1 origin main -q
git -C "$APP_DIR" checkout -f -B main origin/main -q
ok "at $(git -C "$APP_DIR" rev-parse --short HEAD)"

# Runtime state — never wiped by updates.
mkdir -p "$APP_DIR"/{sessions,logs,data/uploads}

# ── 5. Virtualenv ──
say "Installing Python dependencies"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "dependencies installed"

# ── 6. Environment ──
if [[ -f "$APP_DIR/.env" ]]; then
    ok ".env already present — left untouched"
    NEEDS_ENV=0
else
    say "Creating .env"
    cat > "$APP_DIR/.env" <<EOF
# Telegram Tool — secrets. Never commit this file.

ADMIN_USER=admin
ADMIN_PASS=CHANGE_ME_BEFORE_YOU_LOG_IN
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

# Supabase — optional on a VPS (the disk persists). Keep for off-box backup.
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
sed -e "s#/opt/armedias#${APP_DIR}#g" \
    -e "s/^User=armedias/User=${APP_USER}/" \
    -e "s/^Group=armedias/Group=${APP_USER}/" \
    -e "s/127\.0\.0\.1:5001/127.0.0.1:${APP_PORT}/" \
    -e "s/^SyslogIdentifier=armedias/SyslogIdentifier=${APP_NAME}/" \
    "$APP_DIR/deploy/armedias.service" > "/etc/systemd/system/${APP_NAME}.service"
chmod 644 "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload
systemctl enable -q "$APP_NAME"
ok "service ${APP_NAME} installed"

# ── 8. nginx ──
say "Configuring nginx"
sed -e "s/__DOMAIN__/${DOMAIN}/g" \
    -e "s/127\.0\.0\.1:5001/127.0.0.1:${APP_PORT}/" \
    -e "s#/opt/armedias#${APP_DIR}#g" \
    -e "s/armedias_app/${APP_USER}_app/g" \
    -e "s/armedias\.access\.log/${APP_NAME}.access.log/" \
    -e "s/armedias\.error\.log/${APP_NAME}.error.log/" \
    "$APP_DIR/deploy/nginx-armedias.conf" > "/etc/nginx/sites-available/${APP_NAME}"
ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
# The default site is left alone — on a shared VPS it may belong to another project.
if ! nginx -t; then
    rm -f "/etc/nginx/sites-enabled/${APP_NAME}"
    die "nginx config test failed — our site was removed, nothing else was touched"
fi
systemctl reload nginx
ok "nginx site added for $DOMAIN (existing sites untouched)"

# ── 9. Firewall ──
say "Checking firewall"
if command -v ufw >/dev/null && ufw status 2>/dev/null | head -1 | grep -qi "^Status: active"; then
    ufw allow 'Nginx Full' >/dev/null 2>&1 || true
    ok "ufw active; http/https allowed"
else
    warn "ufw inactive — deliberately not enabling it on a shared server"
fi
ok "port $APP_PORT is loopback-only and not publicly reachable"

# ── 10. Start ──
say "Starting application"
systemctl restart "$APP_NAME"
sleep 6
if systemctl is-active --quiet "$APP_NAME"; then
    ok "$APP_NAME is running"
else
    echo
    journalctl -u "$APP_NAME" -n 40 --no-pager
    die "service failed to start — log above"
fi

# ── 11. TLS ──
SCHEME=http
if [[ -n "$DOMAIN_IP" && ( -z "$SERVER_IP" || "$DOMAIN_IP" == "$SERVER_IP" ) ]]; then
    say "Requesting Let's Encrypt certificate"
    # --cert-name scopes this so certbot cannot rewrite or renew certificates
    # belonging to the other sites on this box.
    if certbot --nginx -d "$DOMAIN" --cert-name "$DOMAIN" \
               --non-interactive --agree-tos \
               --register-unsafely-without-email --redirect; then
        ok "HTTPS enabled (auto-renews)"
        SCHEME=https
    else
        warn "certbot failed — site is up on http; retry: certbot --nginx -d $DOMAIN"
    fi
else
    warn "skipping SSL until DNS points here; then: certbot --nginx -d $DOMAIN"
fi

echo
echo "${GRN}${BLD}Deployment complete.${OFF}"
echo "  URL      ${SCHEME}://${DOMAIN}"
echo "  Path     ${APP_DIR}"
echo "  Logs     journalctl -u ${APP_NAME} -f"
echo "  Restart  systemctl restart ${APP_NAME}"
echo "  Update   sudo bash ${APP_DIR}/deploy/update.sh ${APP_NAME}"
if [[ "$NEEDS_ENV" == "1" ]]; then
    echo
    echo "${YLW}${BLD}Before you log in:${OFF}"
    echo "  1. sudo nano ${APP_DIR}/.env       set ADMIN_PASS"
    echo "  2. sudo systemctl restart ${APP_NAME}"
    echo
    echo "  ADMIN_PASS is hashed at startup, so the restart is required."
fi
