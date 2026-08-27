#!/usr/bin/env bash
#
# Pull the latest code and restart. Run from anywhere:
#   sudo bash /opt/telegram-tool/deploy/update.sh [app-name]
#
# Never touches .env, sessions/ or data/ — your logins, user pool and
# monitored groups survive every update.
#
set -euo pipefail

APP_NAME="${1:-telegram-tool}"
APP_DIR="/opt/${APP_NAME}"
APP_USER="$(echo "$APP_NAME" | tr -cd '[:alnum:]' | cut -c1-30)"

GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; OFF=$'\e[0m'

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "${BLD}==>${OFF} Fetching latest code"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
BEFORE="$(git -C "$APP_DIR" rev-parse --short HEAD)"
git -C "$APP_DIR" fetch --all -q
git -C "$APP_DIR" reset --hard origin/main -q
AFTER="$(git -C "$APP_DIR" rev-parse --short HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "  already up to date ($AFTER)"
else
    echo "  $BEFORE -> $AFTER"
fi

echo "${BLD}==>${OFF} Syncing dependencies"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# A new release may add deploy config; keep the installed copies current.
if [[ -f "$APP_DIR/deploy/armedias.service" ]] && systemctl is-enabled "$APP_NAME" >/dev/null 2>&1; then
    APP_PORT="$(grep -oP 'bind 127\.0\.0\.1:\K[0-9]+' "/etc/systemd/system/${APP_NAME}.service" 2>/dev/null || echo 5001)"
    sed -e "s#/opt/armedias#${APP_DIR}#g"         -e "s/^User=armedias/User=${APP_USER}/"         -e "s/^Group=armedias/Group=${APP_USER}/"         -e "s/127\.0\.0\.1:5001/127.0.0.1:${APP_PORT}/"         -e "s/^SyslogIdentifier=armedias/SyslogIdentifier=${APP_NAME}/"         "$APP_DIR/deploy/armedias.service" > "/etc/systemd/system/${APP_NAME}.service"
    systemctl daemon-reload
fi

mkdir -p "$APP_DIR"/{sessions,logs,data/uploads}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

echo "${BLD}==>${OFF} Restarting"

# Restart with whichever supervisor actually owns this app. Using systemctl
# unconditionally would start a SECOND copy alongside a PM2-managed one, and
# the two would fight over the port and double-send every message.
if command -v pm2 >/dev/null 2>&1 && pm2 jlist 2>/dev/null | grep -q "\"name\":\"${APP_NAME}\""; then
    SUPERVISOR="pm2"
elif systemctl is-enabled "$APP_NAME" >/dev/null 2>&1; then
    SUPERVISOR="systemd"
else
    SUPERVISOR="none"
fi
echo "        supervisor: ${SUPERVISOR}"

case "$SUPERVISOR" in
  pm2)
    # `pm2 restart` errors on a process left in a stopped/errored state, so
    # re-register from the ecosystem file, which handles either case.
    pm2 delete "$APP_NAME" >/dev/null 2>&1 || true
    pm2 start "$APP_DIR/deploy/ecosystem.config.js" --update-env >/dev/null
    pm2 save >/dev/null 2>&1 || true
    sleep 6
    if pm2 jlist 2>/dev/null | grep -q "\"name\":\"${APP_NAME}\".*\"status\":\"online\""        || pm2 jlist 2>/dev/null | python3 -c "
import json,sys
print(any(a['name']=='${APP_NAME}' and a['pm2_env']['status']=='online' for a in json.load(sys.stdin)))" 2>/dev/null | grep -q True; then
        echo "${GRN}${BLD}Updated and running.${OFF}  Logs: pm2 logs $APP_NAME"
    else
        echo "App did not come back online:" >&2
        pm2 logs "$APP_NAME" --lines 30 --nostream >&2 2>/dev/null || true
        exit 1
    fi
    ;;
  systemd)
    systemctl restart "$APP_NAME"
    sleep 5
    if systemctl is-active --quiet "$APP_NAME"; then
        echo "${GRN}${BLD}Updated and running.${OFF}  Logs: journalctl -u $APP_NAME -f"
    else
        echo "Service failed to start:" >&2
        journalctl -u "$APP_NAME" -n 40 --no-pager >&2
        exit 1
    fi
    ;;
  *)
    echo "${YLW}Code updated, but no supervisor manages this app.${OFF}" >&2
    echo "Start it with:  pm2 start $APP_DIR/deploy/ecosystem.config.js" >&2
    echo "           or:  systemctl enable --now $APP_NAME" >&2
    ;;
esac
