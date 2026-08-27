#!/usr/bin/env bash
#
# Move the Telegram Tool from systemd to PM2, so it sits alongside your other
# PM2 apps and answers to the same commands.
#
#   sudo bash use-pm2.sh [app-name]        # default: telegram-tool
#
# Only ever touches this one app. Your other PM2 processes are not restarted,
# reloaded or reconfigured.
#
set -euo pipefail

APP_NAME="${1:-telegram-tool}"
APP_DIR="/opt/${APP_NAME}"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; OFF=$'\e[0m'
say()  { echo "${BLD}==>${OFF} $*"; }
ok()   { echo "  ${GRN}ok${OFF}  $*"; }
warn() { echo "  ${YLW}!!${OFF}  $*"; }
die()  { echo "${RED}error:${OFF} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -d "$APP_DIR" ]] || die "$APP_DIR not found — run setup.sh first"
command -v pm2 >/dev/null || die "pm2 is not installed"
[[ -x "$APP_DIR/venv/bin/gunicorn" ]] || die "venv missing — run setup.sh first"

say "Switching ${BLD}${APP_NAME}${OFF} from systemd to PM2"

# ── 1. Carry the port over so nginx keeps pointing at the right place ──
PORT="$(grep -oP 'bind 127\.0\.0\.1:\K[0-9]+' \
        "/etc/systemd/system/${APP_NAME}.service" 2>/dev/null | head -1 || true)"
if [[ -z "$PORT" ]]; then
    PORT="$(grep -oP 'proxy_pass http://127\.0\.0\.1:\K[0-9]+' \
            "/etc/nginx/sites-available/${APP_NAME}" 2>/dev/null | head -1 || true)"
fi
PORT="${PORT:-5001}"
ok "using port $PORT (matches the nginx proxy)"

# ── 2. Stop systemd first — two supervisors running one app would double-send ──
if systemctl cat "$APP_NAME" >/dev/null 2>&1; then
    say "Stopping and disabling the systemd unit"
    systemctl stop "$APP_NAME" 2>/dev/null || true
    systemctl disable "$APP_NAME" 2>/dev/null || true
    # Kept, not deleted, so switching back is one command.
    ok "systemd unit stopped and disabled (file left in place)"
else
    ok "no systemd unit to stop"
fi

# Make sure nothing is still holding the port.
sleep 2
if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
    warn "port $PORT is still held; waiting"
    sleep 4
fi

# ── 3. Hand it to PM2 ──
say "Starting under PM2"
mkdir -p "$APP_DIR/logs"
pm2 delete "$APP_NAME" >/dev/null 2>&1 || true
TELEGRAM_TOOL_PORT="$PORT" pm2 start "$APP_DIR/deploy/ecosystem.config.js" --update-env
pm2 save
ok "registered with PM2 and saved to the startup list"

# ── 4. Make sure PM2 itself comes back after a reboot ──
if systemctl is-enabled pm2-root >/dev/null 2>&1 || systemctl is-enabled "pm2-$(logname 2>/dev/null || echo root)" >/dev/null 2>&1; then
    ok "PM2 resurrect on boot is already enabled"
else
    warn "PM2 is not set to start on boot. Enable it once with:"
    warn "    pm2 startup    (then run the command it prints)"
fi

# ── 5. Verify it is actually serving ──
sleep 6
say "Verifying"
if pm2 jlist 2>/dev/null | grep -q "\"name\":\"${APP_NAME}\""; then
    STATUS="$(pm2 jlist | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    if a['name']=='${APP_NAME}': print(a['pm2_env']['status'])" 2>/dev/null || echo unknown)"
    [[ "$STATUS" == "online" ]] && ok "PM2 reports: online" || warn "PM2 reports: $STATUS"
fi

if curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:${PORT}/login"; then
    ok "app responds on 127.0.0.1:${PORT}"
else
    warn "no response yet on port ${PORT} — check: pm2 logs ${APP_NAME}"
fi

echo
echo "${GRN}${BLD}Now managed by PM2.${OFF}"
echo
echo "  pm2 list                       see it alongside your other apps"
echo "  pm2 logs ${APP_NAME}           live logs"
echo "  pm2 restart ${APP_NAME}        restart"
echo "  pm2 stop ${APP_NAME}           stop"
echo "  pm2 monit                      live CPU/memory dashboard"
echo
echo "  cd ${APP_DIR}                  the project directory"
echo
echo "${YLW}Note:${OFF} PM2 runs this as root, where the systemd unit ran it as an"
echo "unprivileged user with a restricted writable path. That matches how your"
echo "other PM2 apps already run, but it is a real reduction in isolation."
echo "To go back:  sudo pm2 delete ${APP_NAME} && sudo pm2 save && sudo systemctl enable --now ${APP_NAME}"
